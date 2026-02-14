"""Google Gemini REST API backend — pure HTTP, no SDK dependency.

Supports tool use via Gemini's functionDeclarations / functionCall /
functionResponse protocol.  Multi-turn conversation is maintained as
a growing ``contents`` list with alternating user/model roles.
"""

import json
import logging
import urllib.request
import urllib.error
import uuid

log = logging.getLogger("nexus")

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _convert_schemas_to_gemini(openai_schemas: list[dict]) -> list[dict]:
    """Convert OpenAI function-calling schemas to Gemini functionDeclarations.

    OpenAI:  {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    Gemini:  {"name": ..., "description": ..., "parameters": ...}
    """
    decls = []
    for schema in openai_schemas:
        func = schema.get("function", {})
        decls.append({
            "name": func["name"],
            "description": func.get("description", ""),
            "parameters": func.get("parameters", {"type": "object", "properties": {}}),
        })
    return decls


class GeminiAPIBackend:
    """Backend using the Google Gemini REST API directly."""

    def __init__(self):
        from config import GEMINI_API_KEY, GEMINI_API_MODEL
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is required for gemini_api backend")
        self._api_key = GEMINI_API_KEY
        self._default_model = GEMINI_API_MODEL

        # Tool calling config
        try:
            from config import API_TOOLS_ENABLED, API_TOOL_MAX_ITER, API_TOOL_TIMEOUT
            self._tools_enabled = API_TOOLS_ENABLED
            self._max_iterations = API_TOOL_MAX_ITER
            self._tool_timeout = API_TOOL_TIMEOUT
        except ImportError:
            self._tools_enabled = True
            self._max_iterations = 25
            self._tool_timeout = 30
        try:
            from config import CLAUDE_CWD
            self._cwd = CLAUDE_CWD
        except ImportError:
            self._cwd = "/home/puretensorai"

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
        return self._tools_enabled

    @property
    def supports_sessions(self) -> bool:
        return False

    def _get_tools_payload(self) -> list[dict] | None:
        """Return Gemini tools payload or None."""
        if not self._tools_enabled:
            return None
        from backends.tools import TOOL_SCHEMAS
        decls = _convert_schemas_to_gemini(TOOL_SCHEMAS)
        return [{"functionDeclarations": decls}]

    def _build_payload(
        self,
        prompt: str,
        system_prompt: str | None = None,
        memory_context: str | None = None,
        extra_system_prompt: str | None = None,
        tools_payload: list[dict] | None = None,
    ) -> dict:
        """Build Gemini generateContent request payload."""
        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
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

        if tools_payload:
            payload["tools"] = tools_payload

        return payload

    def _endpoint(self, stream: bool = False) -> str:
        """Return the API endpoint URL."""
        if stream:
            return f"{_BASE_URL}/{self._default_model}:streamGenerateContent?alt=sse&key={self._api_key}"
        return f"{_BASE_URL}/{self._default_model}:generateContent?key={self._api_key}"

    def _send_gemini_request(self, payload: dict, timeout: int = 300) -> dict:
        """Send a generateContent request and return parsed JSON."""
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._endpoint(stream=False),
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    # ------------------------------------------------------------------
    # Tool-loop adapter callbacks
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(response: dict):
        """Parse Gemini generateContent response into (text, tool_calls, assistant_msg).

        Returns the assistant message in Gemini multi-turn format:
        {"role": "model", "parts": [...]}
        """
        from backends.tools import ToolCall

        candidates = response.get("candidates", [])
        if not candidates:
            empty_msg = {"role": "model", "parts": [{"text": "(no candidates)"}]}
            return "(no candidates in response)", [], empty_msg

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])

        text_parts = []
        tool_calls = []
        serialized_parts = []

        for part in parts:
            if "text" in part:
                text_parts.append(part["text"])
                serialized_parts.append({"text": part["text"]})
            elif "functionCall" in part:
                fc = part["functionCall"]
                call_id = str(uuid.uuid4())  # Gemini has no call IDs
                tool_calls.append(ToolCall(
                    id=call_id,
                    name=fc.get("name", ""),
                    arguments=fc.get("args", {}),
                ))
                serialized_parts.append({"functionCall": fc})

        text = "".join(text_parts)
        assistant_msg = {"role": "model", "parts": serialized_parts or [{"text": ""}]}
        return text, tool_calls, assistant_msg

    @staticmethod
    def _format_tool_result(tool_name: str, call_id: str, result_str: str) -> dict:
        """Format a tool result as a Gemini functionResponse user message."""
        return {
            "role": "user",
            "parts": [{
                "functionResponse": {
                    "name": tool_name,
                    "response": {"result": result_str},
                }
            }],
        }

    # ------------------------------------------------------------------
    # Synchronous call with tool loop
    # ------------------------------------------------------------------

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
        """Synchronous call to Gemini API with tool loop."""
        tools_payload = self._get_tools_payload()
        payload = self._build_payload(prompt, system_prompt, memory_context, tools_payload=tools_payload)

        log.info(
            "Gemini API call (sync): model=%s, tools=%s, prompt=%d chars",
            self._default_model, "enabled" if tools_payload else "disabled", len(prompt),
        )

        if not tools_payload:
            # No tools — single-shot
            try:
                result = self._send_gemini_request(payload, timeout=timeout)
                candidates = result.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts)
                    return {"result": text or "(empty response)", "session_id": None}
                return {"result": "(no candidates in response)", "session_id": None}
            except Exception as e:
                log.error("Gemini API error: %s", e)
                return {"result": f"Gemini API error: {e}", "session_id": None}

        # Tools enabled — use shared tool loop
        # Extract the contents list and system instruction for multi-turn
        from backends.tools import run_tool_loop_sync

        contents = payload["contents"]  # mutable list
        system_instruction = payload.get("systemInstruction")

        def send_request(msgs):
            req_payload = {
                "contents": msgs,
                "generationConfig": {"maxOutputTokens": 8192},
                "tools": tools_payload,
            }
            if system_instruction:
                req_payload["systemInstruction"] = system_instruction
            return self._send_gemini_request(req_payload, timeout=timeout)

        try:
            return run_tool_loop_sync(
                contents,
                send_request,
                self._parse_response,
                self._format_tool_result,
                max_iterations=self._max_iterations,
                tool_timeout=self._tool_timeout,
                total_timeout=timeout,
                cwd=self._cwd,
            )
        except Exception as e:
            log.error("Gemini API error: %s", e)
            return {"result": f"Gemini API error: {e}", "session_id": None}

    # ------------------------------------------------------------------
    # Async streaming call with tool loop
    # ------------------------------------------------------------------

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
        """Async streaming call to Gemini API with tool loop."""
        import asyncio

        tools_payload = self._get_tools_payload()
        payload = self._build_payload(
            message, system_prompt, memory_context, extra_system_prompt,
            tools_payload=tools_payload,
        )

        log.info("Gemini API call (streaming): model=%s, tools=%s", self._default_model, "enabled" if tools_payload else "disabled")

        try:
            if not tools_payload:
                # No tools — pure streaming
                try:
                    import aiohttp
                    return await self._stream_aiohttp(payload, streaming_editor)
                except ImportError:
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(
                        None, lambda: self._stream_sync(payload)
                    )
                    return result

            # Tools enabled: first turn non-streaming, then tool loop
            loop = asyncio.get_event_loop()
            contents = payload["contents"]
            system_instruction = payload.get("systemInstruction")

            first_response = await loop.run_in_executor(
                None, lambda: self._send_gemini_request(payload, timeout=300),
            )

            text, tool_calls, assistant_msg = self._parse_response(first_response)
            contents.append(assistant_msg)

            if text and streaming_editor:
                await streaming_editor.add_text(text)

            if not tool_calls:
                return {
                    "result": text or "(empty response)",
                    "session_id": None,
                    "written_files": [],
                }

            # Execute first round of tool calls
            from backends.tools import execute_tool, _format_tool_status, run_tool_loop_async
            written_files = []

            for tc in tool_calls:
                status = _format_tool_status(tc.name, tc.arguments)
                if streaming_editor:
                    await streaming_editor.add_tool_status(status)
                elif on_progress:
                    await on_progress(status)

                result_str, new_files = await loop.run_in_executor(
                    None,
                    lambda n=tc.name, a=tc.arguments: execute_tool(
                        n, a, timeout=self._tool_timeout, cwd=self._cwd,
                    ),
                )
                written_files.extend(new_files)
                contents.append(self._format_tool_result(tc.name, tc.id, result_str))

            # Continue with async tool loop
            async def send_request_async(msgs):
                req_payload = {
                    "contents": msgs,
                    "generationConfig": {"maxOutputTokens": 8192},
                    "tools": tools_payload,
                }
                if system_instruction:
                    req_payload["systemInstruction"] = system_instruction
                return await loop.run_in_executor(
                    None, lambda: self._send_gemini_request(req_payload, timeout=300),
                )

            result = await run_tool_loop_async(
                contents,
                send_request_async,
                self._parse_response,
                self._format_tool_result,
                max_iterations=self._max_iterations - 1,
                tool_timeout=self._tool_timeout,
                total_timeout=300,
                cwd=self._cwd,
                streaming_editor=streaming_editor,
                on_progress=on_progress,
            )
            result["written_files"] = written_files + result.get("written_files", [])
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
