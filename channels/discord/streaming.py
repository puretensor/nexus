"""Discord-specific StreamingEditor — manages a live-edited Discord message
as the LLM streams output.

Same pattern as channels/telegram/streaming.py but adapted for Discord's
rate limits (5 edits per 10s) and message size (2000 chars).
"""

import logging
import time

log = logging.getLogger("nexus")


class DiscordStreamingEditor:
    """Manages a single Discord message that gets edited as text streams in.

    Handles rate limiting, message splitting at 1900 chars, and markdown
    rendering on finalize.
    """

    EDIT_INTERVAL = 2.0   # seconds between edits (Discord: ~5 edits/10s)
    MSG_LIMIT = 1900      # leave room below Discord's 2000

    def __init__(self, channel):
        self.channel = channel        # discord.TextChannel or similar
        self.text = ""
        self.message = None           # current discord.Message object
        self.sent_messages = []       # all messages we've sent
        self.last_edit_time = 0.0
        self.last_edit_text = ""
        self._tool_status = ""
        self._progress_msgs = []

    async def add_tool_status(self, status: str):
        """Show tool-use progress. Before any text arrives, send as separate
        italic messages. Once text is streaming, ignore tool events."""
        if self.text:
            return
        if status == self._tool_status:
            return
        now = time.monotonic()
        if now - self.last_edit_time < 2.5:
            return
        self._tool_status = status
        self.last_edit_time = now
        try:
            msg = await self.channel.send(f"*{status}*")
            self._progress_msgs.append(msg)
        except Exception:
            pass

    async def add_text(self, delta: str):
        """Append a text delta and update the Discord message."""
        self.text += delta

        # Would this message exceed the limit? Finalize and start a new one.
        if self.message and len(self.text) > self.MSG_LIMIT:
            overflow_point = self.MSG_LIMIT
            idx = self.text.rfind("\n", 0, overflow_point)
            if idx == -1:
                idx = overflow_point
            finalize_text = self.text[:idx]
            remainder = self.text[idx:].lstrip("\n")

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
        """Send or edit the streaming message."""
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

                self.message = await self.channel.send(text)
            else:
                await self.message.edit(content=text)
            self.last_edit_text = text
            self.last_edit_time = time.monotonic()
        except Exception as e:
            log.debug("Discord stream edit failed: %s", e)

    async def finalize(self) -> list:
        """Final edit. Returns list of all sent messages."""
        # Clean up any remaining progress messages
        for pm in self._progress_msgs:
            try:
                await pm.delete()
            except Exception:
                pass
        self._progress_msgs.clear()

        if not self.text.strip():
            return self.sent_messages

        # Final edit — Discord auto-renders markdown
        if self.message:
            try:
                await self.message.edit(content=self.text)
            except Exception:
                pass
            self.sent_messages.append(self.message)
        else:
            try:
                msg = await self.channel.send(self.text)
                self.sent_messages.append(msg)
            except Exception:
                pass

        return self.sent_messages
