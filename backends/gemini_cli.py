"""Gemini CLI backend — shells out to the Gemini CLI binary.

Requires: npm install -g @anthropic-ai/gemini-cli (or equivalent)
CLI flags verified against `gemini --help` (v0.28.2).
"""

import asyncio
import json
import logging
import subprocess

log = logging.getLogger("nexus")


class GeminiCLIBackend:
    """Backend that shells out to the Gemini CLI for full agentic tool use."""

    def __init__(self):
        from config import GEMINI_BIN, GEMINI_CLI_MODEL
        self._bin = GEMINI_BIN
        self._model = GEMINI_CLI_MODEL

    def get_model_display(self, model: str) -> str:
        return f"Gemini CLI ({self._model})" if self._model else "Gemini CLI"

    @property
    def name(self) -> str:
        return "gemini_cli"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return True

    @property
    def supports_sessions(self) -> bool:
        return True

    def call_sync(
        self,
        prompt: str,
        *,
        model: str = "sonnet",
        session_id: str | None = None,
        timeout: int = 300,
        system_prompt: str | None = None,
        memory_context: str | None = None,
    ) -> dict:
        """Synchronous Gemini CLI call.

        Returns {"result": str, "session_id": str | None}
        """
        cmd = [self._bin, "-p", prompt, "--output-format", "json", "--yolo"]
        if self._model:
            cmd.extend(["-m", self._model])
        if session_id:
            cmd.extend(["-r", session_id])

        log.info("Gemini CLI call (sync): %s", " ".join(cmd[:6]) + " ...")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            return {
                "result": f"Gemini CLI not found at {self._bin}. Install it first.",
                "session_id": None,
            }
        except subprocess.TimeoutExpired:
            return {"result": f"Gemini CLI timed out after {timeout}s", "session_id": None}

        if result.returncode != 0:
            return {
                "result": f"Gemini CLI error (exit {result.returncode}): {result.stderr[:500]}",
                "session_id": None,
            }

        try:
            data = json.loads(result.stdout)
            return {
                "result": data.get("response", data.get("result", result.stdout.strip()[:4000])),
                "session_id": data.get("session_id"),
            }
        except json.JSONDecodeError:
            return {
                "result": result.stdout.strip()[:4000] if result.stdout else "(no output)",
                "session_id": None,
            }

    async def call_streaming(
        self,
        message: str,
        *,
        session_id: str | None = None,
        model: str = "sonnet",
        on_progress=None,
        streaming_editor=None,
        system_prompt: str | None = None,
        memory_context: str | None = None,
        extra_system_prompt: str | None = None,
    ) -> dict:
        """Async streaming Gemini CLI call.

        Returns {"result": str, "session_id": str | None, "written_files": list}
        """
        cmd = [self._bin, "-p", message, "--output-format", "stream-json", "--yolo"]
        if self._model:
            cmd.extend(["-m", self._model])
        if session_id:
            cmd.extend(["-r", session_id])

        log.info("Gemini CLI (streaming): %s", " ".join(cmd[:6]) + " ...")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
            )
        except FileNotFoundError:
            return {
                "result": f"Gemini CLI not found at {self._bin}. Install it first.",
                "session_id": None,
                "written_files": [],
            }

        try:
            data = await asyncio.wait_for(
                _read_gemini_stream(proc, on_progress=on_progress, streaming_editor=streaming_editor),
                timeout=1800,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise TimeoutError("Gemini CLI timed out after 1800s")

        await proc.wait()

        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            err = stderr_bytes.decode().strip()
            log.warning("Gemini CLI exited %d (stream mode), stderr: %s", proc.returncode, err[:500])
            if data and data.get("result"):
                return data
            raise RuntimeError(f"Gemini CLI exited {proc.returncode}: {err}")

        return data


async def _read_gemini_stream(proc, on_progress=None, streaming_editor=None) -> dict:
    """Read stream-json output from Gemini CLI line by line.

    Gemini CLI emits JSONL events. We parse them for text content,
    tool-use status, and the final result.
    """
    result = None
    written_files = []
    streamed_text = ""

    while True:
        try:
            raw_line = await proc.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError) as e:
            log.warning("Gemini stream line too large, skipping: %s", e)
            try:
                proc.stdout._buffer.clear()
            except Exception:
                pass
            continue

        if not raw_line:
            break
        line = raw_line.decode().strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.debug("Non-JSON gemini stream line: %s", line[:200])
            continue

        event_type = event.get("type", "")

        # Text content events
        if event_type in ("text", "content", "message"):
            text = event.get("text", event.get("content", ""))
            if text and streaming_editor:
                streamed_text += text
                await streaming_editor.add_text(text)
            elif text:
                streamed_text += text

        # Tool-use events
        elif event_type in ("tool_use", "tool_call", "action"):
            tool_name = event.get("name", event.get("tool", ""))
            status = f"Using tool: {tool_name}" if tool_name else "Running tool..."
            if streaming_editor:
                await streaming_editor.add_tool_status(status)
            elif on_progress:
                await on_progress(status)

        # Result / completion events
        elif event_type == "result":
            result = {
                "result": event.get("response", event.get("result", "")),
                "session_id": event.get("session_id"),
                "written_files": written_files,
            }

    # If no explicit result event but we streamed text, synthesize one
    if result is None:
        if streamed_text.strip():
            return {
                "result": streamed_text,
                "session_id": None,
                "written_files": written_files,
            }
        raise RuntimeError("No result event in Gemini CLI stream output")

    return result
