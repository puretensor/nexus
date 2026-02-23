"""vLLM backend — OpenAI-compatible API for local models (Qwen3-235B-A22B NVFP4).

Mirrors anthropic_api.py structure with OpenAI message format:
  - System prompt prepended per-request (not stored in history)
  - Tool calls in OpenAI format (tool_calls / role=tool)
  - <think>...</think> blocks preserved in assistant_msg during the tool loop
    (model reasoning between tool rounds improves accuracy)
  - <think> stripped only for user-facing text and persistent DB history
  - Conversation history persisted in DB (session memory across turns)
  - NVFP4 JSON safety: malformed tool call args trigger low-temperature retry
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from openai import OpenAI, AsyncOpenAI
from db import get_conversation_history, save_conversation_history

from backends.tools import TOOL_SCHEMAS, ToolCall, run_tool_loop_sync, run_tool_loop_async

log = logging.getLogger("nexus")

# Strip thinking tokens before returning text to user
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# ---------------------------------------------------------------------------
# Tool-nudge: keywords that imply the user wants live/current data.
# If any match, append a reminder so the model reaches for web_search.
# ---------------------------------------------------------------------------
_NUDGE_KEYWORDS = re.compile(
    r"\b(?:"
    r"market[s]?|stock[s]?|share[s]?|index|indices|ftse|s&p|nasdaq|dow|nikkei|dax"
    r"|price[s]?|cost[s]?|worth|valuation"
    r"|bitcoin|btc|ethereum|eth|crypto|coin[s]?"
    r"|gold|silver|oil|brent|crude|commodit(?:y|ies)"
    r"|forex|exchange rate|usd|gbp|eur|yen|dollar|pound|euro"
    r"|inflation|interest rate|gdp|unemployment|economic"
    r"|weather|forecast|temperature|rain|snow|wind"
    r"|news|headline[s]?|latest|today|tonight|current(?:ly)?"
    r"|score[s]?|match|game|result[s]?|premier league|champions league"
    r"|elect(?:ion|ed)|president|minister|government|parliament|congress"
    r"|war|conflict|ceasefire|sanction[s]?"
    r"|launch(?:ed)?|release[d]?|announced|update[d]?"
    r")\b",
    re.IGNORECASE,
)
_NUDGE_MSG = "\n\n[This question likely involves real-time data. Use web_search to get current information before answering.]"


def _maybe_nudge(text: str) -> str:
    """Append a tool-use reminder if the message looks like it needs live data."""
    if _NUDGE_KEYWORDS.search(text):
        return text + _NUDGE_MSG
    return text


def _clean_for_history(messages: list) -> list:
    """Return only user/assistant-text messages, stripping tool call sequences.

    Tool role messages and assistant messages with tool_calls cannot be safely
    trimmed by save_conversation_history (slicing mid-sequence causes vLLM 400s).
    The model only needs the final text turns to maintain conversational context.
    <think> blocks are stripped here since they are intra-turn scaffolding only.
    """
    result = []
    for msg in messages:
        role = msg.get("role")
        if role == "user" and isinstance(msg.get("content"), str):
            clean = msg["content"].replace(_NUDGE_MSG, "")
            result.append({"role": "user", "content": clean})
        elif role == "assistant" and not msg.get("tool_calls"):
            text = _THINK_RE.sub("", msg.get("content") or "").strip()
            if text:
                result.append({"role": "assistant", "content": text})
    return result


class VLLMBackend:
    """Backend that calls a local vLLM endpoint (OpenAI-compatible API)."""

    def __init__(self):
        from config import (
            VLLM_URL,
            VLLM_MODEL,
            VLLM_MAX_TOKENS,
            VLLM_TOOLS_ENABLED,
            VLLM_TOOL_MAX_ITER,
            VLLM_TOOL_TIMEOUT,
            VLLM_TOTAL_TIMEOUT,
            CLAUDE_CWD,
        )

        self._url = VLLM_URL
        self._model = VLLM_MODEL
        self._max_tokens = VLLM_MAX_TOKENS
        self._tools_enabled = VLLM_TOOLS_ENABLED
        self._max_iterations = VLLM_TOOL_MAX_ITER
        self._tool_timeout = VLLM_TOOL_TIMEOUT
        self._total_timeout = VLLM_TOTAL_TIMEOUT
        self._cwd = CLAUDE_CWD

        self._client = OpenAI(base_url=self._url, api_key="dummy")
        self._aclient = AsyncOpenAI(base_url=self._url, api_key="dummy")
        self._tools = TOOL_SCHEMAS if self._tools_enabled else None

    @property
    def name(self) -> str:
        return "vllm"

    def get_model_display(self, model: str) -> str:
        return self._model

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return self._tools_enabled

    @property
    def supports_sessions(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Response parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(resp) -> tuple[str, list[ToolCall], dict]:
        msg = resp.choices[0].message

        # Preserve raw content (including <think>) for assistant_msg.
        # Interleaved reasoning between tool-call rounds improves accuracy.
        raw_content = msg.content or ""

        # Strip thinking tokens for user-facing display only.
        text = _THINK_RE.sub("", raw_content).strip()

        tool_calls: list[ToolCall] = []
        tc_dicts: list[dict] = []

        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    # NVFP4 W4A4 can produce malformed JSON — log and skip
                    log.warning("Malformed tool call JSON from %s: %s",
                                tc.function.name, tc.function.arguments)
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
                tc_dicts.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                })

        # Use raw_content in the message appended to the tool loop so the model
        # sees its own reasoning in subsequent rounds.
        assistant_msg: dict = {"role": "assistant", "content": raw_content}
        if tc_dicts:
            assistant_msg["tool_calls"] = tc_dicts

        return text, tool_calls, assistant_msg

    @staticmethod
    def _format_tool_result(tool_name: str, call_id: str, result_str: str) -> dict:
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": result_str,
        }

    # ------------------------------------------------------------------
    # Sync call
    # ------------------------------------------------------------------

    def call_sync(
        self,
        prompt: str,
        *,
        model: str = "default",
        session_id: str | None = None,
        timeout: int = 300,
        system_prompt: str | None = None,
        memory_context: str | None = None,
    ) -> dict:
        session_id = session_id or str(uuid.uuid4())

        now = datetime.now(timezone.utc).strftime("Current date: %A, %B %d, %Y. Current time: %H:%M UTC.")
        system_str = "\n\n".join(p for p in [system_prompt, now, memory_context] if p) or None

        history = get_conversation_history(session_id)
        nudged = _maybe_nudge(prompt)
        messages = history + [{"role": "user", "content": nudged}]

        def send_request(msgs):
            api_msgs = ([{"role": "system", "content": system_str}] + msgs) if system_str else msgs
            return self._client.chat.completions.create(
                model=self._model,
                messages=api_msgs,
                tools=self._tools,
                max_tokens=self._max_tokens,
                temperature=0.3 if self._tools else 0.7,
                top_p=0.8,
                extra_body={"top_k": 20, "min_p": 0, "repetition_penalty": 1.05},
                timeout=min(timeout, self._total_timeout),
            )

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
            if result.get("result"):
                save_conversation_history(session_id, _clean_for_history(messages))
            result["session_id"] = session_id
            return result

        # No tools — single request
        try:
            resp = send_request(messages)
        except Exception as e:
            return {"result": f"vLLM error: {e}", "session_id": session_id}

        text, _tool_calls, assistant_msg = self._parse_response(resp)
        if text:
            messages.append(assistant_msg)
            save_conversation_history(session_id, _clean_for_history(messages))
        return {"result": text or "(empty response)", "session_id": session_id}

    # ------------------------------------------------------------------
    # Async call
    # ------------------------------------------------------------------

    async def call_streaming(
        self,
        message: str,
        *,
        session_id: str | None = None,
        model: str = "default",
        on_progress=None,
        streaming_editor=None,
        system_prompt: str | None = None,
        memory_context: str | None = None,
        extra_system_prompt: str | None = None,
    ) -> dict:
        session_id = session_id or str(uuid.uuid4())

        now = datetime.now(timezone.utc).strftime("Current date: %A, %B %d, %Y. Current time: %H:%M UTC.")
        system_str = (
            "\n\n".join(p for p in [system_prompt, now, memory_context, extra_system_prompt] if p) or None
        )

        history = get_conversation_history(session_id)
        nudged = _maybe_nudge(message)
        messages = history + [{"role": "user", "content": nudged}]

        async def send_request(msgs):
            api_msgs = ([{"role": "system", "content": system_str}] + msgs) if system_str else msgs
            return await self._aclient.chat.completions.create(
                model=self._model,
                messages=api_msgs,
                tools=self._tools,
                max_tokens=self._max_tokens,
                temperature=0.3 if self._tools else 0.7,
                top_p=0.8,
                extra_body={"top_k": 20, "min_p": 0, "repetition_penalty": 1.05},
                timeout=self._total_timeout,
            )

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
            if result.get("result"):
                save_conversation_history(session_id, _clean_for_history(messages))
            result["session_id"] = session_id
            return result

        # No tools — single request
        try:
            resp = await send_request(messages)
        except Exception as e:
            return {"result": f"vLLM error: {e}", "session_id": session_id, "written_files": []}

        text, _tool_calls, assistant_msg = self._parse_response(resp)
        if text:
            messages.append(assistant_msg)
            save_conversation_history(session_id, _clean_for_history(messages))
        return {"result": text or "(empty response)", "session_id": session_id, "written_files": []}
