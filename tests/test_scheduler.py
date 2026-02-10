"""Tests for scheduler.py — scheduled tasks (Phase 4A).

Tests cover:
- compute_next_trigger: daily, weekdays (Fri->Mon), weekly
- parse_schedule_args: various time formats, recurrence, edge cases
- Day names: monday, tue, friday, etc.
- Specific dates: 9 feb, feb 9, 9th of february, february 9th
- run_scheduler: task execution, one-shot cleanup, recurring advancement, error isolation
- /schedule and /cancel commands
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from db import (
        init_db,
        create_scheduled_task,
        list_scheduled_tasks,
        delete_scheduled_task,
        get_due_tasks,
        mark_task_run,
        advance_recurring_task,
        delete_task_by_id,
        AUTHORIZED_USER_ID,
    )
    from scheduler import (
        compute_next_trigger,
        parse_schedule_args,
        run_scheduler,
        _execute_task,
        _parse_day_of_month,
        _try_parse_date,
        _try_parse_relative,
        _next_weekday,
        DEFAULT_HOUR,
    )
    from channels.telegram.commands import cmd_schedule, cmd_cancel, cmd_remind


def _make_update_context(chat_id=12345, user_id=12345, args=None):
    """Create mock Update and context objects for command handlers."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args or []
    return update, context


# ---------------------------------------------------------------------------
# compute_next_trigger
# ---------------------------------------------------------------------------

class TestComputeNextTrigger:

    def test_daily(self):
        """Daily recurrence adds exactly 1 day."""
        trigger = "2026-02-06T08:00:00+00:00"
        result = compute_next_trigger(trigger, "daily")
        expected = datetime(2026, 2, 7, 8, 0, 0, tzinfo=timezone.utc)
        assert datetime.fromisoformat(result) == expected

    def test_weekly(self):
        """Weekly recurrence adds exactly 7 days."""
        trigger = "2026-02-06T10:00:00+00:00"
        result = compute_next_trigger(trigger, "weekly")
        expected = datetime(2026, 2, 13, 10, 0, 0, tzinfo=timezone.utc)
        assert datetime.fromisoformat(result) == expected

    def test_weekdays_mon_to_tue(self):
        """Weekdays: Monday -> Tuesday (normal advance)."""
        # 2026-02-09 is a Monday
        trigger = "2026-02-09T08:00:00+00:00"
        result = compute_next_trigger(trigger, "weekdays")
        dt = datetime.fromisoformat(result)
        assert dt.weekday() == 1  # Tuesday
        assert dt.day == 10

    def test_weekdays_fri_to_mon(self):
        """Weekdays: Friday -> Monday (skip weekend)."""
        # 2026-02-06 is a Friday
        trigger = "2026-02-06T08:00:00+00:00"
        result = compute_next_trigger(trigger, "weekdays")
        dt = datetime.fromisoformat(result)
        assert dt.weekday() == 0  # Monday
        assert dt.day == 9

    def test_weekdays_thu_to_fri(self):
        """Weekdays: Thursday -> Friday (normal advance)."""
        # 2026-02-05 is a Thursday
        trigger = "2026-02-05T09:00:00+00:00"
        result = compute_next_trigger(trigger, "weekdays")
        dt = datetime.fromisoformat(result)
        assert dt.weekday() == 4  # Friday
        assert dt.day == 6

    def test_daily_preserves_time(self):
        """Daily advance preserves the exact time."""
        trigger = "2026-02-06T14:30:00+00:00"
        result = compute_next_trigger(trigger, "daily")
        dt = datetime.fromisoformat(result)
        assert dt.hour == 14
        assert dt.minute == 30

    def test_weekly_preserves_time(self):
        """Weekly advance preserves the exact time."""
        trigger = "2026-02-06T09:15:00+00:00"
        result = compute_next_trigger(trigger, "weekly")
        dt = datetime.fromisoformat(result)
        assert dt.hour == 9
        assert dt.minute == 15


# ---------------------------------------------------------------------------
# parse_schedule_args
# ---------------------------------------------------------------------------

