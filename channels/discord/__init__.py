"""Discord channel — wraps discord.py Client for persistent gateway connection."""

import asyncio

import discord

from channels.base import Channel
from config import DISCORD_BOT_TOKEN, DISCORD_AUTHORIZED_USER_ID, log


class DiscordChannel(Channel):
    def __init__(self):
        self.client = None
        self._task = None
        self._message_content_intent = True

    def _build_client(self, message_content: bool = True) -> discord.Client:
        """Build a Discord client with the appropriate intents."""
        intents = discord.Intents.default()
        if message_content:
            intents.message_content = True
        return discord.Client(intents=intents)

    async def start(self):
        from channels.discord.handlers import handle_command, handle_message, _authorized

        self.client = self._build_client(message_content=self._message_content_intent)
        self._register_events(_authorized, handle_command, handle_message)

        # Run client in a background task (non-blocking)
        self._task = asyncio.create_task(self._run_client())
        log.info("Discord channel starting...")

    def _register_events(self, _authorized, handle_command, handle_message):
        """Register all event handlers on the current client."""
        client = self.client

        @client.event
        async def on_ready():
            log.info("Discord bot connected as %s (ID: %s)", client.user, client.user.id)
            await client.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name="messages",
                ),
            )

        @client.event
        async def on_message(msg: discord.Message):
            if msg.author == client.user:
                return
            if msg.author.bot:
                return
            if not _authorized(msg.author.id):
                return

            content = msg.content.strip()

            # Strip bot mention prefix if present
            if client.user:
                bot_mention = f"<@{client.user.id}>"
                if content.startswith(bot_mention):
                    content = content[len(bot_mention):].strip()

            if not content:
                return

            # Update message content for downstream handlers
            msg.content = content

            if await handle_command(msg):
                return

            await handle_message(msg, client)

    async def _run_client(self):
        """Run the Discord client. Falls back to no MESSAGE_CONTENT intent if needed."""
        try:
            await self.client.start(DISCORD_BOT_TOKEN)
        except discord.errors.PrivilegedIntentsRequired:
            if self._message_content_intent:
                log.warning(
                    "Discord MESSAGE_CONTENT privileged intent not enabled in Developer Portal. "
                    "Enable it at https://discord.com/developers/applications/ for best experience. "
                    "Retrying without it (bot will only see mentions)..."
                )
                self._message_content_intent = False
                self.client = self._build_client(message_content=False)
                from channels.discord.handlers import handle_command, handle_message, _authorized
                self._register_events(_authorized, handle_command, handle_message)
                try:
                    await self.client.start(DISCORD_BOT_TOKEN)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.error("Discord client error (fallback): %s", e)
            else:
                log.error("Discord intents error — cannot connect")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Discord client error: %s", e)

    async def stop(self):
        if self.client and not self.client.is_closed():
            await self.client.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Discord channel stopped.")
