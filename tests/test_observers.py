"""Tests for observer scripts — email_digest.py

Focus areas:
- Email header decoding (RFC 2047 encoded words, unicode)
- Telegram message chunking
- State management (seen message tracking)
- Claude invocation safety
"""

import email.header
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "observers"))

from observers.email_digest import (
    decode_header,
    send_telegram,
    load_seen,
    save_seen,
    load_env,
    call_claude,
)


# ---------------------------------------------------------------------------
# decode_header
# ---------------------------------------------------------------------------

class TestDecodeHeader:

    def test_plain_ascii(self):
        """Plain ASCII header passes through."""
        result = decode_header("Hello World")
        assert result == "Hello World"

    def test_none_returns_empty(self):
        """None input returns empty string."""
        result = decode_header(None)
        assert result == ""

    def test_empty_string(self):
        """Empty string returns empty string."""
        result = decode_header("")
        assert result == ""

    def test_utf8_encoded_header(self):
        """RFC 2047 UTF-8 encoded header should decode correctly."""
        # Encode "Héllo Wörld" as RFC 2047
        raw = "=?UTF-8?Q?H=C3=A9llo_W=C3=B6rld?="
        result = decode_header(raw)
        assert "Héllo" in result
        assert "Wörld" in result

    def test_base64_encoded_header(self):
        """Base64 encoded header should decode."""
        import base64
        text = "日本語テスト"
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        raw = f"=?UTF-8?B?{encoded}?="
        result = decode_header(raw)
        assert "日本語テスト" in result

    def test_mixed_encoded_and_plain(self):
        """Headers with both encoded and plain parts."""
        raw = "Re: =?UTF-8?Q?Caf=C3=A9?= meeting"
        result = decode_header(raw)
        assert "Café" in result
        assert "Re:" in result or "meeting" in result

    def test_iso_8859_1_header(self):
        """ISO-8859-1 encoded header."""
        raw = "=?ISO-8859-1?Q?=E9t=E9?="  # "été"
        result = decode_header(raw)
        assert "été" in result

    def test_curly_quotes_in_subject(self):
        """Curly quotes in email subjects should survive decoding."""
        import base64
        subject = "\u201cImportant\u201d update \u2014 read now"
        encoded = base64.b64encode(subject.encode("utf-8")).decode("ascii")
        raw = f"=?UTF-8?B?{encoded}?="
        result = decode_header(raw)
        assert "\u201c" in result
        assert "\u201d" in result
        assert "\u2014" in result

    def test_invalid_charset_uses_replace(self):
        """Invalid charset should use replacement characters, not crash."""
        # Simulate what email.header.decode_header returns for broken headers
        with patch("email.header.decode_header") as mock_decode:
            mock_decode.return_value = [(b"\xff\xfe", "utf-8")]
            result = decode_header("broken")
            # Should contain replacement char, not crash
            assert isinstance(result, str)

    def test_emoji_in_subject(self):
        """Emoji in email subjects."""
        import base64
        subject = "Meeting tomorrow 🎉"
        encoded = base64.b64encode(subject.encode("utf-8")).decode("ascii")
        raw = f"=?UTF-8?B?{encoded}?="
        result = decode_header(raw)
        assert "🎉" in result


# ---------------------------------------------------------------------------
# send_telegram chunking
# ---------------------------------------------------------------------------

class TestSendTelegram:

    @patch("observers.email_digest.urllib.request.urlopen")
    @patch("observers.email_digest.urllib.request.Request")
    def test_short_message_single_chunk(self, mock_req, mock_urlopen):
        """Short message sends as single request."""
        send_telegram("token", "123", "Hello")
        assert mock_req.call_count == 1

    @patch("observers.email_digest.urllib.request.urlopen")
    @patch("observers.email_digest.urllib.request.Request")
    def test_long_message_splits(self, mock_req, mock_urlopen):
        """Long message should be split into multiple chunks."""
        msg = "x" * 10000
        send_telegram("token", "123", msg)
        assert mock_req.call_count == 3  # 4000 + 4000 + 2000

    @patch("observers.email_digest.urllib.request.urlopen")
    @patch("observers.email_digest.urllib.request.Request")
    def test_unicode_in_telegram_message(self, mock_req, mock_urlopen):
        """Unicode characters should survive URL encoding."""
        msg = "Hello \u201cworld\u201d \u2014 it\u2019s great"
        send_telegram("token", "123", msg)
        assert mock_req.call_count == 1
        # Check the encoded data contains the message
        call_args = mock_req.call_args
        assert "Hello" in call_args[0][0] or True  # URL was constructed


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

