"""Google Gemini backend — via google-genai SDK with tool support.

Uses GOOGLE_API_KEY from environment. Mirrors the BedrockAPIBackend interface:
tool loop, history sanitization, streaming progress callbacks.

Google Gemini API docs:
  https://ai.google.dev/gemini-api/docs
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from google import genai
from google.genai import types as gtypes

from db import get_conversation_history, save_conversation_history
from backends.anthropic_api import _sanitize_history, _build_system_blocks
from backends.tools import TOOL_SCHEMAS, ToolCall, run_tool_loop_sync, run_tool_loop_async

log = logging.getLogger("nexus")

# Model map — short names to Gemini model IDs
_GEMINI_MODEL_MAP = {
    "sonnet": "gemini-2.5-flash",
    "opus": "gemini-2.5-pro",
    "haiku": "gemini-2.0-flash",
    # Legacy Bedrock IDs
    "us.anthropic.claude-sonnet-4-6": "gemini-2.5-flash",
    "us.anthropic.claude-opus-4-6": "gemini-2.5-pro",
    "us.anthropic.claude-haiku-4-5-20251001": "gemini-2.0-flash",
}

# Pricing per million tokens (USD) for cost logging
_PRICING = {
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.0-flash": (0.10, 0.40),
}


def _gemini_tools() -> list[gtypes.Tool]:
    """Convert OpenAI-style tool schemas to Gemini Tool format."""
    declarations = []
    for t in TOOL_SCHEMAS:
        fn = t.get("function", {})
        params = fn.get("parameters", {"type": "object", "properties": {}})
        declarations.append(gtypes.FunctionDeclaration(
            name=fn.get("name", ""),
            description=fn.get("description", ""),
            parameters=params,
        ))
    return [gtypes.Tool(function_declarations=declarations)]


def _convert_history_to_gemini(messages: list[dict]) -> list[gtypes.Content]:
    """Convert Anthropic-format history to Gemini Content objects.

    Anthropic uses:
      {"role": "user", "content": "text"} or
      {"role": "user", "content": [{"type": "text", ...}, ...]}
      {"role": "assistant", "content": [{"type": "text", ...}, {"type": "tool_use", ...}]}
      {"role": "user", "content": [{"type": "tool_result", ...}]}

    Gemini uses:
      Content(role="user", parts=[Part.from_text("...")])
      Content(role="model", parts=[Part.from_text("..."), Part.from_function_call(...)])
      Content(role="user", parts=[Part.from_function_response(...)])

    Gemini requires strictly alternating user/model roles.
    """
    raw: list[gtypes.Content] = []

    for msg in messages:
        role_raw = msg.get("role", "user")
        role = "model" if role_raw == "assistant" else "user"
        content = msg.get("content")

        if isinstance(content, str):
            raw.append(gtypes.Content(role=role, parts=[gtypes.Part.from_text(content)]))
            continue

        if not isinstance(content, list):
            raw.append(gtypes.Content(role=role, parts=[gtypes.Part.from_text(str(content))]))
            continue

        parts: list[gtypes.Part] = []
        for block in content:
            if isinstance(block, str):
                parts.append(gtypes.Part.from_text(block))
            elif not isinstance(block, dict):
                continue
            elif block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    parts.append(gtypes.Part.from_text(text))
            elif block.get("type") == "thinking":
                # Thinking blocks — include as text for context
                thinking = block.get("thinking", "")
                if thinking:
                    parts.append(gtypes.Part.from_text(f"[thinking]: {thinking[:500]}"))
            elif block.get("type") == "tool_use":
                parts.append(gtypes.Part.from_function_call(
                    name=block.get("name", ""),
                    args=block.get("input", {}),
                ))
            elif block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    result_text = result_content
                elif isinstance(result_content, list):
                    result_text = " ".join(
                        rb.get("text", "") if isinstance(rb, dict) else str(rb)
                        for rb in result_content
                    )
                else:
                    result_text = str(result_content)
                # tool_result needs to be a function_response — we need the tool name
                # The tool_use_id doesn't map to Gemini, so we use a generic name
                tool_name = block.get("_tool_name", "tool")
                parts.append(gtypes.Part.from_function_response(
                    name=tool_name,
                    response={"result": result_text[:8000]},
                ))

        if parts:
            raw.append(gtypes.Content(role=role, parts=parts))

    # Merge consecutive same-role messages (Gemini requires alternating roles)
    merged: list[gtypes.Content] = []
    for content in raw:
        if merged and merged[-1].role == content.role:
            merged[-1].parts.extend(content.parts)
        else:
            merged.append(content)

    # Ensure conversation starts with user and alternates
    if merged and merged[0].role == "model":
        merged.insert(0, gtypes.Content(role="user", parts=[gtypes.Part.from_text("(continue)")]))

    return merged


def _log_gemini_usage(usage_metadata, model_id: str, label: str = "") -> None:
    """Log token usage and estimated cost from Gemini response."""
    if not usage_metadata:
        return

    input_tokens = getattr(usage_metadata, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage_metadata, "candidates_token_count", 0) or 0

    prefix = f"[{label}] " if label else ""

    input_price, output_price = _PRICING.get(model_id, (0.15, 0.60))
    cost_in = input_tokens * input_price / 1_000_000
    cost_out = output_tokens * output_price / 1_000_000
    total_cost = cost_in + cost_out

    log.info(
        "%sGemini usage: in=%d out=%d cost=$%.4f",
        prefix, input_tokens, output_tokens, total_cost,
    )


class GeminiAPIBackend:
    """Backend that calls Gemini via google-genai SDK with tool loop."""

    def __init__(self):
        import os
        from config import (
            ANTHROPIC_TOOLS_ENABLED,
            ANTHROPIC_TOOL_MAX_ITER,
            ANTHROPIC_TOOL_TIMEOUT,
            ANTHROPIC_TOTAL_TIMEOUT,
            CLAUDE_CWD,
        )

        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY / GEMINI_API_KEY not set")

        self._client = genai.Client(api_key=api_key)
        self._default_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self._max_tokens = int(os.environ.get("GEMINI_MAX_TOKENS", "65536"))
        self._thinking_budget = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))
        self._tools_enabled = ANTHROPIC_TOOLS_ENABLED
        self._max_iterations = ANTHROPIC_TOOL_MAX_ITER
        self._tool_timeout = ANTHROPIC_TOOL_TIMEOUT
        self._total_timeout = ANTHROPIC_TOTAL_TIMEOUT
        self._cwd = CLAUDE_CWD

        self._tools = _gemini_tools() if self._tools_enabled else None

    @property
    def name(self) -> str:
        return "gemini_api"

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
        if not model:
            return self._default_model
        return _GEMINI_MODEL_MAP.get(model, model)

    # ------------------------------------------------------------------
    # Response parsing helpers
    # ------------------------------------------------------------------

    def _parse_response(self, response) -> tuple[str, list[ToolCall], dict]:
        """Parse Gemini response into (text, tool_calls, assistant_msg).

        Returns the assistant message in Anthropic-compatible format so it can
        be appended to the shared conversation history without conversion.
        """
        _log_gemini_usage(
            getattr(response, "usage_metadata", None),
            self._default_model,
            "gemini",
        )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        anthropic_blocks: list[dict] = []

        if not response.candidates:
            return ("", [], {"role": "assistant", "content": []})

        for part in response.candidates[0].content.parts:
            if hasattr(part, "thought") and part.thought:
                # Thinking content
                thought_text = part.text or ""
                anthropic_blocks.append({
                    "type": "thinking",
                    "thinking": thought_text,
                    "signature": "",
                })
            elif part.function_call:
                call_id = f"toolu_{uuid.uuid4().hex[:24]}"
                fc = part.function_call
                args = dict(fc.args) if fc.args else {}
                tool_calls.append(ToolCall(
                    id=call_id,
                    name=fc.name,
                    arguments=args,
                ))
                anthropic_blocks.append({
                    "type": "tool_use",
                    "id": call_id,
                    "name": fc.name,
                    "input": args,
                })
            elif part.text:
                text_parts.append(part.text)
                anthropic_blocks.append({"type": "text", "text": part.text})

        assistant_msg = {"role": "assistant", "content": anthropic_blocks}
        return ("\n".join(text_parts).strip(), tool_calls, assistant_msg)

    @staticmethod
    def _format_tool_result(tool_name: str, call_id: str, result_str: str) -> dict:
        """Format tool result in Anthropic-compatible format for history."""
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "_tool_name": tool_name,  # Extra field for Gemini conversion
                    "content": result_str,
                }
            ],
        }

    # ------------------------------------------------------------------
    # Core API call
    # ------------------------------------------------------------------

    def _build_config(
        self,
        model_id: str,
        system_prompt: str | None,
        memory_context: str | None,
        extra_system_prompt: str | None = None,
    ) -> gtypes.GenerateContentConfig:
        """Build Gemini GenerateContentConfig."""
        # Build system instruction from components
        system_blocks = _build_system_blocks(system_prompt, memory_context, extra_system_prompt)
        system_text = "\n\n".join(b.get("text", "") for b in system_blocks if b.get("text"))

        config_kwargs = {
            "max_output_tokens": self._max_tokens,
        }

        if system_text:
            config_kwargs["system_instruction"] = system_text

        if self._tools_enabled and self._tools:
            config_kwargs["tools"] = self._tools

        # Thinking budget — Gemini 2.5 models support thinking
        if self._thinking_budget and self._thinking_budget > 0:
            config_kwargs["thinking_config"] = gtypes.ThinkingConfig(
                thinking_budget=self._thinking_budget,
            )
        else:
            # No thinking → safe to set temperature
            config_kwargs["temperature"] = 0.7

        return gtypes.GenerateContentConfig(**config_kwargs)

    def _generate(self, model_id: str, messages: list[dict], **system_kw):
        """Synchronous generate call."""
        config = self._build_config(model_id, **system_kw)
        gemini_contents = _convert_history_to_gemini(messages)
        return self._client.models.generate_content(
            model=model_id,
            contents=gemini_contents,
            config=config,
        )

    async def _generate_async(self, model_id: str, messages: list[dict], **system_kw):
        """Async generate call (runs sync client in thread pool)."""
        config = self._build_config(model_id, **system_kw)
        gemini_contents = _convert_history_to_gemini(messages)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._client.models.generate_content(
                model=model_id,
                contents=gemini_contents,
                config=config,
            ),
        )

    async def _stream_request(
        self,
        messages: list[dict],
        model_id: str,
        streaming_editor=None,
        **system_kw,
    ) -> tuple[str, list, dict]:
        """Send request via generate_content_stream and consume with streaming.

        Returns (text, tool_calls, assistant_msg) — same as _parse_response().
        """
        config = self._build_config(model_id, **system_kw)
        gemini_contents = _convert_history_to_gemini(messages)
        loop = asyncio.get_event_loop()

        def _sync_stream():
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            anthropic_blocks: list[dict] = []
            usage_metadata = None

            response_stream = self._client.models.generate_content_stream(
                model=model_id,
                contents=gemini_contents,
                config=config,
            )

            for chunk in response_stream:
                usage_metadata = getattr(chunk, "usage_metadata", usage_metadata)

                if not chunk.candidates:
                    continue

                for part in chunk.candidates[0].content.parts:
                    if hasattr(part, "thought") and part.thought:
                        anthropic_blocks.append({
                            "type": "thinking",
                            "thinking": part.text or "",
                            "signature": "",
                        })
                    elif part.function_call:
                        call_id = f"toolu_{uuid.uuid4().hex[:24]}"
                        fc = part.function_call
                        args = dict(fc.args) if fc.args else {}
                        tool_calls.append(ToolCall(
                            id=call_id,
                            name=fc.name,
                            arguments=args,
                        ))
                        anthropic_blocks.append({
                            "type": "tool_use",
                            "id": call_id,
                            "name": fc.name,
                            "input": args,
                        })
                    elif part.text:
                        text_parts.append(part.text)
                        # Stream text to editor
                        if streaming_editor and loop:
                            try:
                                future = asyncio.run_coroutine_threadsafe(
                                    streaming_editor.add_text(part.text), loop
                                )
                                future.result(timeout=2)
                            except Exception:
                                pass

            # Final text assembly
            full_text = "".join(text_parts).strip()
            if full_text:
                # Collapse text parts into a single block
                anthropic_blocks = [
                    b for b in anthropic_blocks
                    if b.get("type") != "text"
                ] + [{"type": "text", "text": full_text}] if full_text else anthropic_blocks

            if usage_metadata:
                _log_gemini_usage(usage_metadata, model_id, "gemini-stream")

            assistant_msg = {"role": "assistant", "content": anthropic_blocks}
            return (full_text, tool_calls, assistant_msg)

        return await loop.run_in_executor(None, _sync_stream)

    # ------------------------------------------------------------------
    # Sync call (for observers)
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
        session_id = session_id or str(uuid.uuid4())
        model_id = self._resolve_model(model)

        return self._call_sync_inner(
            prompt, model_id=model_id, session_id=session_id,
            timeout=timeout, system_prompt=system_prompt,
            memory_context=memory_context,
        )

    def _call_sync_inner(
        self,
        prompt: str,
        *,
        model_id: str,
        session_id: str,
        timeout: int,
        system_prompt: str | None,
        memory_context: str | None,
    ) -> dict:
        from context_compression import compress_tool_results, compress_history
        history = _sanitize_history(get_conversation_history(session_id))
        history = compress_tool_results(history)
        history = compress_history(history)
        messages = history + [{"role": "user", "content": prompt}]

        system_kw = dict(
            system_prompt=system_prompt,
            memory_context=memory_context,
        )

        def send_request(msgs):
            return self._generate(model_id, msgs, **system_kw)

        if self._tools_enabled:
            try:
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
            except Exception as e:
                log.error("Gemini tool loop error (sync): %s", e)
                return {"result": f"Gemini error: {e}", "session_id": session_id}
            result_text = result.get("result", "")
            if result_text:
                save_conversation_history(session_id, messages)
            result["session_id"] = session_id
            return result

        # No tools — single request
        try:
            resp = send_request(messages)
        except Exception as e:
            return {"result": f"Gemini error: {e}", "session_id": session_id}

        text, _tool_calls, _assistant_msg = self._parse_response(resp)
        if text:
            save_conversation_history(session_id, messages + [
                {"role": "assistant", "content": text}
            ])
        return {"result": text or "(empty response)", "session_id": session_id}

    # ------------------------------------------------------------------
    # Async call (for Telegram with progress)
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
        session_id = session_id or str(uuid.uuid4())
        model_id = self._resolve_model(model)

        from context_compression import compress_tool_results, compress_history
        history = _sanitize_history(get_conversation_history(session_id))
        history = compress_tool_results(history)
        history = compress_history(history)
        messages = history + [{"role": "user", "content": message}]

        system_kw = dict(
            system_prompt=system_prompt,
            memory_context=memory_context,
            extra_system_prompt=extra_system_prompt,
        )

        async def send_and_parse_stream(msgs, editor):
            return await self._stream_request(
                msgs, model_id, streaming_editor=editor, **system_kw,
            )

        async def send_request(msgs):
            return await self._generate_async(model_id, msgs, **system_kw)

        if self._tools_enabled:
            try:
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
                    send_and_parse_stream=send_and_parse_stream,
                )
            except Exception as e:
                log.error("Gemini tool loop error (async): %s", e)
                return {"result": f"Gemini error: {e}", "session_id": session_id, "written_files": []}
            result_text = result.get("result", "")
            if result_text:
                save_conversation_history(session_id, messages)
            result["session_id"] = session_id
            return result

        # No tools — single streaming request
        try:
            text, _tool_calls, _assistant_msg = await send_and_parse_stream(
                messages, streaming_editor,
            )
        except Exception as e:
            return {"result": f"Gemini error: {e}", "session_id": session_id, "written_files": []}

        if text:
            save_conversation_history(session_id, messages + [
                {"role": "assistant", "content": text}
            ])
        return {"result": text or "(empty response)", "session_id": session_id, "written_files": []}
