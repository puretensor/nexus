"""Scheduled tasks — background loop, time parsing, next-trigger computation."""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from db import (
    get_due_tasks,
    mark_task_run,
    advance_recurring_task,
    delete_task_by_id,
)

log = logging.getLogger("nexus")

# Default time when a date is given without a specific time
DEFAULT_HOUR = 9  # 9:00 UTC

# ---------------------------------------------------------------------------
# Day / month lookups
# ---------------------------------------------------------------------------

_DAY_NAMES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------


def _parse_time(token: str, ref: datetime) -> datetime | None:
    """Parse a time token like '5pm', '17:00', '9am', '14:30' relative to ref date.

    Returns a datetime on ref's date with the parsed time, or None if unparsable.
    """
    token = token.lower().strip()

    # Match "5pm", "5am", "11pm", "12am"
    m = re.fullmatch(r"(\d{1,2})(am|pm)", token)
    if m:
        hour = int(m.group(1))
        meridiem = m.group(2)
        if meridiem == "am":
            if hour == 12:
                hour = 0
        else:  # pm
            if hour != 12:
                hour += 12
        return ref.replace(hour=hour, minute=0, second=0, microsecond=0)

    # Match "5:30pm", "9:15am"
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(am|pm)", token)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        meridiem = m.group(3)
        if meridiem == "am":
            if hour == 12:
                hour = 0
        else:
            if hour != 12:
                hour += 12
        return ref.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Match "17:00", "9:30" (24h format)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", token)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return ref.replace(hour=hour, minute=minute, second=0, microsecond=0)

    return None


def _parse_day_of_month(token: str) -> int | None:
    """Parse a day-of-month token like '9', '9th', '21st', '2nd', '3rd'.

    Returns the day number or None.
    """
    m = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?", token.lower().strip())
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            return day
    return None


def _next_weekday(ref: datetime, target_weekday: int) -> datetime:
    """Return the next occurrence of target_weekday (0=Mon) after ref."""
    days_ahead = target_weekday - ref.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return ref + timedelta(days=days_ahead)


def _resolve_date(day: int, month: int, ref: datetime) -> datetime:
    """Build a datetime for a specific day/month, rolling to next year if past."""
    year = ref.year
    try:
        target = ref.replace(year=year, month=month, day=day,
                             hour=DEFAULT_HOUR, minute=0, second=0, microsecond=0)
    except ValueError:
        raise ValueError(f"Invalid date: day {day} of month {month}")
    if target <= ref:
        try:
            target = target.replace(year=year + 1)
        except ValueError:
            raise ValueError(f"Invalid date: day {day} of month {month}")
    return target


def _try_parse_date(args: list[str], start: int, ref: datetime) -> tuple[datetime, int] | None:
    """Try to parse a date from args starting at index `start`.

    Supported formats (case-insensitive):
        "9 feb", "9th feb", "9 february", "9th of february"
        "feb 9", "feb 9th", "february 9", "february 9th"

    Returns (target_datetime, tokens_consumed) or None.
    """
    if start >= len(args):
        return None

    remaining = len(args) - start

    tok0 = args[start].lower().rstrip(",")

    # Pattern 1: day month — "9 feb", "9th february", "9th of feb"
    day = _parse_day_of_month(tok0)
    if day is not None and remaining >= 2:
        tok1 = args[start + 1].lower().rstrip(",")
        # Skip "of" if present: "9th of february"
        if tok1 == "of" and remaining >= 3:
            tok2 = args[start + 2].lower().rstrip(",")
            month = _MONTH_NAMES.get(tok2)
            if month is not None:
                return _resolve_date(day, month, ref), 3
        month = _MONTH_NAMES.get(tok1)
        if month is not None:
            return _resolve_date(day, month, ref), 2

    # Pattern 2: month day — "feb 9", "february 9th"
    month = _MONTH_NAMES.get(tok0)
    if month is not None and remaining >= 2:
        tok1 = args[start + 1].lower().rstrip(",")
        day = _parse_day_of_month(tok1)
        if day is not None:
            return _resolve_date(day, month, ref), 2

    return None


_RELATIVE_UNITS = {
    "m": "minutes", "min": "minutes", "mins": "minutes",
    "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours",
    "hour": "hours", "hours": "hours",
}


