"""AWS Bedrock backend — Claude via boto3 Converse API with tool support.

Uses existing AWS credentials (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
from environment. Mirrors the AnthropicAPIBackend interface: tool loop,
history sanitization, prompt caching hints, streaming progress callbacks.

Bedrock Converse API docs:
  https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import boto3

from db import get_conversation_history, save_conversation_history
from backends.anthropic_api import _sanitize_history, _build_system_blocks
from backends.tools import TOOL_SCHEMAS, ToolCall, run_tool_loop_sync, run_tool_loop_async

log = logging.getLogger("nexus")

# Bedrock model ID map
_BEDROCK_MODEL_MAP = {
    "sonnet": "us.anthropic.claude-sonnet-4-6",
    "opus": "us.anthropic.claude-opus-4-6",
    "haiku": "us.anthropic.claude-haiku-4-5-20251001",
}

# Pricing per million tokens (USD) for cost logging
_PRICING = {
    "us.anthropic.claude-sonnet-4-6": (3.0, 15.0),
    "us.anthropic.claude-opus-4-6": (15.0, 75.0),
    "us.anthropic.claude-haiku-4-5-20251001": (0.80, 4.0),
}


def _bedrock_tools() -> list[dict]:
    """Convert OpenAI-style tool schemas to Bedrock toolConfig format."""
    tools = []
    for t in TOOL_SCHEMAS:
        fn = t.get("function", {})
        params = fn.get("parameters", {"type": "object", "properties": {}})
        tools.append({
            "toolSpec": {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "inputSchema": {"json": params},
            }
        })
    return tools


def _convert_history_to_bedrock(messages: list[dict]) -> list[dict]:
    """Convert Anthropic-format history to Bedrock Converse format.

    Anthropic uses:
      {"role": "user", "content": "text"} or
      {"role": "user", "content": [{"type": "text", "text": ...}, ...]}
      {"role": "assistant", "content": [{"type": "text", ...}, {"type": "tool_use", ...}]}
      {"role": "user", "content": [{"type": "tool_result", "tool_use_id": ..., "content": ...}]}

    Bedrock uses:
      {"role": "user", "content": [{"text": "..."}]}
      {"role": "assistant", "content": [{"text": "..."}, {"toolUse": {"toolUseId": ..., "name": ..., "input": ...}}]}
      {"role": "user", "content": [{"toolResult": {"toolUseId": ..., "content": [{"text": "..."}]}}]}

    Bedrock requires strictly alternating user/assistant roles. The tool loop
    may produce consecutive user messages (one per tool_result), so we merge
    consecutive same-role messages into one.
    """
    raw = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        if isinstance(content, str):
            raw.append({"role": role, "content": [{"text": content}]})
            continue

        if not isinstance(content, list):
            raw.append({"role": role, "content": [{"text": str(content)}]})
            continue

        blocks = []
        for block in content:
            if isinstance(block, str):
                blocks.append({"text": block})
            elif not isinstance(block, dict):
                continue
            elif block.get("type") == "text":
                blocks.append({"text": block.get("text", "")})
            elif block.get("type") == "thinking":
                # Extended thinking block — convert back to Bedrock format
                rc = {"reasoningText": {"text": block.get("thinking", "")}}
                sig = block.get("signature", "")
                if sig:
                    rc["signature"] = sig
                blocks.append({"reasoningContent": rc})
            elif block.get("type") == "redacted_thinking":
                # Redacted thinking — pass through opaquely
                blocks.append({"reasoningContent": {
                    "redactedContent": block.get("data", ""),
                }})
            elif block.get("type") == "tool_use":
                blocks.append({
                    "toolUse": {
                        "toolUseId": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                    }
                })
            elif block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    result_blocks = [{"text": result_content}]
                elif isinstance(result_content, list):
                    result_blocks = []
                    for rb in result_content:
                        if isinstance(rb, dict) and rb.get("type") == "text":
                            result_blocks.append({"text": rb.get("text", "")})
                        elif isinstance(rb, str):
                            result_blocks.append({"text": rb})
                    if not result_blocks:
                        result_blocks = [{"text": "(empty)"}]
                else:
                    result_blocks = [{"text": str(result_content)}]

                blocks.append({
                    "toolResult": {
                        "toolUseId": block.get("tool_use_id", ""),
                        "content": result_blocks,
                    }
                })
            else:
                # Unknown block type — convert to text
                blocks.append({"text": str(block)})

        if blocks:
            raw.append({"role": role, "content": blocks})

    # Merge consecutive same-role messages (Bedrock requires alternating roles)
    converted = []
    for msg in raw:
        if converted and converted[-1]["role"] == msg["role"]:
            converted[-1]["content"].extend(msg["content"])
        else:
            converted.append(msg)

    return converted


def _log_bedrock_usage(usage: dict, model_id: str, label: str = "") -> None:
    """Log token usage and estimated cost from Bedrock response."""
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)
    cache_read = usage.get("cacheReadInputTokens", 0)
    cache_write = usage.get("cacheWriteInputTokens", 0)

    prefix = f"[{label}] " if label else ""

    # Estimate cost
    input_price, output_price = _PRICING.get(model_id, (3.0, 15.0))
    cost_in = input_tokens * input_price / 1_000_000
    cost_out = output_tokens * output_price / 1_000_000
    total_cost = cost_in + cost_out

    log.info(
        "%sBedrock usage: in=%d (cache_read=%d, cache_write=%d) out=%d cost=$%.4f",
        prefix, input_tokens, cache_read, cache_write, output_tokens, total_cost,
    )


class BedrockAPIBackend:
    """Backend that calls Claude via AWS Bedrock Converse API with tool loop."""

    def __init__(self):
        from config import (
            BEDROCK_REGION,
            BEDROCK_MODEL,
            BEDROCK_MAX_TOKENS,
            BEDROCK_THINKING_BUDGET,
            ANTHROPIC_TOOLS_ENABLED,
            ANTHROPIC_TOOL_MAX_ITER,
            ANTHROPIC_TOOL_TIMEOUT,
            ANTHROPIC_TOTAL_TIMEOUT,
            CLAUDE_CWD,
        )

        self._region = BEDROCK_REGION
        self._default_model = BEDROCK_MODEL
        self._max_tokens = BEDROCK_MAX_TOKENS
        self._thinking_budget = BEDROCK_THINKING_BUDGET
        self._tools_enabled = ANTHROPIC_TOOLS_ENABLED
        self._max_iterations = ANTHROPIC_TOOL_MAX_ITER
        self._tool_timeout = ANTHROPIC_TOOL_TIMEOUT
        self._total_timeout = ANTHROPIC_TOTAL_TIMEOUT
        self._cwd = CLAUDE_CWD

        self._client = boto3.client("bedrock-runtime", region_name=self._region)
        self._tools = _bedrock_tools() if self._tools_enabled else None

    @property
    def name(self) -> str:
        return "bedrock_api"

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
        return _BEDROCK_MODEL_MAP.get(model, model)

    # ------------------------------------------------------------------
    # Response parsing helpers
    # ------------------------------------------------------------------

    def _parse_response(self, resp: dict) -> tuple[str, list[ToolCall], dict]:
        """Parse Bedrock converse response into (text, tool_calls, assistant_msg).

        Returns the assistant message in Anthropic-compatible format so it can
        be appended to the shared conversation history without conversion.
        """
        usage = resp.get("usage", {})
        model_id = self._default_model  # approximate — response doesn't echo model
        _log_bedrock_usage(usage, model_id, "bedrock")

        output = resp.get("output", {})
        message = output.get("message", {})
        content_blocks = message.get("content", [])

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        anthropic_blocks: list[dict] = []  # Store in Anthropic format for history

        for block in content_blocks:
            if "reasoningContent" in block:
                # Extended thinking block — preserve for tool loop continuity.
                # Store in Anthropic-compatible format for history round-trip.
                rc = block["reasoningContent"]
                thinking_text = ""
                if "reasoningText" in rc:
                    rt = rc["reasoningText"]
                    thinking_text = rt.get("text", "") if isinstance(rt, dict) else str(rt)
                signature = rc.get("signature", "")
                if "redactedContent" in rc:
                    # Redacted thinking — preserve opaquely
                    anthropic_blocks.append({
                        "type": "redacted_thinking",
                        "data": rc.get("redactedContent", ""),
                    })
                else:
                    anthropic_blocks.append({
                        "type": "thinking",
                        "thinking": thinking_text,
                        "signature": signature,
                    })
            elif "text" in block:
                text_parts.append(block["text"])
                anthropic_blocks.append({"type": "text", "text": block["text"]})
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append(
                    ToolCall(
                        id=tu.get("toolUseId", ""),
                        name=tu.get("name", ""),
                        arguments=tu.get("input", {}),
                    )
                )
                anthropic_blocks.append({
                    "type": "tool_use",
                    "id": tu.get("toolUseId", ""),
                    "name": tu.get("name", ""),
                    "input": tu.get("input", {}),
                })

        # Return assistant msg in Anthropic format (consistent with history DB)
        assistant_msg = {"role": "assistant", "content": anthropic_blocks}
        return ("\n".join(text_parts).strip(), tool_calls, assistant_msg)

    def _consume_stream(self, event_stream, streaming_editor=None, loop=None) -> tuple[str, list, dict]:
        """Consume a Bedrock converse_stream EventStream.

        Streams text deltas to streaming_editor if provided (scheduled on
        the given asyncio event loop via run_coroutine_threadsafe, since
        this method runs in a thread-pool executor).

        Returns (text, tool_calls, assistant_msg) — same shape as _parse_response().
        """
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        anthropic_blocks: list[dict] = []

        # Per-block accumulators
        current_block_type: str | None = None  # "text", "toolUse", "thinking"
        current_text_buf: list[str] = []
        current_tool_id: str = ""
        current_tool_name: str = ""
        current_tool_input_chunks: list[str] = []
        current_thinking_buf: list[str] = []
        current_thinking_signature: str = ""

        usage: dict = {}
        stop_reason: str = ""

        def _flush_block():
            """Finalize the current content block and append to outputs."""
            nonlocal current_block_type
            if current_block_type == "text":
                joined = "".join(current_text_buf)
                if joined:
                    text_parts.append(joined)
                    anthropic_blocks.append({"type": "text", "text": joined})
                current_text_buf.clear()
            elif current_block_type == "toolUse":
                raw_input = "".join(current_tool_input_chunks)
                try:
                    parsed_input = json.loads(raw_input) if raw_input else {}
                except json.JSONDecodeError:
                    log.warning("Failed to parse tool input JSON: %s", raw_input[:200])
                    parsed_input = {}
                tool_calls.append(ToolCall(
                    id=current_tool_id,
                    name=current_tool_name,
                    arguments=parsed_input,
                ))
                anthropic_blocks.append({
                    "type": "tool_use",
                    "id": current_tool_id,
                    "name": current_tool_name,
                    "input": parsed_input,
                })
                current_tool_input_chunks.clear()
            elif current_block_type == "thinking":
                joined = "".join(current_thinking_buf)
                anthropic_blocks.append({
                    "type": "thinking",
                    "thinking": joined,
                    "signature": current_thinking_signature,
                })
                current_thinking_buf.clear()
            current_block_type = None

        try:
            for event in event_stream:
                log.debug("Bedrock stream event keys: %s", list(event.keys()))

                # --- messageStart ---
                if "messageStart" in event:
                    pass  # role info, nothing to accumulate

                # --- contentBlockStart ---
                elif "contentBlockStart" in event:
                    # Flush any previous block (shouldn't happen mid-stream, but safety)
                    if current_block_type is not None:
                        _flush_block()

                    start = event["contentBlockStart"].get("start", {})
                    if "toolUse" in start:
                        current_block_type = "toolUse"
                        current_tool_id = start["toolUse"].get("toolUseId", "")
                        current_tool_name = start["toolUse"].get("name", "")
                        current_tool_input_chunks.clear()
                    elif "reasoningContent" in start:
                        current_block_type = "thinking"
                        current_thinking_buf.clear()
                        current_thinking_signature = ""
                    else:
                        current_block_type = "text"
                        current_text_buf.clear()

                # --- contentBlockDelta ---
                elif "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})

                    if "text" in delta:
                        if current_block_type is None:
                            current_block_type = "text"
                            current_text_buf.clear()
                        chunk = delta["text"]
                        if current_block_type == "text":
                            current_text_buf.append(chunk)
                            if streaming_editor and loop:
                                future = asyncio.run_coroutine_threadsafe(
                                    streaming_editor.add_text(chunk), loop
                                )
                                # Don't block long — editor is best-effort
                                try:
                                    future.result(timeout=2)
                                except Exception:
                                    pass

                    elif "toolUse" in delta and current_block_type == "toolUse":
                        input_fragment = delta["toolUse"].get("input", "")
                        if input_fragment:
                            current_tool_input_chunks.append(input_fragment)

                    elif "reasoningContent" in delta:
                        if current_block_type is None:
                            current_block_type = "thinking"
                            current_thinking_buf.clear()
                            current_thinking_signature = ""
                        rc = delta["reasoningContent"]
                        if current_block_type == "thinking":
                            if "text" in rc:
                                current_thinking_buf.append(rc["text"])
                            if "signature" in rc:
                                current_thinking_signature = rc["signature"]

                # --- contentBlockStop ---
                elif "contentBlockStop" in event:
                    _flush_block()

                # --- messageStop ---
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason", "")
                    if current_block_type is not None:
                        _flush_block()

                # --- metadata (usage) ---
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})

                # --- error events ---
                elif "internalServerException" in event:
                    raise RuntimeError(
                        f"Bedrock stream error: {event['internalServerException'].get('message', 'internal server error')}"
                    )
                elif "modelStreamErrorException" in event:
                    raise RuntimeError(
                        f"Bedrock model stream error: {event['modelStreamErrorException'].get('message', 'model stream error')}"
                    )
                elif "throttlingException" in event:
                    raise RuntimeError(
                        f"Bedrock throttling: {event['throttlingException'].get('message', 'throttled')}"
                    )
                elif "validationException" in event:
                    raise RuntimeError(
                        f"Bedrock validation error: {event['validationException'].get('message', 'validation error')}"
                    )

        except RuntimeError:
            raise
        except Exception as e:
            log.error("Error consuming Bedrock stream: %s", e)
            raise

        # If the stream ended without an explicit block stop, flush any buffer.
        if current_block_type is not None:
            _flush_block()

        # Log usage if we got it
        if usage:
            model_id = self._default_model
            _log_bedrock_usage(usage, model_id, "bedrock-stream")

        log.info(
            "[bedrock-stream] Result: text_parts=%d (%d chars), tool_calls=%d, blocks=%d, stop=%s",
            len(text_parts),
            sum(len(t) for t in text_parts),
            len(tool_calls),
            len(anthropic_blocks),
            stop_reason,
        )
        for i, block in enumerate(anthropic_blocks):
            btype = block.get("type", "?")
            if btype == "thinking":
                log.info("[bedrock-stream] block[%d] thinking (%d chars)", i, len(block.get("thinking", "")))
            elif btype == "text":
                log.info("[bedrock-stream] block[%d] text (%d chars): %s", i, len(block.get("text", "")), block.get("text", "")[:200])
            elif btype == "tool_use":
                log.info("[bedrock-stream] block[%d] tool_use: %s", i, block.get("name", "?"))

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
                    "content": result_str,
                }
            ],
        }

    # ------------------------------------------------------------------
    # Core API call
    # ------------------------------------------------------------------

    def _build_converse_kwargs(
        self,
        model_id: str,
        messages: list[dict],
        system_prompt: str | None,
        memory_context: str | None,
        extra_system_prompt: str | None = None,
    ) -> dict:
        """Build kwargs for bedrock-runtime converse() call."""
        # Convert history from Anthropic format to Bedrock format
        bedrock_messages = _convert_history_to_bedrock(messages)

        kwargs = {
            "modelId": model_id,
            "messages": bedrock_messages,
            "inferenceConfig": {
                "maxTokens": self._max_tokens,
            },
        }

        # System prompt — Bedrock takes system=[{"text": "..."}]
        # Blocks with cache_control get a cachePoint injected after them so
        # Bedrock caches the static system prompt prefix across turns.
        system_blocks = _build_system_blocks(system_prompt, memory_context, extra_system_prompt)
        if system_blocks:
            bedrock_system = []
            for block in system_blocks:
                text = block.get("text", "")
                if text:
                    bedrock_system.append({"text": text})
                    if block.get("cache_control"):
                        # Mark this position as a cache checkpoint
                        bedrock_system.append({"cachePoint": {"type": "default"}})
            if bedrock_system:
                kwargs["system"] = bedrock_system

        # Tools — append a cachePoint sentinel after all toolSpec entries so
        # the full tool schema (static at runtime) is cached as a prefix.
        if self._tools_enabled and self._tools:
            kwargs["toolConfig"] = {
                "tools": self._tools + [{"cachePoint": {"type": "default"}}],
            }

        # Optional extended thinking with explicit budget — lets Claude reason
        # before responding. Cannot coexist with temperature/topP/topK.
        # Only enable when a positive budget is configured to avoid empty
        # responses when the model returns thinking without visible text.
        if self._thinking_budget and self._thinking_budget > 0:
            kwargs["additionalModelRequestFields"] = {
                "thinking": {"type": "enabled", "budget_tokens": self._thinking_budget},
            }

        return kwargs

    def _converse(self, model_id: str, messages: list[dict], **system_kw) -> dict:
        """Synchronous converse call."""
        kwargs = self._build_converse_kwargs(model_id, messages, **system_kw)
        return self._client.converse(**kwargs)

    async def _converse_async(self, model_id: str, messages: list[dict], **system_kw) -> dict:
        """Async converse call (runs boto3 sync client in thread pool)."""
        kwargs = self._build_converse_kwargs(model_id, messages, **system_kw)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._client.converse(**kwargs))

    async def _stream_request(
        self,
        messages: list[dict],
        model_id: str,
        streaming_editor=None,
        **system_kw,
    ) -> tuple[str, list, dict]:
        """Send request via converse_stream and consume with streaming.

        Returns (text, tool_calls, assistant_msg) — same as _parse_response().
        """
        kwargs = self._build_converse_kwargs(model_id, messages, **system_kw)
        loop = asyncio.get_event_loop()

        def _sync_stream():
            response = self._client.converse_stream(**kwargs)
            return self._consume_stream(
                response["stream"],
                streaming_editor=streaming_editor,
                loop=loop,
            )

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

        # Cap maxTokens for non-streaming converse() — Bedrock limit is 21333
        original_max = self._max_tokens
        self._max_tokens = min(self._max_tokens, 21333)
        try:
            return self._call_sync_inner(
                prompt, model_id=model_id, session_id=session_id,
                timeout=timeout, system_prompt=system_prompt,
                memory_context=memory_context,
            )
        finally:
            self._max_tokens = original_max

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
            return self._converse(model_id, msgs, **system_kw)

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
                log.error("Bedrock tool loop error (sync): %s", e)
                return {"result": f"Bedrock error: {e}", "session_id": session_id}
            result_text = result.get("result", "")
            if result_text:
                # messages was modified in place by the tool loop and already
                # contains the final assistant message — don't append a duplicate
                save_conversation_history(session_id, messages)
            result["session_id"] = session_id
            return result

        # No tools — single request
        try:
            resp = send_request(messages)
        except Exception as e:
            return {"result": f"Bedrock error: {e}", "session_id": session_id}

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

        # Build a combined send+parse that streams text to the editor.
        # Used by the tool loop instead of separate send_request + parse_response.
        async def send_and_parse_stream(msgs, editor):
            return await self._stream_request(
                msgs, model_id, streaming_editor=editor, **system_kw,
            )

        # Fallback send_request for the tool loop (used only if
        # send_and_parse_stream is not available — kept for interface compat)
        async def send_request(msgs):
            return await self._converse_async(model_id, msgs, **system_kw)

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
                log.error("Bedrock tool loop error (async): %s", e)
                return {"result": f"Bedrock error: {e}", "session_id": session_id, "written_files": []}
            result_text = result.get("result", "")
            if result_text:
                # messages was modified in place by the tool loop and already
                # contains the final assistant message — don't append a duplicate
                save_conversation_history(session_id, messages)
            result["session_id"] = session_id
            return result

        # No tools — single streaming request
        try:
            text, _tool_calls, _assistant_msg = await send_and_parse_stream(
                messages, streaming_editor,
            )
        except Exception as e:
            return {"result": f"Bedrock error: {e}", "session_id": session_id, "written_files": []}

        if text:
            save_conversation_history(session_id, messages + [
                {"role": "assistant", "content": text}
            ])
        return {"result": text or "(empty response)", "session_id": session_id, "written_files": []}
