#!/usr/bin/env python3
"""Email digest observer — checks for new unread emails across all your accounts.

Runs via cron. For each configured IMAP account:
  1. Connects and fetches unread message headers
  2. Compares against previously seen messages
  3. If new unreads found, asks Claude to summarize and prioritize
  4. Pushes the digest to Telegram

Works with any email provider that supports IMAP: Gmail, Outlook, Yahoo,
self-hosted, anything. Configure accounts in email_accounts.json.

Setup:
  1. Copy email_accounts.json.example to email_accounts.json
  2. Add your accounts (server, username, password)
  3. Add cron line (see example.cron)

For Gmail: enable 2FA, then create an App Password at
https://myaccount.google.com/apppasswords
"""

import email
import email.header
import email.utils
import imaplib
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
ENV_PATH = PROJECT_DIR / ".env"
STATE_DIR = SCRIPT_DIR / ".state"
ACCOUNTS_FILE = SCRIPT_DIR / "email_accounts.json"
SEEN_FILE = STATE_DIR / "email_seen.json"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", str(Path.home()))

# How many unread to fetch per account (keeps things fast)
MAX_PER_ACCOUNT = 30


# ---------------------------------------------------------------------------
# Helpers (same pattern as node_health.py — no dependencies)
# ---------------------------------------------------------------------------


def load_env():
    """Parse .env file into a dict."""
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def send_telegram(token, chat_id, text):
    """Send a message via Telegram Bot API."""
    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, 4000)
        if idx == -1:
            idx = 4000
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")

    for chunk in chunks:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=15)


def call_claude(message, model="sonnet"):
    """Invoke claude -p and return the result text."""
    cmd = [
        CLAUDE_BIN, "-p", message,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", model,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, cwd=CLAUDE_CWD
        )
    except subprocess.TimeoutExpired:
        return "Claude timed out after 300s"

    if result.returncode != 0:
        return f"Claude error (exit {result.returncode}): {result.stderr[:500]}"

    try:
        data = json.loads(result.stdout)
        return data.get("result", "(empty response)")
    except json.JSONDecodeError:
        return f"Failed to parse Claude output: {result.stdout[:500]}"


def decode_header(raw):
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


# ---------------------------------------------------------------------------
# State tracking — remember which emails we've already reported
# ---------------------------------------------------------------------------


def load_seen():
    """Load set of previously reported message IDs."""
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, TypeError):
            return set()
    return set()


def save_seen(seen_set):
    """Persist seen message IDs. Keep last 5000 to prevent unbounded growth."""
    STATE_DIR.mkdir(exist_ok=True)
    # Only keep the most recent entries
    trimmed = sorted(seen_set)[-5000:]
    SEEN_FILE.write_text(json.dumps(trimmed))


# ---------------------------------------------------------------------------
# IMAP — the actual email checking
# ---------------------------------------------------------------------------


def fetch_unread(account):
    """Connect to one IMAP account and return list of unread email summaries."""
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
        uids = data[0].split()[-MAX_PER_ACCOUNT:]
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

            from_addr = decode_header(msg.get("From", ""))
            subject = decode_header(msg.get("Subject", "(no subject)"))
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    # Load config
    if not ACCOUNTS_FILE.exists():
        print(f"No accounts configured. Copy email_accounts.json.example to {ACCOUNTS_FILE}")
        sys.exit(1)

    accounts = json.loads(ACCOUNTS_FILE.read_text())
    env = load_env()
    token = env["TELEGRAM_BOT_TOKEN"]
    chat_id = env["AUTHORIZED_USER_ID"]
    seen = load_seen()

    # Check each account
    all_new = []
    errors = []

    for account in accounts:
        name, emails, error = fetch_unread(account)
        if error:
            errors.append(f"{name}: {error}")
            continue

        for em in emails:
            if em["id"] not in seen:
                em["account"] = name
                all_new.append(em)
                seen.add(em["id"])

    # Save updated seen set (even if no new emails — cleans up old entries)
    save_seen(seen)

    # Report errors
    if errors:
        print("Errors:", "; ".join(errors), file=sys.stderr)

    # Nothing new? Done.
    if not all_new:
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        print(f"{now} — No new unread emails across {len(accounts)} accounts")
        sys.exit(0)

    # Build a plain text summary for Claude
    summary_lines = []
    for em in all_new:
        summary_lines.append(
            f"[{em['account']}] {em['date']} — From: {em['from']} — Subject: {em['subject']}"
        )
    raw_summary = "\n".join(summary_lines)

    print(f"Found {len(all_new)} new unread emails. Asking Claude to summarize...")

    # Ask Claude to make sense of it
    prompt = (
        f"You are an email assistant. Here are {len(all_new)} new unread emails "
        f"across multiple accounts:\n\n"
        f"{raw_summary}\n\n"
        "Give me a brief morning-style digest:\n"
        "1. Flag anything that looks urgent or needs a reply\n"
        "2. Group newsletters/marketing separately\n"
        "3. Note anything unusual\n"
        "Keep it concise — this goes to a Telegram message. "
        "Use plain text, no markdown."
    )

    digest = call_claude(prompt)

    # Send to Telegram
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    header = f"[{now}] EMAIL DIGEST — {len(all_new)} new"
    send_telegram(token, chat_id, f"{header}\n\n{digest}")

    print("Digest sent to Telegram")


if __name__ == "__main__":
    main()
