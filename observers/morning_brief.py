#!/usr/bin/env python3
"""Morning briefing observer — gathers emails, infra status, and weather.

Runs via cron at 7:30 AM on weekdays. Collects data from:
  1. IMAP accounts (unread email headers)
  2. Prometheus (down nodes via `up == 0`)
  3. Weather API (wttr.in)

Feeds everything to Claude for a concise morning brief, then sends to Telegram.
Any individual data source can fail without crashing the whole brief.

# Cron: 30 7 * * 1-5 cd /home/puretensorai/claude-telegram && python3 observers/morning_brief.py
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
# Config — all paths relative to this script
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
ENV_PATH = PROJECT_DIR / ".env"
STATE_DIR = SCRIPT_DIR / ".state"
ACCOUNTS_FILE = SCRIPT_DIR / "email_accounts.json"

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://MON2_TAILSCALE_IP:9090")
WEATHER_LOCATION = os.environ.get("WEATHER_LOCATION", "London")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", str(Path.home()))

# How many unread to fetch per account (keeps things fast)
MAX_PER_ACCOUNT = 20


# ---------------------------------------------------------------------------
# Helpers (same pattern as email_digest.py / node_health.py — no dependencies)
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
# Data sources — each returns a string summary or raises on failure
# ---------------------------------------------------------------------------


def fetch_emails():
    """Fetch unread email headers from all configured IMAP accounts.

    Returns a string summary of unread emails, or raises on total failure.
    Individual account failures are logged and skipped.
    """
    if not ACCOUNTS_FILE.exists():
        return "Email accounts not configured."

    accounts = json.loads(ACCOUNTS_FILE.read_text())
    all_emails = []
    errors = []

    for account in accounts:
        server = account["server"]
        port = account.get("port", 993)
        username = account["username"]
        password = account["password"]
        name = account.get("name", username)

        try:
            conn = imaplib.IMAP4_SSL(server, port)
            conn.login(username, password)
        except Exception as e:
            errors.append(f"{name}: connection failed: {e}")
            continue

        try:
            conn.select("INBOX", readonly=True)
            status, data = conn.search(None, "UNSEEN")
            if status != "OK" or not data[0]:
                continue

            uids = data[0].split()[-MAX_PER_ACCOUNT:]
            uids.reverse()

            for uid in uids:
                status, msg_data = conn.fetch(
                    uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
                )
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue

                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                msg = email.message_from_bytes(raw)

                from_addr = decode_header(msg.get("From", ""))
                subject = decode_header(msg.get("Subject", "(no subject)"))
                date_str = msg.get("Date", "")

                try:
                    parsed = email.utils.parsedate_to_datetime(date_str)
                    date_display = parsed.strftime("%b %d %H:%M")
                except Exception:
                    date_display = date_str[:16] if date_str else "unknown"

                all_emails.append(
                    f"[{name}] {date_display} -- From: {from_addr} -- Subject: {subject}"
                )
        except Exception as e:
            errors.append(f"{name}: fetch error: {e}")
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    if errors:
        print("Email errors:", "; ".join(errors), file=sys.stderr)

    if not all_emails:
        return "No unread emails."

    return f"{len(all_emails)} unread emails:\n" + "\n".join(all_emails)


def fetch_node_health():
    """Query Prometheus for down nodes.

    Returns a string summary of infrastructure status.
    """
    url = f"{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote('up == 0')}"
    resp = urllib.request.urlopen(url, timeout=10)
    data = json.loads(resp.read())

    results = data.get("data", {}).get("result", [])

    if not results:
        return "All monitored nodes are up."

    down_lines = []
    for r in results:
        instance = r["metric"].get("instance", "unknown")
        job = r["metric"].get("job", "unknown")
        down_lines.append(f"  - {instance} (job: {job})")

    return f"{len(down_lines)} node(s) DOWN:\n" + "\n".join(down_lines)


def fetch_weather():
    """Fetch weather from wttr.in.

    Returns a human-readable weather summary string.
    """
    url = f"https://wttr.in/{urllib.parse.quote(WEATHER_LOCATION)}?format=j1"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "hal-claude-morning-brief/1.0")
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())

    current = data["current_condition"][0]
    temp_c = current.get("temp_C", "?")
    feels_like = current.get("FeelsLikeC", "?")
    desc = current.get("weatherDesc", [{}])[0].get("value", "Unknown")
    humidity = current.get("humidity", "?")
    wind_kmph = current.get("windspeedKmph", "?")

    # Today's forecast
    today_forecast = ""
    if data.get("weather"):
        today = data["weather"][0]
        max_c = today.get("maxtempC", "?")
        min_c = today.get("mintempC", "?")
        today_forecast = f", High: {max_c}C, Low: {min_c}C"

    return (
        f"Weather in {WEATHER_LOCATION}: {desc}, {temp_c}C "
        f"(feels like {feels_like}C), "
        f"Humidity: {humidity}%, Wind: {wind_kmph} km/h"
        f"{today_forecast}"
    )


# ---------------------------------------------------------------------------
# Brief assembly
# ---------------------------------------------------------------------------


def gather_data():
    """Gather data from all sources, returning a dict of section -> content.

    Each source is tried independently; failures are logged and skipped.
    """
    sections = {}

    # Emails
    try:
        sections["emails"] = fetch_emails()
    except Exception as e:
        sections["emails"] = f"Email check failed: {e}"
        print(f"Email source failed: {e}", file=sys.stderr)

    # Node health
    try:
        sections["infrastructure"] = fetch_node_health()
    except Exception as e:
        sections["infrastructure"] = f"Infrastructure check failed: {e}"
        print(f"Prometheus source failed: {e}", file=sys.stderr)

    # Weather
    try:
        sections["weather"] = fetch_weather()
    except Exception as e:
        sections["weather"] = f"Weather check failed: {e}"
        print(f"Weather source failed: {e}", file=sys.stderr)

    return sections


def build_prompt(sections):
    """Build the Claude prompt from gathered data sections."""
    parts = []
    parts.append("Here is the data for today's morning briefing:\n")

    parts.append("== EMAILS ==")
    parts.append(sections.get("emails", "No email data available."))
    parts.append("")

    parts.append("== INFRASTRUCTURE ==")
    parts.append(sections.get("infrastructure", "No infrastructure data available."))
    parts.append("")

    parts.append("== WEATHER ==")
    parts.append(sections.get("weather", "No weather data available."))
    parts.append("")

    parts.append(
        "Create a concise morning briefing covering: emails needing attention, "
        "infrastructure status, and today's weather. Keep it brief and actionable. "
        "Plain text, no markdown."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    env = load_env()
    token = env["TELEGRAM_BOT_TOKEN"]
    chat_id = env["AUTHORIZED_USER_ID"]

    print(f"Morning brief starting at {datetime.now(timezone.utc).strftime('%H:%M UTC')}...")

    # Gather data from all sources
    sections = gather_data()

    # Build prompt and call Claude
    prompt = build_prompt(sections)
    print("Asking Claude for morning brief...")
    brief = call_claude(prompt)

    # Send to Telegram
    timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    header = f"[{timestamp}] MORNING BRIEF"
    send_telegram(token, chat_id, f"{header}\n\n{brief}")

    print("Morning brief sent to Telegram")


if __name__ == "__main__":
    main()
