"""Tests for observers/base.py — Observer ABC, ObserverContext, ObserverResult."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from observers.base import Observer, ObserverContext, ObserverResult


# ---------------------------------------------------------------------------
# Concrete test observer (needed because Observer is abstract)
# ---------------------------------------------------------------------------


class DummyObserver(Observer):
    name = "dummy"
    schedule = "*/5 * * * *"

    def __init__(self, result=None):
        self._result = result or ObserverResult(success=True, message="ok")

    def run(self, ctx: ObserverContext) -> ObserverResult:
        return self._result


# ---------------------------------------------------------------------------
# ObserverResult
# ---------------------------------------------------------------------------


class TestObserverResult:

    def test_default_values(self):
        r = ObserverResult()
        assert r.success is True
        assert r.message == ""
        assert r.error == ""
        assert r.data == {}

    def test_failure_result(self):
        r = ObserverResult(success=False, error="something broke")
        assert r.success is False
        assert r.error == "something broke"

    def test_with_message_and_data(self):
        r = ObserverResult(message="All nodes up", data={"count": 10})
        assert r.message == "All nodes up"
        assert r.data["count"] == 10


# ---------------------------------------------------------------------------
# ObserverContext
# ---------------------------------------------------------------------------


class TestObserverContext:

    def test_default_now_is_utc(self):
        ctx = ObserverContext()
        assert ctx.now.tzinfo is not None
        # Should be very recent
        delta = (datetime.now(timezone.utc) - ctx.now).total_seconds()
        assert delta < 2

    def test_custom_now(self):
        fixed = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
        ctx = ObserverContext(now=fixed)
        assert ctx.now == fixed

    def test_state_dir_default(self):
        ctx = ObserverContext()
        assert ctx.state_dir.name == ".state"
        assert "observers" in str(ctx.state_dir)

    def test_custom_state_dir(self, tmp_path):
        ctx = ObserverContext(state_dir=tmp_path / "custom_state")
        assert ctx.state_dir == tmp_path / "custom_state"
        assert ctx.state_dir.exists()  # __post_init__ creates it


# ---------------------------------------------------------------------------
# Observer ABC
# ---------------------------------------------------------------------------


class TestObserverABC:

    def test_cannot_instantiate_abc(self):
        """Cannot instantiate Observer directly."""
        with pytest.raises(TypeError):
            Observer()

    def test_concrete_subclass_works(self):
        obs = DummyObserver()
        assert obs.name == "dummy"
        assert obs.schedule == "*/5 * * * *"

    def test_run_returns_result(self):
        obs = DummyObserver()
        ctx = ObserverContext()
        result = obs.run(ctx)
        assert result.success is True
        assert result.message == "ok"


# ---------------------------------------------------------------------------
# send_telegram helper
# ---------------------------------------------------------------------------


class TestSendTelegram:

    def test_send_short_message(self):
        obs = DummyObserver()
        with patch("observers.base.urllib.request.urlopen") as mock_urlopen:
            obs.send_telegram("Hello world")
        mock_urlopen.assert_called_once()

    def test_send_long_message_chunks(self):
        obs = DummyObserver()
        # Create a message that needs chunking (> 4000 chars)
        text = "Line\n" * 1000  # 5000 chars
        with patch("observers.base.urllib.request.urlopen") as mock_urlopen:
            obs.send_telegram(text)
        assert mock_urlopen.call_count == 2

    def test_send_uses_config_defaults(self):
        obs = DummyObserver()
        with patch("observers.base.urllib.request.urlopen") as mock_urlopen:
            obs.send_telegram("test")
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "fake:token" in req.full_url
        assert b"12345" in req.data

    def test_send_custom_token_and_chat(self):
        obs = DummyObserver()
        with patch("observers.base.urllib.request.urlopen") as mock_urlopen:
            obs.send_telegram("test", token="custom:tok", chat_id="99999")
        req = mock_urlopen.call_args[0][0]
        assert "custom:tok" in req.full_url
        assert b"99999" in req.data

    def test_send_failure_logged_not_raised(self):
        obs = DummyObserver()
        with patch("observers.base.urllib.request.urlopen", side_effect=Exception("network")):
            # Should not raise
            obs.send_telegram("test")


# ---------------------------------------------------------------------------
# send_telegram_html helper
# ---------------------------------------------------------------------------


class TestSendTelegramHtml:

    def test_sends_with_html_parse_mode(self):
        obs = DummyObserver()
        with patch("observers.base.urllib.request.urlopen") as mock_urlopen:
            obs.send_telegram_html("<b>bold</b>")
        req = mock_urlopen.call_args[0][0]
        assert b"HTML" in req.data


# ---------------------------------------------------------------------------
# call_claude helper
# ---------------------------------------------------------------------------


class TestCallClaude:

    def test_call_claude_returns_result(self):
        obs = DummyObserver()
        with patch("engine.call_sync", return_value={"result": "Claude says hi"}):
            result = obs.call_claude("hello")
        assert result == "Claude says hi"

    def test_call_claude_empty_response(self):
        obs = DummyObserver()
        with patch("engine.call_sync", return_value={"result": ""}):
            result = obs.call_claude("hello")
        assert result == ""

    def test_call_claude_passes_model(self):
        obs = DummyObserver()
        with patch("engine.call_sync", return_value={"result": "ok"}) as mock:
            obs.call_claude("prompt", model="opus", timeout=60)
        mock.assert_called_once_with("prompt", model="opus", timeout=60)


# ---------------------------------------------------------------------------
# now_utc helper
# ---------------------------------------------------------------------------


class TestNowUtc:

    def test_returns_utc_datetime(self):
        obs = DummyObserver()
        now = obs.now_utc()
        assert now.tzinfo is not None
        delta = (datetime.now(timezone.utc) - now).total_seconds()
        assert delta < 2
