"""Claude Code CLI backend — subprocess-based invocation."""

import asyncio
import json
import logging
import os
import subprocess

from config import CLAUDE_BIN, CLAUDE_CWD, TIMEOUT

log = logging.getLogger("nexus")

# Strip ANTHROPIC_API_KEY so CLI uses OAuth credentials from ~/.claude/.credentials.json.
# The API key in the env is used by the Anthropic SDK (for non-CLI backends) but is invalid
# for CLI auth. OAuth tokens are refreshed via K8s secret rotation + init container.
_CLI_ENV = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}


class ClaudeCodeBackend:
    """Backend that shells out to the Claude Code CLI."""

    @property
    def name(self) -> str:
        return "claude_code"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return True

    @property
    def supports_sessions(self) -> bool:
        return True

    def get_model_display(self, model: str) -> str:
        _LABELS = {"opus": "Claude Opus", "sonnet": "Claude Sonnet", "haiku": "Claude Haiku"}
        return _LABELS.get(model, f"Claude ({model})")

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
        """Synchronous Claude Code CLI call.

        Returns {"result": str, "session_id": str | None}
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
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
        if memory_context:
            cmd.extend(["--append-system-prompt", memory_context])

        log.info("Running (sync): %s", " ".join(cmd[:6]) + " ...")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=CLAUDE_CWD, env=_CLI_ENV
            )
        except subprocess.TimeoutExpired:
            return {"result": f"Claude timed out after {timeout}s", "session_id": None, "error": True}

        if result.returncode != 0:
            return {
                "result": f"Claude error (exit {result.returncode}): {result.stderr[:500]}",
                "session_id": None,
                "error": True,
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
        """Async streaming Claude Code CLI call.

        Returns {"result": str, "session_id": str | None, "written_files": list}
        """
        from engine import _read_stream

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
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
        if memory_context:
            cmd.extend(["--append-system-prompt", memory_context])
        if extra_system_prompt:
            cmd.extend(["--append-system-prompt", extra_system_prompt])

        log.info("Running (streaming): %s", " ".join(cmd[:6]) + " ...")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CLAUDE_CWD,
            env=_CLI_ENV,
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
