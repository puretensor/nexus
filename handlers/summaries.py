"""Auto-generate session summaries every N messages."""

import asyncio
import logging

from db import get_session, update_summary

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
    """Call the LLM with a cheap one-shot prompt to summarize the session."""
    from engine import call_sync

    try:
        prompt = (
            "Summarize this conversation in exactly one short sentence (under 60 characters) "
            "for a session list. Just output the summary, nothing else. No quotes."
        )

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            lambda: call_sync(
                prompt, model="haiku", session_id=session_id, timeout=30
            ),
        )

        summary = data.get("result", "").strip()

        if summary and len(summary) < 200:
            update_summary(chat_id, summary)
            log.info("Updated session summary for chat %d: %s", chat_id, summary)
        else:
            log.warning("Summary too long or empty, skipping: %s", summary[:100] if summary else "(empty)")

    except Exception:
        log.exception("Failed to generate summary for chat %d", chat_id)