def _try_parse_relative(args: list[str], start: int, ref: datetime) -> tuple[datetime, int] | None:
    """Try to parse relative time from args starting at index `start`.

    Supported formats:
        "in 5 minutes", "in 2 hours", "in 30 min", "in 1 hour"
        "in 5m", "in 2h"  (number+unit as single token)

    Returns (target_datetime, tokens_consumed) or None.
    """
    if start >= len(args):
        return None

    tok0 = args[start].lower()
    if tok0 != "in":
        return None

    remaining = len(args) - start
    if remaining < 2:
        return None

    tok1 = args[start + 1].lower()

    # Pattern 1: "in 5m", "in 2h", "in 30min", "in 1hour"
    m = re.fullmatch(r"(\d+)(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)", tok1)
    if m:
        amount = int(m.group(1))
        unit = _RELATIVE_UNITS.get(m.group(2))
        if unit and amount > 0:
            delta = timedelta(**{unit: amount})
            return ref + delta, 2

    # Pattern 2: "in 5 minutes", "in 2 hours"
    if remaining >= 3:
        try:
            amount = int(tok1)
        except ValueError:
            return None
        tok2 = args[start + 2].lower()
        unit = _RELATIVE_UNITS.get(tok2)
        if unit and amount > 0:
            delta = timedelta(**{unit: amount})
            return ref + delta, 3

    return None


def parse_schedule_args(args: list[str]) -> tuple[str, str, str | None]:
    """Parse user input for /schedule command.

    Accepts formats like:
        ["5pm", "check", "the", "deployment"]
        ["tomorrow", "9am", "summarize", "emails"]
        ["daily", "8am", "morning", "review"]
        ["weekdays", "7am", "check", "status"]
        ["weekly", "10am", "weekly", "review"]
        ["monday", "9am", "standup"]
        ["monday", "do", "standup"]           (defaults to 9am)
        ["9", "feb", "review", "project"]
        ["9th", "of", "february", "3pm", "review", "project"]
        ["feb", "9", "do", "something"]
        ["february", "9th", "3pm", "do", "something"]
        ["in", "5", "minutes", "check", "build"]
        ["in", "2h", "call", "Alan"]

    Returns (trigger_time_iso, prompt, recurrence_or_none).
    Raises ValueError on invalid input.
    """
    if len(args) < 2:
        raise ValueError(
            "Usage: /schedule <when> <prompt>\n"
            "Examples:\n"
            "  /schedule 5pm check deployment\n"
            "  /schedule tomorrow 9am review PR\n"
            "  /schedule daily 8am morning brief\n"
            "  /schedule monday 9am standup\n"
            "  /schedule 9 feb review project\n"
            "  /schedule in 30 minutes check build"
        )

    now = datetime.now(timezone.utc)
    today = now
    recurrence = None
    idx = 0  # tracks how many args consumed for date/time spec
    date_set = False  # whether a specific date was parsed

    first = args[0].lower()

    # Check for relative time first: "in 5 minutes", "in 2h"
    relative = _try_parse_relative(args, 0, now)
    if relative is not None:
        trigger_dt, consumed = relative
        idx = consumed
        prompt_parts = args[idx:]
        if not prompt_parts:
            raise ValueError("Missing prompt text. Example: /remind in 5 minutes check build")
        prompt = " ".join(prompt_parts)
        return trigger_dt.isoformat(), prompt, None

    # Check for recurrence prefix
    if first in ("daily", "weekdays", "weekly"):
        recurrence = first
        idx = 1
    elif first == "tomorrow":
        today = now + timedelta(days=1)
        idx = 1
        date_set = True
    else:
        # Check for day name: "monday", "tue", etc.
        weekday = _DAY_NAMES.get(first)
        if weekday is not None:
            today = _next_weekday(now, weekday)
            idx = 1
            date_set = True
        else:
            # Check for specific date: "9 feb", "feb 9", "9th of february"
            date_result = _try_parse_date(args, 0, now)
            if date_result is not None:
                today, consumed = date_result
                idx = consumed
                date_set = True

    # Try to parse a time token at current position
    time_parsed = None
    if idx < len(args):
        time_parsed = _parse_time(args[idx], today)
        if time_parsed is not None:
            idx += 1

    # If no date/recurrence was set and no time either, try the first token as time
    if not date_set and recurrence is None and time_parsed is None:
        time_parsed = _parse_time(args[0], today)
        if time_parsed is not None:
            idx = 1

    # If we still have nothing, error out
    if not date_set and recurrence is None and time_parsed is None:
        raise ValueError(
            f"Cannot parse: '{args[0]}'. Use formats like:\n"
            "  5pm, 9am, 14:30, in 30 minutes\n"
            "  tomorrow, monday, friday\n"
            "  9 feb, february 9th, 9th of february"
        )

    # Remaining args are the prompt
    prompt_parts = args[idx:]
    if not prompt_parts:
        raise ValueError("Missing prompt text. Example: /schedule 5pm check deployment")
    prompt = " ".join(prompt_parts)

    # Build final trigger datetime
    if time_parsed is not None:
        trigger = time_parsed
    else:
        # Date was set but no time given — default to DEFAULT_HOUR
        trigger = today.replace(hour=DEFAULT_HOUR, minute=0, second=0, microsecond=0)

    # For bare time (no date, no recurrence): if time already passed today, push to tomorrow
    if recurrence is None and not date_set:
        if trigger <= now:
            trigger += timedelta(days=1)

    # For recurrence: if first trigger already passed today, push to next valid day
    if recurrence is not None:
        if trigger <= now:
            trigger += timedelta(days=1)
        if recurrence == "weekdays":
            while trigger.weekday() >= 5:
                trigger += timedelta(days=1)

    trigger_iso = trigger.isoformat()
    return trigger_iso, prompt, recurrence


