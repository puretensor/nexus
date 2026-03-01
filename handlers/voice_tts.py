"""Voice TTS — convert Claude responses to voice notes using local HAL 9000 voice clone."""

import asyncio
import logging
import re
import tempfile
from pathlib import Path

import os

import aiohttp

log = logging.getLogger("nexus")

# Per-chat voice mode state (in-memory, resets on restart)
_voice_mode: dict[int, bool] = {}

# TTS API — configurable, defaults to TC Tailscale IP
TTS_API_URL = os.environ.get("TTS_URL", "")


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


async def text_to_voice_note(text: str) -> bytes | None:
    """Convert text to OGG voice note bytes using HAL 9000 TTS.

    Returns OGG bytes or None on failure. Skips gracefully when TC is offline.
    """
    from health_probes import is_tc_tts_online

    if not is_tc_tts_online():
        log.info("TC TTS offline — skipping voice note")
        return None

    cleaned = _clean_for_tts(text)
    if not cleaned:
        return None

    # Truncate very long text
    if len(cleaned) > 3000:
        cleaned = cleaned[:3000] + "..."

    wav_path = None
    ogg_path = None
    try:
        # Request WAV from local HAL voice TTS daemon
        async with aiohttp.ClientSession() as session:
            async with session.post(
                TTS_API_URL,
                json={"text": cleaned},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("TTS API returned %d: %s", resp.status, body[:200])
                    return None
                wav_data = await resp.read()

        # Write WAV to temp file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
            tmp.write(wav_data)

        # Convert WAV to OGG Opus (Telegram requires OGG for voice notes)
        ogg_path = wav_path.replace(".wav", ".ogg")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", wav_path, "-c:a", "libopus", "-b:a", "64k",
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
    except aiohttp.ClientError as e:
        log.error("TTS API connection failed: %s", e)
        return None
    except Exception as e:
        log.error("TTS conversion failed: %s", e)
        return None
    finally:
        if wav_path:
            try:
                Path(wav_path).unlink(missing_ok=True)
            except Exception:
                pass
        if ogg_path:
            try:
                Path(ogg_path).unlink(missing_ok=True)
            except Exception:
                pass
