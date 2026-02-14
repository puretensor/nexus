"""Tests for claude_telegram_bot.py

Focus areas:
- Message splitting (4096 char Telegram limit)
- Unicode handling in messages
- Tool status formatting
- Session database operations
- Auth decorator logic
- Reply-to context building
- Streaming editor
- PureClaw system prompt loading
"""

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import json
import time

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
        get_lock,
        DB_PATH,
    )
    from config import _system_prompt, AGENT_NAME
    from engine import (
        split_message,
        _format_tool_status,
        _read_stream,
    )
    from channels.telegram.streaming import StreamingEditor
    from channels.telegram.commands import _build_reply_context


# ---------------------------------------------------------------------------
# split_message
# ---------------------------------------------------------------------------

class TestSplitMessage:

    def test_short_message(self):
        """Message under limit returned as single chunk."""
        result = split_message("Hello world")
        assert result == ["Hello world"]

    def test_exact_limit(self):
        """Message exactly at limit returned as single chunk."""
        msg = "x" * 4000
        result = split_message(msg, limit=4000)
        assert len(result) == 1
        assert result[0] == msg

    def test_one_over_limit(self):
        """Message one char over limit should split."""
        msg = "x" * 4001
        result = split_message(msg, limit=4000)
        assert len(result) == 2

    def test_split_on_newline(self):
        """Should prefer splitting on newline boundaries."""
        lines = ["Line " + str(i) for i in range(100)]
        msg = "\n".join(lines)
        result = split_message(msg, limit=200)
        assert len(result) > 1
        # Each chunk should end at a newline boundary (except possibly the last)
        for chunk in result[:-1]:
            assert len(chunk) <= 200

    def test_no_newlines_splits_at_limit(self):
        """No newlines should force split at exact limit."""
        msg = "x" * 8000
        result = split_message(msg, limit=4000)
        assert len(result) == 2
        assert len(result[0]) == 4000
        assert len(result[1]) == 4000

    def test_empty_message(self):
        """Empty string should return single empty chunk."""
        result = split_message("")
        assert result == [""]

    def test_unicode_characters_not_broken(self):
        """Unicode characters should not be split mid-codepoint."""
        # Build a message with unicode near the split point
        msg = "\u2019" * 2000 + "\n" + "\u201c" * 2000
        result = split_message(msg, limit=3000)
        # Rejoin should equal original (minus stripped newlines)
        rejoined = "".join(result)
        assert rejoined == msg.replace("\n", "")  # newlines stripped by lstrip

    def test_curly_quotes_preserved(self):
        """Curly quotes and em dashes survive splitting."""
        msg = ('She said \u201cIt\u2019s a beautiful day\u201d \u2014 really. ' * 50)
        result = split_message(msg, limit=200)
        rejoined = "".join(result)
        assert "\u201c" in rejoined
        assert "\u201d" in rejoined
        assert "\u2019" in rejoined
        assert "\u2014" in rejoined

    def test_many_short_lines(self):
        """Many short lines should group into chunks."""
        msg = "\n".join(["Hi"] * 5000)  # ~15000 chars
        result = split_message(msg, limit=4000)
        assert len(result) >= 3
        for chunk in result:
            assert len(chunk) <= 4000

    def test_single_very_long_line(self):
        """Single line longer than limit must split at limit."""
        msg = "a" * 10000
        result = split_message(msg, limit=4000)
        assert len(result) == 3
        assert result[0] == "a" * 4000
        assert result[1] == "a" * 4000
        assert result[2] == "a" * 2000

    def test_mixed_lengths(self):
        """Mix of short lines and one long line."""
        short = "Short line\n" * 10
        long_line = "x" * 5000
        msg = short + long_line
        result = split_message(msg, limit=4000)
        assert len(result) >= 2
        # All content should be preserved
        total_len = sum(len(c) for c in result)
        # Account for stripped newlines
        assert total_len >= len(long_line)


# ---------------------------------------------------------------------------
# _format_tool_status
# ---------------------------------------------------------------------------

