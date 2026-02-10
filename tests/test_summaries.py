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
# _generate_summary — Claude CLI call and result handling
# ---------------------------------------------------------------------------

class TestGenerateSummary:

    def _make_proc(self, stdout_data: bytes, stderr_data: bytes = b"", returncode: int = 0):
        """Create a mock process."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(stdout_data, stderr_data))
        proc.returncode = returncode
        return proc

    @pytest.mark.asyncio
    async def test_calls_claude_with_correct_args(self):
        """Should call Claude CLI with haiku model, --resume, and json output."""
        result_json = json.dumps({"result": "Debugging a Python script"})
        proc = self._make_proc(result_json.encode())

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as mock_exec, \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")

            # Verify the command args
            call_args = mock_exec.call_args
            cmd_args = call_args[0]  # positional args
            assert cmd_args[0].endswith("claude") or "claude" in cmd_args[0]
            assert "-p" in cmd_args
            assert "--output-format" in cmd_args
            assert "json" in cmd_args
            assert "--model" in cmd_args
            assert "haiku" in cmd_args
            assert "--resume" in cmd_args
            assert "sess-xyz" in cmd_args

    @pytest.mark.asyncio
    async def test_stores_summary(self):
        """Should call update_summary with the parsed result."""
        result_json = json.dumps({"result": "Debugging a Python script"})
        proc = self._make_proc(result_json.encode())

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_called_once_with(12345, "Debugging a Python script")

    @pytest.mark.asyncio
    async def test_handles_non_json_stdout(self):
        """Should handle plain text stdout (not JSON)."""
        proc = self._make_proc(b"Setting up Docker containers")

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_called_once_with(12345, "Setting up Docker containers")

    @pytest.mark.asyncio
    async def test_handles_nonzero_exit(self):
        """Non-zero exit code should not call update_summary."""
        proc = self._make_proc(b"", stderr_data=b"Error: session not found", returncode=1)

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        """Timeout should be handled gracefully without exception."""
        async def slow_communicate():
            await asyncio.sleep(100)
            return (b"", b"")

        proc = AsyncMock()
        proc.communicate = slow_communicate
        proc.returncode = 0

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
             patch("handlers.summaries.update_summary") as mock_update, \
             patch("handlers.summaries.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            # Should not raise
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_summary_over_200_chars(self):
        """Summary longer than 200 chars should be rejected."""
        long_summary = "x" * 250
        result_json = json.dumps({"result": long_summary})
        proc = self._make_proc(result_json.encode())

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_empty_summary(self):
        """Empty summary should not be stored."""
        result_json = json.dumps({"result": ""})
        proc = self._make_proc(result_json.encode())

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_whitespace_only_summary(self):
        """Whitespace-only summary should not be stored."""
        result_json = json.dumps({"result": "   \n  "})
        proc = self._make_proc(result_json.encode())

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_generic_exception(self):
        """Generic exceptions should be caught and logged."""
        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, side_effect=OSError("spawn failed")), \
             patch("handlers.summaries.update_summary") as mock_update:
            # Should not raise
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_summary_exactly_200_chars_rejected(self):
        """Summary at exactly 200 chars should be rejected (must be < 200)."""
        summary_200 = "x" * 200
        result_json = json.dumps({"result": summary_200})
        proc = self._make_proc(result_json.encode())

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_summary_199_chars_accepted(self):
        """Summary at 199 chars should be accepted (< 200)."""
        summary_199 = "x" * 199
        result_json = json.dumps({"result": summary_199})
        proc = self._make_proc(result_json.encode())

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
             patch("handlers.summaries.update_summary") as mock_update:
            await _generate_summary(12345, "sess-xyz")
            mock_update.assert_called_once_with(12345, summary_199)

    @pytest.mark.asyncio
    async def test_cwd_set_correctly(self):
        """Should pass CLAUDE_CWD as the working directory."""
        result_json = json.dumps({"result": "Test summary"})
        proc = self._make_proc(result_json.encode())

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as mock_exec, \
             patch("handlers.summaries.update_summary"):
            await _generate_summary(12345, "sess-xyz")

            call_kwargs = mock_exec.call_args[1]
            assert "cwd" in call_kwargs

    @pytest.mark.asyncio
    async def test_subprocess_uses_pipes(self):
        """Should capture both stdout and stderr via PIPE."""
        result_json = json.dumps({"result": "Test summary"})
        proc = self._make_proc(result_json.encode())

        with patch("handlers.summaries.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as mock_exec, \
             patch("handlers.summaries.update_summary"):
            await _generate_summary(12345, "sess-xyz")

            call_kwargs = mock_exec.call_args[1]
            assert call_kwargs["stdout"] == asyncio.subprocess.PIPE
            assert call_kwargs["stderr"] == asyncio.subprocess.PIPE
