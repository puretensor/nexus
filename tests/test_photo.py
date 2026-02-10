"""Tests for handlers/photo.py — image support (Feature 1A)."""

import asyncio
import io
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
import uuid

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from handlers.photo import handle_photo, IMAGE_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(chat_id=12345, user_id=12345, caption=None, has_reply=False, reply_text=None):
    """Create a mock Update with photo data."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    update.effective_chat.send_action = AsyncMock()

    # Photo array — largest is last
    photo_small = MagicMock()
    photo_small.file_id = "small_id"
    photo_large = MagicMock()
    photo_large.file_id = "large_id"
    update.message.photo = [photo_small, photo_large]

    update.message.caption = caption

    # Reply context
    if has_reply:
        reply_msg = MagicMock()
        reply_msg.text = reply_text
        reply_msg.caption = None
        update.message.reply_to_message = reply_msg
    else:
        update.message.reply_to_message = None

    return update


def _make_context():
    """Create a mock context with bot.get_file."""
    context = MagicMock()
    mock_file = AsyncMock()
    # download_to_memory writes bytes to the BytesIO buffer
    async def fake_download(buf):
        buf.write(b"\xff\xd8\xff\xe0fake_jpeg_data")
    mock_file.download_to_memory = AsyncMock(side_effect=fake_download)
    context.bot.get_file = AsyncMock(return_value=mock_file)
    return context


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHandlePhoto:

    @pytest.fixture(autouse=True)
    def setup_patches(self, tmp_path):
        """Patch IMAGE_DIR to use a temp directory and mock Claude streaming."""
        self.tmp_image_dir = tmp_path / "hal_images"
        self.tmp_image_dir.mkdir()

        self.mock_editor = AsyncMock()
        self.mock_editor.text = "Claude response text"
        self.mock_editor.finalize = AsyncMock()

        self.mock_data = {
            "result": "I can see the image.",
            "session_id": "sess-photo-123",
        }

        patches = [
            patch("handlers.photo.IMAGE_DIR", self.tmp_image_dir),
            patch("handlers.photo.call_streaming", new_callable=AsyncMock, return_value=self.mock_data),
            patch("handlers.photo.StreamingEditor", return_value=self.mock_editor),
            patch("handlers.photo.get_session", return_value=None),
            patch("handlers.photo.upsert_session"),
            patch("handlers.photo.maybe_generate_summary", new_callable=AsyncMock),
            patch("handlers.photo.get_lock", return_value=asyncio.Lock()),
            patch("channels.telegram.commands._keep_typing", new_callable=AsyncMock),
        ]
        self.mocks = {}
        for p in patches:
            mock = p.start()
            # Store by target name
            attr = p.attribute if hasattr(p, 'attribute') and p.attribute else str(p).split(".")[-1].rstrip("'>")
            self.mocks[attr] = mock
        yield
        for p in patches:
            p.stop()

    @pytest.mark.asyncio
    async def test_photo_with_caption(self):
        """Photo with caption uses caption as prompt text."""
        update = _make_update(caption="What is this?")
        context = _make_context()
        await handle_photo(update, context)

        # Verify call_streaming was called with caption in prompt
        call_args = self.mocks["call_streaming"].call_args
        prompt = call_args[0][0]
        assert "What is this?" in prompt
        assert "read/view this image file" in prompt

    @pytest.mark.asyncio
    async def test_photo_without_caption(self):
        """Photo without caption uses default prompt."""
        update = _make_update(caption=None)
        context = _make_context()
        await handle_photo(update, context)

        call_args = self.mocks["call_streaming"].call_args
        prompt = call_args[0][0]
        assert "describe what you see" in prompt

    @pytest.mark.asyncio
    async def test_photo_empty_caption(self):
        """Photo with whitespace-only caption uses default prompt."""
        update = _make_update(caption="   ")
        context = _make_context()
        await handle_photo(update, context)

        call_args = self.mocks["call_streaming"].call_args
        prompt = call_args[0][0]
        assert "describe what you see" in prompt

    @pytest.mark.asyncio
    async def test_largest_photo_selected(self):
        """Should download the largest photo (last in the array)."""
        update = _make_update()
        context = _make_context()
        await handle_photo(update, context)

        # get_file should be called with the last photo's file_id
        context.bot.get_file.assert_called_once_with("large_id")

    @pytest.mark.asyncio
    async def test_image_saved_to_disk(self):
        """Image bytes should be saved to IMAGE_DIR."""
        update = _make_update()
        context = _make_context()
        await handle_photo(update, context)

        # Image was saved and then cleaned up — check prompt has path
        call_args = self.mocks["call_streaming"].call_args
        prompt = call_args[0][0]
        assert str(self.tmp_image_dir) in prompt
        assert ".jpg" in prompt

    @pytest.mark.asyncio
    async def test_image_cleanup(self):
        """Temp image file should be deleted after processing."""
        update = _make_update()
        context = _make_context()
        await handle_photo(update, context)

        # After handler completes, no .jpg files should remain
        remaining = list(self.tmp_image_dir.glob("*.jpg"))
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_session_upserted(self):
        """Session should be upserted after successful response."""
        update = _make_update()
        context = _make_context()
        await handle_photo(update, context)

        self.mocks["upsert_session"].assert_called_once_with(
            12345, "sess-photo-123", "sonnet", 1
        )

    @pytest.mark.asyncio
    async def test_existing_session_resumed(self):
        """Should resume existing session if one exists."""
        self.mocks["get_session"].return_value = {
            "session_id": "existing-sess",
            "model": "opus",
            "message_count": 5,
            "created_at": "2026-01-01",
        }
        update = _make_update()
        context = _make_context()
        await handle_photo(update, context)

        call_args = self.mocks["call_streaming"].call_args
        assert call_args[0][1] == "existing-sess"  # session_id
        assert call_args[0][2] == "opus"  # model

    @pytest.mark.asyncio
    async def test_locked_rejects(self):
        """Should reject if lock is already held."""
        lock = asyncio.Lock()
        await lock.acquire()  # hold the lock
        self.mocks["get_lock"].return_value = lock

        update = _make_update()
        context = _make_context()
        await handle_photo(update, context)

        update.message.reply_text.assert_called_with(
            "Still processing previous message — please wait."
        )
        self.mocks["call_streaming"].assert_not_called()
        lock.release()

    @pytest.mark.asyncio
    async def test_reply_context_included(self):
        """Reply-to context should be prepended to prompt."""
        update = _make_update(has_reply=True, reply_text="Previous message")
        context = _make_context()
        await handle_photo(update, context)

        call_args = self.mocks["call_streaming"].call_args
        prompt = call_args[0][0]
        assert '[Replying to: "Previous message"]' in prompt

    @pytest.mark.asyncio
    async def test_no_photo_returns_early(self):
        """If message has no photos, handler returns early."""
        update = _make_update()
        update.message.photo = []
        context = _make_context()
        await handle_photo(update, context)

        self.mocks["call_streaming"].assert_not_called()

    @pytest.mark.asyncio
    async def test_ack_message_sent(self):
        """Acknowledgment message should be sent before processing."""
        update = _make_update()
        context = _make_context()
        await handle_photo(update, context)

        # Find the ack call (first reply_text with "Processing" or "Starting")
        calls = update.message.reply_text.call_args_list
        ack_calls = [c for c in calls if "image" in str(c).lower()]
        assert len(ack_calls) >= 1

    @pytest.mark.asyncio
    async def test_editor_finalized(self):
        """StreamingEditor.finalize() should be called when editor has text."""
        update = _make_update()
        context = _make_context()
        await handle_photo(update, context)

        self.mock_editor.finalize.assert_called_once()

    @pytest.mark.asyncio
    async def test_download_error_handled(self):
        """Download failure should send error message."""
        update = _make_update()
        context = _make_context()
        context.bot.get_file = AsyncMock(side_effect=RuntimeError("Download failed"))
        await handle_photo(update, context)

        # Should have sent an error reply
        calls = update.message.reply_text.call_args_list
        error_calls = [c for c in calls if "Error" in str(c) or "error" in str(c)]
        assert len(error_calls) >= 1

    @pytest.mark.asyncio
    async def test_image_dir_created(self):
        """IMAGE_DIR should be created if it doesn't exist."""
        import shutil
        # Remove the dir
        shutil.rmtree(self.tmp_image_dir, ignore_errors=True)
        assert not self.tmp_image_dir.exists()

        update = _make_update()
        context = _make_context()
        await handle_photo(update, context)

        # Dir should have been created during handling
        # (it may have been cleaned up, but the call should have succeeded)
        self.mocks["call_streaming"].assert_called_once()
