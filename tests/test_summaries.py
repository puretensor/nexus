"""Tests for handlers/summaries.py — auto-generate session summaries (Feature 3B)."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from handlers.summaries import maybe_generate_summary, _generate_summary, SUMMARY_INTERVAL


# ---------------------------------------------------------------------------
# maybe_generate_summary — interval gating
# ---------------------------------------------------------------------------

class TestMaybeGenerateSummary:

    @pytest.mark.asyncio
    async def test_no_session_does_nothing(self):
        """No active session means no summary generation."""
        with patch("handlers.summaries.get_session", return_value=None) as mock_get, \
             patch("handlers.summaries.asyncio.create_task") as mock_task:
            await maybe_generate_summary(99999)
            mock_get.assert_called_once_with(99999)
            mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_session_id_does_nothing(self):
        """Session without session_id means no summary generation."""
        session = {"session_id": None, "message_count": 5}
        with patch("handlers.summaries.get_session", return_value=session), \
             patch("handlers.summaries.asyncio.create_task") as mock_task:
            await maybe_generate_summary(12345)
            mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_msg_count_zero_does_nothing(self):
        """Message count of 0 does not trigger summary."""
        session = {"session_id": "sess-123", "message_count": 0}
        with patch("handlers.summaries.get_session", return_value=session), \
             patch("handlers.summaries.asyncio.create_task") as mock_task:
            await maybe_generate_summary(12345)
            mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_multiple_does_nothing(self):
        """Message count not a multiple of SUMMARY_INTERVAL does not trigger."""
        for count in [1, 2, 3, 4, 6, 7, 8, 9, 11]:
            session = {"session_id": "sess-123", "message_count": count}
            with patch("handlers.summaries.get_session", return_value=session), \
                 patch("handlers.summaries.asyncio.create_task") as mock_task:
                await maybe_generate_summary(12345)
                mock_task.assert_not_called(), f"Should not trigger for count={count}"

    @pytest.mark.asyncio
    async def test_multiple_triggers_task(self):
        """Message count at SUMMARY_INTERVAL triggers background task."""
        session = {"session_id": "sess-abc", "message_count": SUMMARY_INTERVAL}
        with patch("handlers.summaries.get_session", return_value=session), \
             patch("handlers.summaries.asyncio.create_task") as mock_task:
            await maybe_generate_summary(12345)
            mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_double_interval_triggers(self):
        """Message count at 2x SUMMARY_INTERVAL also triggers."""
        session = {"session_id": "sess-abc", "message_count": SUMMARY_INTERVAL * 2}
        with patch("handlers.summaries.get_session", return_value=session), \
             patch("handlers.summaries.asyncio.create_task") as mock_task:
            await maybe_generate_summary(12345)
            mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_message_count_does_nothing(self):
        """Session dict without message_count key defaults to 0, does nothing."""
        session = {"session_id": "sess-abc"}
        with patch("handlers.summaries.get_session", return_value=session), \
             patch("handlers.summaries.asyncio.create_task") as mock_task:
            await maybe_generate_summary(12345)
            mock_task.assert_not_called()


# ---------------------------------------------------------------------------
# _generate_summary — engine.call_sync and result handling
# ---------------------------------------------------------------------------

class TestGenerateSummary:

    @pytest.mark.asyncio
    async def test_calls_engine_with_correct_args(self):
        """Should call engine.call_sync with haiku model and session_id."""
        with patch("engine.call_sync", return_value={"result": "Debugging a Python script", "session_id": "sess-xyz"}) as mock_call, \
             patch("handlers.summaries.update_summary"):
            await _generate_summary(12345, "sess-xyz")

            mock_call.assert_called_once()
            call_kwargs = mock_call.call_args
            # call_sync(prompt, model="haiku", session_id="sess-xyz", timeout=30)
            assert call_kwargs[1]["model"] == "haiku"
            assert call_kwargs[1]["session_id"] == "sess-xyz"
            assert call_kwargs[1]["timeout"] == 30

    @pytest.mark.asyncio
    async def test_stores_summary(self):
        """Should call update_summary with the parsed result."""
        with patch("engine.call_sync", return_value={"result": "Debugging a Python script", "session_id": "sess-xyz"}), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_called_once_with(12345, "Debugging a Python script")

    @pytest.mark.asyncio
    async def test_handles_error_result(self):
        """Error result from engine should still be stored if short enough."""
        with patch("engine.call_sync", return_value={"result": "Claude timed out after 30s", "session_id": None}), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_called_once_with(12345, "Claude timed out after 30s")

    @pytest.mark.asyncio
    async def test_rejects_summary_over_200_chars(self):
        """Summary longer than 200 chars should be rejected."""
        long_summary = "x" * 250
        with patch("engine.call_sync", return_value={"result": long_summary, "session_id": None}), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_empty_summary(self):
        """Empty summary should not be stored."""
        with patch("engine.call_sync", return_value={"result": "", "session_id": None}), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_whitespace_only_summary(self):
        """Whitespace-only summary should not be stored."""
        with patch("engine.call_sync", return_value={"result": "   \n  ", "session_id": None}), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_generic_exception(self):
        """Generic exceptions should be caught and logged."""
        with patch("engine.call_sync", side_effect=OSError("spawn failed")), \
             patch("handlers.summaries.update_summary") as mock_update:
            # Should not raise
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_summary_exactly_200_chars_rejected(self):
        """Summary at exactly 200 chars should be rejected (must be < 200)."""
        summary_200 = "x" * 200
        with patch("engine.call_sync", return_value={"result": summary_200, "session_id": None}), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_summary_199_chars_accepted(self):
        """Summary at 199 chars should be accepted (< 200)."""
        summary_199 = "x" * 199
        with patch("engine.call_sync", return_value={"result": summary_199, "session_id": None}), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_called_once_with(12345, summary_199)
