"""Tests for apis/infra.py — infrastructure quick-action commands.

Tests cover:
- NODES and ALLOWED_SERVICES constants
- run_ssh: localhost vs remote dispatch
- check_nodes: parallel node checking
- restart_service: validation and execution
- get_logs: truncation behavior
- get_disk / get_top: basic output
- Bot command handlers: cmd_check, cmd_restart, cmd_logs, cmd_disk, cmd_top, cmd_deploy
- Restart confirmation flow (inline keyboard)
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from dispatcher.apis.infra import (
        NODES, ALLOWED_SERVICES,
        run_ssh, check_nodes, check_sites,
        restart_service, get_logs, get_disk, get_top,
    )
    from channels.telegram.commands import (
        cmd_check, cmd_restart, cmd_logs, cmd_disk, cmd_top, cmd_deploy,
    )


def _make_update_context(chat_id=12345, user_id=12345, args=None):
    """Create mock Update and context objects for command handlers."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args or []
    return update, context


def _mock_subprocess(returncode=0, stdout=b"", stderr=b""):
    """Create a mock for asyncio.create_subprocess_exec."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:

    def test_nodes_dict_not_empty(self):
        """NODES should have entries."""
        assert len(NODES) > 0

    def test_nodes_has_tensor_core(self):
        """NODES should include tensor-core mapped to localhost."""
        assert "tensor-core" in NODES
        assert NODES["tensor-core"] == "localhost"

    def test_allowed_services_not_empty(self):
        """ALLOWED_SERVICES whitelist should have entries."""
        assert len(ALLOWED_SERVICES) > 0

    def test_allowed_services_contains_key_services(self):
        """Whitelist should include known services."""
        assert "nexus" in ALLOWED_SERVICES
        assert "nginx" in ALLOWED_SERVICES


# ---------------------------------------------------------------------------
# run_ssh
# ---------------------------------------------------------------------------

class TestRunSSH:

    @pytest.mark.asyncio
    async def test_run_ssh_localhost(self):
        """For localhost, should run command directly via bash -c."""
        proc = _mock_subprocess(0, b"hello", b"")
        with patch("dispatcher.apis.infra.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc
            rc, stdout, stderr = await run_ssh("localhost", "echo hello")

        assert rc == 0
        assert stdout == "hello"
        # Should call bash -c, not ssh
        call_args = mock_exec.call_args[0]
        assert call_args[0] == "bash"
        assert call_args[1] == "-c"
        assert "echo hello" in call_args[2]

    @pytest.mark.asyncio
    async def test_run_ssh_remote(self):
        """For remote host, should run ssh command."""
        proc = _mock_subprocess(0, b"remote output", b"")
        with patch("dispatcher.apis.infra.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc
            rc, stdout, stderr = await run_ssh("mon1", "uptime")

        assert rc == 0
        assert stdout == "remote output"
        call_args = mock_exec.call_args[0]
        assert call_args[0] == "ssh"
        assert "mon1" in call_args

    @pytest.mark.asyncio
    async def test_run_ssh_timeout(self):
        """Timeout should return -1 and error message."""
        proc = AsyncMock()
        proc.kill = MagicMock()

        async def slow_communicate():
            await asyncio.sleep(100)
            return (b"", b"")

        proc.communicate = slow_communicate

        with patch("dispatcher.apis.infra.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc
            rc, stdout, stderr = await run_ssh("mon1", "sleep 100", timeout=0.01)

        assert rc == -1
        assert "Timed out" in stderr


# ---------------------------------------------------------------------------
# check_nodes
# ---------------------------------------------------------------------------

class TestCheckNodes:

    @pytest.mark.asyncio
    async def test_check_nodes_formats_output(self):
        """check_nodes should return formatted status for all nodes."""
        async def mock_run_ssh(host, command, timeout=10):
            return (0, " 10:00:00 up 5 days", "")

        with patch("dispatcher.apis.infra.run_ssh", side_effect=mock_run_ssh):
            result = await check_nodes()

        assert "tensor-core" in result
        assert "UP" in result
        assert "mon1" in result

    @pytest.mark.asyncio
    async def test_check_nodes_unreachable(self):
        """Unreachable nodes should show UNREACHABLE."""
        async def mock_run_ssh(host, command, timeout=10):
            if host == "mon1":
                return (-1, "", "Connection refused")
            return (0, "up 5 days", "")

        with patch("dispatcher.apis.infra.run_ssh", side_effect=mock_run_ssh):
            result = await check_nodes()

        assert "UNREACHABLE" in result


# ---------------------------------------------------------------------------
# check_sites
# ---------------------------------------------------------------------------

class TestCheckSites:

    @pytest.mark.asyncio
    async def test_check_sites_formats_output(self):
        """check_sites should return status for each monitored site."""
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.head = MagicMock(return_value=mock_resp)

        with patch("dispatcher.apis.infra.get_session", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_session
            result = await check_sites()

        assert "bretalon.com" in result
        assert "200" in result


# ---------------------------------------------------------------------------
# restart_service
# ---------------------------------------------------------------------------

class TestRestartService:

    @pytest.mark.asyncio
    async def test_restart_service_invalid_service(self):
        """Invalid service should raise ValueError."""
        with pytest.raises(ValueError, match="not in whitelist"):
            await restart_service("tensor-core", "not-a-real-service")

    @pytest.mark.asyncio
    async def test_restart_service_invalid_node(self):
        """Invalid node should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown node"):
            await restart_service("not-a-node", "nginx")

    @pytest.mark.asyncio
    async def test_restart_service_success(self):
        """Successful restart returns success message."""
        async def mock_run_ssh(host, command, timeout=30):
            return (0, "", "")

        with patch("dispatcher.apis.infra.run_ssh", side_effect=mock_run_ssh):
            result = await restart_service("mon2", "nginx")

        assert "successfully" in result.lower()

    @pytest.mark.asyncio
    async def test_restart_service_failure(self):
        """Failed restart returns error message."""
        async def mock_run_ssh(host, command, timeout=30):
            return (1, "", "Unit not found")

        with patch("dispatcher.apis.infra.run_ssh", side_effect=mock_run_ssh):
            result = await restart_service("mon2", "nginx")

        assert "Failed" in result


