"""Engine — LLM backend facade, stream reader, and message splitting.

Public API: call_sync(), call_streaming(), split_message()
Backend selection: ENGINE_BACKEND env var (default: claude_code)

Utilities (_read_stream, _format_tool_status, split_message) remain here
for backward compatibility and are used by the claude_code backend.
"""

import asyncio
import json
import logging

from config import SYSTEM_PROMPT

try:
    from memory import get_memories_for_injection
except ImportError:
    get_memories_for_injection = None

log = logging.getLogger("nexus")


# ---------------------------------------------------------------------------
# Message splitting for Telegram's 4096-char limit
# ---------------------------------------------------------------------------


def split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find a newline near the limit to split cleanly
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            # No newline — split at limit
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


# ---------------------------------------------------------------------------
# Tool status formatting
# ---------------------------------------------------------------------------


def _format_tool_status(tool_name: str, tool_input: dict) -> str:
    """Map a tool_use event to a human-readable status line."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"Running: {cmd}"
    elif tool_name == "Read":
        return f"Reading: {tool_input.get('file_path', '?')}"
    elif tool_name == "Edit":
        return f"Editing: {tool_input.get('file_path', '?')}"
    elif tool_name == "Write":
        return f"Writing: {tool_input.get('file_path', '?')}"
    elif tool_name == "Glob":
        return f"Searching files: {tool_input.get('pattern', '?')}"
    elif tool_name == "Grep":
        return f"Searching content: {tool_input.get('pattern', '?')}"
    elif tool_name == "WebFetch":
        url = tool_input.get("url", "?")
        if len(url) > 60:
            url = url[:57] + "..."
        return f"Fetching: {url}"
    elif tool_name == "WebSearch":
        return f"Searching web: {tool_input.get('query', '?')}"
    elif tool_name == "Task":
        desc = tool_input.get("description", "")
        if desc:
            return f"Spawning agent: {desc}"
        return "Spawning agent..."
    else:
        return f"Using tool: {tool_name}"


# ---------------------------------------------------------------------------
# Stream reader (used by claude_code backend)
# ---------------------------------------------------------------------------


async def _read_stream(proc, on_progress=None, streaming_editor=None) -> dict:
    """Read stream-json output line by line.

    If streaming_editor is provided, streams text deltas to it in real-time.
    Otherwise falls back to on_progress for tool events only.

    Returns the final result dict with 'result', 'session_id', and 'written_files'.
    """
    result = None
    written_files = []
    streamed_text = ""
    while True:
        try:
            raw_line = await proc.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError) as e:
            log.warning("Stream line too large, skipping: %s", e)
            try:
                proc.stdout._buffer.clear()
            except Exception:
                pass
            continue
        if not raw_line:
            break  # EOF
        line = raw_line.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.debug("Non-JSON stream line: %s", line[:200])
            continue

        event_type = event.get("type")

        # Streaming text deltas (requires --include-partial-messages)
        if event_type == "stream_event" and streaming_editor:
            inner = event.get("event", {})
            inner_type = inner.get("type")
            if inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        streamed_text += text
                        await streaming_editor.add_text(text)

        # Tool-use events from full assistant messages
        elif event_type == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    status = _format_tool_status(tool_name, tool_input)
                    if streaming_editor:
                        await streaming_editor.add_tool_status(status)
                    elif on_progress:
                        await on_progress(status)
                    # Track files written by Claude
                    if tool_name == "Write":
                        fpath = tool_input.get("file_path", "")
                        if fpath:
                            written_files.append(fpath)

        elif event_type == "result":
            result = {
                "result": event.get("result", ""),
                "session_id": event.get("session_id"),
                "written_files": written_files,
            }

    if result is None:
        # Try to read stderr for context on why the stream ended without a result
        stderr_text = ""
        try:
            stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=5)
            stderr_text = stderr_bytes.decode().strip() if stderr_bytes else ""
        except Exception:
            pass

        # If we already streamed text to the user, synthesize a result
        if streamed_text.strip():
            log.warning(
                "No result event but %d chars already streamed. stderr: %s",
                len(streamed_text), stderr_text[:500] or "(empty)",
            )
            return {
                "result": streamed_text,
                "session_id": None,
                "written_files": written_files,
            }

        # No text was streamed — this is a real failure
        if stderr_text:
            log.error("No result event in stream. stderr: %s", stderr_text[:1000])
            raise RuntimeError(f"Claude stream ended without result: {stderr_text[:300]}")
        raise RuntimeError("No result event in stream output (claude may have crashed)")
    return result


# ---------------------------------------------------------------------------
# Public API — delegates to configured backend
# ---------------------------------------------------------------------------


def get_model_display(model: str = "sonnet") -> str:
    """Return a human-readable label for the current backend + model.

    E.g. 'Sonnet 4.5' for claude_code, 'qwen3:30b-a3b' for ollama.
    """
    from backends import get_backend

    backend = get_backend()
    if hasattr(backend, "get_model_display"):
        return backend.get_model_display(model)
    return f"{backend.name}:{model}"


def call_sync(
    prompt: str,
    model: str = "sonnet",
    session_id: str | None = None,
    timeout: int = 300,
) -> dict:
    """Synchronous LLM call (for observers running in thread pool).

    Returns dict with 'result', 'session_id' keys.
    """
    from backends import get_backend

    return get_backend().call_sync(
        prompt,
        model=model,
        session_id=session_id,
        timeout=timeout,
        system_prompt=SYSTEM_PROMPT,
    )


async def call_streaming(
    message: str,
    session_id: str | None,
    model: str,
    on_progress=None,
    streaming_editor=None,
    extra_system_prompt: str | None = None,
) -> dict:
    """Async streaming LLM call with real-time progress.

    Returns dict with 'result', 'session_id', 'written_files' keys.
    """
    from backends import get_backend

    memory_ctx = None
    if get_memories_for_injection:
        memory_ctx = get_memories_for_injection()

    return await get_backend().call_streaming(
        message,
        session_id=session_id,
        model=model,
        on_progress=on_progress,
        streaming_editor=streaming_editor,
        system_prompt=SYSTEM_PROMPT,
        memory_context=memory_ctx,
        extra_system_prompt=extra_system_prompt,
    )
