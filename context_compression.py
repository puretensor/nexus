"""Context compression for Nexus conversation history.

Tier 1: Tool result truncation (zero-cost, always applied)
Tier 2: LLM-based summarization (triggered when token estimate exceeds threshold)
"""

import json
import logging
import os

log = logging.getLogger("nexus")

# Configuration
COMPRESS_TRIGGER_TOKENS = int(os.environ.get("COMPRESS_TRIGGER_TOKENS", "100000"))
PRESERVE_RECENT_MESSAGES = int(os.environ.get("PRESERVE_RECENT_MESSAGES", "40"))
TOOL_RESULT_FULL_WINDOW = 6  # Keep last N tool results verbatim
TOOL_RESULT_SUMMARY_CHARS = 200  # Truncate older tool results to this
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "gemini-2.0-flash")


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: total JSON chars / 3.5."""
    return int(len(json.dumps(messages, default=str)) / 3.5)


def compress_tool_results(messages: list[dict]) -> list[dict]:
    """Tier 1: Truncate old tool results. Zero cost, always safe.

    Keeps the last TOOL_RESULT_FULL_WINDOW tool_result blocks verbatim.
    Truncates older ones to TOOL_RESULT_SUMMARY_CHARS.
    """
    # Find all message indices containing tool_result blocks
    tool_result_indices = []
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_result_indices.append(i)
                    break

    # Keep the most recent TOOL_RESULT_FULL_WINDOW intact
    if len(tool_result_indices) <= TOOL_RESULT_FULL_WINDOW:
        return messages

    indices_to_compress = set(tool_result_indices[:-TOOL_RESULT_FULL_WINDOW])

    for idx in indices_to_compress:
        msg = messages[idx]
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        new_blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str) and len(result_content) > TOOL_RESULT_SUMMARY_CHARS:
                    truncated = result_content[:TOOL_RESULT_SUMMARY_CHARS] + f"... [truncated from {len(result_content)} chars]"
                    new_blocks.append({**block, "content": truncated})
                else:
                    new_blocks.append(block)
            else:
                new_blocks.append(block)
        messages[idx] = {**msg, "content": new_blocks}

    return messages


def _build_summary_prompt(old_messages: list[dict]) -> str:
    """Build a prompt instructing Claude to summarize old conversation context."""
    parts = []
    for msg in old_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(f"[{role}]: {content[:500]}")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(f"[{role}]: {block.get('text', '')[:500]}")
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    args = str(block.get("input", {}))[:200]
                    parts.append(f"[{role} tool:{name}]: {args}")
                elif block.get("type") == "tool_result":
                    result = block.get("content", "")
                    if isinstance(result, str):
                        parts.append(f"[tool_result]: {result[:200]}...")

    conversation_text = "\n".join(parts)
    if len(conversation_text) > 50000:
        conversation_text = conversation_text[:50000] + "\n... [earlier content omitted]"

    return (
        "Summarize this conversation between a user and an AI assistant. "
        "This summary will REPLACE these messages, so preserve:\n"
        "1. TASK STATE: What was requested, what's done, what's pending\n"
        "2. KEY DECISIONS: Important choices, configurations, parameters\n"
        "3. FILES MODIFIED: Paths of files created/edited/read\n"
        "4. ERRORS & FIXES: Problems encountered and resolutions\n"
        "5. CURRENT OBJECTIVE: What the user is currently working on\n\n"
        "Be concise. Use bullet points. 400-600 words max.\n\n"
        f"CONVERSATION:\n{conversation_text}"
    )


def _call_summarizer_sync(prompt: str) -> str:
    """Call Gemini Flash to summarize. Synchronous (runs in thread pool)."""
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("Gemini: GOOGLE_API_KEY not set")

    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        max_output_tokens=2000,
    )
    response = client.models.generate_content(
        model=SUMMARY_MODEL,
        contents=prompt,
        config=config,
    )
    return (response.text or "").strip()


def compress_history(messages: list[dict]) -> list[dict]:
    """Tier 2: Summarize old messages when token estimate exceeds threshold.

    Returns compressed message list. Falls back to uncompressed on any error.
    """
    estimated = estimate_tokens(messages)
    if estimated < COMPRESS_TRIGGER_TOKENS:
        return messages

    if len(messages) <= PRESERVE_RECENT_MESSAGES:
        return messages

    split_point = len(messages) - PRESERVE_RECENT_MESSAGES
    old_messages = messages[:split_point]
    recent_messages = list(messages[split_point:])

    # Ensure recent starts with user message
    while recent_messages and recent_messages[0].get("role") != "user":
        old_messages.append(recent_messages.pop(0))

    if not recent_messages:
        return messages

    log.info("Context compression triggered: %d messages, ~%d tokens. Summarizing %d old messages.",
             len(messages), estimated, len(old_messages))

    try:
        prompt = _build_summary_prompt(old_messages)
        summary = _call_summarizer_sync(prompt)

        if len(summary) < 50 or len(summary) > 10000:
            log.warning("Summary quality check failed (len=%d), skipping compression", len(summary))
            return messages

        summary_msg = {
            "role": "user",
            "content": (
                "[CONTEXT SUMMARY — Earlier messages were compressed]\n\n"
                f"{summary}\n\n"
                "[END SUMMARY — Recent conversation continues below]"
            ),
        }
        ack_msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "Understood. I have the context from the summary and will continue from where we left off."}],
        }

        compressed = [summary_msg, ack_msg] + recent_messages
        new_estimate = estimate_tokens(compressed)
        log.info("Compression complete: %d->%d messages, ~%d->%d tokens",
                 len(messages), len(compressed), estimated, new_estimate)
        return compressed

    except Exception as e:
        log.error("Context compression failed: %s — returning uncompressed", e)
        return messages
