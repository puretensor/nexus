"""Tests for morning_brief.py observer.

Focus areas:
- Weather fetching (mock urllib)
- Prometheus query (mock urllib)
- Email fetching (mock imaplib)
- Brief assembly (all sources succeed)
- Brief assembly with partial failures
- send_telegram chunking
- call_claude success and failure paths
"""

import json
import subprocess
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "observers"))

# Patch config before importing observer classes
with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from observers.morning_brief import MorningBriefObserver


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def obs(tmp_path):
    """Create a MorningBriefObserver instance with temp accounts file."""
    with patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "fake:token",
        "AUTHORIZED_USER_ID": "12345",
    }):
        observer = MorningBriefObserver()
    observer.ACCOUNTS_FILE = tmp_path / "email_accounts.json"
    return observer


# ---------------------------------------------------------------------------
# Weather fetching
# ---------------------------------------------------------------------------

class TestFetchWeather:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = MorningBriefObserver()

    def _make_weather_response(self, **overrides):
        """Build a wttr.in JSON response."""
        data = {
            "current_condition": [{
                "temp_C": "12",
                "FeelsLikeC": "10",
                "weatherDesc": [{"value": "Partly cloudy"}],
                "humidity": "65",
                "windspeedKmph": "15",
            }],
            "weather": [{
                "maxtempC": "14",
                "mintempC": "7",
            }],
        }
        data.update(overrides)
        return data

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_weather_success(self, mock_urlopen):
        """Successful weather fetch returns formatted string."""
        response_data = self._make_weather_response()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp

        result = self.obs.fetch_weather()
        assert "London" in result
        assert "12" in result  # temp
        assert "Partly cloudy" in result
        assert "High: 14C" in result
        assert "Low: 7C" in result

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_weather_includes_humidity_and_wind(self, mock_urlopen):
        """Weather string includes humidity and wind speed."""
        response_data = self._make_weather_response()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp

        result = self.obs.fetch_weather()
        assert "65%" in result
        assert "15 km/h" in result

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_weather_no_forecast(self, mock_urlopen):
        """Weather works even without forecast data."""
        response_data = self._make_weather_response(weather=[])
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp

        result = self.obs.fetch_weather()
        assert "London" in result
        assert "12" in result
        # No High/Low when forecast is empty
        assert "High" not in result

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_weather_network_error(self, mock_urlopen):
        """Network error raises exception (caller handles it)."""
        mock_urlopen.side_effect = Exception("Connection refused")
        with pytest.raises(Exception, match="Connection refused"):
            self.obs.fetch_weather()

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_weather_custom_location(self, mock_urlopen):
        """Custom location from env var is used."""
        self.obs.WEATHER_LOCATION = "Reykjavik"
        response_data = self._make_weather_response()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp

        result = self.obs.fetch_weather()
        assert "Reykjavik" in result


# ---------------------------------------------------------------------------
# Prometheus / node health
# ---------------------------------------------------------------------------

class TestFetchNodeHealth:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = MorningBriefObserver()

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_all_nodes_up(self, mock_urlopen):
        """No down nodes returns all-clear message."""
        response = {"status": "success", "data": {"result": []}}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response).encode()
        mock_urlopen.return_value = mock_resp

        result = self.obs.fetch_node_health()
        assert "All monitored nodes are up" in result

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_nodes_down(self, mock_urlopen):
        """Down nodes are listed with instance and job."""
        response = {
            "status": "success",
            "data": {"result": [
                {"metric": {"instance": "192.168.4.100:9100", "job": "node"}, "value": [1, "0"]},
                {"metric": {"instance": "192.168.4.101:9100", "job": "node"}, "value": [1, "0"]},
            ]},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response).encode()
        mock_urlopen.return_value = mock_resp

        result = self.obs.fetch_node_health()
        assert "2 node(s) DOWN" in result
        assert "192.168.4.100:9100" in result
        assert "192.168.4.101:9100" in result
        assert "job: node" in result

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_prometheus_unreachable(self, mock_urlopen):
        """Prometheus connection failure raises exception."""
        mock_urlopen.side_effect = Exception("Connection timed out")
        with pytest.raises(Exception, match="Connection timed out"):
            self.obs.fetch_node_health()

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_single_node_down(self, mock_urlopen):
        """Single down node is reported correctly."""
        response = {
            "status": "success",
            "data": {"result": [
                {"metric": {"instance": "mon3:9100", "job": "infra"}, "value": [1, "0"]},
            ]},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response).encode()
        mock_urlopen.return_value = mock_resp

        result = self.obs.fetch_node_health()
        assert "1 node(s) DOWN" in result
        assert "mon3:9100" in result


# ---------------------------------------------------------------------------
# Email fetching
# ---------------------------------------------------------------------------

