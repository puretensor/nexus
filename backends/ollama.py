"""Ollama backend — local models via HTTP API."""

import json
import logging
import urllib.request
import urllib.error

log = logging.getLogger("nexus")

# Model name mapping: friendly name → Ollama model
_MODEL_MAP = {
    "sonnet": None,   # uses OLLAMA_MODEL from config
    "haiku": None,     # uses OLLAMA_MODEL from config
    "opus": None,      # uses OLLAMA_MODEL from config
}


class OllamaBackend:
    """Backend that calls a local Ollama instance via HTTP."""

    def __init__(self):
        from config import OLLAMA_URL, OLLAMA_MODEL
        self._base_url = OLLAMA_URL.rstrip("/")
        self._default_model = OLLAMA_MODEL

    @property
    def name(self) -> str:
        return "ollama"

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
        """Synchronous call to Ollama /api/generate."""
        model_id = self._resolve_model(model)

        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if memory_context:
            system_parts.append(memory_context)
        system = "\n\n".join(system_parts) if system_parts else ""

        payload = {
            "model": model_id,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system

        log.info("Ollama call (sync): model=%s, prompt=%d chars", model_id, len(prompt))

        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"{self._base_url}/api/generate",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode())
                return {
                    "result": result.get("response", "(empty response)"),
                    "session_id": None,
                }
        except urllib.error.URLError as e:
            log.error("Ollama connection error: %s", e)
            return {"result": f"Ollama error: {e}", "session_id": None}
        except Exception as e:
            log.error("Ollama error: %s", e)
            return {"result": f"Ollama error: {e}", "session_id": None}

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
        """Async streaming call to Ollama /api/generate with stream=True."""
        import asyncio

        model_id = self._resolve_model(model)

        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if memory_context:
            system_parts.append(memory_context)
        if extra_system_prompt:
            system_parts.append(extra_system_prompt)
        system = "\n\n".join(system_parts) if system_parts else ""

        payload = {
            "model": model_id,
            "prompt": message,
            "stream": True,
        }
        if system:
            payload["system"] = system

        log.info("Ollama call (streaming): model=%s", model_id)

        try:
            # Use aiohttp if available, fall back to sync in executor
            try:
                import aiohttp
                return await self._stream_aiohttp(payload, streaming_editor)
            except ImportError:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self._stream_sync(payload),
                )
                return result
        except Exception as e:
            log.error("Ollama streaming error: %s", e)
            return {
                "result": f"Ollama error: {e}",
                "session_id": None,
                "written_files": [],
            }

    async def _stream_aiohttp(self, payload: dict, streaming_editor=None) -> dict:
        """Stream using aiohttp for true async."""
        import aiohttp

        result_text = ""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._base_url}/api/generate",
                json=payload,
            ) as resp:
                async for line in resp.content:
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode())
                        text = chunk.get("response", "")
                        if text:
                            result_text += text
                            if streaming_editor:
                                await streaming_editor.add_text(text)
                    except json.JSONDecodeError:
                        continue

        return {
            "result": result_text or "(empty response)",
            "session_id": None,
            "written_files": [],
        }

    def _stream_sync(self, payload: dict) -> dict:
        """Fallback: stream synchronously (no real-time editor updates)."""
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )

        result_text = ""
        with urllib.request.urlopen(req, timeout=300) as resp:
            for line in resp:
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode())
                    result_text += chunk.get("response", "")
                except json.JSONDecodeError:
                    continue

        return {
            "result": result_text or "(empty response)",
            "session_id": None,
            "written_files": [],
        }
