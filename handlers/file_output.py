"""Scan Claude's written files and send them back via Telegram."""

import logging
import os

log = logging.getLogger("nexus")

# Extensions sent as photos (Telegram send_photo supports these)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# Allowed output path prefixes (files outside these are not auto-sent)
ALLOWED_PREFIXES = (
    "/tmp/hal_output/",
    "/tmp/",
    os.path.expanduser("~/images/"),
)

# File extensions/names to skip (internal files)
SKIP_EXTENSIONS = {".db", ".sqlite", ".env", ".pyc", ".pyo"}
SKIP_NAMES = {"sessions.db", ".env", "config.json", "settings.json"}


def _is_allowed(path: str) -> bool:
    """Check if a file path is under an allowed output prefix."""
    for prefix in ALLOWED_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _should_skip(path: str) -> bool:
    """Check if a file should be skipped (internal/config files)."""
    name = os.path.basename(path)
    if name in SKIP_NAMES:
        return True
    _, ext = os.path.splitext(name)
    if ext.lower() in SKIP_EXTENSIONS:
        return True
    return False


async def scan_and_send_outputs(chat, written_files: list[str]) -> int:
    """Check written file paths and send eligible ones via Telegram.

    Returns the number of files successfully sent.
    """
    sent = 0
    for raw_path in written_files:
        if not raw_path:
            continue

        # Expand ~ to home directory
        path = os.path.expanduser(raw_path)

        if not os.path.isfile(path):
            log.debug("Written file not found, skipping: %s", path)
            continue

        if not _is_allowed(path):
            log.debug("Written file outside allowed prefixes, skipping: %s", path)
            continue

        if _should_skip(path):
            log.debug("Skipping internal file: %s", path)
            continue

        ext = os.path.splitext(path)[1].lower()
        filename = os.path.basename(path)

        try:
            if ext in IMAGE_EXTENSIONS:
                with open(path, "rb") as f:
                    await chat.send_photo(photo=f, caption=filename)
                log.info("Sent image to Telegram: %s", path)
            else:
                with open(path, "rb") as f:
                    await chat.send_document(document=f, caption=filename)
                log.info("Sent document to Telegram: %s", path)
            sent += 1
        except Exception:
            log.exception("Failed to send file to Telegram: %s", path)

    return sent
