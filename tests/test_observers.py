"""Tests for observer scripts — email_digest.py

Focus areas:
- Email header decoding (RFC 2047 encoded words, unicode)
- Telegram message chunking (via base class send_telegram)
- State management (seen message tracking)
- Claude invocation safety (via base class call_claude)
"""

import email.header
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "observers"))

# Patch config before importing observer classes
with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from observers.email_digest import EmailDigestObserver


# ---------------------------------------------------------------------------
# decode_header (now a static method on EmailDigestObserver)
# ---------------------------------------------------------------------------

class TestDecodeHeader:

    def test_plain_ascii(self):
        """Plain ASCII header passes through."""
        result = EmailDigestObserver.decode_header("Hello World")
        assert result == "Hello World"

    def test_none_returns_empty(self):
        """None input returns empty string."""
        result = EmailDigestObserver.decode_header(None)
        assert result == ""

    def test_empty_string(self):
        """Empty string returns empty string."""
        result = EmailDigestObserver.decode_header("")
        assert result == ""

    def test_utf8_encoded_header(self):
        """RFC 2047 UTF-8 encoded header should decode correctly."""
        raw = "=?UTF-8?Q?H=C3=A9llo_W=C3=B6rld?="
        result = EmailDigestObserver.decode_header(raw)
        assert "H\u00e9llo" in result
        assert "W\u00f6rld" in result

    def test_base64_encoded_header(self):
        """Base64 encoded header should decode."""
        import base64
        text = "\u65e5\u672c\u8a9e\u30c6\u30b9\u30c8"
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        raw = f"=?UTF-8?B?{encoded}?="
        result = EmailDigestObserver.decode_header(raw)
        assert "\u65e5\u672c\u8a9e\u30c6\u30b9\u30c8" in result

    def test_mixed_encoded_and_plain(self):
        """Headers with both encoded and plain parts."""
        raw = "Re: =?UTF-8?Q?Caf=C3=A9?= meeting"
        result = EmailDigestObserver.decode_header(raw)
        assert "Caf\u00e9" in result
        assert "Re:" in result or "meeting" in result

    def test_iso_8859_1_header(self):
        """ISO-8859-1 encoded header."""
        raw = "=?ISO-8859-1?Q?=E9t=E9?="  # "ete" with accents
        result = EmailDigestObserver.decode_header(raw)
        assert "\u00e9t\u00e9" in result

    def test_curly_quotes_in_subject(self):
        """Curly quotes in email subjects should survive decoding."""
        import base64
        subject = "\u201cImportant\u201d update \u2014 read now"
        encoded = base64.b64encode(subject.encode("utf-8")).decode("ascii")
        raw = f"=?UTF-8?B?{encoded}?="
        result = EmailDigestObserver.decode_header(raw)
        assert "\u201c" in result
        assert "\u201d" in result
        assert "\u2014" in result

    def test_invalid_charset_uses_replace(self):
        """Invalid charset should use replacement characters, not crash."""
        with patch("email.header.decode_header") as mock_decode:
            mock_decode.return_value = [(b"\xff\xfe", "utf-8")]
            result = EmailDigestObserver.decode_header("broken")
            assert isinstance(result, str)

    def test_emoji_in_subject(self):
        """Emoji in email subjects."""
        import base64
        subject = "Meeting tomorrow \U0001f389"
        encoded = base64.b64encode(subject.encode("utf-8")).decode("ascii")
        raw = f"=?UTF-8?B?{encoded}?="
        result = EmailDigestObserver.decode_header(raw)
        assert "\U0001f389" in result


# ---------------------------------------------------------------------------
# send_telegram chunking (now a method on Observer base class)
# ---------------------------------------------------------------------------

class TestSendTelegram:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        """Create an observer instance for testing."""
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = EmailDigestObserver()

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_short_message_single_chunk(self, mock_req, mock_urlopen):
        """Short message sends as single request."""
        self.obs.send_telegram("Hello")
        assert mock_req.call_count == 1

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_long_message_splits(self, mock_req, mock_urlopen):
        """Long message should be split into multiple chunks."""
        msg = "x" * 10000
        self.obs.send_telegram(msg)
        assert mock_req.call_count == 3  # 4000 + 4000 + 2000

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_unicode_in_telegram_message(self, mock_req, mock_urlopen):
        """Unicode characters should survive URL encoding."""
        msg = "Hello \u201cworld\u201d \u2014 it\u2019s great"
        self.obs.send_telegram(msg)
        assert mock_req.call_count == 1


# ---------------------------------------------------------------------------
# State management (now methods on EmailDigestObserver)
# ---------------------------------------------------------------------------

class TestStateManagement:

    @pytest.fixture(autouse=True)
    def use_temp_state(self, tmp_path):
        """Use temp directory for state files."""
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = EmailDigestObserver()

        state_dir = tmp_path / ".state"
        seen_file = state_dir / "email_seen.json"
        self.obs.STATE_DIR = state_dir
        self.obs.SEEN_FILE = seen_file
        self.state_dir = state_dir
        self.seen_file = seen_file

    def test_load_seen_no_file(self):
        """No file returns empty set."""
        result = self.obs.load_seen()
        assert result == set()

    def test_save_and_load_seen(self):
        """Round-trip save/load."""
        ids = {"msg-1", "msg-2", "msg-3"}
        self.obs.save_seen(ids)
        loaded = self.obs.load_seen()
        assert loaded == ids

    def test_save_trims_to_5000(self):
        """Save should trim to 5000 entries."""
        ids = {f"msg-{i}" for i in range(6000)}
        self.obs.save_seen(ids)
        loaded = self.obs.load_seen()
        assert len(loaded) <= 5000

    def test_load_corrupt_json(self):
        """Corrupt JSON file returns empty set."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.seen_file.write_text("not json at all")
        result = self.obs.load_seen()
        assert result == set()


# ---------------------------------------------------------------------------
# call_claude (now a method on Observer base class, calls engine.call_sync)
# ---------------------------------------------------------------------------

class TestCallClaude:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        """Create an observer instance for testing."""
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = EmailDigestObserver()

    @patch("engine.call_sync")
    def test_successful_call(self, mock_call_sync):
        """Successful Claude invocation returns result text."""
        mock_call_sync.return_value = {"result": "Hello from Claude"}
        result = self.obs.call_claude("test message")
        assert result == "Hello from Claude"

    @patch("engine.call_sync")
    def test_empty_result(self, mock_call_sync):
        """Missing result key returns empty string."""
        mock_call_sync.return_value = {}
        result = self.obs.call_claude("test")
        assert result == ""

    @patch("engine.call_sync")
    def test_passes_model_parameter(self, mock_call_sync):
        """Model parameter is forwarded to call_sync."""
        mock_call_sync.return_value = {"result": "OK"}
        self.obs.call_claude("test", model="opus")
        mock_call_sync.assert_called_once_with("test", model="opus", timeout=300)
