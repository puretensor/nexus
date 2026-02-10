"""Tests for observers/registry.py — cron matching, ObserverRegistry."""

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from observers.base import Observer, ObserverContext, ObserverResult
    from observers.registry import (
        _match_cron_field,
        matches_cron,
        ObserverRegistry,
    )


# ---------------------------------------------------------------------------
# Concrete test observer
# ---------------------------------------------------------------------------


class StubObserver(Observer):
    def __init__(self, name="stub", schedule="* * * * *", result=None, side_effect=None):
        self.name = name
        self.schedule = schedule
        self._result = result or ObserverResult(success=True, message="done")
        self._side_effect = side_effect
        self.run_count = 0

    def run(self, ctx: ObserverContext) -> ObserverResult:
        self.run_count += 1
        if self._side_effect:
            raise self._side_effect
        return self._result


# ---------------------------------------------------------------------------
# _match_cron_field
# ---------------------------------------------------------------------------


class TestMatchCronField:

    def test_star_matches_any(self):
        assert _match_cron_field("*", 0, 59) is True
        assert _match_cron_field("*", 30, 59) is True
        assert _match_cron_field("*", 59, 59) is True

    def test_exact_match(self):
        assert _match_cron_field("5", 5, 59) is True
        assert _match_cron_field("5", 6, 59) is False
        assert _match_cron_field("0", 0, 59) is True

    def test_step_star(self):
        """*/N matches values divisible by N."""
        assert _match_cron_field("*/5", 0, 59) is True
        assert _match_cron_field("*/5", 5, 59) is True
        assert _match_cron_field("*/5", 10, 59) is True
        assert _match_cron_field("*/5", 3, 59) is False
        assert _match_cron_field("*/15", 30, 59) is True
        assert _match_cron_field("*/15", 7, 59) is False

    def test_range(self):
        assert _match_cron_field("1-5", 1, 31) is True
        assert _match_cron_field("1-5", 3, 31) is True
        assert _match_cron_field("1-5", 5, 31) is True
        assert _match_cron_field("1-5", 0, 31) is False
        assert _match_cron_field("1-5", 6, 31) is False

    def test_range_with_step(self):
        """N-M/S matches values in range [N,M] where (value-N) % S == 0."""
        assert _match_cron_field("0-10/2", 0, 59) is True
        assert _match_cron_field("0-10/2", 2, 59) is True
        assert _match_cron_field("0-10/2", 4, 59) is True
        assert _match_cron_field("0-10/2", 1, 59) is False
        assert _match_cron_field("0-10/2", 12, 59) is False

    def test_comma_list(self):
        assert _match_cron_field("1,5,10", 1, 59) is True
        assert _match_cron_field("1,5,10", 5, 59) is True
        assert _match_cron_field("1,5,10", 10, 59) is True
        assert _match_cron_field("1,5,10", 3, 59) is False

    def test_comma_with_ranges(self):
        assert _match_cron_field("1-3,7-9", 2, 31) is True
        assert _match_cron_field("1-3,7-9", 8, 31) is True
        assert _match_cron_field("1-3,7-9", 5, 31) is False

    def test_comma_with_step(self):
        assert _match_cron_field("*/10,5", 5, 59) is True
        assert _match_cron_field("*/10,5", 10, 59) is True
        assert _match_cron_field("*/10,5", 7, 59) is False


# ---------------------------------------------------------------------------
# matches_cron
# ---------------------------------------------------------------------------


class TestMatchesCron:

    def test_every_minute(self):
        dt = datetime(2026, 2, 10, 14, 30, tzinfo=timezone.utc)
        assert matches_cron("* * * * *", dt) is True

    def test_specific_time(self):
        dt = datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc)
        assert matches_cron("0 8 * * *", dt) is True
        assert matches_cron("0 9 * * *", dt) is False

    def test_every_30_minutes(self):
        dt_00 = datetime(2026, 2, 10, 14, 0, tzinfo=timezone.utc)
        dt_30 = datetime(2026, 2, 10, 14, 30, tzinfo=timezone.utc)
        dt_15 = datetime(2026, 2, 10, 14, 15, tzinfo=timezone.utc)
        assert matches_cron("*/30 * * * *", dt_00) is True
        assert matches_cron("*/30 * * * *", dt_30) is True
        assert matches_cron("*/30 * * * *", dt_15) is False

    def test_weekday_filter(self):
        """Monday=0, Sunday=6 in Python convention."""
        # 2026-02-10 is a Tuesday (weekday=1)
        dt = datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc)
        assert matches_cron("0 8 * * 1", dt) is True   # Tuesday
        assert matches_cron("0 8 * * 0", dt) is False   # Monday
        assert matches_cron("0 8 * * 1-4", dt) is True  # Tue-Fri

    def test_weekdays_only(self):
        """0-4 = Mon-Fri."""
        monday = datetime(2026, 2, 9, 8, 0, tzinfo=timezone.utc)   # Monday=0
        saturday = datetime(2026, 2, 14, 8, 0, tzinfo=timezone.utc)  # Saturday=5
        assert matches_cron("0 8 * * 0-4", monday) is True
        assert matches_cron("0 8 * * 0-4", saturday) is False

    def test_specific_day_of_month(self):
        dt = datetime(2026, 2, 15, 9, 0, tzinfo=timezone.utc)
        assert matches_cron("0 9 15 * *", dt) is True
        assert matches_cron("0 9 14 * *", dt) is False

    def test_specific_month(self):
        dt = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        assert matches_cron("0 0 1 6 *", dt) is True
        assert matches_cron("0 0 1 7 *", dt) is False

    def test_invalid_cron_returns_false(self):
        dt = datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc)
        assert matches_cron("* * *", dt) is False  # Too few fields
        assert matches_cron("* * * * * *", dt) is False  # Too many

    def test_combined_expression(self):
        """30 7 * * 0-4 = 7:30 AM weekdays."""
        weekday_730 = datetime(2026, 2, 10, 7, 30, tzinfo=timezone.utc)  # Tuesday
        weekend_730 = datetime(2026, 2, 14, 7, 30, tzinfo=timezone.utc)  # Saturday
        weekday_800 = datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc)
        assert matches_cron("30 7 * * 0-4", weekday_730) is True
        assert matches_cron("30 7 * * 0-4", weekend_730) is False
        assert matches_cron("30 7 * * 0-4", weekday_800) is False


