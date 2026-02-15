"""Ollama backend — local models via /api/chat with tool calling support.

Supports OpenAI-compatible function calling for models that support it
(Qwen 3, Llama 3.1+, Mistral, etc.). Falls back to plain chat when
tools are disabled or the model doesn't emit tool calls.
"""

import json
import logging
import re
import time
import urllib.request
import urllib.error

log = logging.getLogger("nexus")

# Strip <think>...</think> blocks from reasoning models (Qwen 3, DeepSeek R1, etc.)
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# Model name mapping: friendly name → Ollama model
_MODEL_MAP = {
    "sonnet": None,   # uses OLLAMA_MODEL from config
    "haiku": None,     # uses OLLAMA_MODEL from config
    "opus": None,      # uses OLLAMA_MODEL from config
}


class OllamaBackend:
    """Backend that calls a local Ollama instance via HTTP with tool support."""

    def __init__(self):
        from config import OLLAMA_URL, OLLAMA_MODEL
        self._base_url = OLLAMA_URL.rstrip("/")
        self._default_model = OLLAMA_MODEL

        # Tool calling config — import with fallbacks for when config hasn't been updated yet
        try:
            from config import OLLAMA_TOOLS_ENABLED
            self._tools_enabled = OLLAMA_TOOLS_ENABLED
        except ImportError:
            self._tools_enabled = True
        try:
            from config import OLLAMA_TOOL_MAX_ITER
            self._max_iterations = OLLAMA_TOOL_MAX_ITER
        except ImportError:
            self._max_iterations = 25
        try:
            from config import OLLAMA_TOOL_TIMEOUT
            self._tool_timeout = OLLAMA_TOOL_TIMEOUT
        except ImportError:
            self._tool_timeout = 30
        try:
            from config import CLAUDE_CWD
            self._cwd = CLAUDE_CWD
        except ImportError:
            self._cwd = "/home/puretensorai"
        try:
            from config import OLLAMA_NUM_PREDICT
            self._num_predict = OLLAMA_NUM_PREDICT
        except ImportError:
            self._num_predict = 8192

    @property
    def name(self) -> str:
        return "ollama"

    def get_model_display(self, model: str) -> str:
        return self._default_model

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
        user_message: str,
        system_prompt: str | None = None,
        memory_context: str | None = None,
        extra_system_prompt: str | None = None,
    ) -> list[dict]:
        """Build the messages list for /api/chat."""
        messages = []

        # System message
        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if memory_context:
            system_parts.append(memory_context)
        if extra_system_prompt:
            system_parts.append(extra_system_prompt)
        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        # User message
        messages.append({"role": "user", "content": user_message})
        return messages

    def _get_tools(self) -> list[dict] | None:
        """Return tool schemas if tools are enabled, else None."""
        if not self._tools_enabled:
            return None
        from backends.tools import TOOL_SCHEMAS
        return TOOL_SCHEMAS

    def _get_options(self) -> dict:
        """Return Ollama options (num_predict etc.)."""
        return {"num_predict": self._num_predict}

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """Remove <think>...</think> blocks from reasoning model output."""
        return _THINK_RE.sub("", text).strip()

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
        """Synchronous call to Ollama /api/chat with optional tool loop."""
        model_id = self._resolve_model(model)
        messages = self._build_messages(prompt, system_prompt, memory_context)
        tools = self._get_tools()
        written_files = []
        start_time = time.time()

        log.info("Ollama call_sync: model=%s, tools=%s", model_id, "enabled" if tools else "disabled")

        for iteration in range(self._max_iterations):
            if time.time() - start_time > timeout:
                log.warning("Ollama call_sync: total timeout reached after %d iterations", iteration)
                break

            payload = {
                "model": model_id,
                "messages": messages,
                "stream": False,
                "options": self._get_options(),
            }
            if tools:
                payload["tools"] = tools

            try:
                data = json.dumps(payload).encode()
                req = urllib.request.Request(
                    f"{self._base_url}/api/chat",
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    result = json.loads(resp.read().decode())
            except urllib.error.URLError as e:
                log.error("Ollama connection error: %s", e)
                return {"result": f"Ollama error: {e}", "session_id": None}
            except Exception as e:
                log.error("Ollama error: %s", e)
                return {"result": f"Ollama error: {e}", "session_id": None}

            msg = result.get("message", {})
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")

            # Append assistant message to conversation
            assistant_msg = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                # No tool calls — we're done
                clean = self._strip_thinking(content)
                # Empty response with tools → model may not support tool protocol.
                # Retry once without tools.
                if not clean and tools and iteration == 0:
                    log.info("Ollama: empty response with tools (sync), retrying without")
                    messages.pop()  # remove empty assistant msg
                    payload_retry = {
                        "model": model_id, "messages": messages,
                        "stream": False, "options": self._get_options(),
                    }
                    try:
                        data = json.dumps(payload_retry).encode()
                        req = urllib.request.Request(
                            f"{self._base_url}/api/chat", data=data,
                            headers={"Content-Type": "application/json"},
                        )
                        with urllib.request.urlopen(req, timeout=timeout) as resp:
                            retry_result = json.loads(resp.read().decode())
                        clean = self._strip_thinking(retry_result.get("message", {}).get("content", ""))
                        messages.append({"role": "assistant", "content": clean})
                    except Exception:
                        pass
                return {
                    "result": clean or "(empty response)",
                    "session_id": None,
                    "written_files": written_files,
                }

            # Execute tool calls
            from backends.tools import execute_tool

            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                log.info("Ollama tool call [sync]: %s(%s)", name, str(args)[:100])
                result_str, new_files = execute_tool(
                    name, args, timeout=self._tool_timeout, cwd=self._cwd
                )
                written_files.extend(new_files)

                messages.append({"role": "tool", "content": result_str})

        return {
            "result": content if content else "(max tool iterations reached)",
            "session_id": None,
            "written_files": written_files,
        }

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
        """Async streaming call to Ollama /api/chat with tool loop."""
        import asyncio

        model_id = self._resolve_model(model)
        messages = self._build_messages(message, system_prompt, memory_context, extra_system_prompt)
        tools = self._get_tools()
        written_files = []
        start_time = time.time()

        log.info("Ollama call_streaming: model=%s, tools=%s", model_id, "enabled" if tools else "disabled")

        try:
            for iteration in range(self._max_iterations):
                if time.time() - start_time > 300:
                    log.warning("Ollama streaming: total timeout after %d iterations", iteration)
                    break

                # Stream the response
                assistant_msg, tool_calls = await self._stream_chat(
                    model_id, messages, tools, streaming_editor
                )

                if not tool_calls:
                    clean = self._strip_thinking(assistant_msg.get("content", ""))
                    # Empty response with tools → model may not support tool protocol.
                    # Retry once without tools so every model works.
                    if not clean and tools and iteration == 0:
                        log.info("Ollama: empty response with tools, retrying without tools")
                        messages.pop() if assistant_msg in messages else None
                        assistant_msg, _ = await self._stream_chat(
                            model_id, messages, None, streaming_editor
                        )
                        clean = self._strip_thinking(assistant_msg.get("content", ""))
                    messages.append(assistant_msg)
                    return {
                        "result": clean or "(empty response)",
                        "session_id": None,
                        "written_files": written_files,
                    }

                messages.append(assistant_msg)

                # Execute tool calls
                from backends.tools import execute_tool
                from engine import _format_tool_status

                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    args = func.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}

                    # Show tool status in Telegram
                    status = _format_tool_status(name, args)
                    if streaming_editor:
                        await streaming_editor.add_tool_status(status)
                    elif on_progress:
                        await on_progress(status)

                    log.info("Ollama tool call [stream]: %s(%s)", name, str(args)[:100])

                    # Execute in thread pool to avoid blocking
                    result_str, new_files = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda n=name, a=args: execute_tool(
                            n, a, timeout=self._tool_timeout, cwd=self._cwd
                        ),
                    )
                    written_files.extend(new_files)
                    messages.append({"role": "tool", "content": result_str})

            return {
                "result": "(max tool iterations reached)",
                "session_id": None,
                "written_files": written_files,
            }

        except Exception as e:
            log.error("Ollama streaming error: %s", e)
            return {
                "result": f"Ollama error: {e}",
                "session_id": None,
                "written_files": written_files,
            }

    async def _stream_chat(
        self,
        model_id: str,
        messages: list[dict],
        tools: list[dict] | None,
        streaming_editor=None,
    ) -> tuple[dict, list[dict] | None]:
        """Stream a single /api/chat call. Returns (assistant_message, tool_calls).

        Accumulates text deltas and sends them to the streaming editor.
        When done, returns the full assistant message and any tool calls.
        """
        try:
            import aiohttp
            return await self._stream_chat_aiohttp(
                model_id, messages, tools, streaming_editor
            )
        except ImportError:
            import asyncio
            return await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._stream_chat_sync(model_id, messages, tools),
            )

    async def _stream_chat_aiohttp(
        self,
        model_id: str,
        messages: list[dict],
        tools: list[dict] | None,
        streaming_editor=None,
    ) -> tuple[dict, list[dict] | None]:
        """Stream using aiohttp for true async."""
        import aiohttp

        payload = {
            "model": model_id,
            "messages": messages,
            "stream": True,
            "options": self._get_options(),
        }
        if tools:
            payload["tools"] = tools

        content = ""
        tool_calls = None
        in_thinking = False

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._base_url}/api/chat",
                json=payload,
            ) as resp:
                async for line in resp.content:
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue

                    msg = chunk.get("message", {})

                    # Accumulate text content
                    text = msg.get("content", "")
                    if text:
                        content += text
                        # Suppress <think>...</think> blocks from streaming output
                        if "<think>" in text:
                            in_thinking = True
                        if in_thinking:
                            if "</think>" in text:
                                in_thinking = False
                            continue
                        if streaming_editor:
                            await streaming_editor.add_text(text)

                    # Check for tool calls (appear in final chunk when done=true)
                    if chunk.get("done") and msg.get("tool_calls"):
                        tool_calls = msg["tool_calls"]

                    # Some Ollama versions send tool_calls incrementally
                    if not chunk.get("done") and msg.get("tool_calls") and tool_calls is None:
                        tool_calls = msg["tool_calls"]

        assistant_msg = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls

        return assistant_msg, tool_calls

    def _stream_chat_sync(
        self,
        model_id: str,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> tuple[dict, list[dict] | None]:
        """Fallback: synchronous streaming (no real-time editor updates)."""
        payload = {
            "model": model_id,
            "messages": messages,
            "stream": True,
            "options": self._get_options(),
        }
        if tools:
            payload["tools"] = tools

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )

        content = ""
        tool_calls = None

        with urllib.request.urlopen(req, timeout=300) as resp:
            for line in resp:
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode())
                except json.JSONDecodeError:
                    continue

                msg = chunk.get("message", {})
                text = msg.get("content", "")
                if text:
                    content += text

                if chunk.get("done") and msg.get("tool_calls"):
                    tool_calls = msg["tool_calls"]
                if not chunk.get("done") and msg.get("tool_calls") and tool_calls is None:
                    tool_calls = msg["tool_calls"]

        assistant_msg = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls

        return assistant_msg, tool_calls