class TestFetchEmails:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = MorningBriefObserver()

    def test_no_accounts_file(self):
        """Missing accounts file returns informational message."""
        self.obs.ACCOUNTS_FILE = Path("/nonexistent/path")
        result = self.obs.fetch_emails()
        assert "not configured" in result.lower()

    @patch("observers.morning_brief.imaplib.IMAP4_SSL")
    def test_fetch_unread_emails(self, mock_imap_class, tmp_path):
        """Unread emails are fetched and formatted."""
        # Set up accounts file
        accounts_file = tmp_path / "email_accounts.json"
        accounts_file.write_text(json.dumps([{
            "name": "test-account",
            "server": "imap.example.com",
            "port": 993,
            "username": "user@example.com",
            "password": "secret",
        }]))
        self.obs.ACCOUNTS_FILE = accounts_file

        # Mock IMAP connection
        mock_conn = MagicMock()
        mock_imap_class.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.search.return_value = ("OK", [b"1 2"])

        # Build raw email header bytes
        header1 = b"From: alice@example.com\r\nSubject: Hello\r\nDate: Thu, 06 Feb 2026 07:00:00 +0000\r\n"
        header2 = b"From: bob@example.com\r\nSubject: Meeting\r\nDate: Thu, 06 Feb 2026 08:00:00 +0000\r\n"

        mock_conn.fetch.side_effect = [
            ("OK", [(b"2", header2)]),
            ("OK", [(b"1", header1)]),
        ]

        result = self.obs.fetch_emails()
        assert "2 unread emails" in result
        assert "alice@example.com" in result
        assert "bob@example.com" in result
        assert "Hello" in result
        assert "Meeting" in result
        assert "test-account" in result

    @patch("observers.morning_brief.imaplib.IMAP4_SSL")
    def test_no_unread_emails(self, mock_imap_class, tmp_path):
        """No unread emails returns appropriate message."""
        accounts_file = tmp_path / "email_accounts.json"
        accounts_file.write_text(json.dumps([{
            "name": "test",
            "server": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        }]))
        self.obs.ACCOUNTS_FILE = accounts_file

        mock_conn = MagicMock()
        mock_imap_class.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.search.return_value = ("OK", [b""])

        result = self.obs.fetch_emails()
        assert "No unread emails" in result

    @patch("observers.morning_brief.imaplib.IMAP4_SSL")
    def test_imap_connection_failure(self, mock_imap_class, tmp_path):
        """IMAP connection failure is handled gracefully."""
        accounts_file = tmp_path / "email_accounts.json"
        accounts_file.write_text(json.dumps([{
            "name": "broken-account",
            "server": "imap.broken.com",
            "username": "user@broken.com",
            "password": "secret",
        }]))
        self.obs.ACCOUNTS_FILE = accounts_file

        mock_imap_class.side_effect = Exception("Connection refused")

        result = self.obs.fetch_emails()
        # Should not crash, returns no-emails message since all accounts failed
        assert "No unread emails" in result

    @patch("observers.morning_brief.imaplib.IMAP4_SSL")
    def test_multiple_accounts_partial_failure(self, mock_imap_class, tmp_path):
        """One account fails, another succeeds -- emails from working account are returned."""
        accounts_file = tmp_path / "email_accounts.json"
        accounts_file.write_text(json.dumps([
            {
                "name": "broken",
                "server": "imap.broken.com",
                "username": "user@broken.com",
                "password": "secret",
            },
            {
                "name": "working",
                "server": "imap.working.com",
                "username": "user@working.com",
                "password": "secret",
            },
        ]))
        self.obs.ACCOUNTS_FILE = accounts_file

        # First call raises, second succeeds
        mock_conn_good = MagicMock()
        mock_imap_class.side_effect = [
            Exception("Connection refused"),
            mock_conn_good,
        ]
        mock_conn_good.select.return_value = ("OK", [b"1"])
        mock_conn_good.search.return_value = ("OK", [b"1"])

        header = b"From: ok@working.com\r\nSubject: Working\r\nDate: Thu, 06 Feb 2026 07:00:00 +0000\r\n"
        mock_conn_good.fetch.return_value = ("OK", [(b"1", header)])

        result = self.obs.fetch_emails()
        assert "1 unread emails" in result
        assert "working" in result
        assert "Working" in result


# ---------------------------------------------------------------------------
# Brief assembly -- all sources succeed (_gather_data and _build_prompt)
# ---------------------------------------------------------------------------

