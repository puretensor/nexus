"""Tests for handlers/file_output.py and written_files tracking in streaming.py."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from handlers.file_output import (
        scan_and_send_outputs,
        _is_allowed,
        _should_skip,
        IMAGE_EXTENSIONS,
    )
    from engine import _read_stream


# ---------------------------------------------------------------------------
# _is_allowed
# ---------------------------------------------------------------------------

class TestIsAllowed:

    def test_tmp_pureclaw_output(self):
        assert _is_allowed("/tmp/pureclaw_output/chart.png") is True

    def test_tmp_prefix(self):
        assert _is_allowed("/tmp/somefile.txt") is True

    def test_home_images(self):
        home = os.path.expanduser("~")
        assert _is_allowed(f"{home}/images/photo.jpg") is True

    def test_outside_allowed(self):
        assert _is_allowed("/home/puretensorai/secret/data.txt") is False

    def test_etc_not_allowed(self):
        assert _is_allowed("/etc/passwd") is False

    def test_root_not_allowed(self):
        assert _is_allowed("/root/file.txt") is False


# ---------------------------------------------------------------------------
# _should_skip
# ---------------------------------------------------------------------------

class TestShouldSkip:

    def test_db_file(self):
        assert _should_skip("/tmp/sessions.db") is True

    def test_env_file(self):
        assert _should_skip("/tmp/.env") is True

    def test_sqlite_extension(self):
        assert _should_skip("/tmp/data.sqlite") is True

    def test_pyc_extension(self):
        assert _should_skip("/tmp/__pycache__/mod.pyc") is True

    def test_config_json(self):
        assert _should_skip("/tmp/config.json") is True

    def test_settings_json(self):
        assert _should_skip("/tmp/settings.json") is True

    def test_normal_txt(self):
        assert _should_skip("/tmp/output.txt") is False

    def test_normal_png(self):
        assert _should_skip("/tmp/chart.png") is False

    def test_normal_py(self):
        assert _should_skip("/tmp/script.py") is False


# ---------------------------------------------------------------------------
# scan_and_send_outputs
# ---------------------------------------------------------------------------

class TestScanAndSendOutputs:

    @pytest.mark.asyncio
    async def test_empty_list(self):
        """Empty written_files is a no-op."""
        chat = AsyncMock()
        result = await scan_and_send_outputs(chat, [])
        assert result == 0
        chat.send_photo.assert_not_called()
        chat.send_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_image_as_photo(self):
        """Image files should be sent via send_photo."""
        chat = AsyncMock()
        with tempfile.NamedTemporaryFile(suffix=".png", dir="/tmp", delete=False) as f:
            f.write(b"fake png data")
            path = f.name
        try:
            result = await scan_and_send_outputs(chat, [path])
            assert result == 1
            chat.send_photo.assert_called_once()
            call_kwargs = chat.send_photo.call_args
            assert os.path.basename(path) in str(call_kwargs)
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_sends_document(self):
        """Non-image files should be sent via send_document."""
        chat = AsyncMock()
        with tempfile.NamedTemporaryFile(suffix=".txt", dir="/tmp", delete=False) as f:
            f.write(b"hello world")
            path = f.name
        try:
            result = await scan_and_send_outputs(chat, [path])
            assert result == 1
            chat.send_document.assert_called_once()
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_skips_nonexistent_file(self):
        """Non-existent file path is silently skipped."""
        chat = AsyncMock()
        result = await scan_and_send_outputs(chat, ["/tmp/does_not_exist_xyz.png"])
        assert result == 0
        chat.send_photo.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_outside_allowed_prefix(self):
        """Files outside allowed prefixes are skipped."""
        chat = AsyncMock()
        with tempfile.NamedTemporaryFile(suffix=".txt", dir=tempfile.gettempdir(), delete=False) as f:
            f.write(b"data")
            path = f.name
        # Move to a non-allowed location (simulate with patch)
        try:
            with patch("handlers.file_output._is_allowed", return_value=False):
                result = await scan_and_send_outputs(chat, [path])
            assert result == 0
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_skips_internal_files(self):
        """Internal files (.db, .env) are skipped."""
        chat = AsyncMock()
        with tempfile.NamedTemporaryFile(suffix=".db", dir="/tmp", delete=False, prefix="test") as f:
            f.write(b"sqlite data")
            path = f.name
        try:
            result = await scan_and_send_outputs(chat, [path])
            assert result == 0
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_skips_empty_path(self):
        """Empty string in written_files is skipped."""
        chat = AsyncMock()
        result = await scan_and_send_outputs(chat, ["", ""])
        assert result == 0

    @pytest.mark.asyncio
    async def test_multiple_files(self):
        """Multiple files: some sent, some skipped."""
        chat = AsyncMock()
        files = []
        try:
            # One image (should send)
            with tempfile.NamedTemporaryFile(suffix=".jpg", dir="/tmp", delete=False) as f:
                f.write(b"jpeg data")
                files.append(f.name)
            # One text file (should send)
            with tempfile.NamedTemporaryFile(suffix=".py", dir="/tmp", delete=False) as f:
                f.write(b"print('hi')")
                files.append(f.name)
            # One .db file (should skip)
            with tempfile.NamedTemporaryFile(suffix=".db", dir="/tmp", delete=False) as f:
                f.write(b"db data")
                files.append(f.name)

            result = await scan_and_send_outputs(chat, files)
            assert result == 2
            chat.send_photo.assert_called_once()
            chat.send_document.assert_called_once()
        finally:
            for f in files:
                os.unlink(f)

    @pytest.mark.asyncio
    async def test_send_failure_does_not_stop_others(self):
        """If sending one file fails, others should still be attempted."""
        chat = AsyncMock()
        chat.send_photo.side_effect = Exception("Telegram error")
        files = []
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", dir="/tmp", delete=False) as f:
                f.write(b"png")
                files.append(f.name)
            with tempfile.NamedTemporaryFile(suffix=".txt", dir="/tmp", delete=False) as f:
                f.write(b"text")
                files.append(f.name)

            result = await scan_and_send_outputs(chat, files)
            # Photo fails, document succeeds
            assert result == 1
            chat.send_photo.assert_called_once()
            chat.send_document.assert_called_once()
        finally:
            for f in files:
                os.unlink(f)

    @pytest.mark.asyncio
    async def test_all_image_extensions(self):
        """All IMAGE_EXTENSIONS should be sent as photos."""
        for ext in IMAGE_EXTENSIONS:
            chat = AsyncMock()
            with tempfile.NamedTemporaryFile(suffix=ext, dir="/tmp", delete=False) as f:
                f.write(b"data")
                path = f.name
            try:
                await scan_and_send_outputs(chat, [path])
                chat.send_photo.assert_called_once(), f"Failed for extension {ext}"
            finally:
                os.unlink(path)

    @pytest.mark.asyncio
    async def test_svg_sent_as_document(self):
        """SVG files should be sent as documents (Telegram doesn't support SVG photos)."""
        chat = AsyncMock()
        with tempfile.NamedTemporaryFile(suffix=".svg", dir="/tmp", delete=False) as f:
            f.write(b"<svg></svg>")
            path = f.name
        try:
            result = await scan_and_send_outputs(chat, [path])
            assert result == 1
            chat.send_document.assert_called_once()
            chat.send_photo.assert_not_called()
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_tilde_expansion(self):
        """Paths with ~ should be expanded to home directory."""
        chat = AsyncMock()
        home = os.path.expanduser("~")
        images_dir = os.path.join(home, "images")
        os.makedirs(images_dir, exist_ok=True)
        path = os.path.join(images_dir, "_test_output_file.txt")
        try:
            with open(path, "w") as f:
                f.write("test")
            result = await scan_and_send_outputs(chat, ["~/images/_test_output_file.txt"])
            assert result == 1
            chat.send_document.assert_called_once()
        finally:
            if os.path.exists(path):
                os.unlink(path)


# ---------------------------------------------------------------------------
# _read_stream — written_files tracking
# ---------------------------------------------------------------------------

class TestReadStreamWrittenFiles:

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
    async def test_write_events_tracked(self):
        """Write tool_use events should populate written_files."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/output.txt"}}
            ]}}),
            json.dumps({"type": "result", "result": "done", "session_id": "s1"}),
        ]
        proc = self._make_proc(lines)
        result = await _read_stream(proc)
        assert result["written_files"] == ["/tmp/output.txt"]

    @pytest.mark.asyncio
    async def test_multiple_writes_tracked(self):
        """Multiple Write events should all be tracked."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/a.txt"}}
            ]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/b.png"}}
            ]}}),
            json.dumps({"type": "result", "result": "done", "session_id": "s2"}),
        ]
        proc = self._make_proc(lines)
        result = await _read_stream(proc)
        assert result["written_files"] == ["/tmp/a.txt", "/tmp/b.png"]

    @pytest.mark.asyncio
    async def test_non_write_tools_not_tracked(self):
        """Non-Write tool events should not appear in written_files."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x.txt"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]}}),
            json.dumps({"type": "result", "result": "ok", "session_id": "s3"}),
        ]
        proc = self._make_proc(lines)
        result = await _read_stream(proc)
        assert result["written_files"] == []

    @pytest.mark.asyncio
    async def test_write_with_empty_path_not_tracked(self):
        """Write event with empty file_path should not be tracked."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": ""}}
            ]}}),
            json.dumps({"type": "result", "result": "ok", "session_id": "s4"}),
        ]
        proc = self._make_proc(lines)
        result = await _read_stream(proc)
        assert result["written_files"] == []

    @pytest.mark.asyncio
    async def test_written_files_in_result_with_no_writes(self):
        """Result should always have written_files key, even if empty."""
        lines = [
            json.dumps({"type": "result", "result": "ok", "session_id": "s5"}),
        ]
        proc = self._make_proc(lines)
        result = await _read_stream(proc)
        assert "written_files" in result
        assert result["written_files"] == []

    @pytest.mark.asyncio
    async def test_mixed_tools_only_write_tracked(self):
        """Only Write tool events are tracked among mixed tool usage."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/read.txt"}},
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/written.txt"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/tmp/edit.txt"}},
            ]}}),
            json.dumps({"type": "result", "result": "ok", "session_id": "s6"}),
        ]
        proc = self._make_proc(lines)
        result = await _read_stream(proc)
        assert result["written_files"] == ["/tmp/written.txt"]
