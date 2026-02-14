#!/usr/bin/env python3
"""Morning briefing observer — gathers emails, infra status, and weather.

Runs at 7:30 AM on weekdays. Collects data from:
  1. IMAP accounts (unread email headers)
  2. Prometheus (down nodes via `up == 0`)
  3. Weather API (wttr.in)

Feeds everything to Claude for a concise morning brief, then sends to Telegram.
Any individual data source can fail without crashing the whole brief.
"""

import email
import email.header
import email.utils
import imaplib
import json
import logging
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from config import PROMETHEUS_URL as _PROMETHEUS_URL
from observers.base import Observer, ObserverResult

log = logging.getLogger("nexus")

SCRIPT_DIR = Path(__file__).parent


class MorningBriefObserver(Observer):
    """Gathers emails, infra health, and weather into a Claude-written morning brief."""

    name = "morning_brief"
    schedule = "30 7 * * 1-5"  # 7:30 AM UTC, weekdays

    # Class attributes with env-var fallbacks
    ACCOUNTS_FILE = SCRIPT_DIR / "email_accounts.json"
    MAX_PER_ACCOUNT = 20
    PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", _PROMETHEUS_URL)
    WEATHER_LOCATION = os.environ.get("WEATHER_LOCATION", "London")
    GCALENDAR_SCRIPT = Path.home() / ".config" / "puretensor" / "gcalendar.py"
    CALENDAR_ACCOUNTS = ["personal", "ops"]

    # -- Data sources ----------------------------------------------------------

    def fetch_emails(self) -> str:
        """Fetch unread email headers from all configured IMAP accounts.

        Returns a string summary of unread emails.
        Individual account failures are logged and skipped.
        """
        if not self.ACCOUNTS_FILE.exists():
            return "Email accounts not configured."

        accounts = json.loads(self.ACCOUNTS_FILE.read_text())
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

                uids = data[0].split()[-self.MAX_PER_ACCOUNT:]
                uids.reverse()

                for uid in uids:
                    status, msg_data = conn.fetch(
                        uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
                    )
                    if status != "OK" or not msg_data or msg_data[0] is None:
                        continue

                    raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                    msg = email.message_from_bytes(raw)

                    from_addr = self._decode_header(msg.get("From", ""))
                    subject = self._decode_header(msg.get("Subject", "(no subject)"))
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
            log.warning("Email errors: %s", "; ".join(errors))

        if not all_emails:
            return "No unread emails."

        return f"{len(all_emails)} unread emails:\n" + "\n".join(all_emails)

    def fetch_node_health(self) -> str:
        """Query Prometheus for down nodes.

        Returns a string summary of infrastructure status.
        """
        url = f"{self.PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote('up == 0')}"
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

    def fetch_weather(self) -> str:
        """Fetch weather from wttr.in.

        Returns a human-readable weather summary string.
        """
        url = f"https://wttr.in/{urllib.parse.quote(self.WEATHER_LOCATION)}?format=j1"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "pureclaw-morning-brief/1.0")
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
            f"Weather in {self.WEATHER_LOCATION}: {desc}, {temp_c}C "
            f"(feels like {feels_like}C), "
            f"Humidity: {humidity}%, Wind: {wind_kmph} km/h"
            f"{today_forecast}"
        )

    def fetch_calendar(self) -> str:
        """Fetch today's calendar events from Google Calendar.

        Calls gcalendar.py CLI for each configured account.
        Returns a string summary of today's events.
        """
        if not self.GCALENDAR_SCRIPT.exists():
            return "Calendar not configured."

        all_events = []
        for account in self.CALENDAR_ACCOUNTS:
            try:
                result = subprocess.run(
                    ["python3", str(self.GCALENDAR_SCRIPT), account, "today"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    output = result.stdout.strip()
                    # Skip header lines, extract event lines
                    lines = output.split("\n")
                    for line in lines:
                        line = line.strip()
                        # Event lines start with a date (YYYY-MM-DD)
                        if line and line[:4].isdigit() and "-" in line[:5]:
                            all_events.append(f"[{account}] {line}")
                else:
                    log.warning("Calendar fetch for %s failed: %s", account, result.stderr[:200])
            except subprocess.TimeoutExpired:
                log.warning("Calendar fetch for %s timed out", account)
            except Exception as e:
                log.warning("Calendar fetch for %s error: %s", account, e)

        if not all_events:
            return "No calendar events today."

        return f"{len(all_events)} event(s) today:\n" + "\n".join(all_events)

    # -- Internal helpers ------------------------------------------------------

    @staticmethod
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

    def _gather_data(self) -> dict[str, str]:
        """Gather data from all sources, returning a dict of section -> content.

        Each source is tried independently; failures are logged and skipped.
        """
        sections = {}

        # Emails
        try:
            sections["emails"] = self.fetch_emails()
        except Exception as e:
            sections["emails"] = f"Email check failed: {e}"
            log.warning("Email source failed: %s", e)

        # Node health
        try:
            sections["infrastructure"] = self.fetch_node_health()
        except Exception as e:
            sections["infrastructure"] = f"Infrastructure check failed: {e}"
            log.warning("Prometheus source failed: %s", e)

        # Weather
        try:
            sections["weather"] = self.fetch_weather()
        except Exception as e:
            sections["weather"] = f"Weather check failed: {e}"
            log.warning("Weather source failed: %s", e)

        # Calendar
        try:
            sections["calendar"] = self.fetch_calendar()
        except Exception as e:
            sections["calendar"] = f"Calendar check failed: {e}"
            log.warning("Calendar source failed: %s", e)

        return sections

    @staticmethod
    def _build_prompt(sections: dict[str, str]) -> str:
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

        parts.append("== CALENDAR ==")
        parts.append(sections.get("calendar", "No calendar data available."))
        parts.append("")

        parts.append(
            "Create a concise morning briefing covering: today's calendar events, "
            "emails needing attention, infrastructure status, and today's weather. "
            "Start with calendar items. Keep it brief and actionable. "
            "Plain text, no markdown."
        )

        return "\n".join(parts)

    # -- Observer interface ----------------------------------------------------

    def run(self, ctx=None) -> ObserverResult:
        """Execute the morning brief: gather data, call Claude, send to Telegram."""
        log.info("Morning brief starting at %s", self.now_utc().strftime("%H:%M UTC"))

        # Gather data from all sources
        sections = self._gather_data()

        # Build prompt and call Claude
        prompt = self._build_prompt(sections)
        log.info("Asking Claude for morning brief...")
        brief = self.call_claude(prompt)

        if not brief:
            return ObserverResult(
                success=False,
                error="Claude returned empty response for morning brief.",
            )

        # Send to Telegram
        timestamp = self.now_utc().strftime("%H:%M UTC")
        header = f"[{timestamp}] MORNING BRIEF"
        message = f"{header}\n\n{brief}"
        self.send_telegram(message)

        log.info("Morning brief sent to Telegram")
        return ObserverResult(success=True, message=message, data=sections)


# ---------------------------------------------------------------------------
# Standalone execution for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    # Ensure the project root is on sys.path so config imports work
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from config import log as _  # noqa: F401 — triggers logging setup

    observer = MorningBriefObserver()
    result = observer.run()

    if result.success:
        print("Morning brief completed successfully.")
    else:
        print(f"Morning brief failed: {result.error}", file=sys.stderr)
        sys.exit(1)
