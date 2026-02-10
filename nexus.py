#!/usr/bin/env python3
"""NEXUS — Multi-Channel Agentic Service.

Entry point that starts all subsystems:
  - Telegram channel (interactive bot)
  - Scheduler (cron-like task runner)
  - (Phase 3) Observer registry
  - (Phase 4) Email input channel + draft queue
"""

import asyncio
import signal
import sys

from config import log
from db import init_db


async def main():
    """Start NEXUS and run until interrupted."""
    log.info("NEXUS starting...")

    # Initialize database
    init_db()

    # Import here to ensure config/db are ready
    from channels.telegram import TelegramChannel

    telegram = TelegramChannel()

    # Graceful shutdown handler
    shutdown_event = asyncio.Event()

    def handle_signal():
        log.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    try:
        # Start Telegram channel
        await telegram.start()
        log.info("NEXUS is running. Press Ctrl+C to stop.")

        # Wait for shutdown signal
        await shutdown_event.wait()

    except KeyboardInterrupt:
        log.info("Keyboard interrupt received")
    finally:
        log.info("NEXUS shutting down...")
        await telegram.stop()
        log.info("NEXUS stopped.")


if __name__ == "__main__":
    asyncio.run(main())
