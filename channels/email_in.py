"""Email input channel — polls IMAP for messages to hal@example.com.

Lifecycle:
  1. Polls IMAP inbox every 2 minutes for new unread messages
  2. Classifies each message (ignore / notify / auto_reply / followup)
  3. For auto_reply: asks Claude to draft a reply, creates a draft for approval
  4. For notify: sends a Telegram notification
  5. Marks messages as seen in SQLite to prevent duplicates

Uses the same IMAP accounts file as EmailDigestObserver.
"""

import asyncio
import email
import email.header
import email.utils
import html
import imaplib
import json
import logging
import re
from pathlib import Path

from channels.base import Channel
from config import AUTHORIZED_USER_ID, AGENT_NAME, log
from db import is_email_seen, mark_email_seen
from drafts.classifier import classify_email
from drafts.queue import create_email_draft

# IMAP accounts config — shared with email_digest observer
ACCOUNTS_FILE = Path(__file__).parent.parent / "observers" / "email_accounts.json"

# Poll interval (seconds)
POLL_INTERVAL = 120  # 2 minutes

# Max emails to process per poll cycle
MAX_PER_POLL = 20


def _decode_header(raw: str) -> str:
    """Decode an email header (handles encoded words like =?UTF-8?Q?...?=)."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def _extract_email_addr(header_value: str) -> str:
    """Extract bare email address from a header like 'Name <user@example.com>'."""
    if not header_value:
        return ""
    _, addr = email.utils.parseaddr(header_value)
    return addr or header_value


def _strip_html(raw_html: str) -> str:
    """Strip HTML tags and decode entities to produce readable plain text."""
    # Remove style/script blocks entirely (content + tags)
    text = re.sub(r"<style[^>]*>.*?</style>", "", raw_html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
    # Replace block-level tags with newlines for readability
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities (&amp; &nbsp; &#8230; etc.)
    text = html.unescape(text)
    # Collapse whitespace runs (but keep single newlines)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _get_body(msg) -> str:
    """Extract plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try text/html (strip tags to plain text)
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return _strip_html(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            # If the single part is HTML, strip it
            if msg.get_content_type() == "text/html":
                return _strip_html(text)
            return text
    return ""


def fetch_new_emails(account: dict) -> list[dict]:
    """Fetch unread emails from a single IMAP account.

    Returns list of dicts with: id, from, from_addr, subject, date, to, body
    """
    server = account["server"]
    port = account.get("port", 993)
    username = account["username"]
    password = account["password"]

    try:
        conn = imaplib.IMAP4_SSL(server, port)
        conn.login(username, password)
    except Exception as e:
        log.warning("Email input: connection to %s failed: %s", server, e)
        return []

    try:
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            return []

        uids = data[0].split()[-MAX_PER_POLL:]
        uids.reverse()

        results = []
        for uid in uids:
            try:
                # Fetch full message (need body for drafting replies)
                status, msg_data = conn.fetch(uid, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue

                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                msg = email.message_from_bytes(raw)

                from_raw = _decode_header(msg.get("From", ""))
                subject = _decode_header(msg.get("Subject", "(no subject)"))
                date_str = msg.get("Date", "")
                msg_id = msg.get("Message-ID", f"{uid.decode()}@{server}")
                to_raw = _decode_header(msg.get("To", ""))
                body = _get_body(msg)

                # Parse date for display
                try:
                    parsed = email.utils.parsedate_to_datetime(date_str)
                    date_display = parsed.strftime("%b %d %H:%M")
                except Exception:
                    date_display = date_str[:16] if date_str else "unknown"

                results.append({
                    "id": msg_id.strip(),
                    "from": from_raw,
                    "from_addr": _extract_email_addr(from_raw),
                    "subject": subject,
                    "date": date_display,
                    "to": to_raw,
                    "body": body[:5000],  # Cap body size
                })
            except Exception as e:
                log.warning("Email input: skipping UID %s: %s", uid.decode(), e)

        return results

    except Exception as e:
        log.warning("Email input: fetch error for %s: %s", server, e)
        return []
    finally:
        try:
            conn.logout()
        except Exception:
            pass


class EmailInputChannel(Channel):
    """Polls IMAP for new emails, classifies them, and creates drafts or notifications."""

    def __init__(self, bot=None):
        self._bot = bot
        self._task = None

    async def start(self):
        """Start the email polling loop."""
        self._task = asyncio.create_task(self._poll_loop())
        log.info("Email input channel started (polling every %ds)", POLL_INTERVAL)

    async def stop(self):
        """Stop the email polling loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Email input channel stopped")

    async def _poll_loop(self):
        """Main loop — checks for new emails every POLL_INTERVAL seconds."""
        while True:
            try:
                await self._poll_once()
            except Exception as e:
                log.exception("Email input poll error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)

    async def _poll_once(self):
        """Single poll cycle — fetch, classify, act."""
        if not ACCOUNTS_FILE.exists():
            return

        accounts = json.loads(ACCOUNTS_FILE.read_text())

        for account in accounts:
            name = account.get("name", account["username"])

            # Run IMAP fetch in thread pool (blocking I/O)
            loop = asyncio.get_event_loop()
            emails = await loop.run_in_executor(None, fetch_new_emails, account)

            for em in emails:
                # Skip already-seen messages
                if is_email_seen(em["id"]):
                    continue

                # Mark as seen immediately to prevent re-processing
                mark_email_seen(em["id"], name)

                # Classify
                classification = classify_email(
                    em["from"], em["subject"], em.get("to", "")
                )

                if classification == "ignore":
                    continue

                if classification == "notify":
                    await self._send_notification(em)

                elif classification == "auto_reply":
                    await self._create_auto_reply(em)

                elif classification == "followup":
                    await self._send_notification(em, followup=True)

    async def _send_notification(self, em: dict, followup: bool = False):
        """Send a Telegram notification about an email."""
        if not self._bot:
            return

        tag = "FOLLOW-UP" if followup else "EMAIL"
        text = (
            f"[{tag}] {em['date']}\n"
            f"From: {em['from']}\n"
            f"Subject: {em['subject']}\n"
        )
        if em.get("body"):
            preview = em["body"][:200].replace("\n", " ")
            text += f"\n{preview}..."

        try:
            await self._bot.send_message(
                chat_id=int(AUTHORIZED_USER_ID),
                text=text,
            )
        except Exception as e:
            log.warning("Email input: failed to send notification: %s", e)

    async def _create_auto_reply(self, em: dict):
        """Use Claude to compose and send a reply autonomously.

        HAL uses his own judgement for whitelisted senders — no approval needed.
        Telegram gets an informational notification after sending.
        Routes through engine.call_sync → Claude Code CLI (OAuth).
        """
        import subprocess
        from engine import call_sync
        from drafts.queue import GMAIL_SCRIPT, GMAIL_IDENTITY

        body_preview = em["body"][:2000] if em.get("body") else "(no body)"
        prompt = (
            f"You are {AGENT_NAME}, an AI agent for PureTensor infrastructure. "
            "You are replying to an email from a trusted colleague. "
            "Be concise, professional, and helpful. Use your judgement. "
            "Sign off as HAL. "
            "Output ONLY the reply text — no subject line, no greeting header, just the content.\n\n"
            f"From: {em['from']}\n"
            f"Subject: {em['subject']}\n\n"
            f"{body_preview}"
        )

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: call_sync(prompt, model="sonnet", timeout=60)
            )
            if result.get("error"):
                raise RuntimeError(result["result"])
            reply_body = result.get("result", "")
        except Exception as e:
            log.warning("Email input: Claude draft failed for %s: %s", em["from"], e)
            await self._send_notification(em)
            return

        if not reply_body:
            await self._send_notification(em)
            return

        # Send immediately — no approval gate
        try:
            reply_to = em["from_addr"] or em["from"]
            reply_subject = em["subject"]
            if not reply_subject.lower().startswith("re:"):
                reply_subject = f"Re: {reply_subject}"
            send_result = await loop.run_in_executor(None, lambda: subprocess.run(
                [
                    "python3", str(GMAIL_SCRIPT),
                    GMAIL_IDENTITY, "reply",
                    "--to", reply_to,
                    "--subject", reply_subject,
                    "--id", em["id"],
                    "--body", reply_body,
                ],
                capture_output=True, text=True, timeout=30,
            ))

            if send_result.returncode == 0:
                log.info("Auto-replied to %s re: %s", em["from_addr"], em["subject"])

                # Notify Telegram (informational only)
                if self._bot:
                    preview = reply_body[:300] + "..." if len(reply_body) > 300 else reply_body
                    text = (
                        f"[SENT] Auto-reply to {em['from']}\n"
                        f"Re: {em['subject']}\n\n"
                        f"{preview}"
                    )
                    try:
                        await self._bot.send_message(
                            chat_id=int(AUTHORIZED_USER_ID), text=text,
                        )
                    except Exception:
                        pass
            else:
                error = send_result.stderr[:200] or send_result.stdout[:200]
                log.warning("Auto-reply send failed for %s: %s", em["from_addr"], error)
                await self._send_notification(em)
        except Exception as e:
            log.warning("Auto-reply error for %s: %s", em["from_addr"], e)
            await self._send_notification(em)
