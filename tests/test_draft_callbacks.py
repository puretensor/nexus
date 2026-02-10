"""Tests for draft callbacks in channels/telegram/callbacks.py.

Covers draft:approve, draft:reject, and invalid draft ID handling.
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
    from channels.telegram.callbacks import handle_callback
    from drafts.queue import create_email_draft
    from db import init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Fresh temp database for each test."""
    db_file = tmp_path / "test_callbacks.db"
    monkeypatch.setattr("db.DB_PATH", db_file)
    monkeypatch.setattr("drafts.queue.AUTHORIZED_USER_ID", 12345)
    init_db()
    yield db_file


def _make_callback_update(callback_data, chat_id=12345, user_id=12345):
    """Create mock Update/Context for callback query handlers."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.text = "Draft #1\nTo: test@test.com\nRe: Test\n\nBody"
    update.callback_query.message.edit_text = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return update, context


# ---------------------------------------------------------------------------
# draft:approve callback
# ---------------------------------------------------------------------------


class TestDraftApproveCallback:

    @pytest.mark.asyncio
    async def test_approve_sends_and_edits_message(self):
        """draft:approve should call approve_draft and edit the message with success."""
        draft_id = await create_email_draft(
            email_from="client@example.com",
            email_subject="Question",
            email_message_id="msg-cb-1",
            draft_body="Draft reply.",
        )

        update, ctx = _make_callback_update(f"draft:approve:{draft_id}")

        with patch("drafts.queue.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Sent: ok", stderr="")
            await handle_callback(update, ctx)

        update.callback_query.answer.assert_awaited_once()
        update.callback_query.message.edit_text.assert_awaited_once()
        edited_text = update.callback_query.message.edit_text.call_args[0][0]
        assert "client@example.com" in edited_text

    @pytest.mark.asyncio
    async def test_approve_nonexistent_draft(self):
        """Approving a nonexistent draft should show error."""
        update, ctx = _make_callback_update("draft:approve:99999")
        await handle_callback(update, ctx)

        update.callback_query.message.edit_text.assert_awaited_once()
        edited_text = update.callback_query.message.edit_text.call_args[0][0]
        assert "not found" in edited_text

    @pytest.mark.asyncio
    async def test_approve_already_rejected(self):
        """Approving an already-rejected draft should show error."""
        from drafts.queue import reject_draft

        draft_id = await create_email_draft(
            email_from="test@test.com",
            email_subject="Test",
            email_message_id="msg-cb-rej",
            draft_body="Body.",
        )
        reject_draft(draft_id)

        update, ctx = _make_callback_update(f"draft:approve:{draft_id}")
        await handle_callback(update, ctx)

        edited_text = update.callback_query.message.edit_text.call_args[0][0]
        assert "rejected" in edited_text


# ---------------------------------------------------------------------------
# draft:reject callback
# ---------------------------------------------------------------------------


class TestDraftRejectCallback:

    @pytest.mark.asyncio
    async def test_reject_edits_message(self):
        """draft:reject should mark draft rejected and edit the message."""
        draft_id = await create_email_draft(
            email_from="person@example.com",
            email_subject="Proposal",
            email_message_id="msg-cb-rej-1",
            draft_body="Draft content.",
        )

        update, ctx = _make_callback_update(f"draft:reject:{draft_id}")
        await handle_callback(update, ctx)

        update.callback_query.answer.assert_awaited_once()
        update.callback_query.message.edit_text.assert_awaited_once()
        edited_text = update.callback_query.message.edit_text.call_args[0][0]
        assert "rejected" in edited_text.lower()

        from db import get_draft
        draft = get_draft(draft_id)
        assert draft["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_reject_nonexistent_draft(self):
        """Rejecting a nonexistent draft should show error."""
        update, ctx = _make_callback_update("draft:reject:99999")
        await handle_callback(update, ctx)

        edited_text = update.callback_query.message.edit_text.call_args[0][0]
        assert "not found" in edited_text


# ---------------------------------------------------------------------------
# Invalid draft callback data
# ---------------------------------------------------------------------------


class TestDraftCallbackInvalid:

    @pytest.mark.asyncio
    async def test_invalid_draft_id(self):
        """Non-numeric draft ID should show 'Invalid draft ID'."""
        update, ctx = _make_callback_update("draft:approve:notanumber")
        await handle_callback(update, ctx)

        update.callback_query.message.edit_text.assert_awaited_once()
        edited_text = update.callback_query.message.edit_text.call_args[0][0]
        assert "Invalid" in edited_text

    @pytest.mark.asyncio
    async def test_missing_draft_id(self):
        """Missing draft ID field should show 'Invalid draft ID'."""
        update, ctx = _make_callback_update("draft:approve:")
        await handle_callback(update, ctx)

        update.callback_query.message.edit_text.assert_awaited_once()
        edited_text = update.callback_query.message.edit_text.call_args[0][0]
        assert "Invalid" in edited_text

    @pytest.mark.asyncio
    async def test_unknown_draft_action(self):
        """Unknown draft action (not approve/reject) should be silently ignored."""
        update, ctx = _make_callback_update("draft:unknown:1")
        await handle_callback(update, ctx)
        # Should answer the query but not edit the message
        update.callback_query.answer.assert_awaited_once()
