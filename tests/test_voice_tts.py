"""Tests for handlers/voice_tts.py — Voice/TTS responses (Feature 6A)."""

import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from handlers.voice_tts import (
        is_voice_mode,
        set_voice_mode,
        get_voice_system_prompt_addition,
        text_to_voice_note,
        _clean_for_tts,
        _voice_mode,
    )


# ---------------------------------------------------------------------------
# Voice mode state
# ---------------------------------------------------------------------------

class TestVoiceMode:

    def setup_method(self):
        """Clear voice mode state before each test."""
        _voice_mode.clear()

    def test_voice_mode_default_off(self):
        """Voice mode is off by default for any chat."""
        assert is_voice_mode(123) is False

    def test_set_voice_mode_on(self):
        """Setting voice mode on works."""
        set_voice_mode(123, True)
        assert is_voice_mode(123) is True

    def test_set_voice_mode_off(self):
        """Setting voice mode off after enabling works."""
        set_voice_mode(123, True)
        set_voice_mode(123, False)
        assert is_voice_mode(123) is False

    def test_voice_mode_per_chat(self):
        """Voice mode is independent per chat ID."""
        set_voice_mode(100, True)
        set_voice_mode(200, False)
        assert is_voice_mode(100) is True
        assert is_voice_mode(200) is False
        assert is_voice_mode(300) is False


# ---------------------------------------------------------------------------
# _clean_for_tts
# ---------------------------------------------------------------------------

class TestCleanForTTS:

    def test_removes_code_blocks(self):
        """Code blocks are stripped."""
        result = _clean_for_tts("text ```python\nprint('hi')\n``` more")
        assert "print" not in result
        assert "text" in result
        assert "more" in result

    def test_removes_inline_code(self):
        """Inline code backticks are removed."""
        result = _clean_for_tts("Use `pip install` to install")
        assert "`" not in result
        assert "Use" in result
        assert "to install" in result

    def test_removes_bold_italic(self):
        """Markdown bold and italic markers are removed."""
        result = _clean_for_tts("This is **bold** and *italic* text")
        assert "**" not in result
        assert "*" not in result
        assert "bold" in result
        assert "italic" in result

    def test_removes_headers(self):
        """Markdown headers (# symbols) are removed."""
        result = _clean_for_tts("# Header\n## Sub header\nText")
        assert "#" not in result
        assert "Header" in result
        assert "Sub header" in result

    def test_removes_links(self):
        """Markdown links [text](url) become just the text."""
        result = _clean_for_tts("Check [this link](https://example.com) out")
        assert "[" not in result
        assert "](https" not in result
        assert "this link" in result

    def test_removes_urls(self):
        """Bare URLs are removed."""
        result = _clean_for_tts("Visit https://example.com/path for info")
        assert "https://" not in result
        assert "Visit" in result
        assert "for info" in result

    def test_collapses_whitespace(self):
        """Multiple spaces and newlines collapse to single space."""
        result = _clean_for_tts("Hello    world\n\n\nfoo   bar")
        assert result == "Hello world foo bar"

    def test_empty_input(self):
        """Empty input returns empty string."""
        assert _clean_for_tts("") == ""

    def test_whitespace_only(self):
        """Whitespace-only input returns empty string."""
        assert _clean_for_tts("   \n  \t  ") == ""

    def test_underscore_italic(self):
        """Underscore italic markers are removed."""
        result = _clean_for_tts("This is _italic_ and __bold__ text")
        assert "_" not in result
        assert "italic" in result
        assert "bold" in result


# ---------------------------------------------------------------------------
# get_voice_system_prompt_addition
# ---------------------------------------------------------------------------

class TestGetVoiceSystemPromptAddition:

    def test_returns_string_with_100_words(self):
        """Prompt addition mentions 100 words limit."""
        result = get_voice_system_prompt_addition()
        assert isinstance(result, str)
        assert "100 words" in result

    def test_contains_voice_mode_marker(self):
        """Prompt addition mentions voice mode."""
        result = get_voice_system_prompt_addition()
        assert "Voice mode" in result

    def test_mentions_no_markdown(self):
        """Prompt addition tells Claude not to use markdown."""
        result = get_voice_system_prompt_addition()
        assert "markdown" in result.lower() or "Markdown" in result


# ---------------------------------------------------------------------------
# text_to_voice_note (uses local HAL TTS API via aiohttp)
# ---------------------------------------------------------------------------

