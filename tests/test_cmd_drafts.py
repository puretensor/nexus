"""Tests for /drafts command in channels/telegram/commands.py."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from channels.telegram.commands import cmd_drafts
    from drafts.queue import create_email_draft
    from db import init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Fresh temp database for each test."""
    db_file = tmp_path / "test_cmd_drafts.db"
    monkeypatch.setattr("db.DB_PATH", db_file)
    monkeypatch.setattr("drafts.queue.AUTHORIZED_USER_ID", 12345)
    init_db()
    yield db_file


def _make_update(user_id=12345):
    """Create mock Update/Context for command handlers."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []
    return update, context


# ---------------------------------------------------------------------------
# /drafts with no pending
# ---------------------------------------------------------------------------


class TestDraftsEmpty:

    @pytest.mark.asyncio
    async def test_no_pending_drafts(self):
        """/drafts with no pending drafts shows message."""
        update, ctx = _make_update()
        await cmd_drafts(update, ctx)

        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.call_args[0][0]
        assert "No pending" in text


# ---------------------------------------------------------------------------
# /drafts with pending drafts
# ---------------------------------------------------------------------------


class TestDraftsPending:

    @pytest.mark.asyncio
    async def test_shows_pending_drafts(self):
        """/drafts lists pending drafts with approve/reject buttons."""
        await create_email_draft("alice@test.com", "Hello", "m1", "Draft body 1")
        await create_email_draft("bob@test.com", "World", "m2", "Draft body 2")

        update, ctx = _make_update()
        await cmd_drafts(update, ctx)

        # Should be called once per draft
        assert update.message.reply_text.await_count == 2

        # Collect all reply texts
        all_texts = [
            call[0][0] for call in update.message.reply_text.call_args_list
        ]
        combined = "\n".join(all_texts)
        assert "alice@test.com" in combined
        assert "bob@test.com" in combined
        assert "Hello" in combined
        assert "World" in combined

        # Check keyboard is passed on first call
        first_call = update.message.reply_text.call_args_list[0]
        kwargs = first_call.kwargs
        assert "reply_markup" in kwargs
        assert kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_skips_rejected_drafts(self):
        """/drafts should not show rejected drafts."""
        from drafts.queue import reject_draft

        d1 = await create_email_draft("a@a.com", "A", "m1", "body1")
        d2 = await create_email_draft("b@b.com", "B", "m2", "body2")
        reject_draft(d1)

        update, ctx = _make_update()
        await cmd_drafts(update, ctx)

        # Only one pending draft
        assert update.message.reply_text.await_count == 1
        text = update.message.reply_text.call_args_list[0][0][0]
        assert "b@b.com" in text

    @pytest.mark.asyncio
    async def test_caps_at_ten(self):
        """/drafts should show at most 10 drafts."""
        for i in range(15):
            await create_email_draft(f"u{i}@test.com", f"Subj {i}", f"m{i}", f"body {i}")

        update, ctx = _make_update()
        await cmd_drafts(update, ctx)

        assert update.message.reply_text.await_count == 10

    @pytest.mark.asyncio
    async def test_long_body_truncated(self):
        """/drafts should truncate long draft bodies with ellipsis."""
        long_body = "x" * 500
        await create_email_draft("test@test.com", "Long", "m-long", long_body)

        update, ctx = _make_update()
        await cmd_drafts(update, ctx)

        text = update.message.reply_text.call_args_list[0][0][0]
        assert "..." in text
        # Body preview should be capped
        assert len(text) < 600

    @pytest.mark.asyncio
    async def test_keyboard_has_approve_reject(self):
        """/drafts keyboard should have Approve and Reject buttons."""
        d1 = await create_email_draft("test@test.com", "Test", "m-kb", "body")

        update, ctx = _make_update()
        await cmd_drafts(update, ctx)

        kwargs = update.message.reply_text.call_args_list[0].kwargs
        keyboard = kwargs["reply_markup"]
        buttons = keyboard.inline_keyboard[0]
        labels = [b.text for b in buttons]
        assert "Approve" in labels
        assert "Reject" in labels
        # Check callback data includes draft ID
        cb_data = [b.callback_data for b in buttons]
        assert f"draft:approve:{d1}" in cb_data
        assert f"draft:reject:{d1}" in cb_data
