"""Google Gemini REST API backend — pure HTTP, no SDK dependency."""

import json
import logging
import urllib.request
import urllib.error

log = logging.getLogger("nexus")

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiAPIBackend:
    """Backend using the Google Gemini REST API directly."""

    def __init__(self):
        from config import GEMINI_API_KEY, GEMINI_API_MODEL
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is required for gemini_api backend")
        self._api_key = GEMINI_API_KEY
        self._default_model = GEMINI_API_MODEL

    def get_model_display(self, model: str) -> str:
        return self._default_model

    @property
    def name(self) -> str:
        return "gemini_api"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return False

    @property
    def supports_sessions(self) -> bool:
        return False

    def _build_payload(
        self,
        prompt: str,
        system_prompt: str | None = None,
        memory_context: str | None = None,
        extra_system_prompt: str | None = None,
    ) -> dict:
        """Build Gemini generateContent request payload."""
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 8192},
        }

        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if memory_context:
            system_parts.append(memory_context)
        if extra_system_prompt:
            system_parts.append(extra_system_prompt)
        if system_parts:
            payload["systemInstruction"] = {
                "parts": [{"text": "\n\n".join(system_parts)}]
            }

        return payload

    def _endpoint(self, stream: bool = False) -> str:
        """Return the API endpoint URL."""
        if stream:
            return f"{_BASE_URL}/{self._default_model}:streamGenerateContent?alt=sse&key={self._api_key}"
        return f"{_BASE_URL}/{self._default_model}:generateContent?key={self._api_key}"

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
        """Synchronous call to Gemini API."""
        payload = self._build_payload(prompt, system_prompt, memory_context)
        data = json.dumps(payload).encode()

        log.info(
            "Gemini API call (sync): model=%s, prompt=%d chars",
            self._default_model,
            len(prompt),
        )

        try:
            req = urllib.request.Request(
                self._endpoint(stream=False),
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode())

            candidates = result.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts)
                return {"result": text or "(empty response)", "session_id": None}
            return {"result": "(no candidates in response)", "session_id": None}
        except Exception as e:
            log.error("Gemini API error: %s", e)
            return {"result": f"Gemini API error: {e}", "session_id": None}

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
        """Async streaming call to Gemini API with SSE."""
        import asyncio

        payload = self._build_payload(
            message, system_prompt, memory_context, extra_system_prompt
        )

        log.info("Gemini API call (streaming): model=%s", self._default_model)

        try:
            try:
                import aiohttp
                return await self._stream_aiohttp(payload, streaming_editor)
            except ImportError:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, lambda: self._stream_sync(payload)
                )
                return result
        except Exception as e:
            log.error("Gemini API streaming error: %s", e)
            return {
                "result": f"Gemini API error: {e}",
                "session_id": None,
                "written_files": [],
            }

    async def _stream_aiohttp(self, payload: dict, streaming_editor=None) -> dict:
        """Stream using aiohttp for true async SSE."""
        import aiohttp

        result_text = ""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._endpoint(stream=True),
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                async for line in resp.content:
                    decoded = line.decode().strip()
                    if not decoded or not decoded.startswith("data: "):
                        continue
                    data_str = decoded[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        candidates = chunk.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            text = "".join(p.get("text", "") for p in parts)
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
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._endpoint(stream=True),
            data=data,
            headers={"Content-Type": "application/json"},
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
                    candidates = chunk.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        result_text += "".join(p.get("text", "") for p in parts)
                except (json.JSONDecodeError, IndexError):
                    continue

        return {
            "result": result_text or "(empty response)",
            "session_id": None,
            "written_files": [],
        }