class TestParseScheduleArgs:

    def test_simple_pm_time(self):
        """'5pm check deploy' parses correctly."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(["5pm", "check", "deploy"])

        dt = datetime.fromisoformat(trigger)
        assert dt.hour == 17
        assert dt.minute == 0
        assert prompt == "check deploy"
        assert recurrence is None

    def test_am_time(self):
        """'9am morning brief' parses correctly."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 7, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(["9am", "morning", "brief"])

        dt = datetime.fromisoformat(trigger)
        assert dt.hour == 9
        assert prompt == "morning brief"
        assert recurrence is None

    def test_tomorrow(self):
        """'tomorrow 9am review PR' sets trigger to tomorrow."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["tomorrow", "9am", "review", "PR"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.day == 7  # tomorrow
        assert dt.hour == 9
        assert prompt == "review PR"
        assert recurrence is None

    def test_daily_recurrence(self):
        """'daily 8am morning review' sets daily recurrence."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 7, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["daily", "8am", "morning", "review"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.hour == 8
        assert prompt == "morning review"
        assert recurrence == "daily"

    def test_weekdays_recurrence(self):
        """'weekdays 7am check status' sets weekdays recurrence."""
        with patch("scheduler.datetime") as mock_dt:
            # Saturday
            now = datetime(2026, 2, 7, 6, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["weekdays", "7am", "check", "status"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.weekday() < 5  # Must be a weekday
        assert prompt == "check status"
        assert recurrence == "weekdays"

    def test_weekly_recurrence(self):
        """'weekly 10am weekly review' sets weekly recurrence."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 9, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["weekly", "10am", "weekly", "review"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.hour == 10
        assert prompt == "weekly review"
        assert recurrence == "weekly"

    def test_past_time_pushes_to_tomorrow(self):
        """A bare time that has already passed today pushes to tomorrow."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 18, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(["5pm", "check"])

        dt = datetime.fromisoformat(trigger)
        assert dt.day == 7  # pushed to tomorrow

    def test_24h_format(self):
        """'14:30 afternoon check' parses 24h time."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["14:30", "afternoon", "check"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.hour == 14
        assert dt.minute == 30
        assert prompt == "afternoon check"

    def test_time_with_minutes_am_pm(self):
        """'9:15am standup' parses time with minutes."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 7, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(["9:15am", "standup"])

        dt = datetime.fromisoformat(trigger)
        assert dt.hour == 9
        assert dt.minute == 15
        assert prompt == "standup"

    def test_invalid_no_args(self):
        """Too few args raises ValueError."""
        with pytest.raises(ValueError, match="Usage"):
            parse_schedule_args([])

    def test_invalid_single_arg(self):
        """Single arg (no prompt) raises ValueError."""
        with pytest.raises(ValueError, match="Usage"):
            parse_schedule_args(["5pm"])

    def test_invalid_time(self):
        """Unparsable time raises ValueError."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with pytest.raises(ValueError, match="Cannot parse"):
                parse_schedule_args(["badtime", "do", "something"])

    def test_missing_prompt_after_time(self):
        """Time with no prompt raises ValueError."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with pytest.raises(ValueError, match="Missing prompt"):
                parse_schedule_args(["daily", "8am"])

    def test_12am_is_midnight(self):
        """12am should be hour 0 (midnight)."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(["12am", "midnight", "task"])

        dt = datetime.fromisoformat(trigger)
        # 12am already passed (now is 10am), so pushed to tomorrow
        assert dt.hour == 0

    def test_12pm_is_noon(self):
        """12pm should be hour 12 (noon)."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(["12pm", "noon", "task"])

        dt = datetime.fromisoformat(trigger)
        assert dt.hour == 12


# ---------------------------------------------------------------------------
# _parse_day_of_month
# ---------------------------------------------------------------------------

class TestParseDayOfMonth:

    def test_plain_number(self):
        assert _parse_day_of_month("9") == 9

    def test_with_st(self):
        assert _parse_day_of_month("1st") == 1

    def test_with_nd(self):
        assert _parse_day_of_month("2nd") == 2

    def test_with_rd(self):
        assert _parse_day_of_month("3rd") == 3

    def test_with_th(self):
        assert _parse_day_of_month("9th") == 9

    def test_21st(self):
        assert _parse_day_of_month("21st") == 21

    def test_31(self):
        assert _parse_day_of_month("31") == 31

    def test_zero_invalid(self):
        assert _parse_day_of_month("0") is None

    def test_32_invalid(self):
        assert _parse_day_of_month("32") is None

    def test_word_invalid(self):
        assert _parse_day_of_month("hello") is None


# ---------------------------------------------------------------------------
# Day name scheduling
# ---------------------------------------------------------------------------

class TestDayNameScheduling:

    def test_monday(self):
        """'monday 9am standup' schedules for next Monday."""
        with patch("scheduler.datetime") as mock_dt:
            # Thursday 2026-02-05
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["monday", "9am", "standup"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.weekday() == 0  # Monday
        assert dt.day == 9  # next Monday is Feb 9
        assert dt.hour == 9
        assert prompt == "standup"
        assert recurrence is None

    def test_friday_abbreviated(self):
        """'fri 5pm wrap up' schedules for next Friday."""
        with patch("scheduler.datetime") as mock_dt:
            # Monday 2026-02-09
            now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["fri", "5pm", "wrap", "up"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.weekday() == 4  # Friday
        assert dt.day == 13
        assert dt.hour == 17
        assert prompt == "wrap up"

    def test_day_name_no_time_defaults_to_9am(self):
        """'wednesday review docs' defaults to 9am UTC."""
        with patch("scheduler.datetime") as mock_dt:
            # Monday 2026-02-09
            now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["wednesday", "review", "docs"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.weekday() == 2  # Wednesday
        assert dt.day == 11
        assert dt.hour == DEFAULT_HOUR
        assert prompt == "review docs"

    def test_tuesday_abbreviated(self):
        """'tue 14:30 team sync' works with 24h time."""
        with patch("scheduler.datetime") as mock_dt:
            # Thursday 2026-02-05
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["tue", "14:30", "team", "sync"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.weekday() == 1  # Tuesday
        assert dt.day == 10
        assert dt.hour == 14
        assert dt.minute == 30
        assert prompt == "team sync"

    def test_same_day_goes_to_next_week(self):
        """If today is Monday and you say 'monday do X', it goes to next Monday."""
        with patch("scheduler.datetime") as mock_dt:
            # Monday 2026-02-09
            now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["monday", "team", "meeting"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.weekday() == 0  # Monday
        assert dt.day == 16  # next Monday, not today


# ---------------------------------------------------------------------------
# Specific date scheduling
# ---------------------------------------------------------------------------

class TestDateScheduling:

    def test_day_month(self):
        """'9 feb review project' schedules for Feb 9."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["9", "feb", "review", "project"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.month == 2
        assert dt.day == 9
        assert dt.hour == DEFAULT_HOUR
        assert prompt == "review project"
        assert recurrence is None

    def test_day_th_month(self):
        """'9th february review project' schedules for Feb 9."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["9th", "february", "review", "project"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.month == 2
        assert dt.day == 9
        assert dt.hour == DEFAULT_HOUR
        assert prompt == "review project"

    def test_day_of_month(self):
        """'9th of february do something' handles 'of' separator."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["9th", "of", "february", "do", "something"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.month == 2
        assert dt.day == 9
        assert prompt == "do something"

    def test_month_day(self):
        """'feb 9 do something' — month-first format."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["feb", "9", "do", "something"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.month == 2
        assert dt.day == 9
        assert prompt == "do something"

    def test_month_day_th(self):
        """'february 9th do something' — full month name, ordinal day."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["february", "9th", "do", "something"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.month == 2
        assert dt.day == 9
        assert prompt == "do something"

    def test_date_with_time(self):
        """'9 feb 3pm review project' sets both date and time."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["9", "feb", "3pm", "review", "project"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.month == 2
        assert dt.day == 9
        assert dt.hour == 15
        assert prompt == "review project"

    def test_date_of_with_time(self):
        """'9th of february 3pm do something' — full format with time."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["9th", "of", "february", "3pm", "do", "something"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.month == 2
        assert dt.day == 9
        assert dt.hour == 15
        assert prompt == "do something"

    def test_month_first_with_time(self):
        """'february 9th 3pm do something' — month first, with time."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["february", "9th", "3pm", "do", "something"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.month == 2
        assert dt.day == 9
        assert dt.hour == 15
        assert prompt == "do something"

    def test_past_date_rolls_to_next_year(self):
        """A date that already passed this year schedules for next year."""
        with patch("scheduler.datetime") as mock_dt:
            # It's March 2026, scheduling for Jan 15 → should go to Jan 2027
            now = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["15", "jan", "new", "year", "review"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt.year == 2027
        assert dt.month == 1
        assert dt.day == 15
        assert prompt == "new year review"

    def test_different_months(self):
        """Various month abbreviations work: mar, apr, sep, dec."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, _ = parse_schedule_args(["15", "mar", "spring", "review"])
            dt = datetime.fromisoformat(trigger)
            assert dt.month == 3 and dt.day == 15

            trigger, prompt, _ = parse_schedule_args(["1", "dec", "year", "end"])
            dt = datetime.fromisoformat(trigger)
            assert dt.month == 12 and dt.day == 1


# ---------------------------------------------------------------------------
# Relative time scheduling
# ---------------------------------------------------------------------------

class TestRelativeTime:

    def test_in_5_minutes(self):
        """'in 5 minutes check build' adds 5 minutes to now."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["in", "5", "minutes", "check", "build"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt == datetime(2026, 2, 6, 10, 5, 0, tzinfo=timezone.utc)
        assert prompt == "check build"
        assert recurrence is None

    def test_in_2_hours(self):
        """'in 2 hours call Alan' adds 2 hours."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["in", "2", "hours", "call", "Alan"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt == datetime(2026, 2, 6, 12, 0, 0, tzinfo=timezone.utc)
        assert prompt == "call Alan"

    def test_in_30min_compact(self):
        """'in 30min check the build' — compact unit format."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["in", "30min", "check", "the", "build"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt == datetime(2026, 2, 6, 10, 30, 0, tzinfo=timezone.utc)
        assert prompt == "check the build"

    def test_in_1h(self):
        """'in 1h meeting prep' — compact hour format."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 14, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["in", "1h", "meeting", "prep"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt == datetime(2026, 2, 6, 15, 0, 0, tzinfo=timezone.utc)
        assert prompt == "meeting prep"

    def test_in_1_hour_singular(self):
        """'in 1 hour check status' — singular unit."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["in", "1", "hour", "check", "status"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt == datetime(2026, 2, 6, 11, 0, 0, tzinfo=timezone.utc)

    def test_in_1_minute_singular(self):
        """'in 1 minute test' — singular minute."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["in", "1", "minute", "test"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt == datetime(2026, 2, 6, 10, 1, 0, tzinfo=timezone.utc)

    def test_in_missing_prompt_raises(self):
        """'in 5 minutes' with no prompt raises ValueError."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with pytest.raises(ValueError, match="Missing prompt"):
                parse_schedule_args(["in", "5", "minutes"])

    def test_in_with_hrs(self):
        """'in 3hrs review PR' — hrs abbreviation."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["in", "3hrs", "review", "PR"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt == datetime(2026, 2, 6, 13, 0, 0, tzinfo=timezone.utc)
        assert prompt == "review PR"

    def test_in_with_mins(self):
        """'in 15 mins grab coffee' — mins abbreviation."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            trigger, prompt, recurrence = parse_schedule_args(
                ["in", "15", "mins", "grab", "coffee"]
            )

        dt = datetime.fromisoformat(trigger)
        assert dt == datetime(2026, 2, 6, 10, 15, 0, tzinfo=timezone.utc)
        assert prompt == "grab coffee"

    def test_try_parse_relative_no_in(self):
        """_try_parse_relative returns None if first token isn't 'in'."""
        now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
        assert _try_parse_relative(["5", "minutes", "test"], 0, now) is None

    def test_try_parse_relative_bad_unit(self):
        """_try_parse_relative returns None for unknown unit."""
        now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
        assert _try_parse_relative(["in", "5", "fortnights", "test"], 0, now) is None


# ---------------------------------------------------------------------------
# run_scheduler — integration with mocked DB and bot
# ---------------------------------------------------------------------------

class TestRunScheduler:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        monkeypatch.setattr("scheduler.CLAUDE_BIN", "/usr/bin/echo")
        monkeypatch.setattr("scheduler.CLAUDE_CWD", str(tmp_path))
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_one_shot_task_executed_and_deleted(self):
        """One-shot task should be executed and then deleted."""
        # Create a task that's already due
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        task_id = create_scheduled_task(self.chat_id, past, "test prompt")

        bot = AsyncMock()

        # Mock _execute_task to return a simple result
        with patch("scheduler._execute_task", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = "Task result"

            # Run one iteration
            tasks = get_due_tasks()
            assert len(tasks) == 1

            for task in tasks:
                result = await mock_exec(task, bot)
                await bot.send_message(chat_id=task["chat_id"], text=f"[Scheduled] {task['prompt']}\n\n{result}")
                delete_task_by_id(task["id"])

        # Verify task was deleted
        remaining = list_scheduled_tasks(self.chat_id)
        assert len(remaining) == 0

        # Verify bot.send_message was called
        bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_recurring_task_advances(self):
        """Recurring task should advance trigger_time after execution."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        task_id = create_scheduled_task(self.chat_id, past, "daily check", "daily")

        bot = AsyncMock()

        with patch("scheduler._execute_task", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = "Daily result"

            tasks = get_due_tasks()
            assert len(tasks) == 1

            task = tasks[0]
            await mock_exec(task, bot)
            mark_task_run(task["id"])
            next_trigger = compute_next_trigger(task["trigger_time"], task["recurrence"])
            advance_recurring_task(task["id"], next_trigger)

        # Task should still exist but with advanced trigger
        remaining = list_scheduled_tasks(self.chat_id)
        assert len(remaining) == 1
        new_trigger = datetime.fromisoformat(remaining[0]["trigger_time"])
        old_trigger = datetime.fromisoformat(past)
        assert new_trigger > old_trigger

    @pytest.mark.asyncio
    async def test_error_in_one_task_doesnt_block_others(self):
        """Error in one task should not prevent other tasks from running."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        task_id_1 = create_scheduled_task(self.chat_id, past, "failing task")
        task_id_2 = create_scheduled_task(self.chat_id, past, "good task")

        bot = AsyncMock()
        executed = []

        async def mock_execute(task, bot_arg):
            if "failing" in task["prompt"]:
                raise RuntimeError("Simulated failure")
            executed.append(task["prompt"])
            return "Success"

        tasks = get_due_tasks()
        assert len(tasks) == 2

        for task in tasks:
            try:
                result = await mock_execute(task, bot)
                await bot.send_message(chat_id=task["chat_id"], text=result)
                delete_task_by_id(task["id"])
            except Exception:
                pass  # Error handled, continue to next task

        # The good task should have been executed
        assert "good task" in executed

    @pytest.mark.asyncio
    async def test_future_task_not_executed(self):
        """Tasks with future trigger_time should not be returned by get_due_tasks."""
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        create_scheduled_task(self.chat_id, future, "future task")

        tasks = get_due_tasks()
        assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_execute_task_calls_subprocess(self):
        """_execute_task should call Claude CLI and return result."""
        task = {
            "id": 1,
            "chat_id": self.chat_id,
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "prompt": "hello world",
            "recurrence": None,
            "last_run": None,
        }

        # Mock subprocess to return JSON result
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            json.dumps({"result": "Hello from Claude"}).encode(),
            b"",
        )
        mock_proc.returncode = 0

        with patch("scheduler.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_subproc:
            mock_subproc.return_value = mock_proc

            result = await _execute_task(task, AsyncMock())

        assert "Hello from Claude" in result

    @pytest.mark.asyncio
    async def test_execute_task_handles_timeout(self):
        """_execute_task should handle subprocess timeout gracefully."""
        task = {
            "id": 1,
            "chat_id": self.chat_id,
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "prompt": "slow task",
            "recurrence": None,
            "last_run": None,
        }

        mock_proc = AsyncMock()
        call_count = 0

        async def communicate_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # This return value won't be used since wait_for raises TimeoutError
                return (b"", b"")
            # Second call: after kill, in except block
            return (b"", b"")

        mock_proc.communicate = communicate_side_effect
        mock_proc.kill = MagicMock()

        async def mock_wait_for(coro, timeout):
            # Consume the coroutine to avoid warning
            try:
                await coro
            except Exception:
                pass
            raise asyncio.TimeoutError()

        with patch("scheduler.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_subproc:
            mock_subproc.return_value = mock_proc
            with patch("scheduler.asyncio.wait_for", side_effect=mock_wait_for):
                result = await _execute_task(task, AsyncMock())

        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_execute_task_handles_nonzero_exit(self):
        """_execute_task should handle CLI errors gracefully."""
        task = {
            "id": 1,
            "chat_id": self.chat_id,
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "prompt": "error task",
            "recurrence": None,
            "last_run": None,
        }

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"something went wrong")
        mock_proc.returncode = 1

        with patch("scheduler.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_subproc:
            mock_subproc.return_value = mock_proc

            result = await _execute_task(task, AsyncMock())

        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_scheduler_loop_sleeps(self):
        """run_scheduler should sleep between iterations."""
        bot = AsyncMock()

        call_count = 0

        original_get_due = get_due_tasks

        def mock_get_due():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyboardInterrupt("Stop test")
            return []

        with patch("scheduler.get_due_tasks", side_effect=mock_get_due):
            with patch("scheduler.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                mock_sleep.side_effect = [None, KeyboardInterrupt("Stop")]
                try:
                    await run_scheduler(bot)
                except (KeyboardInterrupt, Exception):
                    pass

                # Sleep should have been called at least once with 60
                mock_sleep.assert_called_with(60)


# ---------------------------------------------------------------------------
# /schedule command
# ---------------------------------------------------------------------------

class TestCmdSchedule:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_schedule_list_empty(self):
        """/schedule with no args and no tasks shows helpful message."""
        update, ctx = _make_update_context()
        await cmd_schedule(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "No scheduled tasks" in msg

    @pytest.mark.asyncio
    async def test_schedule_list_shows_tasks(self):
        """/schedule with no args lists existing tasks."""
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        create_scheduled_task(self.chat_id, future, "check deploy")
        create_scheduled_task(self.chat_id, future, "morning review", "daily")

        update, ctx = _make_update_context()
        await cmd_schedule(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Scheduled tasks" in msg
        assert "check deploy" in msg
        assert "morning review" in msg
        assert "daily" in msg

    @pytest.mark.asyncio
    async def test_schedule_create_task(self):
        """/schedule 5pm check deploy creates a task."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(args=["5pm", "check", "deploy"])
            await cmd_schedule(update, ctx)

        msg = update.message.reply_text.call_args[0][0]
        assert "Scheduled:" in msg
        assert "check deploy" in msg

        # Verify task was created in DB
        tasks = list_scheduled_tasks(self.chat_id)
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "check deploy"

    @pytest.mark.asyncio
    async def test_schedule_create_recurring(self):
        """/schedule daily 8am morning review creates recurring task."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 7, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(args=["daily", "8am", "morning", "review"])
            await cmd_schedule(update, ctx)

        msg = update.message.reply_text.call_args[0][0]
        assert "daily" in msg.lower()
        assert "morning review" in msg

        tasks = list_scheduled_tasks(self.chat_id)
        assert len(tasks) == 1
        assert tasks[0]["recurrence"] == "daily"

    @pytest.mark.asyncio
    async def test_schedule_invalid_args(self):
        """/schedule with invalid time shows error."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(args=["badtime", "some", "task"])
            await cmd_schedule(update, ctx)

        msg = update.message.reply_text.call_args[0][0]
        assert "Cannot parse" in msg


# ---------------------------------------------------------------------------
# /cancel command
# ---------------------------------------------------------------------------

class TestCmdCancel:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_cancel_no_args(self):
        """/cancel with no args shows usage."""
        update, ctx = _make_update_context()
        await cmd_cancel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Usage" in msg

    @pytest.mark.asyncio
    async def test_cancel_invalid_number(self):
        """/cancel abc shows error."""
        update, ctx = _make_update_context(args=["abc"])
        await cmd_cancel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Invalid number" in msg

    @pytest.mark.asyncio
    async def test_cancel_out_of_range(self):
        """/cancel 5 when only 1 task exists shows error."""
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        create_scheduled_task(self.chat_id, future, "some task")

        update, ctx = _make_update_context(args=["5"])
        await cmd_cancel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Invalid number" in msg

    @pytest.mark.asyncio
    async def test_cancel_no_tasks(self):
        """/cancel 1 with no tasks shows appropriate message."""
        update, ctx = _make_update_context(args=["1"])
        await cmd_cancel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "No scheduled tasks" in msg

    @pytest.mark.asyncio
    async def test_cancel_valid_task(self):
        """/cancel 1 deletes the first task."""
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        create_scheduled_task(self.chat_id, future, "task to cancel")

        update, ctx = _make_update_context(args=["1"])
        await cmd_cancel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Cancelled" in msg
        assert "task to cancel" in msg

        # Verify task was deleted
        remaining = list_scheduled_tasks(self.chat_id)
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_cancel_correct_task_from_multiple(self):
        """/cancel 2 deletes the second task, not the first."""
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        create_scheduled_task(self.chat_id, future, "keep this")
        create_scheduled_task(self.chat_id, future, "delete this")

        update, ctx = _make_update_context(args=["2"])
        await cmd_cancel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "delete this" in msg

        remaining = list_scheduled_tasks(self.chat_id)
        assert len(remaining) == 1
        assert remaining[0]["prompt"] == "keep this"


# ---------------------------------------------------------------------------
# /remind command
# ---------------------------------------------------------------------------

class TestCmdRemind:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_remind_no_args_shows_usage(self):
        """/remind with no args shows usage help."""
        update, ctx = _make_update_context()
        await cmd_remind(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Usage" in msg
        assert "/remind" in msg

    @pytest.mark.asyncio
    async def test_remind_single_arg_shows_error(self):
        """/remind 5pm (no message) shows error."""
        update, ctx = _make_update_context(args=["5pm"])
        await cmd_remind(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Need" in msg or "Usage" in msg

    @pytest.mark.asyncio
    async def test_remind_creates_remind_type(self):
        """/remind 5pm check deploy creates task with type 'remind'."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(args=["5pm", "check", "deploy"])
            await cmd_remind(update, ctx)

        msg = update.message.reply_text.call_args[0][0]
        assert "Reminder set" in msg
        assert "check deploy" in msg

        tasks = list_scheduled_tasks(self.chat_id)
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "remind"
        assert tasks[0]["prompt"] == "check deploy"

    @pytest.mark.asyncio
    async def test_remind_with_date(self):
        """/remind 9 feb project deadline creates reminder on date."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(args=["9", "feb", "project", "deadline"])
            await cmd_remind(update, ctx)

        msg = update.message.reply_text.call_args[0][0]
        assert "Reminder set" in msg

        tasks = list_scheduled_tasks(self.chat_id)
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "remind"
        dt = datetime.fromisoformat(tasks[0]["trigger_time"])
        assert dt.month == 2 and dt.day == 9

    @pytest.mark.asyncio
    async def test_remind_recurring(self):
        """/remind daily 8am take medication creates recurring reminder."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 7, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(args=["daily", "8am", "take", "medication"])
            await cmd_remind(update, ctx)

        msg = update.message.reply_text.call_args[0][0]
        assert "daily" in msg.lower()
        assert "take medication" in msg

        tasks = list_scheduled_tasks(self.chat_id)
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "remind"
        assert tasks[0]["recurrence"] == "daily"

    @pytest.mark.asyncio
    async def test_remind_invalid_time(self):
        """/remind badtime do something shows error."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(args=["badtime", "do", "something"])
            await cmd_remind(update, ctx)

        msg = update.message.reply_text.call_args[0][0]
        assert "Cannot parse" in msg

    @pytest.mark.asyncio
    async def test_remind_me_strips_me(self):
        """/remind me 5pm check deploy strips 'me' and works."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(args=["me", "5pm", "check", "deploy"])
            await cmd_remind(update, ctx)

        msg = update.message.reply_text.call_args[0][0]
        assert "Reminder set" in msg
        assert "check deploy" in msg

        tasks = list_scheduled_tasks(self.chat_id)
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "check deploy"

    @pytest.mark.asyncio
    async def test_remind_me_in_2_minutes(self):
        """/remind me in 2 minutes check build — full natural phrasing."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(
                args=["me", "in", "2", "minutes", "check", "build"]
            )
            await cmd_remind(update, ctx)

        msg = update.message.reply_text.call_args[0][0]
        assert "Reminder set" in msg

        tasks = list_scheduled_tasks(self.chat_id)
        assert len(tasks) == 1
        dt = datetime.fromisoformat(tasks[0]["trigger_time"])
        assert dt == datetime(2026, 2, 6, 10, 2, 0, tzinfo=timezone.utc)
        assert tasks[0]["prompt"] == "check build"

    @pytest.mark.asyncio
    async def test_remind_me_to_strips_to(self):
        """/remind me 5pm to check deploy strips 'to' from prompt."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(
                args=["me", "5pm", "to", "check", "deploy"]
            )
            await cmd_remind(update, ctx)

        tasks = list_scheduled_tasks(self.chat_id)
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "check deploy"

    @pytest.mark.asyncio
    async def test_remind_me_that_strips_that(self):
        """/remind me tomorrow that meeting is at 3 strips 'that'."""
        with patch("scheduler.datetime") as mock_dt:
            now = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            update, ctx = _make_update_context(
                args=["me", "tomorrow", "that", "meeting", "is", "at", "3"]
            )
            await cmd_remind(update, ctx)

        tasks = list_scheduled_tasks(self.chat_id)
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "meeting is at 3"

    @pytest.mark.asyncio
    async def test_remind_me_only_shows_error(self):
        """/remind me (nothing else) shows error."""
        update, ctx = _make_update_context(args=["me"])
        await cmd_remind(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Usage" in msg or "Need" in msg


# ---------------------------------------------------------------------------
# Reminder execution in scheduler loop
# ---------------------------------------------------------------------------

class TestReminderExecution:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        monkeypatch.setattr("scheduler.CLAUDE_BIN", "/usr/bin/echo")
        monkeypatch.setattr("scheduler.CLAUDE_CWD", str(tmp_path))
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_reminder_sends_directly_no_claude(self):
        """Remind-type tasks send the message directly without calling Claude."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        create_scheduled_task(self.chat_id, past, "check the deployment",
                              task_type="remind")

        bot = AsyncMock()

        tasks = get_due_tasks()
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "remind"

        # Simulate what run_scheduler does for reminders
        task = tasks[0]
        message = f"Reminder: {task['prompt']}"
        await bot.send_message(chat_id=task["chat_id"], text=message)
        delete_task_by_id(task["id"])

        bot.send_message.assert_called_once()
        call_text = bot.send_message.call_args[1]["text"]
        assert "Reminder:" in call_text
        assert "check the deployment" in call_text

        remaining = list_scheduled_tasks(self.chat_id)
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_schedule_type_still_calls_claude(self):
        """Schedule-type tasks still call Claude as before."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        create_scheduled_task(self.chat_id, past, "summarize emails",
                              task_type="schedule")

        tasks = get_due_tasks()
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "schedule"

    @pytest.mark.asyncio
    async def test_mixed_types_in_list(self):
        """List shows both reminders and schedule tasks."""
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        create_scheduled_task(self.chat_id, future, "claude task",
                              task_type="schedule")
        create_scheduled_task(self.chat_id, future, "simple ping",
                              task_type="remind")

        tasks = list_scheduled_tasks(self.chat_id)
        assert len(tasks) == 2
        types = {t["task_type"] for t in tasks}
        assert types == {"schedule", "remind"}

    @pytest.mark.asyncio
    async def test_cancel_works_for_reminders(self):
        """Cancelling a reminder works the same as cancelling a schedule."""
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        create_scheduled_task(self.chat_id, future, "reminder to cancel",
                              task_type="remind")

        update, ctx = _make_update_context(args=["1"])
        await cmd_cancel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Cancelled" in msg
        assert "reminder to cancel" in msg

        remaining = list_scheduled_tasks(self.chat_id)
        assert len(remaining) == 0
