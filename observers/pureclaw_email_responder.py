"""PureClaw Email Responder — autonomous email replies for allowlisted senders.

Monitors hal@puretensor.ai via Gmail API and responds autonomously to
trusted contacts. Unknown senders are quarantined — their email content
is NEVER processed by an LLM. Only a sanitised notification (sender
address + subject line) is sent to Heimir via Telegram.

SECURITY MODEL:
  - Strict sender allowlist checked BEFORE any LLM processing
  - Unknown sender content never enters any LLM prompt
  - Sender extraction uses pure string parsing, not LLM
  - Allowlist is hardcoded — changes require code review + restart

Schedule: every 5 minutes.
"""

import json
import logging
import re
import subprocess
from pathlib import Path

from observers.base import Observer, ObserverContext, ObserverResult

log = logging.getLogger("nexus")


class PureClawEmailResponderObserver(Observer):
    """Autonomous email responder for hal@puretensor.ai."""

    name = "pureclaw_email_responder"
    schedule = "*/5 * * * *"

    GMAIL_SCRIPT = Path.home() / ".config" / "puretensor" / "gmail.py"
    STATE_FILE = Path(__file__).parent / ".state" / "pureclaw_email_responder.json"

    # ── SECURITY: SENDER ALLOWLIST ──────────────────────────────
    # ONLY these addresses may have their email content processed
    # by an LLM. All others are quarantined with a Telegram alert.
    # To add senders: edit this set, commit, restart nexus.
    ALLOWED_SENDERS = {
        "alan.apter2@gmail.com",
        "alan.apter@bretalon.com",
        "hh@bretalon.com",
        "heimir.helgason@gmail.com",
        "helen.helgad@hotmail.com",
    }

    # ── State management ────────────────────────────────────────

    def _load_seen(self) -> set:
        """Load set of already-processed Gmail message IDs."""
        if self.STATE_FILE.exists():
            try:
                data = json.loads(self.STATE_FILE.read_text())
                return set(data.get("seen_ids", []))
            except (json.JSONDecodeError, TypeError):
                return set()
        return set()

    def _save_seen(self, seen: set) -> None:
        """Persist seen IDs. Keep last 500 to prevent unbounded growth."""
        self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        trimmed = sorted(seen)[-500:]
        self.STATE_FILE.write_text(json.dumps({"seen_ids": trimmed}))

    # ── Gmail helpers ───────────────────────────────────────────

    def _run_gmail(self, args: list[str]) -> str:
        """Call gmail.py and return stdout."""
        try:
            result = subprocess.run(
                ["python3", str(self.GMAIL_SCRIPT)] + args,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.GMAIL_SCRIPT.parent),
            )
            return result.stdout
        except subprocess.TimeoutExpired:
            log.warning("pureclaw_email_responder: gmail.py timed out: %s", args[:3])
            return ""
        except Exception as e:
            log.warning("pureclaw_email_responder: gmail.py error: %s", e)
            return ""

    def _search_new_emails(self) -> list[str]:
        """Search for unread emails to hal@puretensor.ai. Returns Gmail message IDs."""
        output = self._run_gmail([
            "ops", "search", "-q",
            "{to:hal@puretensor.ai to:hal@puretensor.org} is:unread newer_than:1d"
            " -from:hal@puretensor.ai -from:ops@puretensor.ai"
            " -from:mailer-daemon",
            "-n", "10",
        ])
        msg_ids = []
        for line in output.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("ID") or line.startswith("-") or \
               line.startswith("No ") or line.startswith("(") or \
               line.startswith("Refresh") or line.startswith("Token") or \
               line.startswith("Showing"):
                continue
            parts = line.split()
            if parts:
                candidate = parts[0].lstrip("*")
                if len(candidate) == 16 and all(c in "0123456789abcdef" for c in candidate):
                    msg_ids.append(candidate)
        return msg_ids

    # ── Pure string extraction (NO LLM) ────────────────────────

    @staticmethod
    def _extract_sender(email_text: str) -> str:
        """Extract bare email address from gmail.py read output.

        Uses regex only — no LLM involved. This is the security gate.
        """
        for line in email_text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("From:"):
                match = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", stripped)
                return match.group(0).lower() if match else ""
        return ""

    @staticmethod
    def _extract_subject(email_text: str) -> str:
        """Extract subject line from gmail.py read output. Pure string parsing."""
        for line in email_text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("Subject:"):
                return stripped.split(":", 1)[1].strip()[:120]
        return "(no subject)"

    # ── Main logic ──────────────────────────────────────────────

    def run(self, ctx: ObserverContext = None) -> ObserverResult:
        """Check for new emails and respond if appropriate."""
        if not self.GMAIL_SCRIPT.exists():
            return ObserverResult(success=False, error="gmail.py not found")

        seen = self._load_seen()

        # Find new unread emails to hal@
        msg_ids = self._search_new_emails()
        new_ids = [mid for mid in msg_ids if mid not in seen]

        if not new_ids:
            return ObserverResult(success=True)  # Silent — nothing to do

        replies_sent = 0
        quarantined = 0

        for msg_id in new_ids:
            seen.add(msg_id)

            # Read the message
            content = self._run_gmail(["ops", "read", "--id", msg_id])
            if not content:
                continue

            # Extract sender — pure string parsing, NO LLM
            sender = self._extract_sender(content)
            subject = self._extract_subject(content)

            if not sender:
                log.warning("pureclaw_email_responder: could not extract sender from %s", msg_id)
                continue

            # ── ALLOWLIST GATE ──────────────────────────────────
            if sender not in self.ALLOWED_SENDERS:
                # QUARANTINE: email content NEVER touches the LLM
                safe_sender = sender[:80].replace("<", "&lt;").replace(">", "&gt;")
                safe_subj = subject[:80].replace("<", "&lt;").replace(">", "&gt;")
                self.send_telegram_html(
                    f"<b>PureClaw: Email from unknown sender (quarantined)</b>\n"
                    f"From: <code>{safe_sender}</code>\n"
                    f"Subject: {safe_subj}\n\n"
                    f"Content NOT processed by AI.\n"
                    f"Review in ops@puretensor.ai inbox."
                )
                quarantined += 1
                log.info(
                    "pureclaw_email_responder: quarantined email from %s (%s)",
                    sender, subject[:40],
                )
                continue

            # ── ALLOWED SENDER — assess with Claude ─────────────
            assessment = self.call_claude(
                "You are PureClaw, Deputy CTO of Pure Tensor (hal@puretensor.ai). "
                "You received this email from a trusted contact.\n\n"
                "Assess whether a reply is needed:\n"
                '- Simple acknowledgments ("thanks", "got it", "ok") that '
                "don't invite a response: NO reply\n"
                "- Questions or phrasing that expects a response: reply\n"
                "- Requests for action: reply\n"
                "- Natural end of a thread: NO reply\n"
                "- When in doubt, err on the side of NOT replying\n\n"
                "Respond in this exact JSON format "
                "(no markdown, no code fences):\n"
                '{"reply_needed": true/false, '
                '"reason": "brief explanation", '
                '"reply": "the reply text if needed, or empty string"}\n\n'
                "Sign any reply as:\nPureClaw\nDeputy CTO, Pure Tensor\n"
                "hal@puretensor.ai\n\n"
                f"EMAIL:\n{content}",
                model="sonnet",
            )

            try:
                clean = assessment.strip()
                if clean.startswith("```"):
                    clean = "\n".join(clean.split("\n")[1:-1])
                decision = json.loads(clean)
            except (json.JSONDecodeError, ValueError):
                log.warning(
                    "pureclaw_email_responder: unparseable assessment for %s from %s",
                    msg_id, sender,
                )
                continue

            if decision.get("reply_needed") and decision.get("reply"):
                reply_output = self._run_gmail([
                    "hal", "reply",
                    "--id", msg_id,
                    "--body", decision["reply"],
                ])
                if reply_output:
                    replies_sent += 1
                    log.info(
                        "pureclaw_email_responder: replied to %s — %s",
                        sender, decision.get("reason", "")[:60],
                    )
            else:
                log.info(
                    "pureclaw_email_responder: no reply needed for %s — %s",
                    sender, decision.get("reason", "")[:60],
                )

        self._save_seen(seen)

        parts = []
        if replies_sent:
            parts.append(f"{replies_sent} replied")
        if quarantined:
            parts.append(f"{quarantined} quarantined")

        return ObserverResult(
            success=True,
            message="" if not parts else f"PureClaw Email: {', '.join(parts)}",
            data={"replies_sent": replies_sent, "quarantined": quarantined},
        )


# Standalone testing
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import log as _  # noqa: F811 — triggers logging setup

    observer = PureClawEmailResponderObserver()
    print(f"Allowlisted senders ({len(observer.ALLOWED_SENDERS)}):")
    for s in sorted(observer.ALLOWED_SENDERS):
        print(f"  + {s}")
    print()

    result = observer.run()
    if result.success:
        print(f"OK: {result.message or 'Nothing to process'}")
        if result.data:
            print(f"Data: {result.data}")
    else:
        print(f"FAILED: {result.error}", file=sys.stderr)
        sys.exit(1)
