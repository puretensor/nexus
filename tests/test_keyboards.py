"""Tests for handlers/keyboards.py — contextual inline keyboards (Phase 2C).

Tests cover:
- Pattern matching: infra responses, code responses, long responses
- Keyboard structure: correct buttons and callback_data
- No keyboard for irrelevant responses
- Callback handler integration: action:retry, action:details, action:commit,
  action:diff, action:summarize
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from handlers.keyboards import get_contextual_keyboard, _is_infra_response, _is_code_response
    from channels.telegram.callbacks import handle_callback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_callback_update(callback_data, chat_id=12345, user_id=12345):
    """Create mock Update/Context for callback query handlers."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.text = "Some previous response"
    update.callback_query.message.edit_text = AsyncMock()
    update.callback_query.message.edit_reply_markup = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return update, context


# ---------------------------------------------------------------------------
# No keyboard for normal responses
# ---------------------------------------------------------------------------


class TestNoKeyboard:

    def test_no_keyboard_for_short_normal_response(self):
        """Short, non-infra, non-code response returns None."""
        result = get_contextual_keyboard("The weather in London is 15C and partly cloudy.")
        assert result is None

    def test_no_keyboard_for_greeting(self):
        """Simple greeting returns None."""
        result = get_contextual_keyboard("Hello! How can I help?")
        assert result is None

    def test_short_response_no_summarize(self):
        """500-char normal response returns None (not long enough for Summarize)."""
        text = "Here is some general information about Python. " * 10  # ~470 chars
        assert len(text) < 2000
        result = get_contextual_keyboard(text)
        assert result is None


# ---------------------------------------------------------------------------
# Infrastructure responses
# ---------------------------------------------------------------------------


class TestInfraKeyboard:

    def test_infra_response_gets_retry_details(self):
        """Response with service failure gets Retry+Details buttons."""
        text = "The service nginx failed to restart on mon2."
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None
        buttons = keyboard.inline_keyboard[0]
        labels = [b.text for b in buttons]
        assert "Retry" in labels
        assert "Details" in labels

    def test_infra_node_down(self):
        """Node unreachable with ping timeout triggers infra keyboard."""
        text = "node arx1 is unreachable, ping timeout after 10 seconds"
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None
        callback_data = [b.callback_data for b in keyboard.inline_keyboard[0]]
        assert "action:retry" in callback_data
        assert "action:details" in callback_data

    def test_infra_systemctl(self):
        """Running systemctl restart triggers infra keyboard."""
        text = "Running: systemctl restart prometheus-node-exporter"
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None

    def test_infra_disk_critical(self):
        """Disk usage critical triggers infra keyboard."""
        text = "disk usage is critical at 95% on /dev/sda1"
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None


# ---------------------------------------------------------------------------
# Code change responses
# ---------------------------------------------------------------------------


class TestCodeKeyboard:

    def test_code_response_gets_commit_diff(self):
        """Response about writing a file gets Commit+Diff buttons."""
        text = "I've written the changes to handlers/location.py"
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None
        buttons = keyboard.inline_keyboard[0]
        labels = [b.text for b in buttons]
        assert "Commit" in labels
        assert "Diff" in labels

    def test_code_git_diff(self):
        """Response mentioning git diff triggers code keyboard."""
        text = "Here's the git diff of the changes I made"
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None
        callback_data = [b.callback_data for b in keyboard.inline_keyboard[0]]
        assert "action:commit" in callback_data
        assert "action:diff" in callback_data

    def test_code_modified_file(self):
        """Response about modifying files triggers code keyboard."""
        text = "Modified 3 files in the project to fix the import issue"
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None


# ---------------------------------------------------------------------------
# Long response (Summarize)
# ---------------------------------------------------------------------------


class TestSummarizeKeyboard:

    def test_long_response_gets_summarize(self):
        """Response over 2000 chars (non-infra, non-code) gets Summarize button."""
        text = "Here is a detailed explanation of the topic. " * 60  # ~2700 chars
        assert len(text) > 2000
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None
        buttons = keyboard.inline_keyboard[0]
        assert len(buttons) == 1
        assert buttons[0].text == "Summarize"
        assert buttons[0].callback_data == "action:summarize"


# ---------------------------------------------------------------------------
# Pattern helpers directly
# ---------------------------------------------------------------------------