def _mock_aiohttp_session(status=200, body=b"fake wav data"):
    """Create a mock aiohttp session for TTS API calls."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.read = AsyncMock(return_value=body)
    mock_resp.text = AsyncMock(return_value="error")
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


class TestTextToVoiceNote:

    @pytest.mark.asyncio
    async def test_api_connection_failure_returns_none(self):
        """When TTS API is unreachable, returns None."""
        import aiohttp as aiohttp_mod
        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=aiohttp_mod.ClientError("Connection refused"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("handlers.voice_tts.aiohttp.ClientSession", return_value=mock_session):
            result = await text_to_voice_note("Hello world")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_text_returns_none(self):
        """Empty or whitespace text returns None."""
        result = await text_to_voice_note("   ")
        assert result is None

    @pytest.mark.asyncio
    async def test_success(self):
        """Successful TTS conversion returns OGG bytes."""
        mock_session = _mock_aiohttp_session(200, b"fake wav data")

        mock_proc = AsyncMock()
        mock_proc.returncode = 0

        saved_paths = {}
        orig_write = Path.write_bytes

        async def mock_wait():
            # Find the wav file and create corresponding ogg
            import glob
            import tempfile
            tmp_dir = tempfile.gettempdir()
            for f in Path(tmp_dir).glob("*.wav"):
                ogg = str(f).replace(".wav", ".ogg")
                Path(ogg).write_bytes(b"fake ogg data")
                break
            return 0

        mock_proc.wait = mock_wait

        with patch("handlers.voice_tts.aiohttp.ClientSession", return_value=mock_session):
            with patch("handlers.voice_tts.asyncio.create_subprocess_exec",
                       new_callable=AsyncMock, return_value=mock_proc):
                result = await text_to_voice_note("Hello world")

        assert result == b"fake ogg data"

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self):
        """TTS API returning error status returns None."""
        mock_session = _mock_aiohttp_session(500, b"Internal error")

        with patch("handlers.voice_tts.aiohttp.ClientSession", return_value=mock_session):
            result = await text_to_voice_note("Hello world")

        assert result is None

    @pytest.mark.asyncio
    async def test_ffmpeg_failure_returns_none(self):
        """ffmpeg returning non-zero exit code returns None."""
        mock_session = _mock_aiohttp_session(200, b"fake wav data")

        mock_proc = AsyncMock()
        mock_proc.returncode = 1

        async def mock_wait():
            return 1

        mock_proc.wait = mock_wait

        with patch("handlers.voice_tts.aiohttp.ClientSession", return_value=mock_session):
            with patch("handlers.voice_tts.asyncio.create_subprocess_exec",
                       new_callable=AsyncMock, return_value=mock_proc):
                result = await text_to_voice_note("Hello world")

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """TTS conversion timeout returns None."""
        mock_session = _mock_aiohttp_session(200, b"fake wav data")

        mock_proc = AsyncMock()

        async def mock_wait():
            await asyncio.sleep(100)
            return 0

        mock_proc.wait = mock_wait

        with patch("handlers.voice_tts.aiohttp.ClientSession", return_value=mock_session):
            with patch("handlers.voice_tts.asyncio.create_subprocess_exec",
                       new_callable=AsyncMock, return_value=mock_proc):
                with patch("handlers.voice_tts.asyncio.wait_for",
                           side_effect=asyncio.TimeoutError):
                    result = await text_to_voice_note("Hello world")

        assert result is None

    @pytest.mark.asyncio
    async def test_long_text_truncated(self):
        """Text over 3000 chars is truncated before TTS."""
        long_text = "Hello world. " * 500  # well over 3000 chars

        captured_json = {}
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"fake wav data")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        def capture_post(url, **kwargs):
            captured_json.update(kwargs.get("json", {}))
            return mock_resp
        mock_session.post = capture_post
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        async def mock_wait():
            return 0
        mock_proc.wait = mock_wait

        with patch("handlers.voice_tts.aiohttp.ClientSession", return_value=mock_session):
            with patch("handlers.voice_tts.asyncio.create_subprocess_exec",
                       new_callable=AsyncMock, return_value=mock_proc):
                result = await text_to_voice_note(long_text)

        # Verify text was truncated
        assert len(captured_json.get("text", "")) <= 3004  # 3000 + "..."


# ---------------------------------------------------------------------------
# /voice command handler
# ---------------------------------------------------------------------------

class TestCmdVoice:

    def setup_method(self):
        """Clear voice mode state before each test."""
        _voice_mode.clear()

    def _make_update(self, chat_id=12345, user_id=12345):
        """Create a mock Update for command handlers."""
        update = MagicMock()
        update.effective_chat.id = chat_id
        update.effective_user.id = user_id
        update.message.reply_text = AsyncMock()
        return update

    def _make_context(self, args=None):
        """Create a mock context with optional args."""
        context = MagicMock()
        context.args = args or []
        return context

    @pytest.mark.asyncio
    async def test_toggle_on(self):
        """/voice with no args toggles mode on."""
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            from channels.telegram.commands import cmd_voice

        update = self._make_update()
        context = self._make_context()
        await cmd_voice(update, context)

        assert is_voice_mode(12345) is True
        update.message.reply_text.assert_called_with("Voice mode ON.")

    @pytest.mark.asyncio
    async def test_toggle_off(self):
        """/voice toggles mode off when already on."""
        set_voice_mode(12345, True)

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            from channels.telegram.commands import cmd_voice

        update = self._make_update()
        context = self._make_context()
        await cmd_voice(update, context)

        assert is_voice_mode(12345) is False
        update.message.reply_text.assert_called_with("Voice mode OFF.")

    @pytest.mark.asyncio
    async def test_explicit_on(self):
        """/voice on explicitly enables voice mode."""
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            from channels.telegram.commands import cmd_voice

        update = self._make_update()
        context = self._make_context(args=["on"])
        await cmd_voice(update, context)

        assert is_voice_mode(12345) is True
        update.message.reply_text.assert_called_with(
            "Voice mode ON. Responses will include voice notes."
        )

    @pytest.mark.asyncio
    async def test_explicit_off(self):
        """/voice off explicitly disables voice mode."""
        set_voice_mode(12345, True)

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            from channels.telegram.commands import cmd_voice

        update = self._make_update()
        context = self._make_context(args=["off"])
        await cmd_voice(update, context)

        assert is_voice_mode(12345) is False
        update.message.reply_text.assert_called_with("Voice mode OFF.")


# ---------------------------------------------------------------------------
# Voice mode integration with handle_message
# ---------------------------------------------------------------------------

class TestVoiceModeIntegration:

    def setup_method(self):
        """Clear voice mode state before each test."""
        _voice_mode.clear()

    @pytest.mark.asyncio
    async def test_voice_mode_adds_system_prompt(self):
        """When voice mode is on, extra_system_prompt is passed to call_streaming."""
        set_voice_mode(12345, True)

        mock_data = {
            "result": "Short response.",
            "session_id": "sess-123",
            "written_files": [],
        }

        mock_editor = AsyncMock()
        mock_editor.text = "Short response."
        mock_editor.finalize = AsyncMock(return_value=[])
        mock_editor.sent_messages = []

        update = MagicMock()
        update.effective_chat.id = 12345
        update.effective_user.id = 12345
        update.message.text = "Hello"
        update.message.reply_text = AsyncMock()
        update.message.reply_to_message = None
        update.effective_chat.send_action = AsyncMock()
        update.effective_chat.send_voice = AsyncMock()

        context = MagicMock()

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            with patch("channels.telegram.commands.get_session", return_value={"session_id": "sess-123", "model": "sonnet", "message_count": 1, "name": "default"}), \
                 patch("channels.telegram.commands.call_streaming", new_callable=AsyncMock, return_value=mock_data) as mock_call, \
                 patch("channels.telegram.commands.StreamingEditor", return_value=mock_editor), \
                 patch("channels.telegram.commands.upsert_session"), \
                 patch("channels.telegram.commands.maybe_generate_summary", new_callable=AsyncMock), \
                 patch("channels.telegram.commands.get_lock", return_value=asyncio.Lock()), \
                 patch("channels.telegram.commands._keep_typing", new_callable=AsyncMock), \
                 patch("channels.telegram.commands.text_to_voice_note", new_callable=AsyncMock, return_value=b"fake ogg"), \
                 patch("channels.telegram.commands.get_contextual_keyboard", return_value=None), \
                 patch("channels.telegram.commands.scan_and_send_outputs", new_callable=AsyncMock):

                from channels.telegram.commands import handle_message
                await handle_message(update, context)

                # Verify extra_system_prompt was passed
                call_kwargs = mock_call.call_args
                assert call_kwargs[1].get("extra_system_prompt") is not None
                assert "100 words" in call_kwargs[1]["extra_system_prompt"]

    @pytest.mark.asyncio
    async def test_no_voice_mode_no_system_prompt(self):
        """When voice mode is off, extra_system_prompt is None."""
        # Voice mode not set (default off)

        mock_data = {
            "result": "Normal response.",
            "session_id": "sess-123",
            "written_files": [],
        }

        mock_editor = AsyncMock()
        mock_editor.text = "Normal response."
        mock_editor.finalize = AsyncMock(return_value=[])
        mock_editor.sent_messages = []

        update = MagicMock()
        update.effective_chat.id = 12345
        update.effective_user.id = 12345
        update.message.text = "Hello"
        update.message.reply_text = AsyncMock()
        update.message.reply_to_message = None
        update.effective_chat.send_action = AsyncMock()

        context = MagicMock()

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            with patch("channels.telegram.commands.get_session", return_value={"session_id": "sess-123", "model": "sonnet", "message_count": 1, "name": "default"}), \
                 patch("channels.telegram.commands.call_streaming", new_callable=AsyncMock, return_value=mock_data) as mock_call, \
                 patch("channels.telegram.commands.StreamingEditor", return_value=mock_editor), \
                 patch("channels.telegram.commands.upsert_session"), \
                 patch("channels.telegram.commands.maybe_generate_summary", new_callable=AsyncMock), \
                 patch("channels.telegram.commands.get_lock", return_value=asyncio.Lock()), \
                 patch("channels.telegram.commands._keep_typing", new_callable=AsyncMock), \
                 patch("channels.telegram.commands.get_contextual_keyboard", return_value=None), \
                 patch("channels.telegram.commands.scan_and_send_outputs", new_callable=AsyncMock):

                from channels.telegram.commands import handle_message
                await handle_message(update, context)

                # Verify extra_system_prompt is None
                call_kwargs = mock_call.call_args
                assert call_kwargs[1].get("extra_system_prompt") is None