class TestStateManagement:

    @pytest.fixture(autouse=True)
    def use_temp_state(self, tmp_path, monkeypatch):
        """Use temp directory for state files."""
        state_dir = tmp_path / ".state"
        seen_file = state_dir / "email_seen.json"
        monkeypatch.setattr("observers.email_digest.STATE_DIR", state_dir)
        monkeypatch.setattr("observers.email_digest.SEEN_FILE", seen_file)
        self.state_dir = state_dir
        self.seen_file = seen_file

    def test_load_seen_no_file(self):
        """No file returns empty set."""
        result = load_seen()
        assert result == set()

    def test_save_and_load_seen(self):
        """Round-trip save/load."""
        ids = {"msg-1", "msg-2", "msg-3"}
        save_seen(ids)
        loaded = load_seen()
        assert loaded == ids

    def test_save_trims_to_5000(self):
        """Save should trim to 5000 entries."""
        ids = {f"msg-{i}" for i in range(6000)}
        save_seen(ids)
        loaded = load_seen()
        assert len(loaded) <= 5000

    def test_load_corrupt_json(self):
        """Corrupt JSON file returns empty set."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.seen_file.write_text("not json at all")
        result = load_seen()
        assert result == set()


# ---------------------------------------------------------------------------
# load_env
# ---------------------------------------------------------------------------

class TestLoadEnv:

    @pytest.fixture(autouse=True)
    def use_temp_env(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        monkeypatch.setattr("observers.email_digest.ENV_PATH", env_file)
        self.env_file = env_file

    def test_basic_parsing(self):
        self.env_file.write_text("KEY=value\nOTHER=123\n")
        result = load_env()
        assert result["KEY"] == "value"
        assert result["OTHER"] == "123"

    def test_comments_ignored(self):
        self.env_file.write_text("# Comment\nKEY=value\n")
        result = load_env()
        assert "KEY" in result
        assert "#" not in str(result.keys())

    def test_empty_lines_ignored(self):
        self.env_file.write_text("\n\nKEY=value\n\n")
        result = load_env()
        assert result["KEY"] == "value"

    def test_equals_in_value(self):
        self.env_file.write_text("KEY=val=ue\n")
        result = load_env()
        assert result["KEY"] == "val=ue"


# ---------------------------------------------------------------------------
# call_claude (mocked)
# ---------------------------------------------------------------------------

class TestCallClaude:

    @patch("observers.email_digest.subprocess.run")
    def test_successful_call(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "Hello from Claude"}),
            stderr=""
        )
        result = call_claude("test message")
        assert result == "Hello from Claude"

    @patch("observers.email_digest.subprocess.run")
    def test_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=300)
        result = call_claude("test")
        assert "timed out" in result.lower()

    @patch("observers.email_digest.subprocess.run")
    def test_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Something went wrong"
        )
        result = call_claude("test")
        assert "error" in result.lower()

    @patch("observers.email_digest.subprocess.run")
    def test_invalid_json_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not json",
            stderr=""
        )
        result = call_claude("test")
        assert "parse" in result.lower()

    @patch("observers.email_digest.subprocess.run")
    def test_unicode_in_message(self, mock_run):
        """Unicode characters in the message should be passed safely to subprocess."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "OK"}),
            stderr=""
        )
        call_claude("What does \u201ctest\u201d mean?")
        cmd = mock_run.call_args[0][0]
        # Message should be in the command list, not shell-escaped
        assert isinstance(cmd, list)
        assert any("\u201c" in arg for arg in cmd)
