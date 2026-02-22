"""Anthropic API backend — direct HTTP via the Anthropic SDK with tool support.

Implements prompt caching to minimise cost:
  - Static system prompt cached with cache_control breakpoint
  - Tool definitions cached (breakpoint on last tool)
  - Conversation history persisted in DB (session memory across turns)
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from anthropic import Anthropic, AsyncAnthropic
from db import get_conversation_history, save_conversation_history

from backends.tools import TOOL_SCHEMAS, ToolCall, run_tool_loop_sync, run_tool_loop_async

log = logging.getLogger("nexus")

# Reusable cache directive
_CACHE_EPHEMERAL = {"type": "ephemeral"}


def _build_system_blocks(
    system_prompt: str | None,
    memory_context: str | None,
    extra_system_prompt: str | None = None,
) -> list[dict] | None:
    """Build system prompt as a list of content blocks with cache_control.

    The static system prompt (instructions + context) is marked with
    cache_control so it is cached across turns and tool-loop iterations.
    Memory and extra prompts are separate uncached blocks (they may vary).
    """
    if not system_prompt and not memory_context and not extra_system_prompt:
        return None

    blocks = []
    if system_prompt:
        # Static prompt — cached (this is the bulk of the tokens)
        blocks.append({
            "type": "text",
            "text": system_prompt,
            "cache_control": _CACHE_EPHEMERAL,
        })
    if memory_context:
        # Memory changes between sessions — uncached
        blocks.append({"type": "text", "text": memory_context})
    if extra_system_prompt:
        # Voice mode etc. — uncached
        blocks.append({"type": "text", "text": extra_system_prompt})

    return blocks if blocks else None


def _anthropic_tools() -> list[dict]:
    """Convert OpenAI-style tool schemas to Anthropic format.

    The last tool gets a cache_control breakpoint so the entire tool
    definitions block is cached as a prefix.
    """
    tools = []
    for i, t in enumerate(TOOL_SCHEMAS):
        fn = t.get("function", {})
        tool_def = {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        }
        # Mark the last tool with cache_control
        if i == len(TOOL_SCHEMAS) - 1:
            tool_def["cache_control"] = _CACHE_EPHEMERAL
        tools.append(tool_def)
    return tools


def _log_usage(resp, label: str = "") -> None:
    """Log cache performance metrics from the API response."""
    usage = getattr(resp, "usage", None)
    if not usage:
        return
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_created = getattr(usage, "cache_creation_input_tokens", 0) or 0
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    total_input = cache_read + cache_created + input_tokens
    hit_pct = (cache_read / total_input * 100) if total_input > 0 else 0
    prefix = f"[{label}] " if label else ""
    log.info(
        "%sAPI usage: total_in=%d (cached=%d, written=%d, new=%d) out=%d cache_hit=%.0f%%",
        prefix, total_input, cache_read, cache_created, input_tokens,
        output_tokens, hit_pct,
    )


class AnthropicAPIBackend:
    """Backend that calls Anthropic Messages API with optional tool loop."""

    def __init__(self):
        from config import (
            ANTHROPIC_API_KEY,
            ANTHROPIC_MODEL,
            ANTHROPIC_MAX_TOKENS,
            ANTHROPIC_TOOLS_ENABLED,
            ANTHROPIC_TOOL_MAX_ITER,
            ANTHROPIC_TOOL_TIMEOUT,
            ANTHROPIC_TOTAL_TIMEOUT,
            CLAUDE_CWD,
        )

        self._api_key = ANTHROPIC_API_KEY
        self._default_model = ANTHROPIC_MODEL
        self._max_tokens = ANTHROPIC_MAX_TOKENS
        self._tools_enabled = ANTHROPIC_TOOLS_ENABLED
        self._max_iterations = ANTHROPIC_TOOL_MAX_ITER
        self._tool_timeout = ANTHROPIC_TOOL_TIMEOUT
        self._total_timeout = ANTHROPIC_TOTAL_TIMEOUT
        self._cwd = CLAUDE_CWD

        self._client = Anthropic(api_key=self._api_key) if self._api_key else None
        self._aclient = AsyncAnthropic(api_key=self._api_key) if self._api_key else None
        self._tools = _anthropic_tools() if self._tools_enabled else None

    @property
    def name(self) -> str:
        return "anthropic_api"

    def get_model_display(self, model: str) -> str:
        return self._resolve_model(model)

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
        # Map shorthand to configured default
        if model in ("sonnet", "opus", "haiku") or not model:
            return self._default_model
        return model

    def _require_client(self) -> tuple[Anthropic | None, str | None]:
        if not self._api_key:
            return None, "Anthropic API key not configured (ANTHROPIC_API_KEY)"
        if not self._client:
            return None, "Anthropic client not initialized"
        return self._client, None

    def _require_aclient(self) -> tuple[AsyncAnthropic | None, str | None]:
        if not self._api_key:
            return None, "Anthropic API key not configured (ANTHROPIC_API_KEY)"
        if not self._aclient:
            return None, "Anthropic async client not initialized"
        return self._aclient, None

    # ------------------------------------------------------------------
    # Response parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(resp) -> tuple[str, list[ToolCall], dict]:
        # Log cache metrics on every response
        _log_usage(resp, "anthropic")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        content_blocks: list[dict] = []

        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
                content_blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input or {},
                    )
                )
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input or {},
                    }
                )

        assistant_msg = {"role": "assistant", "content": content_blocks}
        return ("\n".join(text_parts).strip(), tool_calls, assistant_msg)

    @staticmethod
    def _format_tool_result(tool_name: str, call_id: str, result_str: str) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": result_str,
                }
            ],
        }

    # ------------------------------------------------------------------
    # Sync call
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
        client, err = self._require_client()
        if err:
            return {"result": err, "session_id": None}

        session_id = session_id or str(uuid.uuid4())
        model_id = self._resolve_model(model)
        system = _build_system_blocks(system_prompt, memory_context)

        history = get_conversation_history(session_id)
        messages = history + [{"role": "user", "content": prompt}]

        def send_request(msgs):
            kwargs = dict(
                model=model_id,
                max_tokens=self._max_tokens,
                messages=msgs,
                tools=self._tools,
                timeout=timeout,
            )
            if system:
                kwargs["system"] = system
            return client.messages.create(**kwargs)

        if self._tools_enabled:
            result = run_tool_loop_sync(
                messages,
                send_request,
                self._parse_response,
                self._format_tool_result,
                max_iterations=self._max_iterations,
                tool_timeout=self._tool_timeout,
                total_timeout=min(timeout, self._total_timeout),
                cwd=self._cwd,
            )
            result_text = result.get("result", "")
            if result_text:
                save_conversation_history(session_id, messages + [
                    {"role": "assistant", "content": result_text}
                ])
            result["session_id"] = session_id
            return result

        # No tools — single request
        try:
            resp = send_request(messages)
        except Exception as e:
            return {"result": f"Anthropic error: {e}", "session_id": session_id}

        text, _tool_calls, _assistant_msg = self._parse_response(resp)
        if text:
            save_conversation_history(session_id, messages + [
                {"role": "assistant", "content": text}
            ])
        return {"result": text or "(empty response)", "session_id": session_id}

    # ------------------------------------------------------------------
    # Async call (non-streaming response, but tool loop provides progress)
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
        aclient, err = self._require_aclient()
        if err:
            return {"result": err, "session_id": None, "written_files": []}

        session_id = session_id or str(uuid.uuid4())
        model_id = self._resolve_model(model)
        system = _build_system_blocks(system_prompt, memory_context, extra_system_prompt)

        history = get_conversation_history(session_id)
        messages = history + [{"role": "user", "content": message}]

        async def send_request(msgs):
            kwargs = dict(
                model=model_id,
                max_tokens=self._max_tokens,
                messages=msgs,
                tools=self._tools,
                timeout=self._total_timeout,
            )
            if system:
                kwargs["system"] = system
            return await aclient.messages.create(**kwargs)

        if self._tools_enabled:
            result = await run_tool_loop_async(
                messages,
                send_request,
                self._parse_response,
                self._format_tool_result,
                max_iterations=self._max_iterations,
                tool_timeout=self._tool_timeout,
                total_timeout=self._total_timeout,
                cwd=self._cwd,
                streaming_editor=streaming_editor,
                on_progress=on_progress,
            )
            result_text = result.get("result", "")
            if result_text:
                save_conversation_history(session_id, messages + [
                    {"role": "assistant", "content": result_text}
                ])
            result["session_id"] = session_id
            return result

        # No tools — single request
        try:
            resp = await send_request(messages)
        except Exception as e:
            return {"result": f"Anthropic error: {e}", "session_id": session_id, "written_files": []}

        text, _tool_calls, _assistant_msg = self._parse_response(resp)
        if text:
            save_conversation_history(session_id, messages + [
                {"role": "assistant", "content": text}
            ])
        return {"result": text or "(empty response)", "session_id": session_id, "written_files": []}
