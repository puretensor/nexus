"""Telegram-specific StreamingEditor — manages a live-edited Telegram message
as Claude streams output, plus the legacy make_progress_callback factory.

Ported from ~/claude-telegram/streaming.py (StreamingEditor + make_progress_callback only).
The stream reader and Claude CLI caller live in engine.py.
"""

import logging
import time

from telegram.constants import ParseMode

log = logging.getLogger("nexus")


class StreamingEditor:
    """Manages a single Telegram message that gets edited as text streams in.

    Handles rate limiting (Telegram allows ~30 edits/min), message splitting
    at 4000 chars, and plain-text-during-streaming / Markdown-on-final.
    """

    EDIT_INTERVAL = 1.5  # seconds between edits
    MSG_LIMIT = 4000     # leave room below Telegram's 4096

    def __init__(self, chat):
        self.chat = chat
        self.text = ""
        self.message = None          # current Telegram message object
        self.sent_messages = []       # all messages we've sent (for final cleanup)
        self.last_edit_time = 0.0
        self.last_edit_text = ""
        self._tool_status = ""       # current tool-use status line
        self._progress_msgs = []     # separate progress messages sent before first text

    async def add_tool_status(self, status: str):
        """Show tool-use progress. Before any text arrives, send as separate
        italic messages. Once text is streaming, ignore tool events (they're
        redundant with the streaming output)."""
        if self.text:
            return  # text is streaming, skip tool status
        if status == self._tool_status:
            return
        now = time.monotonic()
        if now - self.last_edit_time < 2.0:
            return
        self._tool_status = status
        self.last_edit_time = now
        try:
            msg = await self.chat.send_message(f"_{status}_", parse_mode=ParseMode.MARKDOWN)
            self._progress_msgs.append(msg)
        except Exception:
            try:
                msg = await self.chat.send_message(status)
                self._progress_msgs.append(msg)
            except Exception:
                pass

    async def add_text(self, delta: str):
        """Append a text delta and update the Telegram message."""
        self.text += delta

        # Would this message exceed the limit? Finalize and start a new one.
        if self.message and len(self.text) > self.MSG_LIMIT:
            overflow_point = self.MSG_LIMIT
            # Try to split at a newline
            idx = self.text.rfind("\n", 0, overflow_point)
            if idx == -1:
                idx = overflow_point
            finalize_text = self.text[:idx]
            remainder = self.text[idx:].lstrip("\n")

            # Finalize current message
            await self._edit(finalize_text, force=True)
            self.sent_messages.append(self.message)
            self.message = None
            self.text = remainder
            self.last_edit_text = ""

        # Rate-limit edits
        now = time.monotonic()
        if now - self.last_edit_time < self.EDIT_INTERVAL:
            return

        await self._edit(self.text)

    async def _edit(self, text: str, force: bool = False):
        """Send or edit the streaming message. Uses plain text during streaming."""
        if not text.strip():
            return
        if text == self.last_edit_text and not force:
            return

        try:
            if self.message is None:
                # Delete progress messages now that real text is arriving
                for pm in self._progress_msgs:
                    try:
                        await pm.delete()
                    except Exception:
                        pass
                self._progress_msgs.clear()

                self.message = await self.chat.send_message(text)
            else:
                await self.message.edit_text(text)
            self.last_edit_text = text
            self.last_edit_time = time.monotonic()
        except Exception as e:
            log.debug("Stream edit failed: %s", e)

    async def finalize(self) -> list:
        """Final edit with Markdown formatting. Returns list of all sent messages."""
        # Clean up any remaining progress messages
        for pm in self._progress_msgs:
            try:
                await pm.delete()
            except Exception:
                pass
        self._progress_msgs.clear()

        if not self.text.strip():
            return self.sent_messages

        # Final edit — try Markdown, fall back to plain text
        if self.message:
            try:
                await self.message.edit_text(self.text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                try:
                    await self.message.edit_text(self.text)
                except Exception:
                    pass
            self.sent_messages.append(self.message)
        else:
            # Never sent a message (e.g. very short response) — send now
            try:
                msg = await self.chat.send_message(self.text, parse_mode=ParseMode.MARKDOWN)
                self.sent_messages.append(msg)
            except Exception:
                try:
                    msg = await self.chat.send_message(self.text)
                    self.sent_messages.append(msg)
                except Exception:
                    pass

        return self.sent_messages


# ---------------------------------------------------------------------------
# Progress callback (legacy, kept for backward compat)
# ---------------------------------------------------------------------------


def make_progress_callback(chat, min_interval: float = 3.0):
    """Factory returning an async callback that sends italicized progress messages.

    Built-in dedup (no repeat identical messages) and rate limiting.
    """
    last_text = None
    last_time = 0.0

    async def callback(status_text: str):
        nonlocal last_text, last_time
        now = time.monotonic()
        if status_text == last_text:
            return
        if now - last_time < min_interval:
            return
        last_text = status_text
        last_time = now
        try:
            await chat.send_message(f"_{status_text}_", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            try:
                await chat.send_message(status_text)
            except Exception:
                pass

    return callback
