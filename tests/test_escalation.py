"""Tests for alert escalation -- action buttons on node_health investigation messages.

Tests cover:
- Observer: send_telegram (via base class), get_remediation_commands,
  save_escalation_context (including truncation), cooldown checks
- Bot: escalation callback handling (ignore, commands, fix, fix timeout)
"""

import asyncio
import json
import sys
import urllib.parse
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from observers.node_health import NodeHealthObserver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_callback_query(data, message_text="Investigation results..."):
    """Create mock Update/Context for callback query handlers."""
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message.text = message_text
    query.message.edit_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 12345
    update.effective_user.id = 12345
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return update, context


# ---------------------------------------------------------------------------
# Observer: send_telegram (via base class)
# ---------------------------------------------------------------------------


class TestSendTelegramKeyboard:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = NodeHealthObserver()

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_send_telegram_short_message(self, mock_req, mock_urlopen):
        """Short message sends as a single request."""
        self.obs.send_telegram("Hello")
        assert mock_req.call_count == 1

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_send_telegram_long_message_splits(self, mock_req, mock_urlopen):
        """Long message should be split into multiple chunks."""
        long_text = "x" * 5000
        self.obs.send_telegram(long_text)
        assert mock_req.call_count == 2


# ---------------------------------------------------------------------------
# Observer: get_remediation_commands
# ---------------------------------------------------------------------------


class TestGetRemediationCommands:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = NodeHealthObserver()

    def test_returns_commands_for_instance(self):
        """Should return a list of commands containing the IP."""
        cmds = self.obs.get_remediation_commands("192.168.4.185:9100")
        assert isinstance(cmds, list)
        assert len(cmds) >= 3
        assert any("192.168.4.185" in c for c in cmds)
        assert any("ping" in c for c in cmds)
        assert any("restart" in c for c in cmds)
        assert any("uptime" in c for c in cmds)

    def test_extracts_ip_from_instance(self):
        """IP should be extracted before the colon."""
        cmds = self.obs.get_remediation_commands("10.0.0.5:9100")
        assert all("10.0.0.5" in c for c in cmds)


# ---------------------------------------------------------------------------
# Observer: save_escalation_context
# ---------------------------------------------------------------------------


class TestSaveEscalationContext:

    @pytest.fixture(autouse=True)
    def use_temp_state(self, tmp_path):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = NodeHealthObserver()
        self.state_dir = tmp_path / ".state"
        self.obs.STATE_DIR = self.state_dir

    def test_saves_json_with_correct_structure(self):
        """Should write a JSON file with timestamp, down_nodes, investigation."""
        down_nodes = [{"instance": "192.168.4.185:9100", "job": "node", "key": "test_key"}]
        self.obs.save_escalation_context(down_nodes, "Node appears to be down")

        context_file = self.state_dir / "last_escalation.json"
        assert context_file.exists()

        data = json.loads(context_file.read_text())
        assert "timestamp" in data
        assert data["down_nodes"] == down_nodes
        assert data["investigation"] == "Node appears to be down"

    def test_truncates_long_investigation(self):
        """Investigation text longer than 2000 chars should be truncated."""
        long_text = "A" * 5000
        self.obs.save_escalation_context([], long_text)

        context_file = self.state_dir / "last_escalation.json"
        data = json.loads(context_file.read_text())
        assert len(data["investigation"]) == 2000


# ---------------------------------------------------------------------------
# Observer: cooldown
# ---------------------------------------------------------------------------


class TestCooldown:

    @pytest.fixture(autouse=True)
    def use_temp_state(self, tmp_path):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = NodeHealthObserver()
        self.state_dir = tmp_path / ".state"
        self.obs.STATE_DIR = self.state_dir

    def test_fresh_node_returns_true(self):
        """A node that has never been seen should pass cooldown check."""
        assert self.obs.check_cooldown("new_node") is True

    def test_after_set_returns_false(self):
        """After setting cooldown, check should return False within the window."""
        self.obs.set_cooldown("test_node")
        assert self.obs.check_cooldown("test_node") is False


