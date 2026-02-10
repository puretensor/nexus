"""Tests for handlers/location.py — location sharing (Feature 1C).

Tests cover:
- Nominatim reverse geocoding integration
- Location with and without caption
- Fallback to raw lat/lon on Nominatim failure
- Authorization check
- Lock prevents concurrent processing
- Session updated after response
- StreamingEditor used for response delivery
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from handlers.location import handle_location, _reverse_geocode


def _make_location_update(lat=64.1466, lon=-21.9426, caption=None, user_id=12345):
    """Create a mock Update object with a location message."""
    update = MagicMock()
    update.effective_chat.id = 12345
    update.effective_user.id = user_id
    update.message.location.latitude = lat
    update.message.location.longitude = lon
    update.message.caption = caption
    update.message.reply_text = AsyncMock()
    update.message.reply_to_message = None
    update.effective_chat.send_action = AsyncMock()
    update.effective_chat.send_voice = AsyncMock()
    return update


def _make_context():
    """Create a mock context."""
    context = MagicMock()
    return context


def _nominatim_response(display_name="Reykjavik, Capital Region, Iceland"):
    """Create a mock Nominatim JSON response."""
    return json.dumps({"display_name": display_name}).encode()


# ---------------------------------------------------------------------------
# Nominatim reverse geocoding
# ---------------------------------------------------------------------------


class TestReverseGeocode:

    def test_successful_geocode(self):
        """Nominatim returns display_name on success."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = _nominatim_response("Reykjavik, Iceland")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("handlers.location.urllib.request.urlopen", return_value=mock_resp):
            result = _reverse_geocode(64.1466, -21.9426)

        assert result == "Reykjavik, Iceland"

    def test_nominatim_network_error(self):
        """Returns None on network error."""
        import urllib.error
        with patch("handlers.location.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("Network error")):
            result = _reverse_geocode(64.1466, -21.9426)

        assert result is None

    def test_nominatim_invalid_json(self):
        """Returns None on invalid JSON response."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("handlers.location.urllib.request.urlopen", return_value=mock_resp):
            result = _reverse_geocode(64.1466, -21.9426)

        assert result is None

    def test_nominatim_missing_display_name(self):
        """Returns None when response lacks display_name."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"other": "data"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("handlers.location.urllib.request.urlopen", return_value=mock_resp):
            result = _reverse_geocode(64.1466, -21.9426)

        assert result is None


# ---------------------------------------------------------------------------
# handle_location tests
# ---------------------------------------------------------------------------


class TestHandleLocation:

    @pytest.mark.asyncio
    async def test_location_calls_nominatim(self):
        """When a location is sent, Nominatim is called and address appears in prompt."""
        update = _make_location_update()
        context = _make_context()

        mock_data = {
            "result": "I see you're in Reykjavik!",
            "session_id": "test-session-123",
            "written_files": [],
        }

        mock_editor = MagicMock()
        mock_editor.text = "I see you're in Reykjavik!"
        mock_editor.finalize = AsyncMock()

        with patch("handlers.location._reverse_geocode", return_value="Reykjavik, Capital Region, Iceland") as mock_geo, \
             patch("handlers.location.get_session", return_value={"model": "sonnet", "session_id": "s1", "message_count": 3}), \
             patch("handlers.location.call_streaming", new_callable=AsyncMock, return_value=mock_data) as mock_claude, \
             patch("handlers.location.StreamingEditor", return_value=mock_editor), \
             patch("handlers.location.upsert_session") as mock_upsert, \
             patch("handlers.location.maybe_generate_summary", new_callable=AsyncMock), \
             patch("handlers.location.scan_and_send_outputs", new_callable=AsyncMock), \
             patch("channels.telegram.commands._keep_typing", new_callable=AsyncMock):

            await handle_location(update, context)

        # Verify Nominatim was called
        mock_geo.assert_called_once_with(64.1466, -21.9426)

        # Verify the prompt contains the address
        prompt_arg = mock_claude.call_args[0][0]
        assert "Reykjavik, Capital Region, Iceland" in prompt_arg
        assert "64.1466" in prompt_arg

    @pytest.mark.asyncio
    async def test_location_with_caption(self):
        """When location has a caption, the caption is used in the prompt."""
        update = _make_location_update(caption="What restaurants are nearby?")
        context = _make_context()

        mock_data = {
            "result": "Here are some restaurants...",
            "session_id": "test-session-123",
            "written_files": [],
        }

        mock_editor = MagicMock()
        mock_editor.text = "Here are some restaurants..."
        mock_editor.finalize = AsyncMock()

        with patch("handlers.location._reverse_geocode", return_value="Reykjavik, Iceland"), \
             patch("handlers.location.get_session", return_value={"model": "sonnet", "session_id": "s1", "message_count": 0}), \
             patch("handlers.location.call_streaming", new_callable=AsyncMock, return_value=mock_data) as mock_claude, \
             patch("handlers.location.StreamingEditor", return_value=mock_editor), \
             patch("handlers.location.upsert_session"), \
             patch("handlers.location.maybe_generate_summary", new_callable=AsyncMock), \
             patch("handlers.location.scan_and_send_outputs", new_callable=AsyncMock), \
             patch("channels.telegram.commands._keep_typing", new_callable=AsyncMock):

            await handle_location(update, context)

        prompt_arg = mock_claude.call_args[0][0]
        assert "What restaurants are nearby?" in prompt_arg
        # Should NOT contain the generic prompt
        assert "ask if they need anything location-related" not in prompt_arg

    @pytest.mark.asyncio
    async def test_location_without_caption(self):
        """When no caption, the generic prompt is used."""
        update = _make_location_update(caption=None)
        context = _make_context()

        mock_data = {
            "result": "I can see you're at...",
            "session_id": "test-session-123",
            "written_files": [],
        }

        mock_editor = MagicMock()
        mock_editor.text = "I can see you're at..."
        mock_editor.finalize = AsyncMock()

        with patch("handlers.location._reverse_geocode", return_value="Reykjavik, Iceland"), \
             patch("handlers.location.get_session", return_value={"model": "sonnet", "session_id": "s1", "message_count": 0}), \
             patch("handlers.location.call_streaming", new_callable=AsyncMock, return_value=mock_data) as mock_claude, \
             patch("handlers.location.StreamingEditor", return_value=mock_editor), \
             patch("handlers.location.upsert_session"), \
             patch("handlers.location.maybe_generate_summary", new_callable=AsyncMock), \
             patch("handlers.location.scan_and_send_outputs", new_callable=AsyncMock), \
             patch("channels.telegram.commands._keep_typing", new_callable=AsyncMock):

            await handle_location(update, context)

        prompt_arg = mock_claude.call_args[0][0]
        assert "ask if they need anything location-related" in prompt_arg

    @pytest.mark.asyncio
    async def test_nominatim_failure_fallback(self):
        """When Nominatim fails, falls back to raw lat/lon in the prompt."""
        update = _make_location_update(lat=51.5074, lon=-0.1278)
        context = _make_context()

        mock_data = {
            "result": "I see your coordinates...",
            "session_id": "test-session-123",
            "written_files": [],
        }

        mock_editor = MagicMock()
        mock_editor.text = "I see your coordinates..."
        mock_editor.finalize = AsyncMock()

        with patch("handlers.location._reverse_geocode", return_value=None) as mock_geo, \
             patch("handlers.location.get_session", return_value={"model": "sonnet", "session_id": "s1", "message_count": 0}), \
             patch("handlers.location.call_streaming", new_callable=AsyncMock, return_value=mock_data) as mock_claude, \
             patch("handlers.location.StreamingEditor", return_value=mock_editor), \
             patch("handlers.location.upsert_session"), \
             patch("handlers.location.maybe_generate_summary", new_callable=AsyncMock), \
             patch("handlers.location.scan_and_send_outputs", new_callable=AsyncMock), \
             patch("channels.telegram.commands._keep_typing", new_callable=AsyncMock):

            await handle_location(update, context)

        prompt_arg = mock_claude.call_args[0][0]
        assert "51.5074" in prompt_arg
        assert "-0.1278" in prompt_arg
        # Should use "coordinates" phrasing, not a display_name
        assert "User is at coordinates" in prompt_arg

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self):
        """User ID that doesn't match AUTHORIZED_USER_ID gets rejected."""
        update = _make_location_update(user_id=99999)
        context = _make_context()

        await handle_location(update, context)

        update.message.reply_text.assert_called_once()
        msg = update.message.reply_text.call_args[0][0]
        assert "Unauthorized" in msg

    @pytest.mark.asyncio
    async def test_lock_prevents_concurrent(self):
        """When lock is already held, returns 'Still processing' message."""
        update = _make_location_update()
        context = _make_context()

        # Pre-acquire the lock
        from db import get_lock
        lock = get_lock(12345)

        async with lock:
            await handle_location(update, context)

        # Should have replied with "Still processing"
        update.message.reply_text.assert_called_once()
        msg = update.message.reply_text.call_args[0][0]
        assert "Still processing" in msg

    @pytest.mark.asyncio
    async def test_session_updated_after_response(self):
        """Verify upsert_session is called after Claude responds."""
        update = _make_location_update()
        context = _make_context()

        mock_data = {
            "result": "Response text",
            "session_id": "new-session-456",
            "written_files": [],
        }

        mock_editor = MagicMock()
        mock_editor.text = "Response text"
        mock_editor.finalize = AsyncMock()

        with patch("handlers.location._reverse_geocode", return_value="Test Location"), \
             patch("handlers.location.get_session", return_value={"model": "opus", "session_id": "old-session", "message_count": 5}), \
             patch("handlers.location.call_streaming", new_callable=AsyncMock, return_value=mock_data), \
             patch("handlers.location.StreamingEditor", return_value=mock_editor), \
             patch("handlers.location.upsert_session") as mock_upsert, \
             patch("handlers.location.maybe_generate_summary", new_callable=AsyncMock), \
             patch("handlers.location.scan_and_send_outputs", new_callable=AsyncMock), \
             patch("channels.telegram.commands._keep_typing", new_callable=AsyncMock):

            await handle_location(update, context)

        mock_upsert.assert_called_once_with(12345, "new-session-456", "opus", 6)

    @pytest.mark.asyncio
    async def test_streaming_editor_used(self):
        """Verify StreamingEditor is instantiated and used for response delivery."""
        update = _make_location_update()
        context = _make_context()

        mock_data = {
            "result": "Streamed response",
            "session_id": "test-session-123",
            "written_files": [],
        }

        mock_editor = MagicMock()
        mock_editor.text = "Streamed response"
        mock_editor.finalize = AsyncMock()

        with patch("handlers.location._reverse_geocode", return_value="Test Location"), \
             patch("handlers.location.get_session", return_value={"model": "sonnet", "session_id": "s1", "message_count": 0}), \
             patch("handlers.location.call_streaming", new_callable=AsyncMock, return_value=mock_data) as mock_claude, \
             patch("handlers.location.StreamingEditor", return_value=mock_editor) as mock_se_cls, \
             patch("handlers.location.upsert_session"), \
             patch("handlers.location.maybe_generate_summary", new_callable=AsyncMock), \
             patch("handlers.location.scan_and_send_outputs", new_callable=AsyncMock), \
             patch("channels.telegram.commands._keep_typing", new_callable=AsyncMock):

            await handle_location(update, context)

        # StreamingEditor was instantiated with the chat
        mock_se_cls.assert_called_once_with(update.effective_chat)

        # It was passed to call_streaming
        call_kwargs = mock_claude.call_args[1]
        assert call_kwargs["streaming_editor"] is mock_editor

        # finalize was called
        mock_editor.finalize.assert_called_once()
