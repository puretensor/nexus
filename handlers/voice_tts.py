"""Voice TTS — convert Claude responses to voice notes using edge-tts."""

import asyncio
import logging
import re
import tempfile
from pathlib import Path

log = logging.getLogger("nexus")

# Per-chat voice mode state (in-memory, resets on restart)
_voice_mode: dict[int, bool] = {}

# Default TTS voice
DEFAULT_VOICE = "en-GB-SoniaNeural"


def is_voice_mode(chat_id: int) -> bool:
    """Check if voice mode is enabled for a chat."""
    return _voice_mode.get(chat_id, False)


def set_voice_mode(chat_id: int, enabled: bool):
    """Enable or disable voice mode for a chat."""
    _voice_mode[chat_id] = enabled


def get_voice_system_prompt_addition() -> str:
    """Return system prompt addition for voice mode — keeps responses concise."""
    return (
        "\n\nIMPORTANT: Voice mode is ON. Keep your response under 100 words. "
        "Be concise and conversational. No markdown formatting, no code blocks, "
        "no bullet points. Speak naturally as if talking to someone."
    )


def _clean_for_tts(text: str) -> str:
    """Clean markdown/code formatting from text for TTS."""
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove inline code
    text = re.sub(r'`[^`]+`', '', text)
    # Remove markdown bold/italic
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', text)
    # Remove markdown headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove markdown links [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Remove URLs
    text = re.sub(r'https?://\S+', '', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


async def text_to_voice_note(text: str, voice: str = DEFAULT_VOICE) -> bytes | None:
    """Convert text to OGG voice note bytes using edge-tts.

    Returns OGG bytes or None on failure.
    """
    try:
        import edge_tts
    except ImportError:
        log.error("edge-tts not installed. Run: pip install edge-tts")
        return None

    cleaned = _clean_for_tts(text)
    if not cleaned:
        return None

    # Truncate very long text (edge-tts has limits)
    if len(cleaned) > 3000:
        cleaned = cleaned[:3000] + "..."

    tmp_path = None
    ogg_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name

        communicate = edge_tts.Communicate(cleaned, voice)
        await communicate.save(tmp_path)

        # Convert MP3 to OGG (Telegram requires OGG for voice notes)
        ogg_path = tmp_path.replace(".mp3", ".ogg")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", tmp_path, "-c:a", "libopus", "-b:a", "64k",
            "-y", ogg_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)

        if proc.returncode != 0:
            log.warning("ffmpeg conversion failed, returncode=%d", proc.returncode)
            return None

        ogg_data = Path(ogg_path).read_bytes()
        return ogg_data

    except asyncio.TimeoutError:
        log.error("TTS conversion timed out")
        return None
    except Exception as e:
        log.error("TTS conversion failed: %s", e)
        return None
    finally:
        # Ensure cleanup
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
        if ogg_path:
            try:
                Path(ogg_path).unlink(missing_ok=True)
            except Exception:
                pass