class TestBriefAssemblySuccess:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = MorningBriefObserver()

    @patch.object(MorningBriefObserver, "fetch_weather")
    @patch.object(MorningBriefObserver, "fetch_node_health")
    @patch.object(MorningBriefObserver, "fetch_emails")
    def test_all_sources_succeed(self, mock_emails, mock_nodes, mock_weather):
        """All sources succeed -- sections dict has all keys."""
        mock_emails.return_value = "3 unread emails:\n[personal] ..."
        mock_nodes.return_value = "All monitored nodes are up."
        mock_weather.return_value = "Weather in London: Sunny, 15C"

        sections = self.obs._gather_data()
        assert "emails" in sections
        assert "infrastructure" in sections
        assert "weather" in sections
        assert "3 unread emails" in sections["emails"]
        assert "All monitored nodes are up" in sections["infrastructure"]
        assert "Sunny" in sections["weather"]

    @patch.object(MorningBriefObserver, "fetch_weather")
    @patch.object(MorningBriefObserver, "fetch_node_health")
    @patch.object(MorningBriefObserver, "fetch_emails")
    def test_build_prompt_includes_all_sections(self, mock_emails, mock_nodes, mock_weather):
        """_build_prompt includes all section data and the final instruction."""
        mock_emails.return_value = "5 unread emails"
        mock_nodes.return_value = "All nodes up"
        mock_weather.return_value = "Sunny, 20C"

        sections = self.obs._gather_data()
        prompt = MorningBriefObserver._build_prompt(sections)

        assert "EMAILS" in prompt
        assert "5 unread emails" in prompt
        assert "INFRASTRUCTURE" in prompt
        assert "All nodes up" in prompt
        assert "WEATHER" in prompt
        assert "Sunny, 20C" in prompt
        assert "morning briefing" in prompt
        assert "Plain text, no markdown" in prompt


# ---------------------------------------------------------------------------
# Brief assembly with partial failures
# ---------------------------------------------------------------------------

class TestBriefAssemblyPartialFailure:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = MorningBriefObserver()

    @patch.object(MorningBriefObserver, "fetch_weather")
    @patch.object(MorningBriefObserver, "fetch_node_health")
    @patch.object(MorningBriefObserver, "fetch_emails")
    def test_email_fails_others_succeed(self, mock_emails, mock_nodes, mock_weather):
        """Email source fails -- brief still generated with infra and weather."""
        mock_emails.side_effect = Exception("IMAP total failure")
        mock_nodes.return_value = "All monitored nodes are up."
        mock_weather.return_value = "Weather in London: Rainy, 8C"

        sections = self.obs._gather_data()
        assert "failed" in sections["emails"].lower()
        assert "All monitored nodes are up" in sections["infrastructure"]
        assert "Rainy" in sections["weather"]

    @patch.object(MorningBriefObserver, "fetch_weather")
    @patch.object(MorningBriefObserver, "fetch_node_health")
    @patch.object(MorningBriefObserver, "fetch_emails")
    def test_prometheus_fails_others_succeed(self, mock_emails, mock_nodes, mock_weather):
        """Prometheus fails -- brief still generated with emails and weather."""
        mock_emails.return_value = "No unread emails."
        mock_nodes.side_effect = Exception("Connection refused")
        mock_weather.return_value = "Weather in London: Cloudy, 10C"

        sections = self.obs._gather_data()
        assert "No unread emails" in sections["emails"]
        assert "failed" in sections["infrastructure"].lower()
        assert "Cloudy" in sections["weather"]

    @patch.object(MorningBriefObserver, "fetch_weather")
    @patch.object(MorningBriefObserver, "fetch_node_health")
    @patch.object(MorningBriefObserver, "fetch_emails")
    def test_weather_fails_others_succeed(self, mock_emails, mock_nodes, mock_weather):
        """Weather fails -- brief still generated with emails and infra."""
        mock_emails.return_value = "2 unread emails:\n..."
        mock_nodes.return_value = "All monitored nodes are up."
        mock_weather.side_effect = Exception("DNS resolution failed")

        sections = self.obs._gather_data()
        assert "2 unread emails" in sections["emails"]
        assert "All monitored nodes are up" in sections["infrastructure"]
        assert "failed" in sections["weather"].lower()

    @patch.object(MorningBriefObserver, "fetch_weather")
    @patch.object(MorningBriefObserver, "fetch_node_health")
    @patch.object(MorningBriefObserver, "fetch_emails")
    def test_all_sources_fail(self, mock_emails, mock_nodes, mock_weather):
        """All sources fail -- sections still populated with error messages."""
        mock_emails.side_effect = Exception("Email down")
        mock_nodes.side_effect = Exception("Prometheus down")
        mock_weather.side_effect = Exception("Weather down")

        sections = self.obs._gather_data()
        assert "emails" in sections
        assert "infrastructure" in sections
        assert "weather" in sections
        assert "failed" in sections["emails"].lower()
        assert "failed" in sections["infrastructure"].lower()
        assert "failed" in sections["weather"].lower()

    @patch.object(MorningBriefObserver, "fetch_weather")
    @patch.object(MorningBriefObserver, "fetch_node_health")
    @patch.object(MorningBriefObserver, "fetch_emails")
    def test_partial_failure_prompt_still_valid(self, mock_emails, mock_nodes, mock_weather):
        """Prompt can be built even when some sources failed."""
        mock_emails.side_effect = Exception("boom")
        mock_nodes.return_value = "1 node(s) DOWN:\n  - mon3:9100 (job: infra)"
        mock_weather.side_effect = Exception("boom")

        sections = self.obs._gather_data()
        prompt = MorningBriefObserver._build_prompt(sections)

        # Prompt still has structure
        assert "EMAILS" in prompt
        assert "INFRASTRUCTURE" in prompt
        assert "WEATHER" in prompt
        assert "mon3:9100" in prompt
        assert "morning briefing" in prompt


