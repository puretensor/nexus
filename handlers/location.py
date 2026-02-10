"""Location handler — reverse geocodes shared locations and passes context to Claude."""

import asyncio
import json
import urllib.request
import urllib.error

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from db import authorized, get_session, upsert_session, get_lock
from config import log
from handlers.summaries import maybe_generate_summary
from channels.telegram.streaming import StreamingEditor
from engine import call_streaming, split_message
from handlers.file_output import scan_and_send_outputs

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json"
USER_AGENT = "HAL1000-TelegramBot/1.0"


def _reverse_geocode(lat: float, lon: float) -> str | None:
    """Reverse geocode lat/lon via Nominatim. Returns display_name or None on failure."""
    url = NOMINATIM_URL.format(lat=lat, lon=lon)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("display_name")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, KeyError) as e:
        log.warning("Nominatim reverse geocode failed: %s", e)
        return None


@authorized
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle location messages — reverse geocode and pass to Claude."""
    from channels.telegram.commands import _build_reply_context, _keep_typing

    chat_id = update.effective_chat.id

    if not update.message.location:
        return

    lock = get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Still processing previous message — please wait.")
        return

    async with lock:
        await update.effective_chat.send_action(ChatAction.TYPING)
        typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

        try:
            lat = update.message.location.latitude
            lon = update.message.location.longitude

            # Reverse geocode (run in thread to avoid blocking the event loop)
            loop = asyncio.get_event_loop()
            display_name = await loop.run_in_executor(None, _reverse_geocode, lat, lon)

            if display_name:
                location_context = f"User is at: {display_name} ({lat}, {lon}). They shared their location."
            else:
                location_context = f"User is at coordinates ({lat}, {lon}). They shared their location."

            # Build prompt
            caption = update.message.caption or ""
            if caption.strip():
                prompt = f"{location_context}\n\n{caption}"
            else:
                prompt = (
                    f"{location_context}\n\n"
                    f"User shared their location. Let them know you're aware of where they are "
                    f"and ask if they need anything location-related (weather, directions, "
                    f"nearby places, etc.)"
                )

            # Prepend reply-to context
            reply_ctx = _build_reply_context(update.message)
            prompt = reply_ctx + prompt

            # Get session info
            session = get_session(chat_id)
            model = session["model"] if session else "sonnet"
            session_id = session["session_id"] if session else None
            msg_count = session["message_count"] if session else 0

            # Acknowledgment
            model_label = "Opus 4.6" if model == "opus" else "Sonnet"
            if session_id:
                ack = f"Processing location... ({model_label}, msg #{msg_count + 1})"
            else:
                ack = f"Starting new session with location... ({model_label})"
            await update.message.reply_text(ack)

            # Stream Claude output
            editor = StreamingEditor(update.effective_chat)
            data = await call_streaming(
                prompt, session_id, model, streaming_editor=editor
            )

            result_text = data.get("result", "")
            new_session_id = data.get("session_id", session_id)

            if not result_text:
                result_text = "(Empty response from Claude)"

            upsert_session(chat_id, new_session_id, model, msg_count + 1)
            await maybe_generate_summary(chat_id)

            if editor.text:
                await editor.finalize()
            else:
                for chunk in split_message(result_text):
                    try:
                        await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        await update.message.reply_text(chunk)

            # Send any files Claude wrote back to Telegram
            written_files = data.get("written_files", [])
            if written_files:
                await scan_and_send_outputs(update.effective_chat, written_files)

        except TimeoutError as e:
            log.error("Timeout: %s", e)
            await update.message.reply_text("Timed out processing location. Try /new to reset.")
        except RuntimeError as e:
            log.error("Location/Claude error: %s", e)
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Unexpected error in location handler")
            await update.message.reply_text(f"Unexpected error: {e}")
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