# ---------------------------------------------------------------------------
# get_logs
# ---------------------------------------------------------------------------

class TestGetLogs:

    @pytest.mark.asyncio
    async def test_get_logs_basic(self):
        """get_logs returns journal output."""
        async def mock_run_ssh(host, command, timeout=15):
            return (0, "Feb 06 10:00:00 test log line", "")

        with patch("dispatcher.apis.infra.run_ssh", side_effect=mock_run_ssh):
            result = await get_logs("tensor-core", "nginx", 20)

        assert "log line" in result

    @pytest.mark.asyncio
    async def test_get_logs_truncation(self):
        """Output over 3500 chars gets truncated."""
        long_output = "x" * 5000

        async def mock_run_ssh(host, command, timeout=15):
            return (0, long_output, "")

        with patch("dispatcher.apis.infra.run_ssh", side_effect=mock_run_ssh):
            result = await get_logs("tensor-core", "nginx")

        assert len(result) <= 3500
        assert result.endswith("...")

    @pytest.mark.asyncio
    async def test_get_logs_invalid_service(self):
        """Invalid service should raise ValueError."""
        with pytest.raises(ValueError, match="not in whitelist"):
            await get_logs("tensor-core", "malicious-service")

    @pytest.mark.asyncio
    async def test_get_logs_invalid_node(self):
        """Invalid node should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown node"):
            await get_logs("nonexistent", "nginx")


# ---------------------------------------------------------------------------
# get_disk
# ---------------------------------------------------------------------------

class TestGetDisk:

    @pytest.mark.asyncio
    async def test_get_disk_default(self):
        """get_disk with no node defaults to tensor-core."""
        async def mock_run_ssh(host, command, timeout=30):
            return (0, "/     100G  50G  50G  50%", "")

        with patch("dispatcher.apis.infra.run_ssh", side_effect=mock_run_ssh) as mock:
            result = await get_disk()

        assert "tensor-core" in result
        assert "50%" in result
        # Should call with localhost
        mock.assert_called_once()
        assert mock.call_args[0][0] == "localhost"

    @pytest.mark.asyncio
    async def test_get_disk_specific_node(self):
        """get_disk with a specific node uses that node."""
        async def mock_run_ssh(host, command, timeout=30):
            return (0, "/     200G  100G  100G  50%", "")

        with patch("dispatcher.apis.infra.run_ssh", side_effect=mock_run_ssh) as mock:
            result = await get_disk("mon1")

        assert "mon1" in result
        mock.assert_called_once()
        assert mock.call_args[0][0] == "mon1"


# ---------------------------------------------------------------------------
# get_top
# ---------------------------------------------------------------------------

class TestGetTop:

    @pytest.mark.asyncio
    async def test_get_top_default(self):
        """get_top with no node defaults to tensor-core."""
        async def mock_run_ssh(host, command, timeout=30):
            return (0, "10:00 up 5 days\n---\nMem: 64G\n---\n/  100G\n---\nNo GPU", "")

        with patch("dispatcher.apis.infra.run_ssh", side_effect=mock_run_ssh):
            result = await get_top()

        assert "tensor-core" in result
        assert "up 5 days" in result

    @pytest.mark.asyncio
    async def test_get_top_specific_node(self):
        """get_top with a specific node uses that node."""
        async def mock_run_ssh(host, command, timeout=30):
            return (0, "10:00 up 2 days\n---\nMem: 32G", "")

        with patch("dispatcher.apis.infra.run_ssh", side_effect=mock_run_ssh) as mock:
            result = await get_top("mon2")

        assert "mon2" in result
        mock.assert_called_once()
        assert mock.call_args[0][0] == "mon2"


# ---------------------------------------------------------------------------
# Bot command handlers
# ---------------------------------------------------------------------------

class TestCmdCheck:

    @pytest.mark.asyncio
    async def test_check_nodes_default(self):
        """/check with no args defaults to nodes."""
        update, ctx = _make_update_context()

        with patch("channels.telegram.commands.check_nodes", new_callable=AsyncMock) as mock:
            mock.return_value = "tensor-core  UP  10:00"
            await cmd_check(update, ctx)

        mock.assert_called_once()
        # Should have sent at least 2 messages: "Checking..." and the result
        assert update.message.reply_text.call_count >= 2

    @pytest.mark.asyncio
    async def test_check_sites(self):
        """/check sites calls check_sites."""
        update, ctx = _make_update_context(args=["sites"])

        with patch("channels.telegram.commands.check_sites", new_callable=AsyncMock) as mock:
            mock.return_value = "bretalon.com  200  50ms"
            await cmd_check(update, ctx)

        mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_error_handling(self):
        """/check should handle errors gracefully."""
        update, ctx = _make_update_context()

        with patch("channels.telegram.commands.check_nodes", new_callable=AsyncMock) as mock:
            mock.side_effect = Exception("Network error")
            await cmd_check(update, ctx)

        last_call = update.message.reply_text.call_args[0][0]
        assert "Check failed" in last_call


class TestCmdRestart:

    @pytest.mark.asyncio
    async def test_restart_no_args_shows_usage(self):
        """/restart with no args shows usage and services list."""
        update, ctx = _make_update_context()
        await cmd_restart(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Usage" in msg
        assert "Allowed services" in msg

    @pytest.mark.asyncio
    async def test_restart_invalid_service(self):
        """/restart with unlisted service shows error."""
        update, ctx = _make_update_context(args=["bad-service"])
        await cmd_restart(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "not in whitelist" in msg

    @pytest.mark.asyncio
    async def test_restart_invalid_node(self):
        """/restart with unknown node shows error."""
        update, ctx = _make_update_context(args=["nginx", "bad-node"])
        await cmd_restart(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Unknown node" in msg

    @pytest.mark.asyncio
    async def test_restart_requires_confirmation(self):
        """/restart should send inline keyboard for confirmation, not execute immediately."""
        update, ctx = _make_update_context(args=["nginx", "mon2"])
        await cmd_restart(update, ctx)

        call_kwargs = update.message.reply_text.call_args
        # Should have reply_markup with inline keyboard
        assert "reply_markup" in call_kwargs[1]
        keyboard = call_kwargs[1]["reply_markup"]
        # Check callback_data contains the restart info
        buttons = keyboard.inline_keyboard[0]
        assert any("infra:restart:mon2:nginx" in b.callback_data for b in buttons)
        assert any("infra:cancel" in b.callback_data for b in buttons)


class TestCmdLogs:

    @pytest.mark.asyncio
    async def test_logs_no_args_shows_usage(self):
        """/logs with no args shows usage."""
        update, ctx = _make_update_context()
        await cmd_logs(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Usage" in msg

    @pytest.mark.asyncio
    async def test_logs_basic(self):
        """/logs nginx fetches logs for tensor-core."""
        update, ctx = _make_update_context(args=["nginx"])

        with patch("channels.telegram.commands.get_logs", new_callable=AsyncMock) as mock:
            mock.return_value = "Feb 06 10:00:00 log line"
            await cmd_logs(update, ctx)

        mock.assert_called_once_with("tensor-core", "nginx", 50)

    @pytest.mark.asyncio
    async def test_logs_with_node_and_lines(self):
        """/logs nginx mon2 100 passes correct args."""
        update, ctx = _make_update_context(args=["nginx", "mon2", "100"])

        with patch("channels.telegram.commands.get_logs", new_callable=AsyncMock) as mock:
            mock.return_value = "log output"
            await cmd_logs(update, ctx)

        mock.assert_called_once_with("mon2", "nginx", 100)


class TestCmdDisk:

    @pytest.mark.asyncio
    async def test_disk_no_args(self):
        """/disk with no args uses default (None)."""
        update, ctx = _make_update_context()

        with patch("channels.telegram.commands.get_disk", new_callable=AsyncMock) as mock:
            mock.return_value = "Disk usage (tensor-core):\n/ 100G"
            await cmd_disk(update, ctx)

        mock.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_disk_with_node(self):
        """/disk mon1 checks mon1."""
        update, ctx = _make_update_context(args=["mon1"])

        with patch("channels.telegram.commands.get_disk", new_callable=AsyncMock) as mock:
            mock.return_value = "Disk usage (mon1):\n/ 50G"
            await cmd_disk(update, ctx)

        mock.assert_called_once_with("mon1")


class TestCmdTop:

    @pytest.mark.asyncio
    async def test_top_default(self):
        """/top with no args defaults to None (tensor-core)."""
        update, ctx = _make_update_context()

        with patch("channels.telegram.commands.get_top", new_callable=AsyncMock) as mock:
            mock.return_value = "System overview:\nup 5 days"
            await cmd_top(update, ctx)

        mock.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_top_with_node(self):
        """/top mon2 checks mon2."""
        update, ctx = _make_update_context(args=["mon2"])

        with patch("channels.telegram.commands.get_top", new_callable=AsyncMock) as mock:
            mock.return_value = "System overview:\nup 2 days"
            await cmd_top(update, ctx)

        mock.assert_called_once_with("mon2")


class TestCmdDeploy:

    @pytest.mark.asyncio
    async def test_deploy_no_args_shows_usage(self):
        """/deploy with no args shows usage."""
        update, ctx = _make_update_context()
        await cmd_deploy(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Usage" in msg

    @pytest.mark.asyncio
    async def test_deploy_requires_confirmation(self):
        """/deploy should send inline keyboard for confirmation."""
        update, ctx = _make_update_context(args=["varangian-website"])
        await cmd_deploy(update, ctx)

        call_kwargs = update.message.reply_text.call_args
        assert "reply_markup" in call_kwargs[1]
        keyboard = call_kwargs[1]["reply_markup"]
        buttons = keyboard.inline_keyboard[0]
        assert any("infra:deploy:varangian-website" in b.callback_data for b in buttons)
        assert any("infra:cancel" in b.callback_data for b in buttons)