class TestFormatToolStatus:

    def test_bash_command(self):
        result = _format_tool_status("Bash", {"command": "ls -la"})
        assert "Running:" in result
        assert "ls -la" in result

    def test_bash_long_command_truncated(self):
        long_cmd = "find / -name " + "x" * 200
        result = _format_tool_status("Bash", {"command": long_cmd})
        assert len(result) < 200
        assert "..." in result

    def test_read_tool(self):
        result = _format_tool_status("Read", {"file_path": "/home/user/test.py"})
        assert "Reading:" in result
        assert "/home/user/test.py" in result

    def test_edit_tool(self):
        result = _format_tool_status("Edit", {"file_path": "/home/user/test.py"})
        assert "Editing:" in result

    def test_write_tool(self):
        result = _format_tool_status("Write", {"file_path": "/tmp/output.txt"})
        assert "Writing:" in result

    def test_glob_tool(self):
        result = _format_tool_status("Glob", {"pattern": "**/*.py"})
        assert "Searching files:" in result
        assert "**/*.py" in result

    def test_grep_tool(self):
        result = _format_tool_status("Grep", {"pattern": "def main"})
        assert "Searching content:" in result

    def test_webfetch_long_url_truncated(self):
        long_url = "https://example.com/" + "path/" * 30
        result = _format_tool_status("WebFetch", {"url": long_url})
        assert "Fetching:" in result
        assert "..." in result
        assert len(result) < 100

    def test_websearch(self):
        result = _format_tool_status("WebSearch", {"query": "python async tutorial"})
        assert "Searching web:" in result

    def test_task_with_description(self):
        result = _format_tool_status("Task", {"description": "Run tests"})
        assert "Spawning agent:" in result
        assert "Run tests" in result

    def test_task_without_description(self):
        result = _format_tool_status("Task", {})
        assert "Spawning agent" in result

    def test_unknown_tool(self):
        result = _format_tool_status("CustomTool", {})
        assert "Using tool: CustomTool" in result

    def test_empty_input(self):
        result = _format_tool_status("Bash", {})
        assert "Running:" in result

    def test_unicode_in_command(self):
        """Tool status with unicode characters should not crash."""
        result = _format_tool_status("Bash", {"command": "echo '\u201cHello\u201d'"})
        assert "Running:" in result


# ---------------------------------------------------------------------------
# Session database
# ---------------------------------------------------------------------------

class TestSessionDB:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        """Use a temporary database for each test."""
        db_path = tmp_path / "test_sessions.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()
        self.db_path = db_path

    def test_init_creates_table(self):
        """init_db should create sessions table."""
        con = sqlite3.connect(self.db_path)
        tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        con.close()
        assert ("sessions",) in tables

    def test_get_session_nonexistent(self):
        """Getting a non-existent session returns None."""
        result = get_session(99999)
        assert result is None

    def test_upsert_and_get_session(self):
        """Insert a session and retrieve it."""
        upsert_session(12345, "sess-abc-123", "opus", 5)
        session = get_session(12345)
        assert session is not None
        assert session["session_id"] == "sess-abc-123"
        assert session["model"] == "opus"
        assert session["message_count"] == 5

    def test_upsert_updates_existing(self):
        """Upserting same chat_id should update, not duplicate."""
        upsert_session(12345, "sess-1", "sonnet", 1)
        upsert_session(12345, "sess-2", "opus", 2)
        session = get_session(12345)
        assert session["session_id"] == "sess-2"
        assert session["model"] == "opus"
        assert session["message_count"] == 2

    def test_update_model(self):
        """update_model should change model for existing session."""
        upsert_session(12345, "sess-1", "sonnet", 3)
        update_model(12345, "opus")
        session = get_session(12345)
        assert session["model"] == "opus"

    def test_update_model_creates_if_not_exists(self):
        """update_model should create session if none exists."""
        update_model(99999, "opus")
        session = get_session(99999)
        assert session is not None
        assert session["model"] == "opus"
        assert session["session_id"] is None

    def test_delete_session(self):
        """Delete a session."""
        upsert_session(12345, "sess-1", "sonnet", 1)
        delete_session(12345)
        session = get_session(12345)
        assert session is None

    def test_delete_nonexistent_session(self):
        """Deleting non-existent session should not crash."""
        delete_session(99999)  # Should not raise


# ---------------------------------------------------------------------------
# get_lock
# ---------------------------------------------------------------------------

class TestGetLock:

    def test_same_chat_gets_same_lock(self):
        lock1 = get_lock(12345)
        lock2 = get_lock(12345)
        assert lock1 is lock2

    def test_different_chats_get_different_locks(self):
        lock1 = get_lock(12345)
        lock2 = get_lock(67890)
        assert lock1 is not lock2

    def test_lock_is_asyncio_lock(self):
        lock = get_lock(11111)
        assert isinstance(lock, asyncio.Lock)


# ---------------------------------------------------------------------------
# _build_reply_context (1D: Reply-to context)
# ---------------------------------------------------------------------------