class TestIsInfraResponse:

    def test_service_down(self):
        assert _is_infra_response("The docker container failed to start")

    def test_ssh_timeout(self):
        assert _is_infra_response("ssh connection timeout to 192.168.4.185")

    def test_cpu_high(self):
        assert _is_infra_response("CPU load is high at 98% on tensor-core")

    def test_restarted_service(self):
        assert _is_infra_response("Successfully restarted the service on mon2")

    def test_prometheus(self):
        assert _is_infra_response("Checking prometheus status")

    def test_grafana(self):
        assert _is_infra_response("grafana is responding on port 3000")

    def test_normal_text_not_infra(self):
        assert not _is_infra_response("The weather is nice today")

    def test_empty_string(self):
        assert not _is_infra_response("")


class TestIsCodeResponse:

    def test_wrote_file(self):
        assert _is_code_response("I wrote the changes to config.py")

    def test_modified_file(self):
        assert _is_code_response("Updated the handler in routes.js")

    def test_git_commit(self):
        assert _is_code_response("Running git commit with message")

    def test_git_add(self):
        assert _is_code_response("Running git add for the new files")

    def test_edit_tool(self):
        assert _is_code_response("Used the Edit tool to update the function")

    def test_changes_to_file(self):
        assert _is_code_response("Made changes to streaming.py")

    def test_modified_n_files(self):
        assert _is_code_response("modified 5 files in the repository")

    def test_normal_text_not_code(self):
        assert not _is_code_response("Here is how to cook pasta")

    def test_empty_string(self):
        assert not _is_code_response("")


# ---------------------------------------------------------------------------
# Keyboard structure
# ---------------------------------------------------------------------------


class TestKeyboardStructure:

    def test_infra_keyboard_structure(self):
        """Infra keyboard has exactly one row with Retry and Details."""
        text = "The server nginx failed with error code 1"
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 1
        row = keyboard.inline_keyboard[0]
        assert len(row) == 2
        assert row[0].text == "Retry"
        assert row[0].callback_data == "action:retry"
        assert row[1].text == "Details"
        assert row[1].callback_data == "action:details"

    def test_code_keyboard_structure(self):
        """Code keyboard has exactly one row with Commit and Diff."""
        text = "I've written the fix to handlers/bot.py"
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 1
        row = keyboard.inline_keyboard[0]
        assert len(row) == 2
        assert row[0].text == "Commit"
        assert row[0].callback_data == "action:commit"
        assert row[1].text == "Diff"
        assert row[1].callback_data == "action:diff"

    def test_summarize_keyboard_structure(self):
        """Summarize keyboard has exactly one row with one button."""
        text = "Detailed explanation " * 150  # > 2000 chars
        keyboard = get_contextual_keyboard(text)
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 1
        row = keyboard.inline_keyboard[0]
        assert len(row) == 1
        assert row[0].text == "Summarize"
        assert row[0].callback_data == "action:summarize"


# ---------------------------------------------------------------------------
# Callback handler integration tests
# ---------------------------------------------------------------------------


def _passthrough_authorized(func):
    """No-op authorized decorator for testing."""
    return func