# ---------------------------------------------------------------------------
# Bot: escalation callback -- ignore
# ---------------------------------------------------------------------------


class TestEscalationIgnoreCallback:

    @pytest.mark.asyncio
    async def test_ignore_edits_message(self):
        """'escalation:ignore' should edit the message with acknowledgment."""
        update, ctx = _make_callback_query("escalation:ignore", "Investigation text here")

        # Import and call the handler
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            from channels.telegram.callbacks import handle_callback
            await handle_callback(update, ctx)

        query = update.callback_query
        query.answer.assert_awaited_once()
        query.message.edit_text.assert_awaited_once()
        edited_text = query.message.edit_text.call_args[0][0]
        assert "Acknowledged" in edited_text
        assert "no action taken" in edited_text
        # Original text should be preserved
        assert "Investigation text here" in edited_text


# ---------------------------------------------------------------------------
# Bot: escalation callback -- commands
# ---------------------------------------------------------------------------


class TestEscalationCommandsCallback:

    @pytest.mark.asyncio
    async def test_commands_sends_list(self):
        """'escalation:commands:ip:port' should send a command list."""
        update, ctx = _make_callback_query("escalation:commands:192.168.4.185:9100")

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            from channels.telegram.callbacks import handle_callback
            await handle_callback(update, ctx)

        query = update.callback_query
        query.answer.assert_awaited_once()
        ctx.bot.send_message.assert_awaited_once()
        msg = ctx.bot.send_message.call_args[1]["text"]
        assert "192.168.4.185" in msg
        assert "ping" in msg
        assert "systemctl" in msg
        assert "Suggested commands" in msg


# ---------------------------------------------------------------------------
# Bot: escalation callback -- fix
# ---------------------------------------------------------------------------


class TestEscalationFixCallback:

    @pytest.mark.asyncio
    async def test_fix_runs_ssh_restart(self):
        """'escalation:fix:ip:port' should SSH to restart node_exporter."""
        update, ctx = _make_callback_query("escalation:fix:192.168.4.185:9100")

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = mock_proc
                with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                    mock_wait.return_value = (b"", b"")

                    from channels.telegram.callbacks import handle_callback
                    await handle_callback(update, ctx)

        query = update.callback_query
        query.answer.assert_awaited_once()
        # Should edit the original message to show "Attempting auto-fix..."
        query.message.edit_text.assert_awaited_once()
        edit_text = query.message.edit_text.call_args[0][0]
        assert "Attempting auto-fix" in edit_text
        # Should send a result message
        ctx.bot.send_message.assert_awaited_once()
        result_msg = ctx.bot.send_message.call_args[1]["text"]
        assert "Restarted" in result_msg or "node_exporter" in result_msg

    @pytest.mark.asyncio
    async def test_fix_timeout_handled(self):
        """SSH timeout should be handled gracefully."""
        update, ctx = _make_callback_query("escalation:fix:10.0.0.99:9100")

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = mock_proc
                with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                    mock_wait.side_effect = asyncio.TimeoutError()

                    from channels.telegram.callbacks import handle_callback
                    await handle_callback(update, ctx)

        query = update.callback_query
        ctx.bot.send_message.assert_awaited_once()
        result_msg = ctx.bot.send_message.call_args[1]["text"]
        assert "timed out" in result_msg

    @pytest.mark.asyncio
    async def test_fix_ssh_failure(self):
        """SSH command failure should report the error."""
        update, ctx = _make_callback_query("escalation:fix:192.168.4.185:9100")

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"Connection refused"))
        mock_proc.returncode = 255

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = mock_proc
                with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                    mock_wait.return_value = (b"", b"Connection refused")

                    from channels.telegram.callbacks import handle_callback
                    await handle_callback(update, ctx)

        ctx.bot.send_message.assert_awaited_once()
        result_msg = ctx.bot.send_message.call_args[1]["text"]
        assert "Failed" in result_msg
        assert "255" in result_msg
