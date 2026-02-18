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
        ("observers.pureclaw_email_responder", "PureClawEmailResponderObserver"),
        ("observers.alertmanager_monitor", "AlertmanagerMonitorObserver"),
        ("observers.cyber_threat_feed", "CyberThreatFeedObserver"),
        ("observers.intel_briefing", "IntelBriefingObserver"),
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
    from config import DISCORD_BOT_TOKEN

    telegram = TelegramChannel()
    registry = _build_observer_registry()
    email_in = EmailInputChannel()

    # Discord — only start if token is configured
    discord_channel = None
    if DISCORD_BOT_TOKEN:
        from channels.discord import DiscordChannel
        discord_channel = DiscordChannel()

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
        if discord_channel:
            await discord_channel.stop()
        await email_in.stop()
        await telegram.stop()
        log.info("NEXUS stopped.")


if __name__ == "__main__":
    asyncio.run(main())
