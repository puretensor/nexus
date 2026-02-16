"""Follow-up reminder observer — nudges about emails awaiting responses.

Runs daily at 9 AM (weekdays). Checks all active followups and sends a
Telegram reminder for any that are past their reminder_days threshold.

Followups are created automatically when a draft is approved and sent
(via drafts/queue.py), or manually via /followup command.
"""

import logging
from datetime import datetime, timezone, timedelta

from observers.base import Observer, ObserverResult
from db import list_active_followups, update_followup_reminded

log = logging.getLogger("nexus")


class FollowupReminderObserver(Observer):
    """Checks for stale followups and sends Telegram reminders."""

    name = "followup_reminder"
    schedule = "0 9 * * 0-4"  # 9:00 AM UTC, weekdays (0=Mon)

    def run(self, ctx=None) -> ObserverResult:
        """Check all active followups and remind about overdue ones."""
        followups = list_active_followups()

        if not followups:
            return ObserverResult(success=True)

        now = datetime.now(timezone.utc)
        due = []

        for fu in followups:
            # Parse sent_at
            try:
                sent_at = datetime.fromisoformat(fu["sent_at"])
            except (ValueError, TypeError):
                continue

            days_since = (now - sent_at).days
            if days_since < fu["reminder_days"]:
                continue

            # Check if we already reminded today
            if fu["last_reminded"]:
                try:
                    last = datetime.fromisoformat(fu["last_reminded"])
                    if (now - last).days < 1:
                        continue
                except (ValueError, TypeError):
                    pass

            due.append((fu, days_since))

        if not due:
            return ObserverResult(success=True)

        # Build reminder message
        lines = [f"FOLLOW-UP REMINDER — {len(due)} item(s) awaiting response:\n"]

        for fu, days in due:
            lines.append(
                f"  #{fu['id']} To: {fu['email_to']}\n"
                f"     Re: {fu['email_subject']}\n"
                f"     Sent {days} day(s) ago"
            )
            update_followup_reminded(fu["id"])

        message = "\n".join(lines)
        self.send_telegram(message)

        return ObserverResult(
            success=True,
            message=f"Reminded about {len(due)} overdue followup(s)",
            data={"due_count": len(due)},
        )
