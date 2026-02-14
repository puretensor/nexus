"""OpenAI-compatible API backend — works with Grok, Mistral, vLLM, etc."""

import json
import logging
import urllib.request
import urllib.error

log = logging.getLogger("nexus")

# Model name mapping: friendly name → defaults (overridden by OPENAI_COMPAT_MODEL)
_MODEL_MAP = {
    "sonnet": None,
    "haiku": None,
    "opus": None,
}


class OpenAICompatBackend:
    """Backend for any OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self):
        from config import OPENAI_COMPAT_URL, OPENAI_COMPAT_KEY, OPENAI_COMPAT_MODEL
        if not OPENAI_COMPAT_URL:
            raise ValueError("OPENAI_COMPAT_URL is required for openai_compat backend")
        self._base_url = OPENAI_COMPAT_URL.rstrip("/")
        self._api_key = OPENAI_COMPAT_KEY
        self._default_model = OPENAI_COMPAT_MODEL

    @property
    def name(self) -> str:
        return "openai_compat"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return False

    @property
    def supports_sessions(self) -> bool:
        return False

    def _resolve_model(self, model: str) -> str:
        return _MODEL_MAP.get(model) or self._default_model

    def _build_messages(
        self,
        prompt: str,
        system_prompt: str | None = None,
        memory_context: str | None = None,
        extra_system_prompt: str | None = None,
    ) -> list[dict]:
        messages = []
        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if memory_context:
            system_parts.append(memory_context)
        if extra_system_prompt:
            system_parts.append(extra_system_prompt)
        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _make_request(self, payload: dict, timeout: int = 300) -> dict:
        """Send a request to the OpenAI-compatible endpoint."""
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        req = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=data,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

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
        """Synchronous call to OpenAI-compatible API."""
        model_id = self._resolve_model(model)
        messages = self._build_messages(prompt, system_prompt, memory_context)

        payload = {
            "model": model_id,
            "messages": messages,
            "max_tokens": 4096,
        }

        log.info("OpenAI-compat call (sync): model=%s, prompt=%d chars", model_id, len(prompt))

        try:
            result = self._make_request(payload, timeout=timeout)
            choices = result.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
                return {"result": text or "(empty response)", "session_id": None}
            return {"result": "(no choices in response)", "session_id": None}
        except Exception as e:
            log.error("OpenAI-compat error: %s", e)
            return {"result": f"OpenAI-compat error: {e}", "session_id": None}

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
        """Async streaming call to OpenAI-compatible API with SSE."""
        import asyncio

        model_id = self._resolve_model(model)
        messages = self._build_messages(message, system_prompt, memory_context, extra_system_prompt)

        payload = {
            "model": model_id,
            "messages": messages,
            "max_tokens": 4096,
            "stream": True,
        }

        log.info("OpenAI-compat call (streaming): model=%s", model_id)

        try:
            try:
                import aiohttp
                return await self._stream_aiohttp(payload, streaming_editor)
            except ImportError:
                # Fall back to sync in executor
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self._stream_sync(payload),
                )
                return result
        except Exception as e:
            log.error("OpenAI-compat streaming error: %s", e)
            return {
                "result": f"OpenAI-compat error: {e}",
                "session_id": None,
                "written_files": [],
            }

    async def _stream_aiohttp(self, payload: dict, streaming_editor=None) -> dict:
        """Stream using aiohttp for true async SSE."""
        import aiohttp

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        result_text = ""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                async for line in resp.content:
                    decoded = line.decode().strip()
                    if not decoded or not decoded.startswith("data: "):
                        continue
                    data_str = decoded[6:]  # strip "data: "
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        text = delta.get("content", "")
                        if text:
                            result_text += text
                            if streaming_editor:
                                await streaming_editor.add_text(text)
                    except (json.JSONDecodeError, IndexError):
                        continue

        return {
            "result": result_text or "(empty response)",
            "session_id": None,
            "written_files": [],
        }

    def _stream_sync(self, payload: dict) -> dict:
        """Fallback: stream synchronously."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=data,
            headers=headers,
        )

        result_text = ""
        with urllib.request.urlopen(req, timeout=300) as resp:
            for line in resp:
                decoded = line.decode().strip()
                if not decoded or not decoded.startswith("data: "):
                    continue
                data_str = decoded[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    result_text += delta.get("content", "")
                except (json.JSONDecodeError, IndexError):
                    continue

        return {
            "result": result_text or "(empty response)",
            "session_id": None,
            "written_files": [],
        }
