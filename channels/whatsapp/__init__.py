"""WhatsApp channel — receives messages from wa-bridge webhooks, routes through engine.

Lifecycle:
  1. wa-bridge (Node.js) connects to WhatsApp Web via baileys
  2. On incoming message, bridge POSTs normalized JSON to Nexus /wa/incoming
  3. WebhookHandler routes to WhatsAppChannel.handle_incoming()
  4. Router resolves JID → mode (SILENT/NOTIFY/SUGGEST/AUTONOMOUS)
  5. For SUGGEST/AUTONOMOUS: calls engine.call_streaming() for response
  6. Replies sent back through wa-bridge HTTP API

Chat ID scheme: WA_CHAT_ID_OFFSET (800B) + CRC32 of normalized JID.
"""

import asyncio
import logging
import time
import zlib

from channels.base import Channel
from channels.whatsapp.bridge import WABridge
from channels.whatsapp.router import MessageRouter, Mode
from config import AUTHORIZED_USER_ID, AGENT_NAME
from db import get_session, upsert_session, get_lock

log = logging.getLogger("nexus")

# Offset WhatsApp chat IDs to avoid collision with Telegram/Email
WA_CHAT_ID_OFFSET = 800_000_000_000

# Rate limit: minimum seconds between autonomous replies per JID
AUTONOMOUS_RATE_LIMIT_SECS = 5

# System prompt for WhatsApp replies — the agent IS the user
WA_SYSTEM_PROMPT_TEMPLATE = (
    "IMPORTANT: You are replying to a WhatsApp message AS {user_name}.\n"
    "You ARE {user_name} — you are NOT an AI assistant in this context.\n\n"
    "Contact: {contact_name} ({contact_jid})\n"
    "{group_context}"
    "\nCRITICAL RULES:\n"
    "- Your text output IS the WhatsApp reply. It will be sent directly.\n"
    "- Write in {user_name}'s natural style — casual, concise, human.\n"
    "- Match the language and formality of the incoming message.\n"
    "- Keep replies short (1-3 sentences typically) unless the topic requires more.\n"
    "- No markdown formatting. Plain text only.\n"
    "- Never mention being an AI, assistant, or language model.\n"
    "- Use emoji sparingly and naturally, only if {user_name} would.\n"
)


def _wa_chat_id(jid: str) -> int:
    """Deterministic chat ID for a WhatsApp JID."""
    normalized = jid.lower().strip()
    return WA_CHAT_ID_OFFSET + abs(zlib.crc32(normalized.encode())) % (2**31)


