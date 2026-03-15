#!/usr/bin/env python3
"""NEXUS — Multi-Channel Agentic Service.

Entry point that starts all subsystems:
  - Telegram channel (interactive bot)
  - Discord channel (interactive bot)
  - Scheduler (cron-like task runner)
  - Observer registry (cron-scheduled background observers)
  - Email input channel + draft queue
"""

import asyncio
import signal
import sys

from config import log
from db import init_db


def _build_observer_registry():
    """Create and populate the observer registry.

    Import and register all available observer classes here.
    Observers that fail to import are logged and skipped.
    """
    from observers.registry import ObserverRegistry

    registry = ObserverRegistry()

    observers = [
        ("observers.email_digest", "EmailDigestObserver"),
        ("observers.morning_brief", "MorningBriefObserver"),
        # node_health disabled — alerting handled by Alertmanager on mon2
        # ("observers.node_health", "NodeHealthObserver"),
        ("observers.daily_snippet", "DailySnippetObserver"),
        ("observers.bretalon_review", "BretalonReviewObserver"),
        ("observers.git_push", "GitPushObserver"),
        ("observers.darwin_consumer", "DarwinConsumer"),
        ("observers.followup_reminder", "FollowupReminderObserver"),
        # alertmanager_monitor disabled — alerts suppressed from HAL interface
        # ("observers.alertmanager_monitor", "AlertmanagerMonitorObserver"),
        ("observers.cyber_threat_feed", "CyberThreatFeedObserver"),
        # intel_briefing disabled — replaced by intel_deep_analysis which
        # generates both full analysis articles and summary briefing cards
        # ("observers.intel_briefing", "IntelBriefingObserver"),
        ("observers.intel_deep_analysis", "IntelDeepAnalysisObserver"),
        ("observers.memory_sync", "MemorySyncObserver"),
        ("observers.daily_report", "DailyReportObserver"),
        ("observers.doc_compiler", "DocCompilerObserver"),
        ("observers.weekly_report", "WeeklyReportObserver"),
        ("observers.git_security_audit", "GitSecurityAuditObserver"),
        ("observers.git_auto_sync", "GitAutoSyncObserver"),
        ("observers.github_activity", "GitHubActivityObserver"),
        ("observers.pipeline_watchdog", "PipelineWatchdog"),
    ]

    import importlib
    for module_path, class_name in observers:
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            registry.register(cls())
        except Exception as e:
            log.warning("Failed to register %s: %s", class_name, e)

    return registry


async def main():
    """Start NEXUS and run until interrupted."""
    log.info("NEXUS starting...")

    # Initialize database
    init_db()

    # Import here to ensure config/db are ready
    from channels.telegram import TelegramChannel
    from channels.email_in import EmailInputChannel
    from config import DISCORD_BOT_TOKEN, WA_ENABLED

    telegram = TelegramChannel()
    registry = _build_observer_registry()
    email_in = EmailInputChannel()

    # Discord — only start if token is configured
    discord_channel = None
    if DISCORD_BOT_TOKEN:
        from channels.discord import DiscordChannel
        discord_channel = DiscordChannel()

    # WhatsApp — only start if enabled
    wa_channel = None
    if WA_ENABLED:
        import json as _json
        from config import WA_INSTANCES, WA_ROUTING_CONFIG
        from channels.whatsapp import WhatsAppChannel

        try:
            instances = _json.loads(WA_INSTANCES)
        except Exception as e:
            log.warning("Failed to parse WA_INSTANCES: %s", e)
            instances = []

        wa_channel = WhatsAppChannel(
            instances=instances,
        )

    # Graceful shutdown handler
    shutdown_event = asyncio.Event()
    observer_task = None

    def handle_signal():
        log.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Start TC health probe
    from health_probes import get_probe
    health_probe_task = asyncio.create_task(get_probe().run_loop())

    try:
        # Start Telegram channel
        await telegram.start()

        # Start email input channel (needs bot reference for notifications)
        email_in._bot = telegram.app.bot
        await email_in.start()

        # Start Discord channel (if configured)
        if discord_channel:
            await discord_channel.start()

        # Start WhatsApp channel (if enabled)
        if wa_channel:
            wa_channel.set_telegram_bot(telegram.app.bot)
            await wa_channel.start()

            # Set global ref for Telegram callback handler (wa:approve/reject)
            from channels.telegram import callbacks as _tg_cb
            _tg_cb._wa_channel = wa_channel

            # Wire up the webhook handler: set wa_channel + event loop
            # on the GitPushObserver so WebhookHandler can dispatch
            for obs_instance in registry._observers:
                if hasattr(obs_instance, 'LISTEN_PORT'):  # GitPushObserver
                    obs_instance._wa_channel = wa_channel
                    obs_instance._event_loop = asyncio.get_event_loop()
                    break

        # Start observer registry loop
        observer_task = asyncio.create_task(registry.run_loop())

        log.info("NEXUS is running. Press Ctrl+C to stop.")

        # Wait for shutdown signal
        await shutdown_event.wait()

    except KeyboardInterrupt:
        log.info("Keyboard interrupt received")
    finally:
        log.info("NEXUS shutting down...")
        health_probe_task.cancel()
        try:
            await health_probe_task
        except asyncio.CancelledError:
            pass
        if observer_task:
            observer_task.cancel()
            try:
                await observer_task
            except asyncio.CancelledError:
                pass
        if wa_channel:
            await wa_channel.stop()
        if discord_channel:
            await discord_channel.stop()
        await email_in.stop()
        await telegram.stop()
        log.info("NEXUS stopped.")


if __name__ == "__main__":
    asyncio.run(main())
