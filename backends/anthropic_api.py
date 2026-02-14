"""Anthropic Messages API backend — HTTP-based, pay-per-token.

Supports tool use via the Anthropic tool_use protocol.  Schemas are
converted from OpenAI format (parameters → input_schema) at init time.
"""

import json
import logging

log = logging.getLogger("nexus")

# Model name mapping: friendly name → Anthropic model ID
_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _convert_schemas_to_anthropic(openai_schemas: list[dict]) -> list[dict]:
    """Convert OpenAI function-calling schemas to Anthropic tool format.

    OpenAI:   {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    Anthropic: {"name": ..., "description": ..., "input_schema": ...}
    """
    result = []
    for schema in openai_schemas:
        func = schema.get("function", {})
        result.append({
            "name": func["name"],
            "description": func.get("description", ""),
            "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


class AnthropicAPIBackend:
    """Backend using the Anthropic Messages API directly."""

    def __init__(self):
        from config import ANTHROPIC_API_KEY
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is required for anthropic_api backend")
        self._api_key = ANTHROPIC_API_KEY

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

    @property
    def name(self) -> str:
        return "anthropic_api"

    def get_model_display(self, model: str) -> str:
        _LABELS = {"opus": "Opus 4.6", "sonnet": "Sonnet 4.5", "haiku": "Haiku 4.5"}
        return _LABELS.get(model, model)

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return self._tools_enabled

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

    def _get_tools(self) -> list[dict] | None:
        if not self._tools_enabled:
            return None
        from backends.tools import TOOL_SCHEMAS
        return _convert_schemas_to_anthropic(TOOL_SCHEMAS)

    # ------------------------------------------------------------------
    # Tool-loop adapter callbacks
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(response):
        """Parse Anthropic Messages response into (text, tool_calls, assistant_msg).

        response is an anthropic.types.Message object.
        """
        from backends.tools import ToolCall

        text_parts = []
        tool_calls = []

        # Serialize the content blocks for the assistant message
        content_serialized = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
                content_serialized.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {},
                ))
                content_serialized.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input if isinstance(block.input, dict) else {},
                })

        text = "\n".join(text_parts)
        assistant_msg = {"role": "assistant", "content": content_serialized}
        return text, tool_calls, assistant_msg

    @staticmethod
    def _format_tool_result(tool_name: str, call_id: str, result_str: str) -> dict:
        """Format a tool result as an Anthropic tool_result user message."""
        return {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": result_str,
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
        """Synchronous call via Anthropic Messages API with tool loop."""
        client = self._get_client()
        model_id = self._resolve_model(model)
        tools = self._get_tools()

        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if memory_context:
            system_parts.append(memory_context)
        system = "\n\n".join(system_parts) if system_parts else None

        messages = [{"role": "user", "content": prompt}]

        log.info(
            "Anthropic API call (sync): model=%s, tools=%s, prompt=%d chars",
            model_id, "enabled" if tools else "disabled", len(prompt),
        )

        if not tools:
            # No tools — single-shot
            kwargs = {
                "model": model_id,
                "max_tokens": 4096,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system

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

        # Tools enabled — use shared tool loop
        from backends.tools import run_tool_loop_sync

        def send_request(msgs):
            kwargs = {
                "model": model_id,
                "max_tokens": 16384,
                "messages": msgs,
                "tools": tools,
            }
            if system:
                kwargs["system"] = system
            return client.messages.create(**kwargs)

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
            log.error("Anthropic API error: %s", e)
            return {"result": f"Anthropic API error: {e}", "session_id": None}

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
        """Async streaming call via Anthropic Messages API with tool loop."""
        import asyncio

        client = self._get_client()
        model_id = self._resolve_model(model)
        tools = self._get_tools()

        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if memory_context:
            system_parts.append(memory_context)
        if extra_system_prompt:
            system_parts.append(extra_system_prompt)
        system = "\n\n".join(system_parts) if system_parts else None

        messages = [{"role": "user", "content": message}]

        log.info("Anthropic API call (streaming): model=%s, tools=%s", model_id, "enabled" if tools else "disabled")

        try:
            if not tools:
                # No tools — pure streaming
                kwargs = {
                    "model": model_id,
                    "max_tokens": 4096,
                    "messages": messages,
                }
                if system:
                    kwargs["system"] = system

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

            # Tools enabled: stream first turn, then tool loop
            first_kwargs = {
                "model": model_id,
                "max_tokens": 16384,
                "messages": messages,
                "tools": tools,
            }
            if system:
                first_kwargs["system"] = system

            # Stream first turn to get text + detect tool calls
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.messages.create(**first_kwargs),
            )

            text, tool_calls, assistant_msg = self._parse_response(response)
            messages.append(assistant_msg)

            # Stream the text from first response
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
                messages.append(self._format_tool_result(tc.name, tc.id, result_str))

            # Continue with async tool loop for remaining iterations
            async def send_request_async(msgs):
                kwargs = {
                    "model": model_id,
                    "max_tokens": 16384,
                    "messages": msgs,
                    "tools": tools,
                }
                if system:
                    kwargs["system"] = system
                return await loop.run_in_executor(
                    None, lambda: client.messages.create(**kwargs),
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
            log.error("Anthropic API streaming error: %s", e)
            return {
                "result": f"Anthropic API error: {e}",
                "session_id": None,
                "written_files": [],
            }
