"""OpenAI Codex CLI backend — subprocess stub, same pattern as claude_code."""

import asyncio
import json
import logging
import subprocess

log = logging.getLogger("nexus")


class CodexCLIBackend:
    """Backend that shells out to the OpenAI Codex CLI (stub).

    This is a placeholder for OpenAI's agentic CLI.
    The interface mirrors ClaudeCodeBackend.
    """

    def __init__(self):
        from config import CODEX_BIN
        self._bin = CODEX_BIN

    @property
    def name(self) -> str:
        return "codex_cli"

    @property
    def supports_streaming(self) -> bool:
        return False  # TBD when Codex CLI ships

    @property
    def supports_tools(self) -> bool:
        return False

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
        """Synchronous Codex CLI call (stub)."""
        cmd = [self._bin, "--prompt", prompt]

        log.info("Codex CLI call (sync): %s", " ".join(cmd[:4]) + " ...")

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

        try:
            data = json.loads(result.stdout)
            return {
                "result": data.get("result", result.stdout.strip()[:4000]),
                "session_id": None,
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
        """Async streaming — falls back to sync for now."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.call_sync(
                message, model=model, timeout=300,
                system_prompt=system_prompt, memory_context=memory_context,
            ),
        )
        result["written_files"] = []
        return result
