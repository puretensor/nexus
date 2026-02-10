"""Tests for session management commands (3A: Named Sessions, 3C: Session History & Resume).

Tests the new /session, /history, /resume commands and modifications to /new and /status.
"""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

# Patch out imports that require telegram/aiohttp before importing
with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from db import (
        init_db,
        get_session,
        upsert_session,
        update_model,
        delete_session,
        list_sessions,
        switch_session,
        delete_session_by_name,
        archive_session,
        list_archived,
        restore_session,
        update_summary,
        AUTHORIZED_USER_ID,
    )
    from channels.telegram.commands import (
        cmd_new,
        cmd_session,
        cmd_history,
        cmd_resume,
        cmd_status,
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


# ---------------------------------------------------------------------------
# /session — list sessions
# ---------------------------------------------------------------------------

class TestCmdSessionList:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_session_list_empty(self):
        """No sessions should show helpful message."""
        update, ctx = _make_update_context()
        await cmd_session(update, ctx)
        update.message.reply_text.assert_called_once()
        msg = update.message.reply_text.call_args[0][0]
        assert "No active sessions" in msg

    @pytest.mark.asyncio
    async def test_session_list_shows_sessions(self):
        """List sessions shows all active sessions."""
        switch_session(self.chat_id, "default", "sonnet")
        upsert_session(self.chat_id, "sess-1", "sonnet", 5)
        switch_session(self.chat_id, "work", "opus")
        upsert_session(self.chat_id, "sess-2", "opus", 3)

        # Switch back to default so it's the current one
        switch_session(self.chat_id, "default", "sonnet")

        update, ctx = _make_update_context()
        await cmd_session(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Sessions:" in msg
        assert "default" in msg
        assert "work" in msg

    @pytest.mark.asyncio
    async def test_session_list_marks_current(self):
        """Current session should be marked with arrow."""
        switch_session(self.chat_id, "default", "sonnet")
        switch_session(self.chat_id, "work", "opus")
        # 'work' is now the most recently used (current)

        update, ctx = _make_update_context()
        await cmd_session(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        # Arrow should appear before 'work' (the current session)
        lines = msg.split("\n")
        work_line = [l for l in lines if "work" in l][0]
        assert work_line.startswith("\u2192")

    @pytest.mark.asyncio
    async def test_session_list_shows_summary(self):
        """Sessions with summaries should display them."""
        switch_session(self.chat_id, "default", "sonnet")
        upsert_session(self.chat_id, "sess-1", "sonnet", 5)
        update_summary(self.chat_id, "Debugging deploy script")

        update, ctx = _make_update_context()
        await cmd_session(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Debugging deploy script" in msg

    @pytest.mark.asyncio
    async def test_session_list_shows_no_messages(self):
        """Sessions with 0 messages should show '(no messages)'."""
        switch_session(self.chat_id, "empty", "sonnet")

        update, ctx = _make_update_context()
        await cmd_session(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "(no messages)" in msg


# ---------------------------------------------------------------------------
# /session <name> — switch/create
# ---------------------------------------------------------------------------

class TestCmdSessionSwitch:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_session_switch_creates_new(self):
        """/session work creates a new session if it doesn't exist."""
        update, ctx = _make_update_context(args=["work"])
        await cmd_session(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Created new session: work" in msg

        # Verify session exists
        sessions = list_sessions(self.chat_id)
        names = [s["name"] for s in sessions]
        assert "work" in names

    @pytest.mark.asyncio
    async def test_session_switch_to_existing(self):
        """/session work switches to existing session."""
        switch_session(self.chat_id, "work", "opus")
        upsert_session(self.chat_id, "sess-work", "opus", 3)
        # Switch to default first
        switch_session(self.chat_id, "default", "sonnet")

        update, ctx = _make_update_context(args=["work"])
        await cmd_session(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Switched to session: work" in msg

    @pytest.mark.asyncio
    async def test_session_switch_preserves_model(self):
        """/session <name> inherits current model when creating."""
        # Set current model to opus
        switch_session(self.chat_id, "default", "opus")

        update, ctx = _make_update_context(args=["research"])
        await cmd_session(update, ctx)

        # The new session should have opus model
        sessions = list_sessions(self.chat_id)
        research = [s for s in sessions if s["name"] == "research"][0]
        assert research["model"] == "opus"


# ---------------------------------------------------------------------------
# /session delete <name>
# ---------------------------------------------------------------------------

class TestCmdSessionDelete:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_session_delete(self):
        """/session delete work deletes the session."""
        switch_session(self.chat_id, "default", "sonnet")
        switch_session(self.chat_id, "work", "opus")
        # Switch back to default so work isn't current
        switch_session(self.chat_id, "default", "sonnet")

        update, ctx = _make_update_context(args=["delete", "work"])
        await cmd_session(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Deleted session: work" in msg

        # Verify it's gone
        sessions = list_sessions(self.chat_id)
        names = [s["name"] for s in sessions]
        assert "work" not in names

    @pytest.mark.asyncio
    async def test_session_delete_not_found(self):
        """/session delete nonexistent shows error."""
        update, ctx = _make_update_context(args=["delete", "nonexistent"])
        await cmd_session(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Session not found: nonexistent" in msg

    @pytest.mark.asyncio
    async def test_session_delete_current_blocked(self):
        """Cannot delete the current active session."""
        switch_session(self.chat_id, "default", "sonnet")

        update, ctx = _make_update_context(args=["delete", "default"])
        await cmd_session(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Cannot delete the current active session" in msg

        # Verify it still exists
        sessions = list_sessions(self.chat_id)
        names = [s["name"] for s in sessions]
        assert "default" in names


# ---------------------------------------------------------------------------
# /history — list archived sessions
# ---------------------------------------------------------------------------

class TestCmdHistory:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_history_empty(self):
        """No archived sessions shows helpful message."""
        update, ctx = _make_update_context()
        await cmd_history(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "No archived sessions" in msg

    @pytest.mark.asyncio
    async def test_history_shows_archived(self):
        """Archived sessions are listed."""
        switch_session(self.chat_id, "old-project", "sonnet")
        upsert_session(self.chat_id, "sess-old", "sonnet", 5)
        archive_session(self.chat_id, "old-project")

        update, ctx = _make_update_context()
        await cmd_history(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Archived sessions:" in msg
        assert "old-project" in msg
        assert "5 msgs" in msg

    @pytest.mark.asyncio
    async def test_history_shows_summary(self):
        """Archived sessions with summaries display them."""
        switch_session(self.chat_id, "k8s", "sonnet")
        upsert_session(self.chat_id, "sess-k8s", "sonnet", 8)
        update_summary(self.chat_id, "Kubernetes migration", "k8s")
        archive_session(self.chat_id, "k8s")

        update, ctx = _make_update_context()
        await cmd_history(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Kubernetes migration" in msg

    @pytest.mark.asyncio
    async def test_history_numbered(self):
        """Archived sessions are numbered for /resume reference."""
        switch_session(self.chat_id, "proj1", "sonnet")
        archive_session(self.chat_id, "proj1")
        switch_session(self.chat_id, "proj2", "opus")
        archive_session(self.chat_id, "proj2")

        update, ctx = _make_update_context()
        await cmd_history(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "1." in msg
        assert "2." in msg


# ---------------------------------------------------------------------------
# /resume <n> — restore archived session
# ---------------------------------------------------------------------------

class TestCmdResume:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_resume_restores_session(self):
        """/resume 1 restores the first archived session."""
        switch_session(self.chat_id, "old-work", "opus")
        upsert_session(self.chat_id, "sess-old", "opus", 10)
        archive_session(self.chat_id, "old-work")

        update, ctx = _make_update_context(args=["1"])
        await cmd_resume(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Restored session: old-work" in msg

        # Verify it's active again
        sessions = list_sessions(self.chat_id)
        names = [s["name"] for s in sessions]
        assert "old-work" in names

        # Verify it's no longer archived
        archived = list_archived(self.chat_id)
        archived_names = [s["name"] for s in archived]
        assert "old-work" not in archived_names

    @pytest.mark.asyncio
    async def test_resume_no_args(self):
        """/resume with no args shows usage."""
        update, ctx = _make_update_context(args=[])
        await cmd_resume(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Usage" in msg

    @pytest.mark.asyncio
    async def test_resume_invalid_number(self):
        """/resume with invalid number shows error."""
        update, ctx = _make_update_context(args=["abc"])
        await cmd_resume(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Invalid number" in msg

    @pytest.mark.asyncio
    async def test_resume_out_of_range(self):
        """/resume with out-of-range number shows error."""
        switch_session(self.chat_id, "proj", "sonnet")
        archive_session(self.chat_id, "proj")

        update, ctx = _make_update_context(args=["5"])
        await cmd_resume(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Invalid number" in msg

    @pytest.mark.asyncio
    async def test_resume_zero_invalid(self):
        """/resume 0 is invalid (1-indexed)."""
        switch_session(self.chat_id, "proj", "sonnet")
        archive_session(self.chat_id, "proj")

        update, ctx = _make_update_context(args=["0"])
        await cmd_resume(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Invalid number" in msg


# ---------------------------------------------------------------------------
# /new — archives and creates fresh session
# ---------------------------------------------------------------------------

class TestCmdNew:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_new_clears_current_default(self):
        """/new with no args clears the current default session and creates a fresh one."""
        switch_session(self.chat_id, "default", "sonnet")
        upsert_session(self.chat_id, "sess-1", "sonnet", 5)

        update, ctx = _make_update_context(args=[])
        await cmd_new(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "cleared" in msg.lower() or "fresh" in msg.lower()

        # A new default session should exist
        session = get_session(self.chat_id)
        assert session is not None
        assert session["name"] == "default"
        assert session["message_count"] == 0

    @pytest.mark.asyncio
    async def test_new_with_name_archives_current(self):
        """/new research archives current 'default' and creates 'research'."""
        switch_session(self.chat_id, "default", "sonnet")
        upsert_session(self.chat_id, "sess-1", "sonnet", 5)

        update, ctx = _make_update_context(args=["research"])
        await cmd_new(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "archived" in msg.lower()
        assert "research" in msg

        # Old 'default' session should be archived
        archived = list_archived(self.chat_id)
        assert len(archived) >= 1
        assert any(s["name"] == "default" for s in archived)

        # Active session should be 'research'
        session = get_session(self.chat_id)
        assert session is not None
        assert session["name"] == "research"

    @pytest.mark.asyncio
    async def test_new_with_name_preserves_model(self):
        """/new research preserves the model from the current session."""
        switch_session(self.chat_id, "default", "opus")
        upsert_session(self.chat_id, "sess-1", "opus", 3)

        update, ctx = _make_update_context(args=["research"])
        await cmd_new(update, ctx)

        # New session should inherit opus model
        session = get_session(self.chat_id)
        assert session is not None
        assert session["name"] == "research"
        assert session["model"] == "opus"

    @pytest.mark.asyncio
    async def test_new_preserves_model(self):
        """/new should preserve the current model preference."""
        switch_session(self.chat_id, "default", "opus")

        update, ctx = _make_update_context(args=[])
        await cmd_new(update, ctx)

        session = get_session(self.chat_id)
        assert session["model"] == "opus"

    @pytest.mark.asyncio
    async def test_new_when_no_session(self):
        """/new when no session exists should still create one."""
        update, ctx = _make_update_context(args=[])
        await cmd_new(update, ctx)

        session = get_session(self.chat_id)
        assert session is not None
        assert session["name"] == "default"


# ---------------------------------------------------------------------------
# /status — shows name and summary
# ---------------------------------------------------------------------------

class TestCmdStatus:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.chat_id = 12345

    @pytest.mark.asyncio
    async def test_status_shows_name(self):
        """/status should include the session name."""
        switch_session(self.chat_id, "work", "opus")
        upsert_session(self.chat_id, "sess-work-123", "opus", 5)

        update, ctx = _make_update_context()
        await cmd_status(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "name: work" in msg

    @pytest.mark.asyncio
    async def test_status_shows_summary(self):
        """/status should include the summary if available."""
        switch_session(self.chat_id, "default", "sonnet")
        upsert_session(self.chat_id, "sess-123", "sonnet", 3)
        update_summary(self.chat_id, "Debugging the deploy script")

        update, ctx = _make_update_context()
        await cmd_status(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Summary: Debugging the deploy script" in msg

    @pytest.mark.asyncio
    async def test_status_no_summary(self):
        """/status without summary should not include Summary line."""
        switch_session(self.chat_id, "default", "sonnet")
        upsert_session(self.chat_id, "sess-123", "sonnet", 3)

        update, ctx = _make_update_context()
        await cmd_status(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Summary:" not in msg

    @pytest.mark.asyncio
    async def test_status_no_session(self):
        """/status with no active session shows appropriate message."""
        update, ctx = _make_update_context()
        await cmd_status(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "No active session" in msg

    @pytest.mark.asyncio
    async def test_status_includes_model_and_messages(self):
        """/status still includes model and message count."""
        switch_session(self.chat_id, "default", "opus")
        upsert_session(self.chat_id, "sess-abc-123456", "opus", 12)

        update, ctx = _make_update_context()
        await cmd_status(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Model: opus" in msg
        assert "Messages: 12" in msg
        assert "sess-abc-123" in msg  # first 12 chars of session id
