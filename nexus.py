#!/usr/bin/env python3
"""NEXUS — Multi-Channel Agentic Service.

Entry point that starts all subsystems:
  - Telegram channel (interactive bot)
  - Scheduler (cron-like task runner)
  - Observer registry (cron-scheduled background observers)
  - (Phase 4) Email input channel + draft queue
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
        ("observers.node_health", "NodeHealthObserver"),
        ("observers.daily_snippet", "DailySnippetObserver"),
        ("observers.bretalon_review", "BretalonReviewObserver"),
        ("observers.git_push", "GitPushObserver"),
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

    telegram = TelegramChannel()
    registry = _build_observer_registry()
    email_in = EmailInputChannel()

    # Graceful shutdown handler
    shutdown_event = asyncio.Event()
    observer_task = None

    def handle_signal():
        log.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    try:
        # Start Telegram channel
        await telegram.start()

        # Start email input channel (needs bot reference for notifications)
        email_in._bot = telegram.app.bot
        await email_in.start()

        # Start observer registry loop
        observer_task = asyncio.create_task(registry.run_loop())

        log.info("NEXUS is running. Press Ctrl+C to stop.")

        # Wait for shutdown signal
        await shutdown_event.wait()

    except KeyboardInterrupt:
        log.info("Keyboard interrupt received")
    finally:
        log.info("NEXUS shutting down...")
        if observer_task:
            observer_task.cancel()
            try:
                await observer_task
            except asyncio.CancelledError:
                pass
        await email_in.stop()
        await telegram.stop()
        log.info("NEXUS stopped.")


if __name__ == "__main__":
    asyncio.run(main())
