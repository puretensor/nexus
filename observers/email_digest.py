#!/usr/bin/env python3
"""Email digest observer — checks for new unread emails across all accounts.

Runs every 30 minutes via the observer registry. For each configured IMAP account:
  1. Connects and fetches unread message headers
  2. Compares against previously seen messages (file-based state)
  3. If new unreads found, asks Claude to summarize and prioritize
  4. Pushes the digest to Telegram

Works with any email provider that supports IMAP: Gmail, Outlook, Yahoo,
self-hosted, anything. Configure accounts in email_accounts.json.

Setup:
  1. Copy email_accounts.json.example to email_accounts.json
  2. Add your accounts (server, username, password)

For Gmail: enable 2FA, then create an App Password at
https://myaccount.google.com/apppasswords
"""

import email
import email.header
import email.utils
import imaplib
import json
import logging
from pathlib import Path

from observers.base import Observer, ObserverResult

log = logging.getLogger("nexus")


class EmailDigestObserver(Observer):
    """Periodic email digest — fetches unread emails and sends Claude summary."""

    name = "email_digest"
    schedule = "0 * * * *"

    # Paths relative to this file
    OBSERVER_DIR = Path(__file__).parent
    STATE_DIR = OBSERVER_DIR / ".state"
    ACCOUNTS_FILE = OBSERVER_DIR / "email_accounts.json"
    SEEN_FILE = STATE_DIR / "email_seen.json"

    # How many unread to fetch per account (keeps things fast)
    MAX_PER_ACCOUNT = 30

    # -- Email header decoding --

    @staticmethod
    def decode_header(raw: str) -> str:
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

    # -- State tracking (file-based seen.json) --

    def load_seen(self) -> set[str]:
        """Load set of previously reported message IDs."""
        if self.SEEN_FILE.exists():
            try:
                return set(json.loads(self.SEEN_FILE.read_text()))
            except (json.JSONDecodeError, TypeError):
                return set()
        return set()

    def save_seen(self, seen_set: set[str]) -> None:
        """Persist seen message IDs. Keep last 5000 to prevent unbounded growth."""
        self.STATE_DIR.mkdir(exist_ok=True)
        trimmed = sorted(seen_set)[-5000:]
        self.SEEN_FILE.write_text(json.dumps(trimmed))

    # -- IMAP fetching --

    def fetch_unread(self, account: dict) -> tuple[str, list[dict], str | None]:
        """Connect to one IMAP account and return list of unread email summaries.

        Returns:
            (account_name, list_of_email_dicts, error_string_or_None)
        """
        server = account["server"]
        port = account.get("port", 993)
        username = account["username"]
        password = account["password"]
        name = account.get("name", username)

        try:
            conn = imaplib.IMAP4_SSL(server, port)
            conn.login(username, password)
        except Exception as e:
            return name, [], f"Connection failed: {e}"

        try:
            conn.select("INBOX", readonly=True)
            status, data = conn.search(None, "UNSEEN")
            if status != "OK" or not data[0]:
                return name, [], None

            # Most recent first, capped
            uids = data[0].split()[-self.MAX_PER_ACCOUNT:]
            uids.reverse()

            emails = []
            for uid in uids:
                status, msg_data = conn.fetch(
                    uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
                )
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue

                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                msg = email.message_from_bytes(raw)

                from_addr = self.decode_header(msg.get("From", ""))
                subject = self.decode_header(msg.get("Subject", "(no subject)"))
                date_str = msg.get("Date", "")
                msg_id = msg.get("Message-ID", f"{uid.decode()}@{server}")

                # Parse date for display
                try:
                    parsed = email.utils.parsedate_to_datetime(date_str)
                    date_display = parsed.strftime("%b %d %H:%M")
                except Exception:
                    date_display = date_str[:16] if date_str else "unknown"

                emails.append({
                    "id": msg_id.strip(),
                    "from": from_addr,
                    "subject": subject,
                    "date": date_display,
                })

            return name, emails, None

        except Exception as e:
            return name, [], f"Fetch error: {e}"
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    # -- Observer entry point --

    def run(self, ctx=None) -> ObserverResult:
        """Check all IMAP accounts for new unread emails and send a digest."""

        # Validate accounts file
        if not self.ACCOUNTS_FILE.exists():
            return ObserverResult(
                success=False,
                error=f"No accounts configured at {self.ACCOUNTS_FILE}",
            )

        accounts = json.loads(self.ACCOUNTS_FILE.read_text())
        seen = self.load_seen()

        # Check each account
        all_new: list[dict] = []
        errors: list[str] = []

        for account in accounts:
            name, emails, error = self.fetch_unread(account)
            if error:
                errors.append(f"{name}: {error}")
                continue

            for em in emails:
                if em["id"] not in seen:
                    em["account"] = name
                    all_new.append(em)
                    seen.add(em["id"])

        # Save updated seen set (even if no new emails — cleans up old entries)
        self.save_seen(seen)

        # Log errors but don't fail the whole run
        if errors:
            log.warning("Email digest errors: %s", "; ".join(errors))

        # Nothing new? Silent success.
        if not all_new:
            now = self.now_utc().strftime("%H:%M UTC")
            log.info("%s -- No new unread emails across %d accounts", now, len(accounts))
            return ObserverResult(
                success=True,
                data={"new_count": 0, "accounts": len(accounts), "errors": errors},
            )

        # Build a plain text summary for Claude
        summary_lines = []
        for em in all_new:
            summary_lines.append(
                f"[{em['account']}] {em['date']} -- From: {em['from']} -- Subject: {em['subject']}"
            )
        raw_summary = "\n".join(summary_lines)

        log.info("Found %d new unread emails. Asking Claude to summarize...", len(all_new))

        # Ask Claude to make sense of it
        prompt = (
            f"You are an email assistant. Here are {len(all_new)} new unread emails "
            f"across multiple accounts:\n\n"
            f"{raw_summary}\n\n"
            "Give me a brief morning-style digest:\n"
            "1. Flag anything that looks urgent or needs a reply\n"
            "2. Group newsletters/marketing separately\n"
            "3. Note anything unusual\n"
            "Keep it concise -- this goes to a Telegram message. "
            "Use plain text, no markdown."
        )

        digest = self.call_claude(prompt)

        # Send to Telegram
        now = self.now_utc().strftime("%H:%M UTC")
        header = f"[{now}] EMAIL DIGEST -- {len(all_new)} new"
        self.send_telegram(f"{header}\n\n{digest}")

        return ObserverResult(
            success=True,
            message=f"Sent digest with {len(all_new)} new emails",
            data={
                "new_count": len(all_new),
                "accounts": len(accounts),
                "errors": errors,
            },
        )


# ---------------------------------------------------------------------------
# Standalone testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    # Add parent directory to path so config module is importable
    sys.path.insert(0, str(Path(__file__).parent.parent))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    observer = EmailDigestObserver()
    result = observer.run()

    if result.success:
        if result.message:
            print(f"OK: {result.message}")
        else:
            print(f"OK: no new emails (checked {result.data.get('accounts', '?')} accounts)")
    else:
        print(f"FAILED: {result.error}", file=sys.stderr)
        sys.exit(1)
