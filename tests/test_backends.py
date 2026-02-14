"""Tests for backends — protocol compliance, factory, per-backend unit tests."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from backends import get_backend, reset_backend
    from backends.base import Backend
    from backends.claude_code import ClaudeCodeBackend
    from backends.ollama import OllamaBackend
    from backends.gemini_cli import GeminiCLIBackend
    from backends.codex_cli import CodexCLIBackend


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

class TestProtocolCompliance:

    def test_claude_code_implements_protocol(self):
        """ClaudeCodeBackend satisfies the Backend protocol."""
        backend = ClaudeCodeBackend()
        assert isinstance(backend, Backend)

    def test_ollama_implements_protocol(self):
        """OllamaBackend satisfies the Backend protocol."""
        backend = OllamaBackend()
        assert isinstance(backend, Backend)

    def test_gemini_cli_implements_protocol(self):
        """GeminiCLIBackend satisfies the Backend protocol."""
        backend = GeminiCLIBackend()
        assert isinstance(backend, Backend)

    def test_codex_cli_implements_protocol(self):
        """CodexCLIBackend satisfies the Backend protocol."""
        backend = CodexCLIBackend()
        assert isinstance(backend, Backend)

    def test_anthropic_api_implements_protocol(self):
        """AnthropicAPIBackend satisfies the Backend protocol."""
        with patch("config.ANTHROPIC_API_KEY", "test-key"):
            from backends.anthropic_api import AnthropicAPIBackend
            backend = AnthropicAPIBackend()
            assert isinstance(backend, Backend)

    def test_openai_compat_implements_protocol(self):
        """OpenAICompatBackend satisfies the Backend protocol."""
        with patch("config.OPENAI_COMPAT_URL", "http://localhost:8080"):
            from backends.openai_compat import OpenAICompatBackend
            backend = OpenAICompatBackend()
            assert isinstance(backend, Backend)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:

    def setup_method(self):
        reset_backend()

    def teardown_method(self):
        reset_backend()

    def test_default_is_claude_code(self):
        """Default ENGINE_BACKEND creates ClaudeCodeBackend."""
        with patch("config.ENGINE_BACKEND", "claude_code"):
            backend = get_backend()
            assert backend.name == "claude_code"
            assert isinstance(backend, ClaudeCodeBackend)

    def test_singleton(self):
        """get_backend returns the same instance on repeated calls."""
        with patch("config.ENGINE_BACKEND", "claude_code"):
            b1 = get_backend()
            b2 = get_backend()
            assert b1 is b2

    def test_reset_clears_singleton(self):
        """reset_backend allows creating a new instance."""
        with patch("config.ENGINE_BACKEND", "claude_code"):
            b1 = get_backend()
            reset_backend()
            b2 = get_backend()
            assert b1 is not b2

    def test_ollama_backend(self):
        """ENGINE_BACKEND=ollama creates OllamaBackend."""
        with patch("config.ENGINE_BACKEND", "ollama"):
            backend = get_backend()
            assert backend.name == "ollama"

    def test_unknown_backend_raises(self):
        """Unknown ENGINE_BACKEND raises ValueError."""
        with patch("config.ENGINE_BACKEND", "nonexistent"):
            with pytest.raises(ValueError, match="Unknown ENGINE_BACKEND"):
                get_backend()

    def test_anthropic_api_requires_key(self):
        """anthropic_api backend requires ANTHROPIC_API_KEY."""
        with patch("config.ENGINE_BACKEND", "anthropic_api"), \
             patch("config.ANTHROPIC_API_KEY", ""):
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                get_backend()

    def test_openai_compat_requires_url(self):
        """openai_compat backend requires OPENAI_COMPAT_URL."""
        with patch("config.ENGINE_BACKEND", "openai_compat"), \
             patch("config.OPENAI_COMPAT_URL", ""):
            with pytest.raises(ValueError, match="OPENAI_COMPAT_URL"):
                get_backend()


# ---------------------------------------------------------------------------
# ClaudeCodeBackend
# ---------------------------------------------------------------------------

class TestClaudeCodeBackend:

    def test_name(self):
        backend = ClaudeCodeBackend()
        assert backend.name == "claude_code"

    def test_supports_streaming(self):
        backend = ClaudeCodeBackend()
        assert backend.supports_streaming is True

    def test_supports_tools(self):
        backend = ClaudeCodeBackend()
        assert backend.supports_tools is True

    def test_supports_sessions(self):
        backend = ClaudeCodeBackend()
        assert backend.supports_sessions is True

    def test_call_sync_returns_result(self):
        """call_sync should shell out and parse JSON response."""
        backend = ClaudeCodeBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "Hello", "session_id": "sess-1"})
        mock_result.stderr = ""

        with patch("backends.claude_code.subprocess.run", return_value=mock_result):
            result = backend.call_sync("test prompt")

        assert result["result"] == "Hello"
        assert result["session_id"] == "sess-1"

    def test_call_sync_passes_system_prompt(self):
        """call_sync should include system_prompt in CLI args."""
        backend = ClaudeCodeBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok"})
        mock_result.stderr = ""

        with patch("backends.claude_code.subprocess.run", return_value=mock_result) as mock_run:
            backend.call_sync("test", system_prompt="Be helpful")
            cmd = mock_run.call_args[0][0]
            assert "--append-system-prompt" in cmd
            idx = cmd.index("--append-system-prompt")
            assert cmd[idx + 1] == "Be helpful"

    def test_call_sync_passes_memory_context(self):
        """call_sync should include memory_context in CLI args."""
        backend = ClaudeCodeBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok"})
        mock_result.stderr = ""

        with patch("backends.claude_code.subprocess.run", return_value=mock_result) as mock_run:
            backend.call_sync("test", system_prompt="sys", memory_context="memory")
            cmd = mock_run.call_args[0][0]
            # Both system_prompt and memory_context should be appended
            indices = [i for i, x in enumerate(cmd) if x == "--append-system-prompt"]
            assert len(indices) == 2

    def test_call_sync_handles_timeout(self):
        """call_sync should handle subprocess timeout."""
        import subprocess
        backend = ClaudeCodeBackend()

        with patch("backends.claude_code.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=300)):
            result = backend.call_sync("test", timeout=300)

        assert "timed out" in result["result"].lower()

    def test_call_sync_handles_nonzero_exit(self):
        """call_sync should handle non-zero exit code."""
        backend = ClaudeCodeBackend()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "something went wrong"
        mock_result.stdout = ""

        with patch("backends.claude_code.subprocess.run", return_value=mock_result):
            result = backend.call_sync("test")

        assert "error" in result["result"].lower()

    def test_call_sync_handles_non_json_output(self):
        """call_sync should handle non-JSON stdout."""
        backend = ClaudeCodeBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "plain text response"
        mock_result.stderr = ""

        with patch("backends.claude_code.subprocess.run", return_value=mock_result):
            result = backend.call_sync("test")

        assert result["result"] == "plain text response"


# ---------------------------------------------------------------------------
# OllamaBackend
# ---------------------------------------------------------------------------

class TestOllamaBackend:

    def test_name(self):
        backend = OllamaBackend()
        assert backend.name == "ollama"

    def test_supports_streaming(self):
        backend = OllamaBackend()
        assert backend.supports_streaming is True

    def test_supports_tools_default(self):
        backend = OllamaBackend()
        assert backend.supports_tools is True

    def test_supports_tools_disabled(self):
        with patch("config.OLLAMA_TOOLS_ENABLED", False):
            backend = OllamaBackend()
            assert backend.supports_tools is False

    def test_no_sessions(self):
        backend = OllamaBackend()
        assert backend.supports_sessions is False

    def test_build_messages(self):
        """_build_messages produces correct chat format."""
        backend = OllamaBackend()
        messages = backend._build_messages("Hello", system_prompt="Be helpful")
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "Be helpful"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Hello"

    def test_build_messages_no_system(self):
        """_build_messages omits system when no system prompt."""
        backend = OllamaBackend()
        messages = backend._build_messages("Hello")
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    def test_call_sync_success(self):
        """call_sync should call Ollama /api/chat and parse response."""
        backend = OllamaBackend()

        response_data = json.dumps({
            "message": {"role": "assistant", "content": "Hello from Ollama"},
            "done": True,
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("backends.ollama.urllib.request.urlopen", return_value=mock_resp):
            result = backend.call_sync("test prompt")

        assert result["result"] == "Hello from Ollama"
        assert result["session_id"] is None

    def test_call_sync_uses_chat_endpoint(self):
        """call_sync should POST to /api/chat, not /api/generate."""
        backend = OllamaBackend()

        response_data = json.dumps({
            "message": {"role": "assistant", "content": "ok"},
            "done": True,
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("backends.ollama.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            backend.call_sync("test")
            req = mock_urlopen.call_args[0][0]
            assert "/api/chat" in req.full_url

    def test_call_sync_sends_tools(self):
        """call_sync should include tools in payload when enabled."""
        backend = OllamaBackend()

        response_data = json.dumps({
            "message": {"role": "assistant", "content": "ok"},
            "done": True,
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("backends.ollama.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            backend.call_sync("test")
            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data.decode())
            assert "tools" in payload
            assert len(payload["tools"]) == 6

    def test_call_sync_no_tools_when_disabled(self):
        """call_sync should not include tools when disabled."""
        with patch("config.OLLAMA_TOOLS_ENABLED", False):
            backend = OllamaBackend()

        response_data = json.dumps({
            "message": {"role": "assistant", "content": "ok"},
            "done": True,
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("backends.ollama.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            backend.call_sync("test")
            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data.decode())
            assert "tools" not in payload

    def test_call_sync_tool_loop(self):
        """call_sync should execute tool calls and loop."""
        backend = OllamaBackend()

        # First response: model requests a tool call
        tool_response = json.dumps({
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "bash",
                        "arguments": {"command": "echo tool_test"},
                    }
                }],
            },
            "done": True,
        }).encode()

        # Second response: model returns final text
        final_response = json.dumps({
            "message": {"role": "assistant", "content": "The result is: tool_test"},
            "done": True,
        }).encode()

        call_count = [0]
        def mock_urlopen(req, **kwargs):
            mock_resp = MagicMock()
            if call_count[0] == 0:
                mock_resp.read.return_value = tool_response
            else:
                mock_resp.read.return_value = final_response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            call_count[0] += 1
            return mock_resp

        with patch("backends.ollama.urllib.request.urlopen", side_effect=mock_urlopen):
            result = backend.call_sync("run echo tool_test")

        assert call_count[0] == 2
        assert result["result"] == "The result is: tool_test"

    def test_call_sync_connection_error(self):
        """call_sync should handle connection errors."""
        import urllib.error
        backend = OllamaBackend()

        with patch("backends.ollama.urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            result = backend.call_sync("test")

        assert "error" in result["result"].lower()

    def test_call_sync_written_files_tracked(self):
        """call_sync should track files written by write_file tool."""
        import tempfile
        backend = OllamaBackend()

        tmpdir = tempfile.mkdtemp()
        target_path = f"{tmpdir}/test_output.txt"

        tool_response = json.dumps({
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "write_file",
                        "arguments": {"file_path": target_path, "content": "hello"},
                    }
                }],
            },
            "done": True,
        }).encode()

        final_response = json.dumps({
            "message": {"role": "assistant", "content": "Done"},
            "done": True,
        }).encode()

        call_count = [0]
        def mock_urlopen(req, **kwargs):
            mock_resp = MagicMock()
            if call_count[0] == 0:
                mock_resp.read.return_value = tool_response
            else:
                mock_resp.read.return_value = final_response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            call_count[0] += 1
            return mock_resp

        with patch("backends.ollama.urllib.request.urlopen", side_effect=mock_urlopen):
            result = backend.call_sync("write hello to a file")

        assert target_path in result.get("written_files", [])

        # Cleanup
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# GeminiCLIBackend (stub)
# ---------------------------------------------------------------------------

class TestGeminiCLIBackend:

    def test_name(self):
        backend = GeminiCLIBackend()
        assert backend.name == "gemini_cli"

    def test_no_streaming(self):
        backend = GeminiCLIBackend()
        assert backend.supports_streaming is False

    def test_call_sync_not_found(self):
        """call_sync should handle missing binary gracefully."""
        backend = GeminiCLIBackend()

        with patch("backends.gemini_cli.subprocess.run", side_effect=FileNotFoundError):
            result = backend.call_sync("test")

        assert "not found" in result["result"].lower()


# ---------------------------------------------------------------------------
# CodexCLIBackend (stub)
# ---------------------------------------------------------------------------

class TestCodexCLIBackend:

    def test_name(self):
        backend = CodexCLIBackend()
        assert backend.name == "codex_cli"

    def test_no_streaming(self):
        backend = CodexCLIBackend()
        assert backend.supports_streaming is False

    def test_call_sync_not_found(self):
        """call_sync should handle missing binary gracefully."""
        backend = CodexCLIBackend()

        with patch("backends.codex_cli.subprocess.run", side_effect=FileNotFoundError):
            result = backend.call_sync("test")

        assert "not found" in result["result"].lower()


# ---------------------------------------------------------------------------
# AnthropicAPIBackend
# ---------------------------------------------------------------------------

class TestAnthropicAPIBackend:

    def test_requires_api_key(self):
        """Should raise ValueError without API key."""
        with patch("config.ANTHROPIC_API_KEY", ""):
            from backends.anthropic_api import AnthropicAPIBackend
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                AnthropicAPIBackend()

    def test_name(self):
        with patch("config.ANTHROPIC_API_KEY", "test-key"):
            from backends.anthropic_api import AnthropicAPIBackend
            backend = AnthropicAPIBackend()
            assert backend.name == "anthropic_api"

    def test_no_sessions(self):
        with patch("config.ANTHROPIC_API_KEY", "test-key"):
            from backends.anthropic_api import AnthropicAPIBackend
            backend = AnthropicAPIBackend()
            assert backend.supports_sessions is False

    def test_supports_streaming(self):
        with patch("config.ANTHROPIC_API_KEY", "test-key"):
            from backends.anthropic_api import AnthropicAPIBackend
            backend = AnthropicAPIBackend()
            assert backend.supports_streaming is True


# ---------------------------------------------------------------------------
# OpenAICompatBackend
# ---------------------------------------------------------------------------

class TestOpenAICompatBackend:

    def test_requires_url(self):
        """Should raise ValueError without URL."""
        with patch("config.OPENAI_COMPAT_URL", ""):
            from backends.openai_compat import OpenAICompatBackend
            with pytest.raises(ValueError, match="OPENAI_COMPAT_URL"):
                OpenAICompatBackend()

    def test_name(self):
        with patch("config.OPENAI_COMPAT_URL", "http://localhost:8080"), \
             patch("config.OPENAI_COMPAT_KEY", ""), \
             patch("config.OPENAI_COMPAT_MODEL", "gpt-4o"):
            from backends.openai_compat import OpenAICompatBackend
            backend = OpenAICompatBackend()
            assert backend.name == "openai_compat"

    def test_call_sync_success(self):
        """call_sync should parse OpenAI-format response."""
        with patch("config.OPENAI_COMPAT_URL", "http://localhost:8080"), \
             patch("config.OPENAI_COMPAT_KEY", "test-key"), \
             patch("config.OPENAI_COMPAT_MODEL", "gpt-4o"):
            from backends.openai_compat import OpenAICompatBackend
            backend = OpenAICompatBackend()

            response_data = json.dumps({
                "choices": [{"message": {"content": "Hello from OpenAI"}}]
            }).encode()
            mock_resp = MagicMock()
            mock_resp.read.return_value = response_data
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)

            with patch("backends.openai_compat.urllib.request.urlopen", return_value=mock_resp):
                result = backend.call_sync("test prompt")

            assert result["result"] == "Hello from OpenAI"

    def test_call_sync_includes_auth_header(self):
        """call_sync should include Authorization header when key is set."""
        with patch("config.OPENAI_COMPAT_URL", "http://localhost:8080"), \
             patch("config.OPENAI_COMPAT_KEY", "sk-test-key"), \
             patch("config.OPENAI_COMPAT_MODEL", "gpt-4o"):
            from backends.openai_compat import OpenAICompatBackend
            backend = OpenAICompatBackend()

            response_data = json.dumps({
                "choices": [{"message": {"content": "ok"}}]
            }).encode()
            mock_resp = MagicMock()
            mock_resp.read.return_value = response_data
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)

            with patch("backends.openai_compat.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                backend.call_sync("test")
                req = mock_urlopen.call_args[0][0]
                assert req.get_header("Authorization") == "Bearer sk-test-key"
