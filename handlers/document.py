"""Document/file sharing handler.

Accepts files sent as Telegram documents, categorizes by type,
and forwards content to Claude via the streaming infrastructure.
"""

import asyncio
import io
import os

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from db import authorized, get_session, upsert_session, get_lock
from config import TIMEOUT, log
from handlers.summaries import maybe_generate_summary
from channels.telegram.streaming import StreamingEditor
from engine import call_streaming, split_message
from handlers.file_output import scan_and_send_outputs

# Directory for temporary document storage
DOC_DIR = "/tmp/pureclaw_docs"

# Max file sizes in bytes
WARN_SIZE = 10 * 1024 * 1024   # 10 MB
MAX_SIZE = 25 * 1024 * 1024    # 25 MB

# Max content length to inline in prompt (100 KB)
MAX_INLINE_CHARS = 100_000

# Extensions treated as text (read content inline)
TEXT_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".csv", ".json", ".md", ".txt", ".rst",
    ".yaml", ".yml", ".toml", ".xml",
    ".html", ".htm", ".css", ".scss",
    ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".log",
    ".conf", ".cfg", ".ini", ".env",
    ".go", ".rs", ".c", ".cpp", ".h", ".hpp",
    ".java", ".kt", ".rb", ".php", ".pl",
    ".r", ".R", ".lua", ".swift", ".m",
    ".dockerfile", ".makefile",
    ".properties", ".gradle",
})

# Filenames (no extension) treated as text
TEXT_FILENAMES = frozenset({
    "Makefile", "Dockerfile", "Vagrantfile",
    "Gemfile", "Rakefile", "Procfile",
    "LICENSE", "README", "CHANGELOG",
    ".env", ".gitignore", ".dockerignore",
})


def _is_text_file(filename: str, mime_type: str | None) -> bool:
    """Determine if a file should be read as text and inlined."""
    if mime_type and mime_type.startswith("text/"):
        return True
    name = os.path.basename(filename)
    if name in TEXT_FILENAMES:
        return True
    _, ext = os.path.splitext(name)
    return ext.lower() in TEXT_EXTENSIONS


def _is_pdf(filename: str, mime_type: str | None) -> bool:
    _, ext = os.path.splitext(filename)
    return ext.lower() == ".pdf" or (mime_type or "") == "application/pdf"


def _is_image(mime_type: str | None) -> bool:
    return bool(mime_type and mime_type.startswith("image/"))


def _build_document_prompt(
    filename: str,
    mime_type: str | None,
    file_path: str,
    file_bytes: bytes,
    caption: str | None,
) -> tuple[str, bool]:
    """Build the prompt for Claude based on file type.

    Returns (prompt, should_cleanup) — should_cleanup is False when
    the file must remain on disk for Claude to read it.
    """
    user_text = caption or f"The user sent a file: {filename}. Analyze its contents."

    if _is_text_file(filename, mime_type):
        try:
            content = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content = file_bytes.decode("utf-8", errors="replace")

        if len(content) > MAX_INLINE_CHARS:
            content = content[:MAX_INLINE_CHARS] + "\n\n[... truncated — file too large to inline ...]"

        prompt = (
            f"The user sent a file: {filename}\n\n"
            f"File contents:\n```\n{content}\n```\n\n{user_text}"
        )
        return prompt, True  # can cleanup, content is inlined

    if _is_pdf(filename, mime_type):
        prompt = (
            f"The user sent a PDF file saved at {file_path}. "
            f"Please read and analyze it using your Read tool.\n\n{user_text}"
        )
        return prompt, False

    if _is_image(mime_type):
        prompt = (
            f"The user sent an image file saved at {file_path}. "
            f"Please analyze it using your Read tool.\n\n{user_text}"
        )
        return prompt, False

    # Other binary files
    type_hint = f" (type: {mime_type})" if mime_type else ""
    prompt = (
        f"The user sent a file: {filename}{type_hint} saved at {file_path}. "
        f"Analyze if possible using your Read tool.\n\n{user_text}"
    )
    return prompt, False


@authorized
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle documents/files sent to the bot."""
    chat_id = update.effective_chat.id
    document = update.message.document

    if document is None:
        return

    filename = document.file_name or "unknown_file"
    file_size = document.file_size or 0
    mime_type = document.mime_type

    # Size checks
    if file_size > MAX_SIZE:
        await update.message.reply_text(
            f"File too large ({file_size / (1024*1024):.1f} MB). Maximum is 25 MB."
        )
        return

    lock = get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Still processing previous message — please wait.")
        return

    async with lock:
        from channels.telegram.commands import _build_reply_context, _keep_typing

        await update.effective_chat.send_action(ChatAction.TYPING)
        typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

        file_path = None
        should_cleanup = True

        try:
            # Warn about large files
            if file_size > WARN_SIZE:
                await update.message.reply_text(
                    f"Large file ({file_size / (1024*1024):.1f} MB) — this may take a moment."
                )

            # Download file
            tg_file = await context.bot.get_file(document.file_id)
            buf = io.BytesIO()
            await tg_file.download_to_memory(buf)
            file_bytes = buf.getvalue()
            log.info("Downloaded document: %s (%d bytes, %s)", filename, len(file_bytes), mime_type)

            # Save to disk
            os.makedirs(DOC_DIR, exist_ok=True)
            file_path = os.path.join(DOC_DIR, filename)
            with open(file_path, "wb") as f:
                f.write(file_bytes)

            # Build prompt
            caption = update.message.caption
            prompt_text, should_cleanup = _build_document_prompt(
                filename, mime_type, file_path, file_bytes, caption
            )

            # Session info
            session = get_session(chat_id)
            model = session["model"] if session else "sonnet"
            session_id = session["session_id"] if session else None
            msg_count = session["message_count"] if session else 0

            # Reply-to context
            reply_ctx = _build_reply_context(update.message)
            prompt = reply_ctx + prompt_text

            # Acknowledgment
            model_label = "Opus 4.6" if model == "opus" else "Sonnet"
            if session_id:
                ack = f"Processing {filename}... ({model_label}, msg #{msg_count + 1})"
            else:
                ack = f"Processing {filename}... ({model_label}, new session)"
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
            await update.message.reply_text(f"Timed out after {TIMEOUT}s. Try a simpler query or /new to reset.")
        except RuntimeError as e:
            log.error("Claude error: %s", e)
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Unexpected error in document handler")
            await update.message.reply_text(f"Unexpected error: {e}")
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
            # Cleanup temp file if content was inlined
            if should_cleanup and file_path and os.path.exists(file_path):
                try:
                    os.unlink(file_path)
                except OSError:
                    pass
