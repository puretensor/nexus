"""Backend Protocol — structural subtyping for LLM backends.

Backends are stateless: system prompt and memory injection is handled
by engine.py before delegating to the backend.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Backend(Protocol):
    """Protocol for LLM backends.

    Implementations must provide call_sync, call_streaming, and property accessors.
    Uses structural subtyping — no inheritance required.
    """

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
        """Synchronous LLM call.

        Returns {"result": str, "session_id": str | None}
        """
        ...

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
        """Async streaming LLM call.

        Returns {"result": str, "session_id": str | None, "written_files": list}
        """
        ...

    @property
    def name(self) -> str:
        """Backend identifier (e.g. 'claude_code', 'anthropic_api')."""
        ...

    @property
    def supports_streaming(self) -> bool:
        """Whether this backend supports real-time streaming."""
        ...

    @property
    def supports_tools(self) -> bool:
        """Whether this backend supports tool use."""
        ...

    @property
    def supports_sessions(self) -> bool:
        """Whether this backend supports session resumption."""
        ...