class WhatsAppChannel(Channel):
    """WhatsApp channel — bridges wa-bridge webhooks to Nexus engine."""

    def __init__(self, instances: list[dict] | None = None):
        """Initialize with bridge instances.

        instances: list of {"name": "wa-1", "url": "http://...:3100"}
        """
        self._bridges: dict[str, WABridge] = {}
        self._router = MessageRouter()
        self._telegram_bot = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task = None
        # Rate limiting: JID → last autonomous reply timestamp
        self._last_reply: dict[str, float] = {}

        for inst in (instances or []):
            name = inst["name"]
            self._bridges[name] = WABridge(name, inst["url"])

    async def start(self):
        """Start the message processing worker."""
        self._worker_task = asyncio.create_task(self._process_loop())
        bridge_names = ", ".join(self._bridges.keys()) or "(none)"
        log.info("WhatsApp channel started — bridges: %s, default mode: %s",
                 bridge_names, self._router.default_mode.value)

    async def stop(self):
        """Stop the worker and close bridge sessions."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        for bridge in self._bridges.values():
            await bridge.close()
        log.info("WhatsApp channel stopped")

    def set_telegram_bot(self, bot):
        """Set the Telegram bot reference for notifications."""
        self._telegram_bot = bot

    async def handle_incoming(self, payload: dict):
        """Called by WebhookHandler when /wa/incoming receives a POST.

        This is called from a sync HTTP handler thread, so we enqueue
        the payload for async processing.
        """
        await self._queue.put(payload)

    def enqueue_sync(self, payload: dict, loop: asyncio.AbstractEventLoop):
        """Thread-safe enqueue for use from sync HTTP handler."""
        asyncio.run_coroutine_threadsafe(self._queue.put(payload), loop)

    async def _process_loop(self):
        """Main worker — dequeues and processes incoming messages."""
        while True:
            try:
                payload = await self._queue.get()
                await self._process_message(payload)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("WhatsApp message processing error: %s", e)

    async def _process_message(self, payload: dict):
        """Process a single incoming WhatsApp message through the routing pipeline."""
        from db import log_wa_message

        instance = payload.get("instance", "unknown")
        jid = payload.get("jid", "")
        sender_jid = payload.get("sender_jid", jid)
        push_name = payload.get("push_name", "")
        msg_type = payload.get("message_type", "text")
        body = payload.get("body", "")
        is_group = payload.get("is_group", False)
        timestamp = payload.get("timestamp", 0)
        message_id = payload.get("message_id", "")

        # Log to wa_messages table
        log_wa_message(
            instance=instance,
            jid=jid,
            sender_jid=sender_jid,
            push_name=push_name,
            message_type=msg_type,
            body=body[:5000],
            is_group=is_group,
            message_id=message_id,
        )

        # Only process text messages for now
        if msg_type not in ("text",):
            log.debug("WA: skipping non-text message type: %s", msg_type)
            # Still notify for media messages
            mode = self._router.resolve(jid, is_group)
            if mode == Mode.NOTIFY:
                await self._notify_telegram(push_name, body, jid, is_group, instance)
            return

        # Resolve routing mode
        mode = self._router.resolve(jid, is_group)
        display_name = push_name or self._router.get_label(jid, is_group) or jid

        if mode == Mode.SILENT:
            log.info("WA [SILENT] %s: %s", display_name, body[:60])
            return

        if mode == Mode.NOTIFY:
            await self._notify_telegram(display_name, body, jid, is_group, instance)
            return

        if mode == Mode.SUGGEST:
            await self._suggest_reply(
                payload, display_name, instance,
            )
            return

        if mode == Mode.AUTONOMOUS:
            await self._autonomous_reply(
                payload, display_name, instance,
            )
            return

    async def _notify_telegram(self, display_name: str, body: str, jid: str,
                                is_group: bool, instance: str):
        """Forward a WhatsApp message preview to Telegram."""
        if not self._telegram_bot:
            return

        prefix = "[WA"
        if is_group:
            group_label = self._router.get_label(jid, True) or jid
            prefix += f"/{group_label}"
        prefix += f"] {display_name}"

        preview = body[:300]
        text = f"{prefix}:\n{preview}"

        try:
            await self._telegram_bot.send_message(
                chat_id=int(AUTHORIZED_USER_ID),
                text=text,
            )
        except Exception as e:
            log.warning("WA: failed to send Telegram notification: %s", e)

    async def _suggest_reply(self, payload: dict, display_name: str,
                              instance: str):
        """Generate a draft reply and send to Telegram with approve/reject buttons."""
        from engine import call_streaming
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        body = payload.get("body", "")
        jid = payload.get("jid", "")
        is_group = payload.get("is_group", False)

        chat_id = _wa_chat_id(jid)
        reply_text = await self._generate_reply(payload, chat_id, display_name)

        if not reply_text:
            await self._notify_telegram(display_name, body, jid, is_group, instance)
            return

        if not self._telegram_bot:
            return

        # Send draft to Telegram with approve/reject buttons
        # Encode instance and JID in callback data for routing
        cb_data_approve = f"wa:approve:{instance}:{jid}"
        cb_data_reject = f"wa:reject:{instance}:{jid}"

        # Truncate callback data if needed (Telegram limit: 64 bytes)
        if len(cb_data_approve.encode()) > 64:
            # Use a shorter JID reference
            jid_short = jid[:20]
            cb_data_approve = f"wa:approve:{instance}:{jid_short}"
            cb_data_reject = f"wa:reject:{instance}:{jid_short}"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve", callback_data=cb_data_approve),
                InlineKeyboardButton("Reject", callback_data=cb_data_reject),
            ]
        ])

        preview = body[:200]
        draft_preview = reply_text[:500]

        text = (
            f"[WA DRAFT] {display_name}:\n"
            f"{preview}\n\n"
            f"Draft reply:\n{draft_preview}"
        )

        try:
            msg = await self._telegram_bot.send_message(
                chat_id=int(AUTHORIZED_USER_ID),
                text=text,
                reply_markup=keyboard,
            )
            # Store the pending draft for approval callback
            from db import store_wa_draft
            store_wa_draft(
                telegram_msg_id=msg.message_id,
                instance=instance,
                jid=jid,
                draft_body=reply_text,
            )
        except Exception as e:
            log.warning("WA: failed to send suggest notification: %s", e)

    async def _autonomous_reply(self, payload: dict, display_name: str,
                                 instance: str):
        """Generate and send a reply automatically (rate-limited)."""
        jid = payload.get("jid", "")
        is_group = payload.get("is_group", False)
        body = payload.get("body", "")

        # Rate limit check
        now = time.time()
        last = self._last_reply.get(jid, 0)
        if now - last < AUTONOMOUS_RATE_LIMIT_SECS:
            log.info("WA: rate-limited autonomous reply to %s", display_name)
            await self._notify_telegram(display_name, body, jid, is_group, instance)
            return

        chat_id = _wa_chat_id(jid)
        reply_text = await self._generate_reply(payload, chat_id, display_name)

        if not reply_text:
            await self._notify_telegram(display_name, body, jid, is_group, instance)
            return

        # Send via bridge
        bridge = self._bridges.get(instance)
        if not bridge:
            log.warning("WA: no bridge for instance %s", instance)
            return

        try:
            result = await bridge.send_text(jid, reply_text)
            self._last_reply[jid] = time.time()
            log.info("WA [AUTO] → %s: %s", display_name, reply_text[:60])

            # Notify on Telegram that an auto-reply was sent
            if self._telegram_bot:
                await self._telegram_bot.send_message(
                    chat_id=int(AUTHORIZED_USER_ID),
                    text=f"[WA SENT] → {display_name}:\n{reply_text[:300]}",
                )
        except Exception as e:
            log.warning("WA: failed to send autonomous reply: %s", e)

    async def _generate_reply(self, payload: dict, chat_id: int,
                               display_name: str) -> str | None:
        """Call the engine to generate a reply. Returns reply text or None."""
        from engine import call_streaming

        body = payload.get("body", "")
        jid = payload.get("jid", "")
        is_group = payload.get("is_group", False)
        push_name = payload.get("push_name", "")

        lock = get_lock(chat_id)

        async with lock:
            session = get_session(chat_id)
            session_id = session["session_id"] if session else None
            model = session["model"] if session else "sonnet"
            msg_count = session["message_count"] if session else 0

            user_message = f"[WhatsApp from {display_name}]\n{body}"

            group_ctx = ""
            if is_group:
                group_label = self._router.get_label(jid, True) or jid
                group_ctx = f"Group: {group_label}\n"

            extra_sp = WA_SYSTEM_PROMPT_TEMPLATE.format(
                user_name="Heimir",
                contact_name=push_name or display_name,
                contact_jid=jid,
                group_context=group_ctx,
            )

            try:
                data = await call_streaming(
                    user_message, session_id, model,
                    streaming_editor=None,
                    extra_system_prompt=extra_sp,
                )
                reply_body = data.get("result", "").strip()
                new_session_id = data.get("session_id", session_id)
            except Exception as e:
                log.warning("WA: engine call failed for %s: %s", display_name, e)
                return None

            if not reply_body:
                log.warning("WA: empty reply for %s", display_name)
                return None

            # Persist session
            upsert_session(chat_id, new_session_id, model, msg_count + 1)

            return reply_body

    async def send_approved_draft(self, instance: str, jid: str, text: str):
        """Send a previously approved draft via the correct bridge."""
        bridge = self._bridges.get(instance)
        if not bridge:
            log.warning("WA: no bridge for instance %s", instance)
            return False

        try:
            await bridge.send_text(jid, text)
            log.info("WA [APPROVED] → %s: %s", jid, text[:60])
            return True
        except Exception as e:
            log.warning("WA: failed to send approved draft: %s", e)
            return False

    def get_router(self) -> MessageRouter:
        """Return the router for external config management."""
        return self._router

    def get_bridges(self) -> dict[str, WABridge]:
        """Return bridge instances for status checks."""
        return self._bridges
