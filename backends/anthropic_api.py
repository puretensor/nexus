"""Anthropic Messages API backend — HTTP-based, pay-per-token."""

import json
import logging

log = logging.getLogger("nexus")

# Model name mapping: friendly name → Anthropic model ID
_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


class AnthropicAPIBackend:
    """Backend using the Anthropic Messages API directly."""

    def __init__(self):
        from config import ANTHROPIC_API_KEY
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is required for anthropic_api backend")
        self._api_key = ANTHROPIC_API_KEY

    @property
    def name(self) -> str:
        return "anthropic_api"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return False  # Tool use not implemented in this backend

    @property
    def supports_sessions(self) -> bool:
        return False  # No session resumption via API

    def _resolve_model(self, model: str) -> str:
        return _MODEL_MAP.get(model, model)

    def _get_client(self):
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "The 'anthropic' package is required for the anthropic_api backend. "
                "Install it with: pip install anthropic"
            )
        return anthropic.Anthropic(api_key=self._api_key)

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
        """Synchronous call via Anthropic Messages API."""
        client = self._get_client()
        model_id = self._resolve_model(model)

        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if memory_context:
            system_parts.append(memory_context)
        system = "\n\n".join(system_parts) if system_parts else None

        kwargs = {
            "model": model_id,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        log.info("Anthropic API call (sync): model=%s, prompt=%d chars", model_id, len(prompt))

        try:
            response = client.messages.create(**kwargs)
            result_text = ""
            for block in response.content:
                if block.type == "text":
                    result_text += block.text
            return {"result": result_text or "(empty response)", "session_id": None}
        except Exception as e:
            log.error("Anthropic API error: %s", e)
            return {"result": f"Anthropic API error: {e}", "session_id": None}

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
        """Async streaming call via Anthropic Messages API with SSE."""
        client = self._get_client()
        model_id = self._resolve_model(model)

        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if memory_context:
            system_parts.append(memory_context)
        if extra_system_prompt:
            system_parts.append(extra_system_prompt)
        system = "\n\n".join(system_parts) if system_parts else None

        kwargs = {
            "model": model_id,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": message}],
        }
        if system:
            kwargs["system"] = system

        log.info("Anthropic API call (streaming): model=%s", model_id)

        try:
            result_text = ""
            with client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    result_text += text
                    if streaming_editor:
                        await streaming_editor.add_text(text)

            return {
                "result": result_text or "(empty response)",
                "session_id": None,
                "written_files": [],
            }
        except Exception as e:
            log.error("Anthropic API streaming error: %s", e)
            return {
                "result": f"Anthropic API error: {e}",
                "session_id": None,
                "written_files": [],
            }
