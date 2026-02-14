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

    def test_gemini_api_implements_protocol(self):
        """GeminiAPIBackend satisfies the Backend protocol."""
        with patch("config.GEMINI_API_KEY", "test-key"):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()
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

    def test_gemini_api_creates_backend(self):
        """ENGINE_BACKEND=gemini_api creates GeminiAPIBackend."""
        with patch("config.ENGINE_BACKEND", "gemini_api"), \
             patch("config.GEMINI_API_KEY", "test-key"):
            backend = get_backend()
            assert backend.name == "gemini_api"

    def test_gemini_api_requires_key(self):
        """gemini_api backend requires GEMINI_API_KEY."""
        with patch("config.ENGINE_BACKEND", "gemini_api"), \
             patch("config.GEMINI_API_KEY", ""):
            with pytest.raises(ValueError, match="GEMINI_API_KEY"):
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
# GeminiCLIBackend
# ---------------------------------------------------------------------------

class TestGeminiCLIBackend:

    def test_name(self):
        backend = GeminiCLIBackend()
        assert backend.name == "gemini_cli"

    def test_supports_streaming(self):
        backend = GeminiCLIBackend()
        assert backend.supports_streaming is True

    def test_supports_tools(self):
        backend = GeminiCLIBackend()
        assert backend.supports_tools is True

    def test_supports_sessions(self):
        backend = GeminiCLIBackend()
        assert backend.supports_sessions is True

    def test_call_sync_not_found(self):
        """call_sync should handle missing binary gracefully."""
        backend = GeminiCLIBackend()

        with patch("backends.gemini_cli.subprocess.run", side_effect=FileNotFoundError):
            result = backend.call_sync("test")

        assert "not found" in result["result"].lower()

    def test_call_sync_parses_json(self):
        """call_sync should parse JSON response from Gemini CLI."""
        backend = GeminiCLIBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"response": "Hello from Gemini", "session_id": "gem-1"})
        mock_result.stderr = ""

        with patch("backends.gemini_cli.subprocess.run", return_value=mock_result):
            result = backend.call_sync("test prompt")

        assert result["result"] == "Hello from Gemini"
        assert result["session_id"] == "gem-1"

    def test_call_sync_correct_flags(self):
        """call_sync should use correct CLI flags (-p, --output-format json, --yolo)."""
        backend = GeminiCLIBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"response": "ok"})
        mock_result.stderr = ""

        with patch("backends.gemini_cli.subprocess.run", return_value=mock_result) as mock_run:
            backend.call_sync("test prompt")
            cmd = mock_run.call_args[0][0]
            assert "-p" in cmd
            assert "--output-format" in cmd
            idx = cmd.index("--output-format")
            assert cmd[idx + 1] == "json"
            assert "--yolo" in cmd

    def test_call_sync_session_flag(self):
        """call_sync should pass -r flag when session_id is provided."""
        backend = GeminiCLIBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"response": "ok"})
        mock_result.stderr = ""

        with patch("backends.gemini_cli.subprocess.run", return_value=mock_result) as mock_run:
            backend.call_sync("test", session_id="latest")
            cmd = mock_run.call_args[0][0]
            assert "-r" in cmd
            idx = cmd.index("-r")
            assert cmd[idx + 1] == "latest"

    def test_call_sync_model_flag(self):
        """call_sync should pass -m flag for model selection."""
        backend = GeminiCLIBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"response": "ok"})
        mock_result.stderr = ""

        with patch("backends.gemini_cli.subprocess.run", return_value=mock_result) as mock_run:
            backend.call_sync("test")
            cmd = mock_run.call_args[0][0]
            assert "-m" in cmd

    def test_call_sync_handles_timeout(self):
        """call_sync should handle subprocess timeout."""
        import subprocess
        backend = GeminiCLIBackend()

        with patch("backends.gemini_cli.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="gemini", timeout=300)):
            result = backend.call_sync("test", timeout=300)

        assert "timed out" in result["result"].lower()

    def test_call_sync_handles_nonzero_exit(self):
        """call_sync should handle non-zero exit code."""
        backend = GeminiCLIBackend()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "something went wrong"
        mock_result.stdout = ""

        with patch("backends.gemini_cli.subprocess.run", return_value=mock_result):
            result = backend.call_sync("test")

        assert "error" in result["result"].lower()


# ---------------------------------------------------------------------------
# CodexCLIBackend
# ---------------------------------------------------------------------------

class TestCodexCLIBackend:

    def test_name(self):
        backend = CodexCLIBackend()
        assert backend.name == "codex_cli"

    def test_supports_streaming(self):
        backend = CodexCLIBackend()
        assert backend.supports_streaming is True

    def test_supports_tools(self):
        backend = CodexCLIBackend()
        assert backend.supports_tools is True

    def test_no_sessions(self):
        backend = CodexCLIBackend()
        assert backend.supports_sessions is False

    def test_call_sync_not_found(self):
        """call_sync should handle missing binary gracefully."""
        backend = CodexCLIBackend()

        with patch("backends.codex_cli.subprocess.run", side_effect=FileNotFoundError):
            result = backend.call_sync("test")

        assert "not found" in result["result"].lower()

    def test_call_sync_uses_exec_subcommand(self):
        """call_sync should use 'exec' subcommand."""
        backend = CodexCLIBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"type": "message", "role": "assistant", "content": "ok"})
        mock_result.stderr = ""

        with patch("backends.codex_cli.subprocess.run", return_value=mock_result) as mock_run:
            backend.call_sync("test prompt")
            cmd = mock_run.call_args[0][0]
            assert cmd[1] == "exec"

    def test_call_sync_correct_flags(self):
        """call_sync should use correct flags (--json, --dangerously-bypass..., --skip-git-repo-check)."""
        backend = CodexCLIBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"type": "message", "role": "assistant", "content": "ok"})
        mock_result.stderr = ""

        with patch("backends.codex_cli.subprocess.run", return_value=mock_result) as mock_run:
            backend.call_sync("test prompt")
            cmd = mock_run.call_args[0][0]
            assert "--json" in cmd
            assert "--dangerously-bypass-approvals-and-sandbox" in cmd
            assert "--skip-git-repo-check" in cmd

    def test_call_sync_parses_jsonl(self):
        """call_sync should parse JSONL output from codex exec."""
        backend = CodexCLIBackend()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(
            {"type": "message", "role": "assistant", "content": "Hello from Codex"}
        )
        mock_result.stderr = ""

        with patch("backends.codex_cli.subprocess.run", return_value=mock_result):
            result = backend.call_sync("test prompt")

        assert result["result"] == "Hello from Codex"
        assert result["session_id"] is None

    def test_call_sync_handles_timeout(self):
        """call_sync should handle subprocess timeout."""
        import subprocess
        backend = CodexCLIBackend()

        with patch("backends.codex_cli.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=300)):
            result = backend.call_sync("test", timeout=300)

        assert "timed out" in result["result"].lower()

    def test_call_sync_handles_nonzero_exit(self):
        """call_sync should handle non-zero exit code."""
        backend = CodexCLIBackend()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "something went wrong"
        mock_result.stdout = ""

        with patch("backends.codex_cli.subprocess.run", return_value=mock_result):
            result = backend.call_sync("test")

        assert "error" in result["result"].lower()


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

    def test_supports_tools_default(self):
        with patch("config.ANTHROPIC_API_KEY", "test-key"):
            from backends.anthropic_api import AnthropicAPIBackend
            backend = AnthropicAPIBackend()
            assert backend.supports_tools is True

    def test_supports_tools_disabled(self):
        with patch("config.ANTHROPIC_API_KEY", "test-key"), \
             patch("config.API_TOOLS_ENABLED", False):
            from backends.anthropic_api import AnthropicAPIBackend
            backend = AnthropicAPIBackend()
            assert backend.supports_tools is False


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

    def test_supports_tools_default(self):
        with patch("config.OPENAI_COMPAT_URL", "http://localhost:8080"), \
             patch("config.OPENAI_COMPAT_KEY", ""), \
             patch("config.OPENAI_COMPAT_MODEL", "gpt-4o"):
            from backends.openai_compat import OpenAICompatBackend
            backend = OpenAICompatBackend()
            assert backend.supports_tools is True

    def test_supports_tools_disabled(self):
        with patch("config.OPENAI_COMPAT_URL", "http://localhost:8080"), \
             patch("config.OPENAI_COMPAT_KEY", ""), \
             patch("config.OPENAI_COMPAT_MODEL", "gpt-4o"), \
             patch("config.API_TOOLS_ENABLED", False):
            from backends.openai_compat import OpenAICompatBackend
            backend = OpenAICompatBackend()
            assert backend.supports_tools is False


# ---------------------------------------------------------------------------
# GeminiAPIBackend
# ---------------------------------------------------------------------------

class TestGeminiAPIBackend:

    def test_requires_api_key(self):
        """Should raise ValueError without API key."""
        with patch("config.GEMINI_API_KEY", ""):
            from backends.gemini_api import GeminiAPIBackend
            with pytest.raises(ValueError, match="GEMINI_API_KEY"):
                GeminiAPIBackend()

    def test_name(self):
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()
            assert backend.name == "gemini_api"

    def test_no_sessions(self):
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()
            assert backend.supports_sessions is False

    def test_supports_streaming(self):
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()
            assert backend.supports_streaming is True

    def test_supports_tools_default(self):
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()
            assert backend.supports_tools is True

    def test_supports_tools_disabled(self):
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"), \
             patch("config.API_TOOLS_ENABLED", False):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()
            assert backend.supports_tools is False

    def test_call_sync_success(self):
        """call_sync should parse Gemini response format."""
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()

            response_data = json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [{"text": "Hello from Gemini"}],
                        "role": "model",
                    }
                }]
            }).encode()
            mock_resp = MagicMock()
            mock_resp.read.return_value = response_data
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)

            with patch("backends.gemini_api.urllib.request.urlopen", return_value=mock_resp):
                result = backend.call_sync("test prompt")

            assert result["result"] == "Hello from Gemini"
            assert result["session_id"] is None

    def test_call_sync_includes_system_instruction(self):
        """call_sync should include systemInstruction when system_prompt is set."""
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()

            response_data = json.dumps({
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
            }).encode()
            mock_resp = MagicMock()
            mock_resp.read.return_value = response_data
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)

            with patch("backends.gemini_api.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                backend.call_sync("test", system_prompt="Be helpful")
                req = mock_urlopen.call_args[0][0]
                payload = json.loads(req.data.decode())
                assert "systemInstruction" in payload
                assert "Be helpful" in payload["systemInstruction"]["parts"][0]["text"]

    def test_call_sync_uses_correct_endpoint(self):
        """call_sync should use the correct Gemini generateContent endpoint."""
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()

            response_data = json.dumps({
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
            }).encode()
            mock_resp = MagicMock()
            mock_resp.read.return_value = response_data
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)

            with patch("backends.gemini_api.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                backend.call_sync("test")
                req = mock_urlopen.call_args[0][0]
                assert "generativelanguage.googleapis.com" in req.full_url
                assert "gemini-2.5-flash:generateContent" in req.full_url
                assert "key=test-key" in req.full_url

    def test_call_sync_connection_error(self):
        """call_sync should handle connection errors."""
        import urllib.error
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()

            with patch("backends.gemini_api.urllib.request.urlopen",
                       side_effect=urllib.error.URLError("Connection refused")):
                result = backend.call_sync("test")

            assert "error" in result["result"].lower()


# ---------------------------------------------------------------------------
# Shared tool infrastructure tests
# ---------------------------------------------------------------------------

class TestToolInfra:

    def test_toolcall_dataclass(self):
        """ToolCall should hold id, name, arguments."""
        from backends.tools import ToolCall
        tc = ToolCall(id="tc-1", name="bash", arguments={"command": "echo hi"})
        assert tc.id == "tc-1"
        assert tc.name == "bash"
        assert tc.arguments == {"command": "echo hi"}

    def test_schema_conversion_anthropic(self):
        """OpenAI schemas should convert to Anthropic format."""
        from backends.anthropic_api import _convert_schemas_to_anthropic
        from backends.tools import TOOL_SCHEMAS
        result = _convert_schemas_to_anthropic(TOOL_SCHEMAS)
        assert len(result) == 6
        assert result[0]["name"] == "bash"
        assert "input_schema" in result[0]
        assert "type" not in result[0]  # no "type": "function" wrapper

    def test_schema_conversion_gemini(self):
        """OpenAI schemas should convert to Gemini functionDeclarations format."""
        from backends.gemini_api import _convert_schemas_to_gemini
        from backends.tools import TOOL_SCHEMAS
        result = _convert_schemas_to_gemini(TOOL_SCHEMAS)
        assert len(result) == 6
        assert result[0]["name"] == "bash"
        assert "parameters" in result[0]
        assert "input_schema" not in result[0]

    def test_format_tool_status(self):
        """_format_tool_status should produce readable status lines."""
        from backends.tools import _format_tool_status
        assert "echo" in _format_tool_status("bash", {"command": "echo hi"})
        assert "Reading" in _format_tool_status("read_file", {"file_path": "/tmp/x"})
        assert "Writing" in _format_tool_status("write_file", {"file_path": "/tmp/x"})
        assert "Editing" in _format_tool_status("edit_file", {"file_path": "/tmp/x"})
        assert "Searching files" in _format_tool_status("glob", {"pattern": "*.py"})
        assert "Searching content" in _format_tool_status("grep", {"pattern": "foo"})


# ---------------------------------------------------------------------------
# OpenAI-compat tool-use tests
# ---------------------------------------------------------------------------

class TestOpenAICompatToolUse:

    def _make_backend(self):
        with patch("config.OPENAI_COMPAT_URL", "http://localhost:8080"), \
             patch("config.OPENAI_COMPAT_KEY", "test-key"), \
             patch("config.OPENAI_COMPAT_MODEL", "gpt-4o"):
            from backends.openai_compat import OpenAICompatBackend
            return OpenAICompatBackend()

    def test_call_sync_sends_tools(self):
        """call_sync should include tools in payload when enabled."""
        backend = self._make_backend()

        response_data = json.dumps({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("backends.openai_compat.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            backend.call_sync("test")
            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data.decode())
            assert "tools" in payload
            assert len(payload["tools"]) == 6

    def test_call_sync_no_tools_when_disabled(self):
        """call_sync should not include tools when disabled."""
        with patch("config.OPENAI_COMPAT_URL", "http://localhost:8080"), \
             patch("config.OPENAI_COMPAT_KEY", "test-key"), \
             patch("config.OPENAI_COMPAT_MODEL", "gpt-4o"), \
             patch("config.API_TOOLS_ENABLED", False):
            from backends.openai_compat import OpenAICompatBackend
            backend = OpenAICompatBackend()

        response_data = json.dumps({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("backends.openai_compat.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            backend.call_sync("test")
            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data.decode())
            assert "tools" not in payload

    def test_parse_response_text_only(self):
        """_parse_response should handle text-only responses."""
        backend = self._make_backend()
        response = {
            "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}]
        }
        text, tool_calls, msg = backend._parse_response(response)
        assert text == "Hello"
        assert tool_calls == []
        assert msg["role"] == "assistant"

    def test_parse_response_with_tool_calls(self):
        """_parse_response should extract tool calls."""
        backend = self._make_backend()
        response = {
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }
        text, tool_calls, msg = backend._parse_response(response)
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "bash"
        assert tool_calls[0].arguments == {"command": "ls"}
        assert tool_calls[0].id == "call_1"
        assert "tool_calls" in msg

    def test_format_tool_result(self):
        """_format_tool_result should produce correct message format."""
        from backends.openai_compat import OpenAICompatBackend
        result = OpenAICompatBackend._format_tool_result("bash", "call_1", "output")
        assert result["role"] == "tool"
        assert result["tool_call_id"] == "call_1"
        assert result["content"] == "output"

    def test_call_sync_tool_loop(self):
        """call_sync should execute tool calls and loop."""
        backend = self._make_backend()

        tool_response = json.dumps({
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "echo tool_test"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }).encode()

        final_response = json.dumps({
            "choices": [{"message": {"content": "The result is: tool_test"}, "finish_reason": "stop"}]
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

        with patch("backends.openai_compat.urllib.request.urlopen", side_effect=mock_urlopen):
            result = backend.call_sync("run echo tool_test")

        assert call_count[0] == 2
        assert result["result"] == "The result is: tool_test"

    def test_call_sync_written_files_tracked(self):
        """call_sync should track files written by write_file tool."""
        import tempfile
        backend = self._make_backend()

        tmpdir = tempfile.mkdtemp()
        target_path = f"{tmpdir}/test_output.txt"

        tool_response = json.dumps({
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"file_path": target_path, "content": "hello"}),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }).encode()

        final_response = json.dumps({
            "choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}]
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

        with patch("backends.openai_compat.urllib.request.urlopen", side_effect=mock_urlopen):
            result = backend.call_sync("write hello")

        assert target_path in result.get("written_files", [])

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Anthropic API tool-use tests
# ---------------------------------------------------------------------------

class TestAnthropicAPIToolUse:

    def _make_backend(self):
        with patch("config.ANTHROPIC_API_KEY", "test-key"):
            from backends.anthropic_api import AnthropicAPIBackend
            return AnthropicAPIBackend()

    def test_get_tools_returns_anthropic_format(self):
        """_get_tools should return Anthropic-format schemas."""
        backend = self._make_backend()
        tools = backend._get_tools()
        assert tools is not None
        assert len(tools) == 6
        assert tools[0]["name"] == "bash"
        assert "input_schema" in tools[0]
        assert "type" not in tools[0]  # not the OpenAI wrapper

    def test_get_tools_disabled(self):
        """_get_tools should return None when tools disabled."""
        with patch("config.ANTHROPIC_API_KEY", "test-key"), \
             patch("config.API_TOOLS_ENABLED", False):
            from backends.anthropic_api import AnthropicAPIBackend
            backend = AnthropicAPIBackend()
        assert backend._get_tools() is None

    def test_parse_response_text_only(self):
        """_parse_response should handle text-only responses."""
        backend = self._make_backend()
        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello from Claude"
        mock_response.content = [text_block]

        text, tool_calls, msg = backend._parse_response(mock_response)
        assert text == "Hello from Claude"
        assert tool_calls == []
        assert msg["role"] == "assistant"

    def test_parse_response_with_tool_use(self):
        """_parse_response should extract tool_use blocks."""
        backend = self._make_backend()
        mock_response = MagicMock()

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Let me check"

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "toolu_123"
        tool_block.name = "bash"
        tool_block.input = {"command": "ls"}

        mock_response.content = [text_block, tool_block]

        text, tool_calls, msg = backend._parse_response(mock_response)
        assert "Let me check" in text
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "bash"
        assert tool_calls[0].id == "toolu_123"
        assert tool_calls[0].arguments == {"command": "ls"}

    def test_format_tool_result(self):
        """_format_tool_result should produce Anthropic tool_result format."""
        from backends.anthropic_api import AnthropicAPIBackend
        result = AnthropicAPIBackend._format_tool_result("bash", "toolu_123", "file.txt")
        assert result["role"] == "user"
        assert result["content"][0]["type"] == "tool_result"
        assert result["content"][0]["tool_use_id"] == "toolu_123"
        assert result["content"][0]["content"] == "file.txt"

    def test_call_sync_tool_loop(self):
        """call_sync should execute tool calls via the shared tool loop."""
        backend = self._make_backend()

        # First response: tool_use
        tool_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = ""
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "toolu_1"
        tool_block.name = "bash"
        tool_block.input = {"command": "echo test"}
        tool_response.content = [text_block, tool_block]
        tool_response.stop_reason = "tool_use"

        # Second response: final text
        final_response = MagicMock()
        final_text = MagicMock()
        final_text.type = "text"
        final_text.text = "Result: test"
        final_response.content = [final_text]
        final_response.stop_reason = "end_turn"

        call_count = [0]
        def mock_create(**kwargs):
            if call_count[0] == 0:
                call_count[0] += 1
                return tool_response
            call_count[0] += 1
            return final_response

        mock_client = MagicMock()
        mock_client.messages.create = mock_create

        with patch.object(backend, "_get_client", return_value=mock_client):
            result = backend.call_sync("run echo test")

        assert call_count[0] == 2
        assert result["result"] == "Result: test"

    def test_call_sync_sends_tools_in_payload(self):
        """call_sync should pass tools to the API when enabled."""
        backend = self._make_backend()

        text_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "ok"
        text_response.content = [text_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = text_response

        with patch.object(backend, "_get_client", return_value=mock_client):
            backend.call_sync("test")
            call_kwargs = mock_client.messages.create.call_args[1]
            assert "tools" in call_kwargs
            assert len(call_kwargs["tools"]) == 6
            assert call_kwargs["max_tokens"] == 16384  # higher for tool use

    def test_call_sync_no_tools_when_disabled(self):
        """call_sync should not pass tools when disabled."""
        with patch("config.ANTHROPIC_API_KEY", "test-key"), \
             patch("config.API_TOOLS_ENABLED", False):
            from backends.anthropic_api import AnthropicAPIBackend
            backend = AnthropicAPIBackend()

        text_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "ok"
        text_response.content = [text_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = text_response

        with patch.object(backend, "_get_client", return_value=mock_client):
            backend.call_sync("test")
            call_kwargs = mock_client.messages.create.call_args[1]
            assert "tools" not in call_kwargs
            assert call_kwargs["max_tokens"] == 4096  # normal for no tools


# ---------------------------------------------------------------------------
# Gemini API tool-use tests
# ---------------------------------------------------------------------------

class TestGeminiAPIToolUse:

    def _make_backend(self):
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"):
            from backends.gemini_api import GeminiAPIBackend
            return GeminiAPIBackend()

    def test_get_tools_payload(self):
        """_get_tools_payload should return Gemini functionDeclarations format."""
        backend = self._make_backend()
        tools = backend._get_tools_payload()
        assert tools is not None
        assert len(tools) == 1
        decls = tools[0]["functionDeclarations"]
        assert len(decls) == 6
        assert decls[0]["name"] == "bash"
        assert "parameters" in decls[0]

    def test_get_tools_payload_disabled(self):
        """_get_tools_payload should return None when tools disabled."""
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"), \
             patch("config.API_TOOLS_ENABLED", False):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()
        assert backend._get_tools_payload() is None

    def test_parse_response_text_only(self):
        """_parse_response should handle text-only responses."""
        from backends.gemini_api import GeminiAPIBackend
        response = {
            "candidates": [{
                "content": {
                    "parts": [{"text": "Hello from Gemini"}],
                    "role": "model",
                }
            }]
        }
        text, tool_calls, msg = GeminiAPIBackend._parse_response(response)
        assert text == "Hello from Gemini"
        assert tool_calls == []
        assert msg["role"] == "model"

    def test_parse_response_with_function_call(self):
        """_parse_response should extract functionCall parts."""
        from backends.gemini_api import GeminiAPIBackend
        response = {
            "candidates": [{
                "content": {
                    "parts": [
                        {"functionCall": {"name": "bash", "args": {"command": "ls"}}},
                    ],
                    "role": "model",
                }
            }]
        }
        text, tool_calls, msg = GeminiAPIBackend._parse_response(response)
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "bash"
        assert tool_calls[0].arguments == {"command": "ls"}
        assert tool_calls[0].id  # should be a UUID string

    def test_parse_response_mixed(self):
        """_parse_response should handle text + functionCall mixed."""
        from backends.gemini_api import GeminiAPIBackend
        response = {
            "candidates": [{
                "content": {
                    "parts": [
                        {"text": "Let me check: "},
                        {"functionCall": {"name": "read_file", "args": {"file_path": "/tmp/x"}}},
                    ],
                    "role": "model",
                }
            }]
        }
        text, tool_calls, msg = GeminiAPIBackend._parse_response(response)
        assert "Let me check" in text
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "read_file"

    def test_format_tool_result(self):
        """_format_tool_result should produce Gemini functionResponse format."""
        from backends.gemini_api import GeminiAPIBackend
        result = GeminiAPIBackend._format_tool_result("bash", "any-id", "output text")
        assert result["role"] == "user"
        assert result["parts"][0]["functionResponse"]["name"] == "bash"
        assert result["parts"][0]["functionResponse"]["response"]["result"] == "output text"

    def test_call_sync_sends_tools(self):
        """call_sync should include tools in payload when enabled."""
        backend = self._make_backend()

        response_data = json.dumps({
            "candidates": [{
                "content": {"parts": [{"text": "ok"}], "role": "model"}
            }]
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("backends.gemini_api.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            backend.call_sync("test")
            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data.decode())
            assert "tools" in payload
            assert "functionDeclarations" in payload["tools"][0]
            assert len(payload["tools"][0]["functionDeclarations"]) == 6

    def test_call_sync_no_tools_when_disabled(self):
        """call_sync should not include tools when disabled."""
        with patch("config.GEMINI_API_KEY", "test-key"), \
             patch("config.GEMINI_API_MODEL", "gemini-2.5-flash"), \
             patch("config.API_TOOLS_ENABLED", False):
            from backends.gemini_api import GeminiAPIBackend
            backend = GeminiAPIBackend()

        response_data = json.dumps({
            "candidates": [{
                "content": {"parts": [{"text": "ok"}], "role": "model"}
            }]
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("backends.gemini_api.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            backend.call_sync("test")
            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data.decode())
            assert "tools" not in payload

    def test_call_sync_tool_loop(self):
        """call_sync should execute functionCall and loop."""
        backend = self._make_backend()

        tool_response_data = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"functionCall": {"name": "bash", "args": {"command": "echo gemini_test"}}}],
                    "role": "model",
                }
            }]
        }).encode()

        final_response_data = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": "The result is: gemini_test"}],
                    "role": "model",
                }
            }]
        }).encode()

        call_count = [0]
        def mock_urlopen(req, **kwargs):
            mock_resp = MagicMock()
            if call_count[0] == 0:
                mock_resp.read.return_value = tool_response_data
            else:
                mock_resp.read.return_value = final_response_data
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            call_count[0] += 1
            return mock_resp

        with patch("backends.gemini_api.urllib.request.urlopen", side_effect=mock_urlopen):
            result = backend.call_sync("run echo gemini_test")

        assert call_count[0] == 2
        assert result["result"] == "The result is: gemini_test"

    def test_call_sync_multi_turn_contents(self):
        """call_sync should build multi-turn contents with user/model/user roles."""
        backend = self._make_backend()

        tool_response_data = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"functionCall": {"name": "bash", "args": {"command": "echo x"}}}],
                    "role": "model",
                }
            }]
        }).encode()

        final_response_data = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": "done"}],
                    "role": "model",
                }
            }]
        }).encode()

        payloads_sent = []
        call_count = [0]
        def mock_urlopen(req, **kwargs):
            payloads_sent.append(json.loads(req.data.decode()))
            mock_resp = MagicMock()
            if call_count[0] == 0:
                mock_resp.read.return_value = tool_response_data
            else:
                mock_resp.read.return_value = final_response_data
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            call_count[0] += 1
            return mock_resp

        with patch("backends.gemini_api.urllib.request.urlopen", side_effect=mock_urlopen):
            backend.call_sync("test")

        # Second request should have 3 items in contents: user, model (functionCall), user (functionResponse)
        second_payload = payloads_sent[1]
        contents = second_payload["contents"]
        assert len(contents) == 3
        assert contents[0]["role"] == "user"       # original prompt
        assert contents[1]["role"] == "model"       # functionCall
        assert contents[2]["role"] == "user"        # functionResponse
        assert "functionResponse" in contents[2]["parts"][0]

    def test_call_sync_written_files_tracked(self):
        """call_sync should track files written by write_file tool."""
        import tempfile
        backend = self._make_backend()

        tmpdir = tempfile.mkdtemp()
        target_path = f"{tmpdir}/test_output.txt"

        tool_response_data = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{
                        "functionCall": {
                            "name": "write_file",
                            "args": {"file_path": target_path, "content": "hello"},
                        }
                    }],
                    "role": "model",
                }
            }]
        }).encode()

        final_response_data = json.dumps({
            "candidates": [{
                "content": {"parts": [{"text": "Done"}], "role": "model"}
            }]
        }).encode()

        call_count = [0]
        def mock_urlopen(req, **kwargs):
            mock_resp = MagicMock()
            if call_count[0] == 0:
                mock_resp.read.return_value = tool_response_data
            else:
                mock_resp.read.return_value = final_response_data
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            call_count[0] += 1
            return mock_resp

        with patch("backends.gemini_api.urllib.request.urlopen", side_effect=mock_urlopen):
            result = backend.call_sync("write hello")

        assert target_path in result.get("written_files", [])

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Shared tool loop tests
# ---------------------------------------------------------------------------