class TestBuildReplyContext:

    def test_no_reply(self):
        """No reply_to_message returns empty string."""
        msg = MagicMock()
        msg.reply_to_message = None
        assert _build_reply_context(msg) == ""

    def test_reply_with_text(self):
        """Reply with text includes quoted text."""
        reply = MagicMock()
        reply.text = "Hello world"
        reply.caption = None
        msg = MagicMock()
        msg.reply_to_message = reply
        result = _build_reply_context(msg)
        assert '[Replying to: "Hello world"]' in result
        assert result.endswith("\n\n")

    def test_reply_with_caption(self):
        """Reply to a photo with caption uses caption text."""
        reply = MagicMock()
        reply.text = None
        reply.caption = "Photo caption"
        msg = MagicMock()
        msg.reply_to_message = reply
        result = _build_reply_context(msg)
        assert "Photo caption" in result

    def test_reply_truncation(self):
        """Long reply text is truncated to 500 chars."""
        reply = MagicMock()
        reply.text = "x" * 1000
        reply.caption = None
        msg = MagicMock()
        msg.reply_to_message = reply
        result = _build_reply_context(msg)
        # Should contain truncated text with ellipsis
        assert "..." in result
        assert len(result) < 600

    def test_reply_empty_text(self):
        """Reply with empty text returns empty string."""
        reply = MagicMock()
        reply.text = ""
        reply.caption = None
        msg = MagicMock()
        msg.reply_to_message = reply
        assert _build_reply_context(msg) == ""

    def test_reply_whitespace_only(self):
        """Reply with whitespace-only text returns empty string."""
        reply = MagicMock()
        reply.text = "   \n  "
        reply.caption = None
        msg = MagicMock()
        msg.reply_to_message = reply
        assert _build_reply_context(msg) == ""


# ---------------------------------------------------------------------------
# StreamingEditor (2A: Streaming text with message editing)
# ---------------------------------------------------------------------------

