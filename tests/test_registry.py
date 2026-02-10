"""Tests for observer registry and cron matching."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from observers.registry import (
        _match_cron_field,
        matches_cron,
        ObserverRegistry,
    )
    from observers.base import Observer, ObserverContext, ObserverResult


# ── Cron field matching ─────────────────────────────────────────────────────

class TestMatchCronField:

    def test_star_matches_anything(self):
        assert _match_cron_field("*", 0, 59)
        assert _match_cron_field("*", 30, 59)
        assert _match_cron_field("*", 59, 59)

    def test_exact_match(self):
        assert _match_cron_field("5", 5, 59)
        assert not _match_cron_field("5", 6, 59)

    def test_step(self):
        assert _match_cron_field("*/5", 0, 59)
        assert _match_cron_field("*/5", 15, 59)
        assert not _match_cron_field("*/5", 3, 59)

    def test_range(self):
        assert _match_cron_field("1-5", 1, 59)
        assert _match_cron_field("1-5", 3, 59)
        assert _match_cron_field("1-5", 5, 59)
        assert not _match_cron_field("1-5", 0, 59)
        assert not _match_cron_field("1-5", 6, 59)

    def test_range_with_step(self):
        assert _match_cron_field("0-10/2", 0, 59)
        assert _match_cron_field("0-10/2", 4, 59)
        assert not _match_cron_field("0-10/2", 3, 59)
        assert not _match_cron_field("0-10/2", 12, 59)

    def test_comma_list(self):
        assert _match_cron_field("1,5,10", 1, 59)
        assert _match_cron_field("1,5,10", 5, 59)
        assert _match_cron_field("1,5,10", 10, 59)
        assert not _match_cron_field("1,5,10", 3, 59)

    def test_mixed_comma_list(self):
        assert _match_cron_field("1,5-10,*/15", 1, 59)
        assert _match_cron_field("1,5-10,*/15", 7, 59)
        assert _match_cron_field("1,5-10,*/15", 30, 59)
        assert not _match_cron_field("1,5-10,*/15", 3, 59)


# ── Full cron expression matching ────────────────────────────────────────────

class TestMatchesCron:

    def test_every_minute(self):
        dt = datetime(2026, 2, 10, 14, 30, tzinfo=timezone.utc)
        assert matches_cron("* * * * *", dt)

    def test_specific_time(self):
        # 7:30 AM
        dt = datetime(2026, 2, 10, 7, 30, tzinfo=timezone.utc)
        assert matches_cron("30 7 * * *", dt)
        assert not matches_cron("30 8 * * *", dt)

    def test_every_30_minutes(self):
        dt0 = datetime(2026, 2, 10, 14, 0, tzinfo=timezone.utc)
        dt30 = datetime(2026, 2, 10, 14, 30, tzinfo=timezone.utc)
        dt15 = datetime(2026, 2, 10, 14, 15, tzinfo=timezone.utc)
        assert matches_cron("*/30 * * * *", dt0)
        assert matches_cron("*/30 * * * *", dt30)
        assert not matches_cron("*/30 * * * *", dt15)

    def test_weekdays_only(self):
        # Monday = 0 in Python
        monday = datetime(2026, 2, 9, 7, 30, tzinfo=timezone.utc)  # Monday
        saturday = datetime(2026, 2, 14, 7, 30, tzinfo=timezone.utc)  # Saturday
        assert matches_cron("30 7 * * 0-4", monday)
        assert not matches_cron("30 7 * * 0-4", saturday)

    def test_every_2_hours(self):
        dt0 = datetime(2026, 2, 10, 0, 0, tzinfo=timezone.utc)
        dt2 = datetime(2026, 2, 10, 2, 0, tzinfo=timezone.utc)
        dt3 = datetime(2026, 2, 10, 3, 0, tzinfo=timezone.utc)
        assert matches_cron("0 */2 * * *", dt0)
        assert matches_cron("0 */2 * * *", dt2)
        assert not matches_cron("0 */2 * * *", dt3)

    def test_invalid_expression(self):
        dt = datetime(2026, 2, 10, 14, 30, tzinfo=timezone.utc)
        assert not matches_cron("* * *", dt)  # too few fields


# ── Observer registry ────────────────────────────────────────────────────────

class _TestObserver(Observer):
    """Concrete observer for testing."""
    name = "test_observer"
    schedule = "*/5 * * * *"

    def __init__(self):
        self.run_count = 0

    def run(self, ctx=None):
        self.run_count += 1
        return ObserverResult(success=True, message="test done")


class _FailingObserver(Observer):
    name = "failing_observer"
    schedule = "* * * * *"

    def run(self, ctx=None):
        raise RuntimeError("boom")


class _PersistentObserver(Observer):
    name = "persistent_test"
    schedule = ""
    persistent = True

    def run(self, ctx=None):
        return ObserverResult(success=True)


class TestObserverRegistry:

    def test_register_cron_observer(self):
        reg = ObserverRegistry()
        obs = _TestObserver()
        reg.register(obs)
        assert len(reg.observers) == 1
        assert len(reg._persistent) == 0

    def test_register_persistent_observer(self):
        reg = ObserverRegistry()
        obs = _PersistentObserver()
        reg.register(obs)
        assert len(reg.observers) == 0
        assert len(reg._persistent) == 1

    def test_is_due_matches_schedule(self):
        reg = ObserverRegistry()
        obs = _TestObserver()
        reg.register(obs)
        # At minute 0, */5 should match
        dt = datetime(2026, 2, 10, 14, 0, tzinfo=timezone.utc)
        assert reg._is_due(obs, dt)

    def test_is_due_wrong_minute(self):
        reg = ObserverRegistry()
        obs = _TestObserver()
        reg.register(obs)
        dt = datetime(2026, 2, 10, 14, 3, tzinfo=timezone.utc)
        assert not reg._is_due(obs, dt)

    def test_is_due_prevents_double_run(self):
        reg = ObserverRegistry()
        obs = _TestObserver()
        reg.register(obs)
        dt = datetime(2026, 2, 10, 14, 0, tzinfo=timezone.utc)
        assert reg._is_due(obs, dt)
        reg._last_run[obs.name] = dt.timestamp()
        assert not reg._is_due(obs, dt)

    def test_run_observer_success(self):
        reg = ObserverRegistry()
        obs = _TestObserver()
        result = reg._run_observer(obs)
        assert result.success
        assert result.message == "test done"
        assert obs.run_count == 1

    def test_run_observer_catches_crash(self):
        reg = ObserverRegistry()
        obs = _FailingObserver()
        result = reg._run_observer(obs)
        assert not result.success
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_tick_runs_due_observers(self):
        reg = ObserverRegistry()
        obs = _TestObserver()
        obs.schedule = "* * * * *"  # every minute
        reg.register(obs)
        await reg.tick()
        assert obs.run_count == 1


# ── Observer context ─────────────────────────────────────────────────────────

class TestObserverContext:

    def test_context_has_now(self):
        ctx = ObserverContext()
        assert ctx.now is not None
        assert ctx.now.tzinfo == timezone.utc

    def test_context_creates_state_dir(self, tmp_path):
        state = tmp_path / "test_state"
        ctx = ObserverContext(state_dir=state)
        assert state.exists()


# ── Base observer helpers ────────────────────────────────────────────────────

class TestObserverBase:

    def test_call_claude(self):
        obs = _TestObserver()
        with patch("engine.call_sync", return_value={"result": "hello"}) as mock:
            result = obs.call_claude("test prompt")
        assert result == "hello"
        mock.assert_called_once()

    def test_call_claude_empty_result(self):
        obs = _TestObserver()
        with patch("engine.call_sync", return_value={}):
            result = obs.call_claude("test")
        assert result == ""

    @patch("observers.base.urllib.request.urlopen")
    def test_send_telegram(self, mock_urlopen):
        obs = _TestObserver()
        obs.send_telegram("test message")
        mock_urlopen.assert_called_once()

    @patch("observers.base.urllib.request.urlopen")
    def test_send_telegram_chunking(self, mock_urlopen):
        obs = _TestObserver()
        obs.send_telegram("x" * 8000)
        assert mock_urlopen.call_count == 2