class TestRunToolLoopSync:

    def test_no_tool_calls_returns_immediately(self):
        """Should return text when no tool calls in first response."""
        from backends.tools import run_tool_loop_sync

        messages = [{"role": "user", "content": "hello"}]

        def send(msgs):
            return {"text": "Hi there"}

        def parse(resp):
            return resp["text"], [], {"role": "assistant", "content": resp["text"]}

        def fmt(name, cid, result):
            return {"role": "tool", "content": result}

        result = run_tool_loop_sync(messages, send, parse, fmt)
        assert result["result"] == "Hi there"
        assert result["written_files"] == []

    def test_tool_loop_executes_and_continues(self):
        """Should execute tools and continue to next iteration."""
        from backends.tools import run_tool_loop_sync, ToolCall

        messages = [{"role": "user", "content": "test"}]
        call_count = [0]

        def send(msgs):
            call_count[0] += 1
            return {"iteration": call_count[0]}

        def parse(resp):
            if resp["iteration"] == 1:
                tc = ToolCall(id="tc1", name="bash", arguments={"command": "echo hello"})
                return "", [tc], {"role": "assistant", "content": ""}
            return "Final answer", [], {"role": "assistant", "content": "Final answer"}

        def fmt(name, cid, result):
            return {"role": "tool", "content": result}

        result = run_tool_loop_sync(messages, send, parse, fmt)
        assert call_count[0] == 2
        assert result["result"] == "Final answer"

    def test_max_iterations_respected(self):
        """Should stop at max_iterations."""
        from backends.tools import run_tool_loop_sync, ToolCall

        messages = [{"role": "user", "content": "test"}]

        def send(msgs):
            return {}

        def parse(resp):
            tc = ToolCall(id="tc1", name="bash", arguments={"command": "echo loop"})
            return "looping", [tc], {"role": "assistant", "content": "looping"}

        def fmt(name, cid, result):
            return {"role": "tool", "content": result}

        result = run_tool_loop_sync(messages, send, parse, fmt, max_iterations=3)
        assert "looping" in result["result"] or "max" in result["result"].lower()
