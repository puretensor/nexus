"""Tests for drafts/queue.py — DraftQueue lifecycle management."""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from drafts.queue import (
        create_email_draft,
        approve_draft,
        reject_draft,
        send_draft,
        get_pending_drafts,
    )
    from db import init_db, DB_PATH


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Use a fresh temp database for each test."""
    db_file = tmp_path / "test_drafts.db"
    monkeypatch.setattr("db.DB_PATH", db_file)
    monkeypatch.setattr("drafts.queue.AUTHORIZED_USER_ID", 12345)
    init_db()
    yield db_file


# ---------------------------------------------------------------------------
# create_email_draft
# ---------------------------------------------------------------------------


class TestCreateEmailDraft:

    @pytest.mark.asyncio
    async def test_create_returns_draft_id(self):
        draft_id = await create_email_draft(
            email_from="sender@example.com",
            email_subject="Test subject",
            email_message_id="msg-123",
            draft_body="Hello, this is a draft.",
        )
        assert isinstance(draft_id, int)
        assert draft_id > 0

    @pytest.mark.asyncio
    async def test_create_stores_in_db(self):
        draft_id = await create_email_draft(
            email_from="alice@example.com",
            email_subject="Inquiry",
            email_message_id="msg-456",
            draft_body="Draft reply content.",
        )

        from db import get_draft
        draft = get_draft(draft_id)
        assert draft is not None
        assert draft["email_from"] == "alice@example.com"
        assert draft["email_subject"] == "Inquiry"
        assert draft["email_message_id"] == "msg-456"
        assert draft["draft_body"] == "Draft reply content."
        assert draft["status"] == "pending"

    @pytest.mark.asyncio
    async def test_create_sends_telegram_notification(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        draft_id = await create_email_draft(
            email_from="bob@example.com",
            email_subject="Hello",
            email_message_id="msg-789",
            draft_body="Draft body here.",
            bot=bot,
        )

        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["chat_id"] == 12345
        assert "bob@example.com" in call_kwargs["text"]
        assert call_kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_create_without_bot_no_error(self):
        """Creating without a bot should not raise."""
        draft_id = await create_email_draft(
            email_from="test@test.com",
            email_subject="No bot",
            email_message_id="msg-0",
            draft_body="No notification.",
        )
        assert draft_id > 0


# ---------------------------------------------------------------------------
# approve_draft
# ---------------------------------------------------------------------------


class TestApproveDraft:

    @pytest.mark.asyncio
    async def test_approve_sends_and_marks_sent(self):
        draft_id = await create_email_draft(
            email_from="client@example.com",
            email_subject="Question",
            email_message_id="msg-approve-1",
            draft_body="Here is my reply.",
        )

        with patch("drafts.queue.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Sent: ok", stderr="")
            success, msg = approve_draft(draft_id)

        assert success is True
        assert "client@example.com" in msg

        from db import get_draft
        draft = get_draft(draft_id)
        assert draft["status"] == "sent"

    @pytest.mark.asyncio
    async def test_approve_nonexistent_draft(self):
        success, msg = approve_draft(99999)
        assert success is False
        assert "not found" in msg

    @pytest.mark.asyncio
    async def test_approve_already_rejected(self):
        draft_id = await create_email_draft(
            email_from="test@test.com",
            email_subject="Test",
            email_message_id="msg-rej",
            draft_body="Body.",
        )
        reject_draft(draft_id)

        success, msg = approve_draft(draft_id)
        assert success is False
        assert "rejected" in msg


# ---------------------------------------------------------------------------
# reject_draft
# ---------------------------------------------------------------------------


class TestRejectDraft:

    @pytest.mark.asyncio
    async def test_reject_marks_rejected(self):
        draft_id = await create_email_draft(
            email_from="person@example.com",
            email_subject="Proposal",
            email_message_id="msg-reject-1",
            draft_body="Draft content.",
        )

        success, msg = reject_draft(draft_id)
        assert success is True
        assert "rejected" in msg.lower()

        from db import get_draft
        draft = get_draft(draft_id)
        assert draft["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_reject_nonexistent(self):
        success, msg = reject_draft(99999)
        assert success is False
        assert "not found" in msg

    @pytest.mark.asyncio
    async def test_reject_already_sent(self):
        draft_id = await create_email_draft(
            email_from="test@test.com",
            email_subject="Test",
            email_message_id="msg-sent",
            draft_body="Body.",
        )
        # Manually set to sent
        from db import update_draft_status
        update_draft_status(draft_id, "sent")

        success, msg = reject_draft(draft_id)
        assert success is False
        assert "sent" in msg


# ---------------------------------------------------------------------------
# send_draft
# ---------------------------------------------------------------------------


class TestSendDraft:

    @pytest.mark.asyncio
    async def test_send_requires_approved_status(self):
        draft_id = await create_email_draft(
            email_from="test@test.com",
            email_subject="Test",
            email_message_id="msg-send-1",
            draft_body="Body.",
        )
        # Draft is "pending", not "approved"
        success, msg = send_draft(draft_id)
        assert success is False
        assert "not approved" in msg or "pending" in msg

    @pytest.mark.asyncio
    async def test_send_gmail_failure(self):
        draft_id = await create_email_draft(
            email_from="test@test.com",
            email_subject="Test",
            email_message_id="msg-fail",
            draft_body="Body.",
        )
        from db import update_draft_status
        update_draft_status(draft_id, "approved")

        with patch("drafts.queue.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="Auth error"
            )
            success, msg = send_draft(draft_id)

        assert success is False
        assert "Auth error" in msg or "failed" in msg.lower()

    @pytest.mark.asyncio
    async def test_send_gmail_timeout(self):
        import subprocess
        draft_id = await create_email_draft(
            email_from="test@test.com",
            email_subject="Test",
            email_message_id="msg-timeout",
            draft_body="Body.",
        )
        from db import update_draft_status
        update_draft_status(draft_id, "approved")

        with patch("drafts.queue.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            success, msg = send_draft(draft_id)

        assert success is False
        assert "timed out" in msg.lower()


# ---------------------------------------------------------------------------
# get_pending_drafts
# ---------------------------------------------------------------------------


class TestGetPendingDrafts:

    @pytest.mark.asyncio
    async def test_lists_pending_only(self):
        # Create 3 drafts, reject one
        d1 = await create_email_draft("a@a.com", "A", "m1", "body1")
        d2 = await create_email_draft("b@b.com", "B", "m2", "body2")
        d3 = await create_email_draft("c@c.com", "C", "m3", "body3")
        reject_draft(d2)

        pending = get_pending_drafts()
        pending_ids = [d["id"] for d in pending]
        assert d1 in pending_ids
        assert d3 in pending_ids
        assert d2 not in pending_ids

    @pytest.mark.asyncio
    async def test_empty_when_none_pending(self):
        pending = get_pending_drafts()
        assert pending == []
