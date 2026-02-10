"""Engine — Claude CLI caller, stream reader, and message splitting.

Ported from ~/claude-telegram/streaming.py. The StreamingEditor (Telegram-specific)
lives in channels/telegram/streaming.py; this module contains the transport-agnostic
Claude invocation and stream parsing logic.
"""

import asyncio
import json
import logging
import subprocess
import time

from config import CLAUDE_BIN, CLAUDE_CWD, TIMEOUT, SYSTEM_PROMPT

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
# Stream reader
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
# Claude CLI caller
# ---------------------------------------------------------------------------


def call_sync(
    prompt: str,
    model: str = "sonnet",
    session_id: str | None = None,
    timeout: int = 300,
) -> dict:
    """Synchronous Claude call (for observers running in thread pool).

    Returns dict with 'result', 'session_id' keys.
    """
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", model,
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    if SYSTEM_PROMPT:
        cmd.extend(["--append-system-prompt", SYSTEM_PROMPT])

    log.info("Running (sync): %s", " ".join(cmd[:6]) + " ...")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=CLAUDE_CWD
        )
    except subprocess.TimeoutExpired:
        return {"result": f"Claude timed out after {timeout}s", "session_id": None}

    if result.returncode != 0:
        return {
            "result": f"Claude error (exit {result.returncode}): {result.stderr[:500]}",
            "session_id": None,
        }

    try:
        data = json.loads(result.stdout)
        return {
            "result": data.get("result", "(empty response)"),
            "session_id": data.get("session_id"),
        }
    except json.JSONDecodeError:
        return {
            "result": result.stdout.strip()[:4000] if result.stdout else "(no output)",
            "session_id": None,
        }


async def call_streaming(
    message: str,
    session_id: str | None,
    model: str,
    on_progress=None,
    streaming_editor=None,
    extra_system_prompt: str | None = None,
) -> dict:
    """Shell out to `claude -p` with stream-json output and return parsed result."""
    cmd = [
        CLAUDE_BIN,
        "-p", message,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model", model,
        "--include-partial-messages",
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    if SYSTEM_PROMPT:
        cmd.extend(["--append-system-prompt", SYSTEM_PROMPT])

    # Inject persistent memories
    if get_memories_for_injection:
        memory_ctx = get_memories_for_injection()
        if memory_ctx:
            cmd.extend(["--append-system-prompt", memory_ctx])

    # Inject extra system prompt (e.g. voice mode instructions)
    if extra_system_prompt:
        cmd.extend(["--append-system-prompt", extra_system_prompt])

    log.info("Running (streaming): %s", " ".join(cmd[:6]) + " ...")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=CLAUDE_CWD,
        limit=10 * 1024 * 1024,  # 10MB buffer for large stream-json lines
    )

    try:
        data = await asyncio.wait_for(
            _read_stream(proc, on_progress=on_progress, streaming_editor=streaming_editor),
            timeout=TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise TimeoutError(f"Claude timed out after {TIMEOUT}s")

    # Wait for process to fully exit
    await proc.wait()

    if proc.returncode != 0:
        stderr_bytes = await proc.stderr.read() if proc.stderr else b""
        err = stderr_bytes.decode().strip()
        log.warning("Claude exited %d (stream mode), stderr: %s", proc.returncode, err[:500])
        # In stream mode, we may still have a valid result even with non-zero exit
        if data and data.get("result"):
            return data
        raise RuntimeError(f"claude exited {proc.returncode}: {err}")

    return data
