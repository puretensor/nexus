"""OpenAI Codex CLI backend — shells out to the Codex CLI binary.

Requires: npm install -g @openai/codex (or equivalent)
CLI flags verified against `codex exec --help` (v0.101.0).
"""

import asyncio
import json
import logging
import subprocess

log = logging.getLogger("nexus")


class CodexCLIBackend:
    """Backend that shells out to the OpenAI Codex CLI for full agentic tool use."""

    def __init__(self):
        from config import CODEX_BIN, CODEX_MODEL, CODEX_CWD
        self._bin = CODEX_BIN
        self._model = CODEX_MODEL
        self._cwd = CODEX_CWD

    def get_model_display(self, model: str) -> str:
        return "Codex"

    @staticmethod
    def _build_prompt(
        user_message: str,
        system_prompt: str | None = None,
        memory_context: str | None = None,
        extra_system_prompt: str | None = None,
    ) -> str:
        """Prepend system context to the user message.

        Codex CLI has no --system-prompt flag, so we inject context
        as a <system> block before the user message.
        """
        context_parts = []
        if system_prompt:
            context_parts.append(system_prompt)
        if memory_context:
            context_parts.append(memory_context)
        if extra_system_prompt:
            context_parts.append(extra_system_prompt)
        if not context_parts:
            return user_message
        context = "\n\n".join(context_parts)
        return f"<system>\n{context}\n</system>\n\n{user_message}"

    @property
    def name(self) -> str:
        return "codex_cli"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return True

    @property
    def supports_sessions(self) -> bool:
        return False

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
        """Synchronous Codex CLI call.

        Returns {"result": str, "session_id": str | None}
        """
        full_prompt = self._build_prompt(prompt, system_prompt, memory_context)
        cmd = [
            self._bin, "exec", full_prompt,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
        if self._model:
            cmd.extend(["-m", self._model])
        if self._cwd:
            cmd.extend(["-C", self._cwd])

        log.info("Codex CLI call (sync): %s", " ".join(cmd[:5]) + " ...")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            return {
                "result": f"Codex CLI not found at {self._bin}. Install it first.",
                "session_id": None,
            }
        except subprocess.TimeoutExpired:
            return {"result": f"Codex CLI timed out after {timeout}s", "session_id": None}

        if result.returncode != 0:
            return {
                "result": f"Codex CLI error (exit {result.returncode}): {result.stderr[:500]}",
                "session_id": None,
            }

        # Codex exec --json emits JSONL lines; extract final result text
        return _parse_codex_jsonl(result.stdout)

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
        """Async streaming Codex CLI call.

        Returns {"result": str, "session_id": str | None, "written_files": list}
        """
        full_prompt = self._build_prompt(message, system_prompt, memory_context, extra_system_prompt)
        cmd = [
            self._bin, "exec", full_prompt,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
        if self._model:
            cmd.extend(["-m", self._model])
        if self._cwd:
            cmd.extend(["-C", self._cwd])

        log.info("Codex CLI (streaming): %s", " ".join(cmd[:5]) + " ...")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
            )
        except FileNotFoundError:
            return {
                "result": f"Codex CLI not found at {self._bin}. Install it first.",
                "session_id": None,
                "written_files": [],
            }

        try:
            data = await asyncio.wait_for(
                _read_codex_stream(proc, on_progress=on_progress, streaming_editor=streaming_editor),
                timeout=1800,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise TimeoutError("Codex CLI timed out after 1800s")

        await proc.wait()

        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            err = stderr_bytes.decode().strip()
            log.warning("Codex CLI exited %d (stream mode), stderr: %s", proc.returncode, err[:500])
            if data and data.get("result"):
                return data
            raise RuntimeError(f"Codex CLI exited {proc.returncode}: {err}")

        return data


def _parse_codex_jsonl(stdout: str) -> dict:
    """Parse JSONL output from `codex exec --json` and extract the final result.

    Supports both legacy format (message/output_text) and v0.101+ format
    (item.completed with item.type == "agent_message").
    """
    last_text = ""
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type", "")

        # v0.101+ format
        if event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                if text:
                    last_text = text

        # Legacy format
        elif event_type == "message" and event.get("role") == "assistant":
            content = event.get("content", "")
            if isinstance(content, str) and content:
                last_text = content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        last_text = part.get("text", last_text)
                    elif isinstance(part, dict) and part.get("type") == "text":
                        last_text = part.get("text", last_text)
        elif event_type in ("output_text", "text"):
            last_text = event.get("text", last_text)

    return {
        "result": last_text.strip()[:4000] if last_text else stdout.strip()[:4000] or "(no output)",
        "session_id": None,
    }


async def _read_codex_stream(proc, on_progress=None, streaming_editor=None) -> dict:
    """Read JSONL output from Codex CLI line by line.

    Parses events for text deltas and tool-use status.
    Supports both legacy format (message/output_text) and v0.101+ format
    (item.started/item.completed with item.type).
    """
    written_files = []
    streamed_text = ""

    while True:
        try:
            raw_line = await proc.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError) as e:
            log.warning("Codex stream line too large, skipping: %s", e)
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
            log.debug("Non-JSON codex stream line: %s", line[:200])
            continue

        event_type = event.get("type", "")

        # --- v0.101+ format: item.completed / item.started ---
        if event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    streamed_text += text
                    if streaming_editor:
                        await streaming_editor.add_text(text)
            elif item_type == "command_execution":
                cmd_str = item.get("command", "")
                status = f"Ran: {cmd_str}" if cmd_str else "Command completed"
                if streaming_editor:
                    await streaming_editor.add_tool_status(status)
                elif on_progress:
                    await on_progress(status)

        elif event_type == "item.started":
            item = event.get("item", {})
            item_type = item.get("type", "")
            if item_type == "command_execution":
                cmd_str = item.get("command", "")
                status = f"Running: {cmd_str}" if cmd_str else "Running command..."
                if streaming_editor:
                    await streaming_editor.add_tool_status(status)
                elif on_progress:
                    await on_progress(status)

        # --- Legacy format (pre-v0.101) ---
        elif event_type == "message" and event.get("role") == "assistant":
            content = event.get("content", "")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                        text = part.get("text", "")
            if text:
                streamed_text += text
                if streaming_editor:
                    await streaming_editor.add_text(text)

        elif event_type in ("output_text", "text"):
            text = event.get("text", "")
            if text:
                streamed_text += text
                if streaming_editor:
                    await streaming_editor.add_text(text)

        elif event_type in ("function_call", "tool_call", "function_call_output"):
            tool_name = event.get("name", event.get("function", ""))
            status = f"Using tool: {tool_name}" if tool_name else "Running tool..."
            if streaming_editor:
                await streaming_editor.add_tool_status(status)
            elif on_progress:
                await on_progress(status)

    if not streamed_text.strip():
        raise RuntimeError("No output from Codex CLI stream")

    return {
        "result": streamed_text,
        "session_id": None,
        "written_files": written_files,
    }
