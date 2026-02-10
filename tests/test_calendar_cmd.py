"""Tests for /calendar and /followups commands."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from channels.telegram.commands import cmd_calendar, cmd_followups
    from db import init_db, create_followup, resolve_followup


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_calendar_cmd.db"
    monkeypatch.setattr("db.DB_PATH", db_file)
    init_db()
    yield db_file


def _make_update(user_id=12345, args=None):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    update.message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.args = args or []
    return update, ctx


# ---------------------------------------------------------------------------
# /calendar command
# ---------------------------------------------------------------------------


class TestCalendarCommand:

    @pytest.mark.asyncio
    async def test_calendar_no_script(self, monkeypatch):
        """If gcalendar.py doesn't exist, show error."""
        monkeypatch.setattr(
            "channels.telegram.commands.GCALENDAR_SCRIPT",
            Path("/nonexistent/gcalendar.py"),
        )
        update, ctx = _make_update()
        await cmd_calendar(update, ctx)

        text = update.message.reply_text.call_args_list[-1][0][0]
        assert "not configured" in text.lower()

    @pytest.mark.asyncio
    async def test_calendar_today_success(self, monkeypatch):
        """Successful calendar fetch should show events."""
        monkeypatch.setattr(
            "channels.telegram.commands.GCALENDAR_SCRIPT",
            Path.home() / ".config" / "puretensor" / "gcalendar.py",
        )

        mock_result = MagicMock(
            returncode=0,
            stdout="Today:\n2026-02-10  11:00-12:00  Meeting with Alan\n\n  Showing 1 events",
            stderr="",
        )

        with patch("channels.telegram.commands.subprocess.run", return_value=mock_result):
            update, ctx = _make_update()
            await cmd_calendar(update, ctx)

        # Should have 2 messages: "Fetching..." and the result
        assert update.message.reply_text.await_count == 2
        result_text = update.message.reply_text.call_args_list[1][0][0]
        assert "Meeting with Alan" in result_text

    @pytest.mark.asyncio
    async def test_calendar_week_arg(self, monkeypatch):
        """Passing 'week' as argument should fetch weekly view."""
        monkeypatch.setattr(
            "channels.telegram.commands.GCALENDAR_SCRIPT",
            Path.home() / ".config" / "puretensor" / "gcalendar.py",
        )

        mock_result = MagicMock(returncode=0, stdout="This week:\nNo events", stderr="")

        with patch("channels.telegram.commands.subprocess.run", return_value=mock_result) as mock_run:
            update, ctx = _make_update(args=["week"])
            await cmd_calendar(update, ctx)

        # Check that 'week' was passed to gcalendar.py
        call_args = mock_run.call_args[0][0]
        assert "week" in call_args

    @pytest.mark.asyncio
    async def test_calendar_invalid_arg_defaults_today(self, monkeypatch):
        """Invalid arg should default to 'today'."""
        monkeypatch.setattr(
            "channels.telegram.commands.GCALENDAR_SCRIPT",
            Path.home() / ".config" / "puretensor" / "gcalendar.py",
        )

        mock_result = MagicMock(returncode=0, stdout="Today: No events", stderr="")

        with patch("channels.telegram.commands.subprocess.run", return_value=mock_result) as mock_run:
            update, ctx = _make_update(args=["invalid"])
            await cmd_calendar(update, ctx)

        call_args = mock_run.call_args[0][0]
        assert "today" in call_args


# ---------------------------------------------------------------------------
# /followups command
# ---------------------------------------------------------------------------


class TestFollowupsCommand:

    @pytest.mark.asyncio
    async def test_no_followups(self):
        update, ctx = _make_update()
        await cmd_followups(update, ctx)

        text = update.message.reply_text.call_args[0][0]
        assert "No active" in text

    @pytest.mark.asyncio
    async def test_list_followups(self):
        create_followup(12345, "alice@test.com", "Project update", "m1")
        create_followup(12345, "bob@test.com", "Invoice question", "m2")

        update, ctx = _make_update()
        await cmd_followups(update, ctx)

        text = update.message.reply_text.call_args[0][0]
        assert "alice@test.com" in text
        assert "bob@test.com" in text
        assert "Project update" in text
        assert "Invoice question" in text

    @pytest.mark.asyncio
    async def test_resolve_followup(self):
        fid = create_followup(12345, "client@test.com", "Deal", "m3")

        update, ctx = _make_update(args=["resolve", "1"])
        await cmd_followups(update, ctx)

        text = update.message.reply_text.call_args[0][0]
        assert "Resolved" in text
        assert "Deal" in text

    @pytest.mark.asyncio
    async def test_resolve_invalid_number(self):
        update, ctx = _make_update(args=["resolve", "abc"])
        await cmd_followups(update, ctx)

        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_resolve_out_of_range(self):
        create_followup(12345, "test@test.com", "Test", "m4")

        update, ctx = _make_update(args=["resolve", "99"])
        await cmd_followups(update, ctx)

        text = update.message.reply_text.call_args[0][0]
        assert "Invalid" in text

    @pytest.mark.asyncio
    async def test_resolved_not_shown(self):
        """Resolved followups should not appear in the list."""
        fid1 = create_followup(12345, "a@a.com", "A", "m5")
        fid2 = create_followup(12345, "b@b.com", "B", "m6")
        resolve_followup(fid1)

        update, ctx = _make_update()
        await cmd_followups(update, ctx)

        text = update.message.reply_text.call_args[0][0]
        assert "b@b.com" in text
        assert "a@a.com" not in text