class TestCallbackRetry:

    @pytest.mark.asyncio
    async def test_callback_retry_calls_claude(self):
        """action:retry should call call_streaming with retry prompt."""
        update, ctx = _make_callback_update("action:retry")

        mock_editor = MagicMock()
        mock_editor.text = "Retry result"
        mock_editor.finalize = AsyncMock()
        mock_editor.sent_messages = []

        mock_session = {
            "session_id": "sess-123",
            "model": "sonnet",
            "message_count": 5,
            "name": "default",
        }

        with patch("channels.telegram.callbacks.get_session", return_value=mock_session), \
             patch("channels.telegram.callbacks.StreamingEditor", return_value=mock_editor), \
             patch("channels.telegram.callbacks.call_streaming", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = {"result": "Retry result", "session_id": "sess-123"}
            await handle_callback(update, ctx)

        mock_claude.assert_awaited_once()
        call_args = mock_claude.call_args
        assert "retry" in call_args[0][0].lower()
        assert call_args[0][1] == "sess-123"
        assert call_args[0][2] == "sonnet"
        mock_editor.finalize.assert_awaited_once()


class TestCallbackDetails:

    @pytest.mark.asyncio
    async def test_callback_details_calls_claude(self):
        """action:details should call call_streaming with details prompt."""
        update, ctx = _make_callback_update("action:details")

        mock_editor = MagicMock()
        mock_editor.text = "Detailed info"
        mock_editor.finalize = AsyncMock()
        mock_editor.sent_messages = []

        mock_session = {
            "session_id": "sess-456",
            "model": "opus",
            "message_count": 3,
            "name": "default",
        }

        with patch("channels.telegram.callbacks.get_session", return_value=mock_session), \
             patch("channels.telegram.callbacks.StreamingEditor", return_value=mock_editor), \
             patch("channels.telegram.callbacks.call_streaming", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = {"result": "Detailed info", "session_id": "sess-456"}
            await handle_callback(update, ctx)

        mock_claude.assert_awaited_once()
        call_args = mock_claude.call_args
        assert "details" in call_args[0][0].lower()
        assert call_args[0][1] == "sess-456"
        assert call_args[0][2] == "opus"
        mock_editor.finalize.assert_awaited_once()


class TestCallbackCommit:

    @pytest.mark.asyncio
    async def test_callback_commit_calls_claude(self):
        """action:commit should call call_streaming with commit prompt."""
        update, ctx = _make_callback_update("action:commit")

        mock_editor = MagicMock()
        mock_editor.text = "Committed changes"
        mock_editor.finalize = AsyncMock()
        mock_editor.sent_messages = []

        mock_session = {
            "session_id": "sess-789",
            "model": "sonnet",
            "message_count": 10,
            "name": "default",
        }

        with patch("channels.telegram.callbacks.get_session", return_value=mock_session), \
             patch("channels.telegram.callbacks.StreamingEditor", return_value=mock_editor), \
             patch("channels.telegram.callbacks.call_streaming", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = {"result": "Committed changes", "session_id": "sess-789"}
            await handle_callback(update, ctx)

        mock_claude.assert_awaited_once()
        call_args = mock_claude.call_args
        assert "commit" in call_args[0][0].lower()
        assert call_args[0][1] == "sess-789"
        mock_editor.finalize.assert_awaited_once()


class TestCallbackDiff:

    @pytest.mark.asyncio
    async def test_callback_diff_calls_claude(self):
        """action:diff should call call_streaming with diff prompt."""
        update, ctx = _make_callback_update("action:diff")

        mock_editor = MagicMock()
        mock_editor.text = "Diff output here"
        mock_editor.finalize = AsyncMock()
        mock_editor.sent_messages = []

        mock_session = {
            "session_id": "sess-diff",
            "model": "sonnet",
            "message_count": 7,
            "name": "default",
        }

        with patch("channels.telegram.callbacks.get_session", return_value=mock_session), \
             patch("channels.telegram.callbacks.StreamingEditor", return_value=mock_editor), \
             patch("channels.telegram.callbacks.call_streaming", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = {"result": "Diff output here", "session_id": "sess-diff"}
            await handle_callback(update, ctx)

        mock_claude.assert_awaited_once()
        call_args = mock_claude.call_args
        assert "diff" in call_args[0][0].lower()
        assert call_args[0][1] == "sess-diff"
        mock_editor.finalize.assert_awaited_once()


class TestCallbackSummarize:

    @pytest.mark.asyncio
    async def test_callback_summarize_calls_claude(self):
        """action:summarize should call call_streaming with summarize prompt."""
        update, ctx = _make_callback_update("action:summarize")

        mock_editor = MagicMock()
        mock_editor.text = "Summary bullets"
        mock_editor.finalize = AsyncMock()
        mock_editor.sent_messages = []

        mock_session = {
            "session_id": "sess-sum",
            "model": "sonnet",
            "message_count": 2,
            "name": "default",
        }

        with patch("channels.telegram.callbacks.get_session", return_value=mock_session), \
             patch("channels.telegram.callbacks.StreamingEditor", return_value=mock_editor), \
             patch("channels.telegram.callbacks.call_streaming", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = {"result": "Summary bullets", "session_id": "sess-sum"}
            await handle_callback(update, ctx)

        mock_claude.assert_awaited_once()
        call_args = mock_claude.call_args
        assert "summarize" in call_args[0][0].lower()
        assert call_args[0][1] == "sess-sum"
        mock_editor.finalize.assert_awaited_once()
