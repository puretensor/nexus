"""Photo handler — forwards images sent via Telegram to Claude for analysis."""

import asyncio
import io
import uuid
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from db import authorized, get_session, upsert_session, get_lock
from config import log
from handlers.summaries import maybe_generate_summary
from channels.telegram.streaming import StreamingEditor
from engine import call_streaming, split_message
from handlers.file_output import scan_and_send_outputs

IMAGE_DIR = Path("/tmp/pureclaw_images")


@authorized
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos sent to the bot — download and pass to Claude."""
    from channels.telegram.commands import _build_reply_context, _keep_typing

    chat_id = update.effective_chat.id

    if not update.message.photo:
        return

    lock = get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Still processing previous message — please wait.")
        return

    image_path = None
    async with lock:
        await update.effective_chat.send_action(ChatAction.TYPING)
        typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

        try:
            # Download largest photo size
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            buf = io.BytesIO()
            await file.download_to_memory(buf)
            image_bytes = buf.getvalue()
            log.info("Downloaded photo: %d bytes", len(image_bytes))

            # Save to temp file
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            image_path = IMAGE_DIR / f"{uuid.uuid4().hex}.jpg"
            image_path.write_bytes(image_bytes)
            log.info("Saved image to %s", image_path)

            # Build prompt with image path
            caption = update.message.caption or ""
            if caption.strip():
                prompt = (
                    f"The user sent an image located at {image_path}. "
                    f"Please read/view this image file first, then respond to their message: "
                    f"{caption}"
                )
            else:
                prompt = (
                    f"The user sent an image located at {image_path}. "
                    f"Please read/view this image file and describe what you see, "
                    f"then ask if they need anything."
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
            from engine import get_model_display
            model_label = get_model_display(model)
            if session_id:
                ack = f"{model_label} processing image... (msg #{msg_count + 1})"
            else:
                ack = f"{model_label} processing image... (new session)"
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
            await update.message.reply_text("Timed out processing image. Try /new to reset.")
        except RuntimeError as e:
            log.error("Photo/Claude error: %s", e)
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Unexpected error in photo handler")
            await update.message.reply_text(f"Unexpected error: {e}")
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
            # Clean up temp file
            if image_path and image_path.exists():
                try:
                    image_path.unlink()
                    log.info("Cleaned up temp image: %s", image_path)
                except OSError:
                    log.warning("Failed to clean up temp image: %s", image_path)