class TestStreamingEditor:

    def _make_chat(self):
        """Create a mock chat object."""
        chat = AsyncMock()
        mock_msg = AsyncMock()
        mock_msg.edit_text = AsyncMock()
        mock_msg.delete = AsyncMock()
        chat.send_message = AsyncMock(return_value=mock_msg)
        return chat

    @pytest.mark.asyncio
    async def test_add_text_creates_message(self):
        """First text delta should send a new message."""
        chat = self._make_chat()
        editor = StreamingEditor(chat)
        editor.EDIT_INTERVAL = 0  # disable rate limiting for tests
        await editor.add_text("Hello")
        chat.send_message.assert_called_once_with("Hello")

    @pytest.mark.asyncio
    async def test_add_text_edits_message(self):
        """Subsequent text deltas should edit the existing message."""
        chat = self._make_chat()
        editor = StreamingEditor(chat)
        editor.EDIT_INTERVAL = 0
        await editor.add_text("Hello")
        mock_msg = chat.send_message.return_value
        await editor.add_text(" world")
        mock_msg.edit_text.assert_called_with("Hello world")

    @pytest.mark.asyncio
    async def test_finalize_applies_markdown(self):
        """Finalize should attempt Markdown formatting."""
        chat = self._make_chat()
        editor = StreamingEditor(chat)
        editor.EDIT_INTERVAL = 0
        await editor.add_text("**bold**")
        mock_msg = chat.send_message.return_value
        await editor.finalize()
        # Last call to edit_text should include parse_mode
        calls = mock_msg.edit_text.call_args_list
        assert any("MARKDOWN" in str(c) or "parse_mode" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_tool_status_before_text(self):
        """Tool status sent before text arrives as separate messages."""
        chat = self._make_chat()
        editor = StreamingEditor(chat)
        editor.last_edit_time = 0  # ensure no rate limit
        await editor.add_tool_status("Reading: config.py")
        chat.send_message.assert_called_once()
        call_args = chat.send_message.call_args
        assert "Reading: config.py" in str(call_args)

    @pytest.mark.asyncio
    async def test_tool_status_ignored_after_text(self):
        """Tool status ignored once text streaming has started."""
        chat = self._make_chat()
        editor = StreamingEditor(chat)
        editor.EDIT_INTERVAL = 0
        await editor.add_text("Response text")
        call_count = chat.send_message.call_count
        await editor.add_tool_status("Reading: something.py")
        # No new messages should be sent for tool status
        assert chat.send_message.call_count == call_count

    @pytest.mark.asyncio
    async def test_progress_messages_deleted_on_text(self):
        """Progress messages should be deleted when real text arrives."""
        chat = self._make_chat()
        progress_msg = AsyncMock()
        progress_msg.delete = AsyncMock()
        chat.send_message = AsyncMock(side_effect=[progress_msg, AsyncMock()])
        editor = StreamingEditor(chat)
        editor.EDIT_INTERVAL = 0
        editor.last_edit_time = 0
        await editor.add_tool_status("Working...")
        await editor.add_text("Result text")
        progress_msg.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_finalize(self):
        """Finalizing with no text should return empty list."""
        chat = self._make_chat()
        editor = StreamingEditor(chat)
        result = await editor.finalize()
        assert result == []

    @pytest.mark.asyncio
    async def test_rate_limiting(self):
        """Edits should be rate-limited."""
        chat = self._make_chat()
        editor = StreamingEditor(chat)
        editor.EDIT_INTERVAL = 10  # high interval
        await editor.add_text("Hello")  # first message always sends
        mock_msg = chat.send_message.return_value
        await editor.add_text(" world")  # should be skipped (rate limit)
        assert mock_msg.edit_text.call_count == 0


# ---------------------------------------------------------------------------
# _read_stream (2A: Stream parsing with deltas)
# ---------------------------------------------------------------------------

class TestReadStream:

    def _make_proc(self, lines: list[str]):
        """Create a mock process with stdout.readline() that returns lines then EOF."""
        proc = MagicMock()
        encoded = [(line + "\n").encode() for line in lines] + [b""]  # EOF
        readline_iter = iter(encoded)
        async def readline():
            return next(readline_iter)
        proc.stdout = MagicMock()
        proc.stdout.readline = readline
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        return proc

    @pytest.mark.asyncio
    async def test_text_deltas_sent_to_editor(self):
        """content_block_delta events should feed text to StreamingEditor."""
        lines = [
            json.dumps({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"}
            }}),
            json.dumps({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": " world"}
            }}),
            json.dumps({"type": "result", "result": "Hello world", "session_id": "abc"}),
        ]
        proc = self._make_proc(lines)
        editor = AsyncMock(spec=StreamingEditor)
        editor.add_text = AsyncMock()
        editor.add_tool_status = AsyncMock()
        result = await _read_stream(proc, streaming_editor=editor)
        assert result["result"] == "Hello world"
        assert result["session_id"] == "abc"
        assert editor.add_text.call_count == 2

    @pytest.mark.asyncio
    async def test_tool_use_events(self):
        """assistant events with tool_use should trigger tool status."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
            ]}}),
            json.dumps({"type": "result", "result": "done", "session_id": "xyz"}),
        ]
        proc = self._make_proc(lines)
        editor = AsyncMock(spec=StreamingEditor)
        editor.add_text = AsyncMock()
        editor.add_tool_status = AsyncMock()
        result = await _read_stream(proc, streaming_editor=editor)
        editor.add_tool_status.assert_called_once()
        assert "Running: ls" in editor.add_tool_status.call_args[0][0]

    @pytest.mark.asyncio
    async def test_fallback_to_progress_callback(self):
        """Without streaming_editor, tool events go to on_progress callback."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x"}}
            ]}}),
            json.dumps({"type": "result", "result": "content", "session_id": "s1"}),
        ]
        proc = self._make_proc(lines)
        progress = AsyncMock()
        result = await _read_stream(proc, on_progress=progress)
        progress.assert_called_once()
        assert "Reading:" in progress.call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_result_raises(self):
        """Missing result event with no streamed text should raise RuntimeError."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
            ]}}),
        ]
        proc = self._make_proc(lines)
        with pytest.raises(RuntimeError, match="No result event|stream ended"):
            await _read_stream(proc)

    @pytest.mark.asyncio
    async def test_non_json_lines_skipped(self):
        """Non-JSON lines in stdout should be silently skipped."""
        lines = [
            "not json at all",
            json.dumps({"type": "result", "result": "ok", "session_id": "s2"}),
        ]
        proc = self._make_proc(lines)
        result = await _read_stream(proc)
        assert result["result"] == "ok"


# ---------------------------------------------------------------------------
# PureClaw System Prompt (5A)
# ---------------------------------------------------------------------------

class TestPureClawSystemPrompt:

    def test_system_prompt_loaded(self):
        """System prompt should be loaded from nexus_system_prompt.md."""
        assert _system_prompt is not None
        assert len(_system_prompt) > 0
        assert AGENT_NAME in _system_prompt

    def test_system_prompt_contains_key_directives(self):
        """System prompt should contain essential personality directives."""
        assert "Direct" in _system_prompt
        assert "concise" in _system_prompt
        assert "tensor-core" in _system_prompt
