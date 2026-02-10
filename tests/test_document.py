"""Tests for handlers/document.py — Feature 1B: Document/File Sharing.

Focus areas:
- File type classification (text, PDF, image, binary)
- Size limit enforcement
- Prompt construction per file type
- Handler integration (lock, session, streaming, cleanup)
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from handlers.document import (
        _is_text_file,
        _is_pdf,
        _is_image,
        _build_document_prompt,
        handle_document,
        DOC_DIR,
        MAX_SIZE,
        WARN_SIZE,
        MAX_INLINE_CHARS,
    )
    from db import init_db


# ---------------------------------------------------------------------------
# File type classification
# ---------------------------------------------------------------------------

class TestIsTextFile:

    def test_python_extension(self):
        assert _is_text_file("script.py", None) is True

    def test_json_extension(self):
        assert _is_text_file("data.json", "application/json") is True

    def test_txt_extension(self):
        assert _is_text_file("notes.txt", "text/plain") is True

    def test_markdown_extension(self):
        assert _is_text_file("README.md", None) is True

    def test_yaml_extension(self):
        assert _is_text_file("config.yaml", None) is True

    def test_yml_extension(self):
        assert _is_text_file("config.yml", None) is True

    def test_csv_extension(self):
        assert _is_text_file("data.csv", "text/csv") is True

    def test_shell_extension(self):
        assert _is_text_file("deploy.sh", None) is True

    def test_sql_extension(self):
        assert _is_text_file("query.sql", None) is True

    def test_text_mime_type(self):
        assert _is_text_file("unknown_file", "text/x-something") is True

    def test_makefile_no_extension(self):
        assert _is_text_file("Makefile", None) is True

    def test_dockerfile_no_extension(self):
        assert _is_text_file("Dockerfile", None) is True

    def test_binary_file(self):
        assert _is_text_file("image.png", "image/png") is False

    def test_pdf_not_text(self):
        assert _is_text_file("doc.pdf", "application/pdf") is False

    def test_zip_not_text(self):
        assert _is_text_file("archive.zip", "application/zip") is False

    def test_case_insensitive_extension(self):
        assert _is_text_file("FILE.PY", None) is True

    def test_env_file(self):
        assert _is_text_file(".env", None) is True

    def test_go_file(self):
        assert _is_text_file("main.go", None) is True

    def test_rust_file(self):
        assert _is_text_file("lib.rs", None) is True


class TestIsPdf:

    def test_pdf_extension(self):
        assert _is_pdf("report.pdf", None) is True

    def test_pdf_mime(self):
        assert _is_pdf("document", "application/pdf") is True

    def test_pdf_extension_uppercase(self):
        assert _is_pdf("REPORT.PDF", None) is True

    def test_not_pdf(self):
        assert _is_pdf("script.py", "text/x-python") is False


class TestIsImage:

    def test_png_mime(self):
        assert _is_image("image/png") is True

    def test_jpeg_mime(self):
        assert _is_image("image/jpeg") is True

    def test_gif_mime(self):
        assert _is_image("image/gif") is True

    def test_webp_mime(self):
        assert _is_image("image/webp") is True

    def test_not_image(self):
        assert _is_image("application/pdf") is False

    def test_none_mime(self):
        assert _is_image(None) is False


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

class TestBuildDocumentPrompt:

    def test_text_file_inlines_content(self):
        content = b"print('hello world')"
        prompt, cleanup = _build_document_prompt(
            "script.py", "text/x-python", "/tmp/hal_docs/script.py", content, None
        )
        assert "print('hello world')" in prompt
        assert "```" in prompt
        assert "script.py" in prompt
        assert cleanup is True

    def test_text_file_with_caption(self):
        content = b"x = 1"
        prompt, cleanup = _build_document_prompt(
            "test.py", None, "/tmp/hal_docs/test.py", content, "Explain this code"
        )
        assert "Explain this code" in prompt
        assert "x = 1" in prompt
        assert cleanup is True

    def test_text_file_without_caption(self):
        content = b"data"
        prompt, cleanup = _build_document_prompt(
            "notes.txt", "text/plain", "/tmp/hal_docs/notes.txt", content, None
        )
        assert "Analyze its contents" in prompt

    def test_text_file_truncates_large_content(self):
        content = b"x" * (MAX_INLINE_CHARS + 5000)
        prompt, cleanup = _build_document_prompt(
            "big.txt", "text/plain", "/tmp/hal_docs/big.txt", content, None
        )
        assert "truncated" in prompt
        assert len(prompt) < MAX_INLINE_CHARS + 1000

    def test_text_file_utf8_errors_replaced(self):
        content = b"hello \xff\xfe world"
        prompt, cleanup = _build_document_prompt(
            "data.txt", "text/plain", "/tmp/hal_docs/data.txt", content, None
        )
        assert "hello" in prompt
        assert "world" in prompt
        assert cleanup is True

    def test_pdf_references_path(self):
        content = b"%PDF-1.4 binary data"
        prompt, cleanup = _build_document_prompt(
            "report.pdf", "application/pdf", "/tmp/hal_docs/report.pdf", content, None
        )
        assert "/tmp/hal_docs/report.pdf" in prompt
        assert "Read tool" in prompt
        assert cleanup is False

    def test_pdf_with_caption(self):
        content = b"%PDF"
        prompt, cleanup = _build_document_prompt(
            "doc.pdf", "application/pdf", "/tmp/hal_docs/doc.pdf", content, "Summarize this"
        )
        assert "Summarize this" in prompt
        assert "/tmp/hal_docs/doc.pdf" in prompt

    def test_image_document_references_path(self):
        content = b"\x89PNG binary"
        prompt, cleanup = _build_document_prompt(
            "photo.png", "image/png", "/tmp/hal_docs/photo.png", content, None
        )
        assert "/tmp/hal_docs/photo.png" in prompt
        assert "image file" in prompt
        assert cleanup is False

    def test_binary_file_references_path(self):
        content = b"\x00\x01 binary"
        prompt, cleanup = _build_document_prompt(
            "data.bin", "application/octet-stream", "/tmp/hal_docs/data.bin", content, None
        )
        assert "/tmp/hal_docs/data.bin" in prompt
        assert "application/octet-stream" in prompt
        assert cleanup is False

    def test_binary_no_mime(self):
        content = b"\x00\x01"
        prompt, cleanup = _build_document_prompt(
            "data.bin", None, "/tmp/hal_docs/data.bin", content, "What is this?"
        )
        assert "What is this?" in prompt
        assert cleanup is False


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------

def _make_update(
    chat_id=12345,
    user_id=12345,
    file_name="test.py",
    file_size=100,
    mime_type="text/x-python",
    file_id="file-abc",
    caption=None,
    reply_to=None,
):
    """Build a mock Update with document attached."""
    update = MagicMock(spec=["effective_chat", "effective_user", "message"])
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.send_action = AsyncMock()

    update.effective_user = MagicMock()
    update.effective_user.id = user_id

    document = MagicMock()
    document.file_name = file_name
    document.file_size = file_size
    document.mime_type = mime_type
    document.file_id = file_id

    update.message = MagicMock()
    update.message.document = document
    update.message.caption = caption
    update.message.reply_text = AsyncMock()
    update.message.reply_to_message = reply_to

    return update


def _make_context(file_bytes=b"print('hello')"):
    """Build a mock context with bot that can download files."""
    context = MagicMock()
    tg_file = MagicMock()

    async def download_to_memory(buf):
        buf.write(file_bytes)

    tg_file.download_to_memory = download_to_memory
    context.bot = MagicMock()
    context.bot.get_file = AsyncMock(return_value=tg_file)
    return context


class TestHandleDocumentSizeCheck:

    @pytest.mark.asyncio
    async def test_rejects_oversized_file(self):
        update = _make_update(file_size=MAX_SIZE + 1)
        context = _make_context()
        await handle_document(update, context)
        update.message.reply_text.assert_called_once()
        call_text = update.message.reply_text.call_args[0][0]
        assert "too large" in call_text.lower()

    @pytest.mark.asyncio
    async def test_rejects_exactly_over_limit(self):
        update = _make_update(file_size=25 * 1024 * 1024 + 1)
        context = _make_context()
        await handle_document(update, context)
        call_text = update.message.reply_text.call_args[0][0]
        assert "25 MB" in call_text


class TestHandleDocumentIntegration:

    @pytest.fixture(autouse=True)
    def use_temp_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test_sessions.db"
        monkeypatch.setattr("db.DB_PATH", db_path)
        init_db()

    @pytest.mark.asyncio
    async def test_lock_blocks_concurrent(self):
        """Handler returns early if lock is held."""
        update = _make_update()
        context = _make_context()
        lock = asyncio.Lock()
        await lock.acquire()  # hold the lock
        with patch("handlers.document.get_lock", return_value=lock):
            await handle_document(update, context)
        update.message.reply_text.assert_called_once()
        assert "still processing" in update.message.reply_text.call_args[0][0].lower()
        lock.release()

    @pytest.mark.asyncio
    async def test_calls_claude_streaming(self):
        """Handler calls call_streaming with the built prompt."""
        update = _make_update(caption="Explain this")
        context = _make_context(b"x = 42")
        mock_data = {"result": "It assigns 42 to x.", "session_id": "sess-123"}

        with patch("handlers.document.call_streaming", new_callable=AsyncMock, return_value=mock_data) as mock_call:
            await handle_document(update, context)
            mock_call.assert_called_once()
            prompt_arg = mock_call.call_args[0][0]
            assert "x = 42" in prompt_arg
            assert "Explain this" in prompt_arg

    @pytest.mark.asyncio
    async def test_upserts_session(self):
        """Handler upserts session after successful response."""
        update = _make_update()
        context = _make_context()
        mock_data = {"result": "Analysis done.", "session_id": "sess-456"}

        with patch("handlers.document.call_streaming", new_callable=AsyncMock, return_value=mock_data):
            with patch("handlers.document.upsert_session") as mock_upsert:
                await handle_document(update, context)
                mock_upsert.assert_called_once()
                args = mock_upsert.call_args[0]
                assert args[0] == 12345       # chat_id
                assert args[1] == "sess-456"  # session_id

    @pytest.mark.asyncio
    async def test_text_file_cleanup(self):
        """Temp file cleaned up for text files (content inlined)."""
        update = _make_update(file_name="code.py", mime_type="text/x-python")
        context = _make_context(b"print(1)")
        mock_data = {"result": "It prints 1.", "session_id": "sess-789"}

        with patch("handlers.document.call_streaming", new_callable=AsyncMock, return_value=mock_data):
            await handle_document(update, context)
        # File should have been cleaned up
        assert not os.path.exists(os.path.join(DOC_DIR, "code.py"))

    @pytest.mark.asyncio
    async def test_pdf_no_cleanup(self):
        """PDF temp file NOT cleaned up (Claude needs to read it)."""
        update = _make_update(file_name="report.pdf", mime_type="application/pdf")
        context = _make_context(b"%PDF-1.4 data")
        mock_data = {"result": "Report summary.", "session_id": "sess-pdf"}

        with patch("handlers.document.call_streaming", new_callable=AsyncMock, return_value=mock_data):
            await handle_document(update, context)
        # File should still exist
        path = os.path.join(DOC_DIR, "report.pdf")
        assert os.path.exists(path)
        # Clean up after test
        os.unlink(path)

    @pytest.mark.asyncio
    async def test_warns_large_file(self):
        """Files over 10MB get a warning message."""
        update = _make_update(file_size=WARN_SIZE + 1)
        context = _make_context(b"data")
        mock_data = {"result": "ok", "session_id": "sess-big"}

        with patch("handlers.document.call_streaming", new_callable=AsyncMock, return_value=mock_data):
            await handle_document(update, context)
        # At least one reply should mention "large file"
        calls = [str(c) for c in update.message.reply_text.call_args_list]
        assert any("large file" in c.lower() for c in calls)

    @pytest.mark.asyncio
    async def test_reply_context_prepended(self):
        """Reply-to context is prepended to the prompt."""
        reply_msg = MagicMock()
        reply_msg.text = "Previous message"
        reply_msg.caption = None

        update = _make_update(reply_to=reply_msg, caption="Follow up")
        context = _make_context(b"content")
        mock_data = {"result": "response", "session_id": "sess-reply"}

        with patch("handlers.document.call_streaming", new_callable=AsyncMock, return_value=mock_data) as mock_call:
            await handle_document(update, context)
            prompt_arg = mock_call.call_args[0][0]
            assert "Replying to" in prompt_arg
            assert "Previous message" in prompt_arg

    @pytest.mark.asyncio
    async def test_no_document_returns_early(self):
        """Handler returns silently if no document attached."""
        update = _make_update()
        update.message.document = None
        context = _make_context()
        await handle_document(update, context)
        # No further calls — only the auth check runs
        context.bot.get_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_timeout_error(self):
        """TimeoutError is caught and reported to user."""
        update = _make_update()
        context = _make_context()

        with patch("handlers.document.call_streaming", new_callable=AsyncMock, side_effect=TimeoutError("timeout")):
            await handle_document(update, context)
        calls = [str(c) for c in update.message.reply_text.call_args_list]
        assert any("timed out" in c.lower() for c in calls)

    @pytest.mark.asyncio
    async def test_handles_runtime_error(self):
        """RuntimeError is caught and reported to user."""
        update = _make_update()
        context = _make_context()

        with patch("handlers.document.call_streaming", new_callable=AsyncMock, side_effect=RuntimeError("claude failed")):
            await handle_document(update, context)
        calls = [str(c) for c in update.message.reply_text.call_args_list]
        assert any("claude failed" in c.lower() for c in calls)