# ---------------------------------------------------------------------------
# send_telegram chunking (now a method on Observer base class)
# ---------------------------------------------------------------------------

class TestSendTelegramChunking:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = MorningBriefObserver()

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_short_message_single_chunk(self, mock_req, mock_urlopen):
        """Short message sends as single request."""
        self.obs.send_telegram("Hello morning!")
        assert mock_req.call_count == 1

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_long_message_splits(self, mock_req, mock_urlopen):
        """Long message should be split into multiple chunks at 4000 chars."""
        msg = "x" * 10000
        self.obs.send_telegram(msg)
        assert mock_req.call_count == 3  # 4000 + 4000 + 2000

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_splits_on_newline(self, mock_req, mock_urlopen):
        """Long message splits at newline boundary when possible."""
        lines = ["Line " + str(i) + " " + "x" * 50 for i in range(100)]
        msg = "\n".join(lines)
        self.obs.send_telegram(msg)
        assert mock_req.call_count >= 2

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_empty_message(self, mock_req, mock_urlopen):
        """Empty message sends nothing (empty string is falsy in the while loop)."""
        self.obs.send_telegram("")
        assert mock_req.call_count == 0

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_exact_4000_chars(self, mock_req, mock_urlopen):
        """Exactly 4000 chars sends as single chunk."""
        msg = "x" * 4000
        self.obs.send_telegram(msg)
        assert mock_req.call_count == 1

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_unicode_in_message(self, mock_req, mock_urlopen):
        """Unicode characters survive URL encoding."""
        msg = "Good morning! Weather: 15\u00b0C, partly cloudy \u2014 no issues"
        self.obs.send_telegram(msg)
        assert mock_req.call_count == 1


# ---------------------------------------------------------------------------
# call_claude success and failure paths (now a method on Observer base class)
# ---------------------------------------------------------------------------

class TestCallClaude:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = MorningBriefObserver()

    @patch("engine.call_sync")
    def test_successful_call(self, mock_call_sync):
        """Successful Claude invocation returns result text."""
        mock_call_sync.return_value = {"result": "Good morning! Here is your brief..."}
        result = self.obs.call_claude("test prompt")
        assert result == "Good morning! Here is your brief..."

    @patch("engine.call_sync")
    def test_empty_result(self, mock_call_sync):
        """Missing result key returns empty string."""
        mock_call_sync.return_value = {}
        result = self.obs.call_claude("test")
        assert result == ""

    @patch("engine.call_sync")
    def test_model_argument_passed(self, mock_call_sync):
        """Model argument is forwarded to call_sync."""
        mock_call_sync.return_value = {"result": "OK"}
        self.obs.call_claude("test", model="opus")
        mock_call_sync.assert_called_once_with("test", model="opus", timeout=300)

    @patch("engine.call_sync")
    def test_unicode_in_prompt(self, mock_call_sync):
        """Unicode in prompt is passed through to call_sync."""
        mock_call_sync.return_value = {"result": "OK"}
        self.obs.call_claude("Weather: 15\u00b0C, humidity 80%")
        call_args = mock_call_sync.call_args
        assert "\u00b0" in call_args[0][0]


# ---------------------------------------------------------------------------
# decode_header (now a static method on MorningBriefObserver: _decode_header)
# ---------------------------------------------------------------------------

class TestDecodeHeader:

    def test_plain_ascii(self):
        assert MorningBriefObserver._decode_header("Hello World") == "Hello World"

    def test_none_returns_empty(self):
        assert MorningBriefObserver._decode_header(None) == ""

    def test_empty_returns_empty(self):
        assert MorningBriefObserver._decode_header("") == ""

    def test_utf8_encoded(self):
        raw = "=?UTF-8?Q?H=C3=A9llo?="
        result = MorningBriefObserver._decode_header(raw)
        assert "H\u00e9llo" in result
