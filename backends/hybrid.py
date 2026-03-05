"""Hybrid backend — routes between Bedrock API (fast) and Claude Code CLI (power)."""

import logging
import os
import re

# Satisfies backends.base.Backend protocol via structural subtyping

log = logging.getLogger("nexus")

_CLI_PATTERNS = re.compile(
    r'\b(deploy|restart|fix|update|install|configure|commit|push|'
    r'write.*report|create.*file|edit.*file|merge|migrate|'
    r'build|compile|test.*suite|debug|diagnose|audit|refactor)\b',
    re.IGNORECASE,
)

_MULTI_STEP = re.compile(
    r'\b(then|after that|next step|also|finally|first.*then)\b',
    re.IGNORECASE,
)


class HybridBackend:
    name = "hybrid"
    supports_streaming = True
    supports_tools = True
    supports_sessions = True

    def __init__(self):
        from backends.bedrock_api import BedrockAPIBackend
        from backends.claude_code import ClaudeCodeBackend

        self._api = BedrockAPIBackend()
        self._cli = ClaudeCodeBackend()
        self._default = os.environ.get("HYBRID_DEFAULT", "api")
        log.info("HybridBackend initialized (default=%s)", self._default)

    def _classify(self, message: str, session: dict | None = None) -> str:
        """Route message to 'api' or 'cli' backend."""
        # Session affinity — keep session on same backend
        if session and session.get("backend") == "cli":
            return "cli"

        # Explicit routing prefixes
        if message.startswith("!cli "):
            return "cli"
        if message.startswith("!api "):
            return "api"

        # Pattern matching for complex tasks
        if _CLI_PATTERNS.search(message):
            return "cli"
        if _MULTI_STEP.search(message):
            return "cli"

        # Long messages with action intent
        if len(message) > 500 and any(w in message.lower() for w in ("implement", "create", "write", "deploy")):
            return "cli"

        return self._default

    def get_model_display(self, model: str) -> str:
        return self._api.get_model_display(model)

    def call_sync(self, prompt: str, **kwargs):
        """Sync call — always use API (observers, background tasks)."""
        return self._api.call_sync(prompt, **kwargs)

    async def call_streaming(self, message: str, **kwargs):
        """Streaming call — route based on complexity."""
        session = kwargs.get("session")
        target = self._classify(message, session)

        # Strip routing prefix if present
        if message.startswith("!cli "):
            message = message[5:]
        elif message.startswith("!api "):
            message = message[5:]

        log.info("Hybrid routing: %s → %s", message[:60], target)

        if target == "cli":
            try:
                result = await self._cli.call_streaming(message, **kwargs)
                return result
            except Exception as e:
                log.warning("CLI backend failed (%s), falling back to API", e)

        return await self._api.call_streaming(message, **kwargs)