# ---------------------------------------------------------------------------
# ObserverRegistry
# ---------------------------------------------------------------------------


class TestObserverRegistry:

    def test_register(self):
        reg = ObserverRegistry()
        obs = StubObserver(name="test_obs", schedule="0 8 * * *")
        reg.register(obs)
        assert len(reg.observers) == 1
        assert reg.observers[0].name == "test_obs"

    def test_register_multiple(self):
        reg = ObserverRegistry()
        reg.register(StubObserver(name="a"))
        reg.register(StubObserver(name="b"))
        assert len(reg.observers) == 2


# ---------------------------------------------------------------------------
# _is_due
# ---------------------------------------------------------------------------


class TestIsDue:

    def test_due_when_schedule_matches(self):
        reg = ObserverRegistry()
        obs = StubObserver(name="test", schedule="30 8 * * *")
        reg.register(obs)
        now = datetime(2026, 2, 10, 8, 30, tzinfo=timezone.utc)
        assert reg._is_due(obs, now) is True

    def test_not_due_when_schedule_doesnt_match(self):
        reg = ObserverRegistry()
        obs = StubObserver(name="test", schedule="30 8 * * *")
        reg.register(obs)
        now = datetime(2026, 2, 10, 9, 0, tzinfo=timezone.utc)
        assert reg._is_due(obs, now) is False

    def test_not_due_when_empty_schedule(self):
        reg = ObserverRegistry()
        obs = StubObserver(name="test", schedule="")
        reg.register(obs)
        now = datetime(2026, 2, 10, 8, 30, tzinfo=timezone.utc)
        assert reg._is_due(obs, now) is False

    def test_dedup_same_minute(self):
        """Should not run twice in the same minute."""
        reg = ObserverRegistry()
        obs = StubObserver(name="test", schedule="* * * * *")
        reg.register(obs)
        now = datetime(2026, 2, 10, 8, 30, 15, tzinfo=timezone.utc)

        assert reg._is_due(obs, now) is True
        # Simulate having run it
        reg._last_run["test"] = now.replace(second=0, microsecond=0).timestamp()
        assert reg._is_due(obs, now) is False

    def test_due_again_next_minute(self):
        reg = ObserverRegistry()
        obs = StubObserver(name="test", schedule="* * * * *")
        reg.register(obs)

        now_830 = datetime(2026, 2, 10, 8, 30, 0, tzinfo=timezone.utc)
        reg._last_run["test"] = now_830.replace(second=0, microsecond=0).timestamp()

        now_831 = datetime(2026, 2, 10, 8, 31, 0, tzinfo=timezone.utc)
        assert reg._is_due(obs, now_831) is True


# ---------------------------------------------------------------------------
# _run_observer
# ---------------------------------------------------------------------------


class TestRunObserver:

    def test_successful_run(self):
        reg = ObserverRegistry()
        obs = StubObserver(result=ObserverResult(success=True, message="all good"))
        result = reg._run_observer(obs)
        assert result.success is True
        assert result.message == "all good"
        assert obs.run_count == 1

    def test_crashed_observer_returns_failure(self):
        reg = ObserverRegistry()
        obs = StubObserver(side_effect=RuntimeError("kaboom"))
        result = reg._run_observer(obs)
        assert result.success is False
        assert "kaboom" in result.error


# ---------------------------------------------------------------------------
# tick (async)
# ---------------------------------------------------------------------------


class TestTick:

    @pytest.mark.asyncio
    async def test_tick_runs_due_observer(self):
        reg = ObserverRegistry()
        obs = StubObserver(name="ticker", schedule="* * * * *")
        reg.register(obs)

        with patch("observers.registry.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 8, 30, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await reg.tick()

        assert obs.run_count == 1

    @pytest.mark.asyncio
    async def test_tick_skips_not_due(self):
        reg = ObserverRegistry()
        obs = StubObserver(name="nope", schedule="0 3 * * *")  # 3 AM only
        reg.register(obs)

        with patch("observers.registry.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 8, 30, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await reg.tick()

        assert obs.run_count == 0

    @pytest.mark.asyncio
    async def test_tick_error_sends_telegram(self):
        reg = ObserverRegistry()
        obs = StubObserver(
            name="crasher",
            schedule="* * * * *",
            side_effect=RuntimeError("boom"),
        )
        reg.register(obs)

        with patch("observers.registry.datetime") as mock_dt, \
             patch.object(obs, "send_telegram") as mock_tg:
            mock_dt.now.return_value = datetime(2026, 2, 10, 8, 30, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await reg.tick()

        mock_tg.assert_called_once()
        assert "ERROR" in mock_tg.call_args[0][0]

    @pytest.mark.asyncio
    async def test_tick_sets_last_run(self):
        reg = ObserverRegistry()
        obs = StubObserver(name="tracker", schedule="* * * * *")
        reg.register(obs)

        with patch("observers.registry.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 8, 30, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await reg.tick()

        assert "tracker" in reg._last_run
        assert reg._last_run["tracker"] > 0
