"""Auto-generate session summaries every N messages."""

import asyncio
import json
import logging

from db import get_session, update_summary
from config import CLAUDE_BIN, CLAUDE_CWD

log = logging.getLogger("nexus")

SUMMARY_INTERVAL = 5  # Generate summary every N messages


async def maybe_generate_summary(chat_id: int):
    """Check if it's time to generate a summary, and if so, do it.

    Called after every message. Only actually generates on every SUMMARY_INTERVAL messages.
    Runs the summary generation in background so it doesn't block the user.
    """
    session = get_session(chat_id)
    if not session or not session.get("session_id"):
        return

    msg_count = session.get("message_count", 0)
    if msg_count == 0 or msg_count % SUMMARY_INTERVAL != 0:
        return

    # Fire and forget — don't block the response
    asyncio.create_task(_generate_summary(chat_id, session["session_id"]))


async def _generate_summary(chat_id: int, session_id: str):
    """Call Claude with a cheap one-shot prompt to summarize the session."""
    try:
        prompt = (
            "Summarize this conversation in exactly one short sentence (under 60 characters) "
            "for a session list. Just output the summary, nothing else. No quotes."
        )
        cmd = [
            CLAUDE_BIN,
            "-p", prompt,
            "--output-format", "json",
            "--model", "haiku",
            "--resume", session_id,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CLAUDE_CWD,
        )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            log.warning("Summary generation failed (exit %d): %s", proc.returncode, stderr.decode()[:200])
            return

        try:
            data = json.loads(stdout.decode())
            summary = data.get("result", "").strip()
        except (json.JSONDecodeError, AttributeError):
            summary = stdout.decode().strip()

        if summary and len(summary) < 200:
            update_summary(chat_id, summary)
            log.info("Updated session summary for chat %d: %s", chat_id, summary)
        else:
            log.warning("Summary too long or empty, skipping: %s", summary[:100] if summary else "(empty)")

    except asyncio.TimeoutError:
        log.warning("Summary generation timed out for chat %d", chat_id)
    except Exception:
        log.exception("Failed to generate summary for chat %d", chat_id)
