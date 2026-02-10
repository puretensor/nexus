"""Telegram channel — wraps python-telegram-bot Application."""

import asyncio

from channels.base import Channel
from config import BOT_TOKEN, log


class TelegramChannel(Channel):
    def __init__(self):
        self.app = None
        self._scheduler_task = None

    async def start(self):
        # Lazy imports to avoid circular dependency (handlers → streaming → channels)
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )
        from channels.telegram.commands import (
            cmd_new, cmd_opus, cmd_sonnet, cmd_voice, cmd_status, cmd_help,
            cmd_weather_card, cmd_crypto_card, cmd_trains_card,
            cmd_gold_card, cmd_markets_card, cmd_forex_card, cmd_nodes_card,
            cmd_session, cmd_history, cmd_resume,
            cmd_schedule, cmd_remind, cmd_cancel,
            cmd_remember, cmd_forget, cmd_memories,
            cmd_check, cmd_restart, cmd_logs, cmd_disk, cmd_top, cmd_deploy,
            cmd_drafts, cmd_calendar, cmd_followups,
            handle_message, handle_voice,
        )
        from channels.telegram.callbacks import handle_callback
        from handlers.photo import handle_photo
        from handlers.document import handle_document
        from handlers.location import handle_location
        from scheduler import run_scheduler

        async def start_scheduler(app):
            self._scheduler_task = asyncio.create_task(run_scheduler(app.bot))

        self.app = Application.builder().token(BOT_TOKEN).post_init(start_scheduler).build()

        # Session commands
        self.app.add_handler(CommandHandler("new", cmd_new))
        self.app.add_handler(CommandHandler("session", cmd_session))
        self.app.add_handler(CommandHandler("history", cmd_history))
        self.app.add_handler(CommandHandler("resume", cmd_resume))
        self.app.add_handler(CommandHandler("opus", cmd_opus))
        self.app.add_handler(CommandHandler("sonnet", cmd_sonnet))
        self.app.add_handler(CommandHandler("voice", cmd_voice))
        self.app.add_handler(CommandHandler("status", cmd_status))
        self.app.add_handler(CommandHandler("help", cmd_help))
        self.app.add_handler(CommandHandler("start", cmd_help))

        # Scheduled task & reminder commands
        self.app.add_handler(CommandHandler("schedule", cmd_schedule))
        self.app.add_handler(CommandHandler("remind", cmd_remind))
        self.app.add_handler(CommandHandler("cancel", cmd_cancel))

        # Draft queue commands
        self.app.add_handler(CommandHandler("drafts", cmd_drafts))

        # Calendar and follow-up commands
        self.app.add_handler(CommandHandler("calendar", cmd_calendar))
        self.app.add_handler(CommandHandler("followups", cmd_followups))

        # Memory commands
        self.app.add_handler(CommandHandler("remember", cmd_remember))
        self.app.add_handler(CommandHandler("forget", cmd_forget))
        self.app.add_handler(CommandHandler("memories", cmd_memories))

        # Quick action commands
        self.app.add_handler(CommandHandler("check", cmd_check))
        self.app.add_handler(CommandHandler("restart", cmd_restart))
        self.app.add_handler(CommandHandler("logs", cmd_logs))
        self.app.add_handler(CommandHandler("disk", cmd_disk))
        self.app.add_handler(CommandHandler("top", cmd_top))
        self.app.add_handler(CommandHandler("deploy", cmd_deploy))

        # Data card commands
        self.app.add_handler(CommandHandler("weather", cmd_weather_card))
        self.app.add_handler(CommandHandler("crypto", cmd_crypto_card))
        self.app.add_handler(CommandHandler("markets", cmd_markets_card))
        self.app.add_handler(CommandHandler("gold", cmd_gold_card))
        self.app.add_handler(CommandHandler("forex", cmd_forex_card))
        self.app.add_handler(CommandHandler("trains", cmd_trains_card))
        self.app.add_handler(CommandHandler("nodes", cmd_nodes_card))

        # Message and callback handlers
        self.app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        self.app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        self.app.add_handler(MessageHandler(filters.LOCATION, handle_location))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
        self.app.add_handler(CallbackQueryHandler(handle_callback))

        log.info("Telegram channel polling...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