# ---------------------------------------------------------------------------
# Next trigger computation
# ---------------------------------------------------------------------------


def compute_next_trigger(current_trigger: str, recurrence: str) -> str:
    """Compute the next trigger time for a recurring task.

    Args:
        current_trigger: ISO 8601 datetime string of the current trigger.
        recurrence: One of "daily", "weekdays", "weekly".

    Returns:
        ISO 8601 datetime string for the next trigger.
    """
    dt = datetime.fromisoformat(current_trigger)

    if recurrence == "daily":
        dt += timedelta(days=1)
    elif recurrence == "weekdays":
        dt += timedelta(days=1)
        # Skip Saturday and Sunday
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
    elif recurrence == "weekly":
        dt += timedelta(days=7)
    else:
        # Unknown recurrence, default to daily
        dt += timedelta(days=1)

    return dt.isoformat()


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------


async def _execute_task(task: dict, bot) -> str:
    """Run a scheduled task via engine.call_sync and return the result text."""
    from engine import call_sync

    prompt = task["prompt"]
    log.info("Scheduler executing task %d: %s", task["id"], prompt[:80])

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None, lambda: call_sync(prompt, model="sonnet", timeout=300)
    )

    return data.get("result", "(Empty response)")


async def run_scheduler(bot):
    """Main scheduler loop. Runs every 60 seconds, executes due tasks."""
    log.info("Scheduler started")

    while True:
        try:
            tasks = get_due_tasks()
            if tasks:
                log.info("Scheduler found %d due task(s)", len(tasks))

            for task in tasks:
                try:
                    task_type = task.get("task_type", "schedule")

                    if task_type == "remind":
                        # Reminder: just send the message directly, no Claude
                        message = f"Reminder: {task['prompt']}"
                        if len(message) > 4000:
                            message = message[:3997] + "..."
                        await bot.send_message(chat_id=task["chat_id"], text=message)
                        log.info("Reminder %d sent: %s", task["id"], task["prompt"][:80])
                    else:
                        # Schedule: run Claude and send result
                        result_text = await _execute_task(task, bot)
                        header = f"[Scheduled] {task['prompt'][:60]}"
                        if task.get("recurrence"):
                            header += f" ({task['recurrence']})"
                        message = f"{header}\n\n{result_text}"
                        if len(message) > 4000:
                            message = message[:3997] + "..."
                        await bot.send_message(chat_id=task["chat_id"], text=message)

                    # Handle one-shot vs recurring
                    if task.get("recurrence"):
                        mark_task_run(task["id"])
                        next_trigger = compute_next_trigger(
                            task["trigger_time"], task["recurrence"]
                        )
                        advance_recurring_task(task["id"], next_trigger)
                        log.info(
                            "Recurring task %d advanced to %s",
                            task["id"], next_trigger,
                        )
                    else:
                        delete_task_by_id(task["id"])
                        log.info("One-shot task %d completed and deleted", task["id"])

                except Exception:
                    log.exception("Error executing scheduled task %d", task["id"])

        except Exception:
            log.exception("Scheduler loop error")

        await asyncio.sleep(60)
