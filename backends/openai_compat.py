"""OpenAI-compatible API backend — works with Grok, Mistral, vLLM, etc.

Supports tool use via the standard OpenAI function-calling protocol.
Schemas are already in OpenAI format so no conversion is needed.
"""

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
        return "openai_compat"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return self._tools_enabled

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

    def _get_tools(self) -> list[dict] | None:
        if not self._tools_enabled:
            return None
        from backends.tools import TOOL_SCHEMAS
        return TOOL_SCHEMAS

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

    # ------------------------------------------------------------------
    # Tool-loop adapter callbacks
    # ------------------------------------------------------------------

    def _parse_response(self, response: dict):
        """Parse OpenAI chat completion into (text, tool_calls, assistant_msg)."""
        from backends.tools import ToolCall

        choices = response.get("choices", [])
        if not choices:
            return "(no choices in response)", [], {"role": "assistant", "content": "(no choices)"}

        message = choices[0].get("message", {})
        text = message.get("content") or ""
        finish_reason = choices[0].get("finish_reason", "")

        tool_calls = []
        raw_tool_calls = message.get("tool_calls", [])
        for tc in raw_tool_calls:
            func = tc.get("function", {})
            args = func.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=func.get("name", ""),
                arguments=args,
            ))

        # Build assistant message to append (must include tool_calls if present)
        assistant_msg = {"role": "assistant", "content": text}
        if raw_tool_calls:
            assistant_msg["tool_calls"] = raw_tool_calls

        return text, tool_calls, assistant_msg

    @staticmethod
    def _format_tool_result(tool_name: str, call_id: str, result_str: str) -> dict:
        """Format a tool result as an OpenAI tool message."""
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": result_str,
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
        """Synchronous call to OpenAI-compatible API with tool loop."""
        model_id = self._resolve_model(model)
        messages = self._build_messages(prompt, system_prompt, memory_context)
        tools = self._get_tools()

        log.info(
            "OpenAI-compat call (sync): model=%s, tools=%s, prompt=%d chars",
            model_id, "enabled" if tools else "disabled", len(prompt),
        )

        if not tools:
            # No tools — single-shot
            payload = {"model": model_id, "messages": messages, "max_tokens": 4096}
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

        # Tools enabled — use shared tool loop
        from backends.tools import run_tool_loop_sync

        def send_request(msgs):
            payload = {
                "model": model_id,
                "messages": msgs,
                "max_tokens": 4096,
                "tools": tools,
            }
            return self._make_request(payload, timeout=timeout)

        try:
            return run_tool_loop_sync(
                messages,
                send_request,
                self._parse_response,
                self._format_tool_result,
                max_iterations=self._max_iterations,
                tool_timeout=self._tool_timeout,
                total_timeout=timeout,
                cwd=self._cwd,
            )
        except Exception as e:
            log.error("OpenAI-compat error: %s", e)
            return {"result": f"OpenAI-compat error: {e}", "session_id": None}

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
        """Async streaming call to OpenAI-compatible API with tool loop."""
        import asyncio

        model_id = self._resolve_model(model)
        messages = self._build_messages(message, system_prompt, memory_context, extra_system_prompt)
        tools = self._get_tools()

        log.info("OpenAI-compat call (streaming): model=%s, tools=%s", model_id, "enabled" if tools else "disabled")

        try:
            if not tools:
                # No tools — pure streaming
                payload = {
                    "model": model_id,
                    "messages": messages,
                    "max_tokens": 4096,
                    "stream": True,
                }
                try:
                    import aiohttp
                    return await self._stream_aiohttp(payload, streaming_editor)
                except ImportError:
                    loop = asyncio.get_event_loop()
                    return await loop.run_in_executor(None, lambda: self._stream_sync(payload))

            # Tools enabled: stream first turn, then tool loop with non-streaming
            first_response = await self._first_turn_streaming(
                model_id, messages, tools, streaming_editor,
            )

            text, tool_calls, assistant_msg = self._parse_response(first_response)
            messages.append(assistant_msg)

            if not tool_calls:
                return {
                    "result": text or "(empty response)",
                    "session_id": None,
                    "written_files": [],
                }

            # Execute first round of tool calls, then enter async tool loop
            from backends.tools import execute_tool, _format_tool_status, run_tool_loop_async
            written_files = []

            for tc in tool_calls:
                status = _format_tool_status(tc.name, tc.arguments)
                if streaming_editor:
                    await streaming_editor.add_tool_status(status)
                elif on_progress:
                    await on_progress(status)

                result_str, new_files = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda n=tc.name, a=tc.arguments: execute_tool(
                        n, a, timeout=self._tool_timeout, cwd=self._cwd,
                    ),
                )
                written_files.extend(new_files)
                messages.append(self._format_tool_result(tc.name, tc.id, result_str))

            # Continue with async tool loop for remaining iterations
            async def send_request_async(msgs):
                loop = asyncio.get_event_loop()
                payload = {
                    "model": model_id,
                    "messages": msgs,
                    "max_tokens": 4096,
                    "tools": tools,
                }
                return await loop.run_in_executor(
                    None, lambda: self._make_request(payload, timeout=300),
                )

            result = await run_tool_loop_async(
                messages,
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
            log.error("OpenAI-compat streaming error: %s", e)
            return {
                "result": f"OpenAI-compat error: {e}",
                "session_id": None,
                "written_files": [],
            }

    async def _first_turn_streaming(
        self, model_id: str, messages: list, tools: list, streaming_editor=None,
    ) -> dict:
        """Stream the first turn and collect the full response for tool-call detection."""
        import asyncio

        payload = {
            "model": model_id,
            "messages": messages,
            "max_tokens": 4096,
            "tools": tools,
            "stream": True,
        }

        result_text = ""
        tool_calls_acc: list[dict] = []
        finish_reason = ""

        try:
            import aiohttp
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

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
                        data_str = decoded[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            fr = chunk.get("choices", [{}])[0].get("finish_reason")
                            if fr:
                                finish_reason = fr

                            # Accumulate text
                            text = delta.get("content", "")
                            if text:
                                result_text += text
                                if streaming_editor:
                                    await streaming_editor.add_text(text)

                            # Accumulate tool calls from deltas
                            for tc_delta in delta.get("tool_calls", []):
                                idx = tc_delta.get("index", 0)
                                while len(tool_calls_acc) <= idx:
                                    tool_calls_acc.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                if "id" in tc_delta and tc_delta["id"]:
                                    tool_calls_acc[idx]["id"] = tc_delta["id"]
                                func_delta = tc_delta.get("function", {})
                                if "name" in func_delta and func_delta["name"]:
                                    tool_calls_acc[idx]["function"]["name"] = func_delta["name"]
                                if "arguments" in func_delta:
                                    tool_calls_acc[idx]["function"]["arguments"] += func_delta["arguments"]
                        except (json.JSONDecodeError, IndexError):
                            continue
        except ImportError:
            # Fallback: non-streaming first turn
            loop = asyncio.get_event_loop()
            payload["stream"] = False
            return await loop.run_in_executor(
                None, lambda: self._make_request(payload, timeout=300),
            )

        # Reconstruct as a non-streaming response dict
        message = {"content": result_text, "role": "assistant"}
        if tool_calls_acc:
            message["tool_calls"] = tool_calls_acc
        return {
            "choices": [{
                "message": message,
                "finish_reason": finish_reason or ("tool_calls" if tool_calls_acc else "stop"),
            }]
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
