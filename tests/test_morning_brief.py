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
sys.path.insert(0, str(Path(__file__).parent.parent / "observers"))

from observers.morning_brief import (
    send_telegram,
    call_claude,
    load_env,
    fetch_emails,
    fetch_node_health,
    fetch_weather,
    gather_data,
    build_prompt,
    decode_header,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def use_temp_env(tmp_path, monkeypatch):
    """Provide a temp .env file for all tests."""
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=test-token\nAUTHORIZED_USER_ID=12345\n")
    monkeypatch.setattr("observers.morning_brief.ENV_PATH", env_file)


# ---------------------------------------------------------------------------
# Weather fetching
# ---------------------------------------------------------------------------

class TestFetchWeather:

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

        result = fetch_weather()
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

        result = fetch_weather()
        assert "65%" in result
        assert "15 km/h" in result

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_weather_no_forecast(self, mock_urlopen):
        """Weather works even without forecast data."""
        response_data = self._make_weather_response(weather=[])
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp

        result = fetch_weather()
        assert "London" in result
        assert "12" in result
        # No High/Low when forecast is empty
        assert "High" not in result

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_weather_network_error(self, mock_urlopen):
        """Network error raises exception (caller handles it)."""
        mock_urlopen.side_effect = Exception("Connection refused")
        with pytest.raises(Exception, match="Connection refused"):
            fetch_weather()

    @patch("observers.morning_brief.WEATHER_LOCATION", "Reykjavik")
    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_weather_custom_location(self, mock_urlopen):
        """Custom location from env var is used."""
        response_data = self._make_weather_response()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp

        result = fetch_weather()
        assert "Reykjavik" in result


# ---------------------------------------------------------------------------
# Prometheus / node health
# ---------------------------------------------------------------------------

class TestFetchNodeHealth:

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_all_nodes_up(self, mock_urlopen):
        """No down nodes returns all-clear message."""
        response = {"status": "success", "data": {"result": []}}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response).encode()
        mock_urlopen.return_value = mock_resp

        result = fetch_node_health()
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

        result = fetch_node_health()
        assert "2 node(s) DOWN" in result
        assert "192.168.4.100:9100" in result
        assert "192.168.4.101:9100" in result
        assert "job: node" in result

    @patch("observers.morning_brief.urllib.request.urlopen")
    def test_prometheus_unreachable(self, mock_urlopen):
        """Prometheus connection failure raises exception."""
        mock_urlopen.side_effect = Exception("Connection timed out")
        with pytest.raises(Exception, match="Connection timed out"):
            fetch_node_health()

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

        result = fetch_node_health()
        assert "1 node(s) DOWN" in result
        assert "mon3:9100" in result


# ---------------------------------------------------------------------------
# Email fetching
# ---------------------------------------------------------------------------

class TestFetchEmails:

    @patch("observers.morning_brief.ACCOUNTS_FILE", Path("/nonexistent/path"))
    def test_no_accounts_file(self):
        """Missing accounts file returns informational message."""
        result = fetch_emails()
        assert "not configured" in result.lower()

    @patch("observers.morning_brief.imaplib.IMAP4_SSL")
    def test_fetch_unread_emails(self, mock_imap_class, tmp_path, monkeypatch):
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
        monkeypatch.setattr("observers.morning_brief.ACCOUNTS_FILE", accounts_file)

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

        result = fetch_emails()
        assert "2 unread emails" in result
        assert "alice@example.com" in result
        assert "bob@example.com" in result
        assert "Hello" in result
        assert "Meeting" in result
        assert "test-account" in result

    @patch("observers.morning_brief.imaplib.IMAP4_SSL")
    def test_no_unread_emails(self, mock_imap_class, tmp_path, monkeypatch):
        """No unread emails returns appropriate message."""
        accounts_file = tmp_path / "email_accounts.json"
        accounts_file.write_text(json.dumps([{
            "name": "test",
            "server": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        }]))
        monkeypatch.setattr("observers.morning_brief.ACCOUNTS_FILE", accounts_file)

        mock_conn = MagicMock()
        mock_imap_class.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.search.return_value = ("OK", [b""])

        result = fetch_emails()
        assert "No unread emails" in result

    @patch("observers.morning_brief.imaplib.IMAP4_SSL")
    def test_imap_connection_failure(self, mock_imap_class, tmp_path, monkeypatch):
        """IMAP connection failure is handled gracefully."""
        accounts_file = tmp_path / "email_accounts.json"
        accounts_file.write_text(json.dumps([{
            "name": "broken-account",
            "server": "imap.broken.com",
            "username": "user@broken.com",
            "password": "secret",
        }]))
        monkeypatch.setattr("observers.morning_brief.ACCOUNTS_FILE", accounts_file)

        mock_imap_class.side_effect = Exception("Connection refused")

        result = fetch_emails()
        # Should not crash, returns no-emails message since all accounts failed
        assert "No unread emails" in result

    @patch("observers.morning_brief.imaplib.IMAP4_SSL")
    def test_multiple_accounts_partial_failure(self, mock_imap_class, tmp_path, monkeypatch):
        """One account fails, another succeeds — emails from working account are returned."""
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
        monkeypatch.setattr("observers.morning_brief.ACCOUNTS_FILE", accounts_file)

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

        result = fetch_emails()
        assert "1 unread emails" in result
        assert "working" in result
        assert "Working" in result


# ---------------------------------------------------------------------------
# Brief assembly — all sources succeed
# ---------------------------------------------------------------------------

class TestBriefAssemblySuccess:

    @patch("observers.morning_brief.fetch_weather")
    @patch("observers.morning_brief.fetch_node_health")
    @patch("observers.morning_brief.fetch_emails")
    def test_all_sources_succeed(self, mock_emails, mock_nodes, mock_weather):
        """All sources succeed — sections dict has all keys."""
        mock_emails.return_value = "3 unread emails:\n[personal] ..."
        mock_nodes.return_value = "All monitored nodes are up."
        mock_weather.return_value = "Weather in London: Sunny, 15C"

        sections = gather_data()
        assert "emails" in sections
        assert "infrastructure" in sections
        assert "weather" in sections
        assert "3 unread emails" in sections["emails"]
        assert "All monitored nodes are up" in sections["infrastructure"]
        assert "Sunny" in sections["weather"]

    @patch("observers.morning_brief.fetch_weather")
    @patch("observers.morning_brief.fetch_node_health")
    @patch("observers.morning_brief.fetch_emails")
    def test_build_prompt_includes_all_sections(self, mock_emails, mock_nodes, mock_weather):
        """build_prompt includes all section data and the final instruction."""
        mock_emails.return_value = "5 unread emails"
        mock_nodes.return_value = "All nodes up"
        mock_weather.return_value = "Sunny, 20C"

        sections = gather_data()
        prompt = build_prompt(sections)

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

    @patch("observers.morning_brief.fetch_weather")
    @patch("observers.morning_brief.fetch_node_health")
    @patch("observers.morning_brief.fetch_emails")
    def test_email_fails_others_succeed(self, mock_emails, mock_nodes, mock_weather):
        """Email source fails — brief still generated with infra and weather."""
        mock_emails.side_effect = Exception("IMAP total failure")
        mock_nodes.return_value = "All monitored nodes are up."
        mock_weather.return_value = "Weather in London: Rainy, 8C"

        sections = gather_data()
        assert "failed" in sections["emails"].lower()
        assert "All monitored nodes are up" in sections["infrastructure"]
        assert "Rainy" in sections["weather"]

    @patch("observers.morning_brief.fetch_weather")
    @patch("observers.morning_brief.fetch_node_health")
    @patch("observers.morning_brief.fetch_emails")
    def test_prometheus_fails_others_succeed(self, mock_emails, mock_nodes, mock_weather):
        """Prometheus fails — brief still generated with emails and weather."""
        mock_emails.return_value = "No unread emails."
        mock_nodes.side_effect = Exception("Connection refused")
        mock_weather.return_value = "Weather in London: Cloudy, 10C"

        sections = gather_data()
        assert "No unread emails" in sections["emails"]
        assert "failed" in sections["infrastructure"].lower()
        assert "Cloudy" in sections["weather"]

    @patch("observers.morning_brief.fetch_weather")
    @patch("observers.morning_brief.fetch_node_health")
    @patch("observers.morning_brief.fetch_emails")
    def test_weather_fails_others_succeed(self, mock_emails, mock_nodes, mock_weather):
        """Weather fails — brief still generated with emails and infra."""
        mock_emails.return_value = "2 unread emails:\n..."
        mock_nodes.return_value = "All monitored nodes are up."
        mock_weather.side_effect = Exception("DNS resolution failed")

        sections = gather_data()
        assert "2 unread emails" in sections["emails"]
        assert "All monitored nodes are up" in sections["infrastructure"]
        assert "failed" in sections["weather"].lower()

    @patch("observers.morning_brief.fetch_weather")
    @patch("observers.morning_brief.fetch_node_health")
    @patch("observers.morning_brief.fetch_emails")
    def test_all_sources_fail(self, mock_emails, mock_nodes, mock_weather):
        """All sources fail — sections still populated with error messages."""
        mock_emails.side_effect = Exception("Email down")
        mock_nodes.side_effect = Exception("Prometheus down")
        mock_weather.side_effect = Exception("Weather down")

        sections = gather_data()
        assert "emails" in sections
        assert "infrastructure" in sections
        assert "weather" in sections
        assert "failed" in sections["emails"].lower()
        assert "failed" in sections["infrastructure"].lower()
        assert "failed" in sections["weather"].lower()

    @patch("observers.morning_brief.fetch_weather")
    @patch("observers.morning_brief.fetch_node_health")
    @patch("observers.morning_brief.fetch_emails")
    def test_partial_failure_prompt_still_valid(self, mock_emails, mock_nodes, mock_weather):
        """Prompt can be built even when some sources failed."""
        mock_emails.side_effect = Exception("boom")
        mock_nodes.return_value = "1 node(s) DOWN:\n  - mon3:9100 (job: infra)"
        mock_weather.side_effect = Exception("boom")

        sections = gather_data()
        prompt = build_prompt(sections)

        # Prompt still has structure
        assert "EMAILS" in prompt
        assert "INFRASTRUCTURE" in prompt
        assert "WEATHER" in prompt
        assert "mon3:9100" in prompt
        assert "morning briefing" in prompt


# ---------------------------------------------------------------------------
# send_telegram chunking
# ---------------------------------------------------------------------------

class TestSendTelegramChunking:

    @patch("observers.morning_brief.urllib.request.urlopen")
    @patch("observers.morning_brief.urllib.request.Request")
    def test_short_message_single_chunk(self, mock_req, mock_urlopen):
        """Short message sends as single request."""
        send_telegram("token", "123", "Hello morning!")
        assert mock_req.call_count == 1

    @patch("observers.morning_brief.urllib.request.urlopen")
    @patch("observers.morning_brief.urllib.request.Request")
    def test_long_message_splits(self, mock_req, mock_urlopen):
        """Long message should be split into multiple chunks at 4000 chars."""
        msg = "x" * 10000
        send_telegram("token", "123", msg)
        assert mock_req.call_count == 3  # 4000 + 4000 + 2000

    @patch("observers.morning_brief.urllib.request.urlopen")
    @patch("observers.morning_brief.urllib.request.Request")
    def test_splits_on_newline(self, mock_req, mock_urlopen):
        """Long message splits at newline boundary when possible."""
        # Build a message with newlines — should split on newline
        lines = ["Line " + str(i) + " " + "x" * 50 for i in range(100)]
        msg = "\n".join(lines)
        send_telegram("token", "123", msg)
        assert mock_req.call_count >= 2

    @patch("observers.morning_brief.urllib.request.urlopen")
    @patch("observers.morning_brief.urllib.request.Request")
    def test_empty_message(self, mock_req, mock_urlopen):
        """Empty message sends nothing (empty string is falsy in the while loop)."""
        send_telegram("token", "123", "")
        assert mock_req.call_count == 0

    @patch("observers.morning_brief.urllib.request.urlopen")
    @patch("observers.morning_brief.urllib.request.Request")
    def test_exact_4000_chars(self, mock_req, mock_urlopen):
        """Exactly 4000 chars sends as single chunk."""
        msg = "x" * 4000
        send_telegram("token", "123", msg)
        assert mock_req.call_count == 1

    @patch("observers.morning_brief.urllib.request.urlopen")
    @patch("observers.morning_brief.urllib.request.Request")
    def test_unicode_in_message(self, mock_req, mock_urlopen):
        """Unicode characters survive URL encoding."""
        msg = "Good morning! Weather: 15\u00b0C, partly cloudy \u2014 no issues"
        send_telegram("token", "123", msg)
        assert mock_req.call_count == 1


# ---------------------------------------------------------------------------
# call_claude success and failure paths
# ---------------------------------------------------------------------------

class TestCallClaude:

    @patch("observers.morning_brief.subprocess.run")
    def test_successful_call(self, mock_run):
        """Successful Claude invocation returns result text."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "Good morning! Here is your brief..."}),
            stderr=""
        )
        result = call_claude("test prompt")
        assert result == "Good morning! Here is your brief..."

    @patch("observers.morning_brief.subprocess.run")
    def test_timeout(self, mock_run):
        """Timeout returns descriptive error, not exception."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=300)
        result = call_claude("test")
        assert "timed out" in result.lower()

    @patch("observers.morning_brief.subprocess.run")
    def test_nonzero_exit(self, mock_run):
        """Non-zero exit code returns error message."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="API rate limit exceeded"
        )
        result = call_claude("test")
        assert "error" in result.lower()
        assert "rate limit" in result.lower()

    @patch("observers.morning_brief.subprocess.run")
    def test_invalid_json_output(self, mock_run):
        """Invalid JSON output returns parse error message."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="this is not json at all",
            stderr=""
        )
        result = call_claude("test")
        assert "parse" in result.lower()

    @patch("observers.morning_brief.subprocess.run")
    def test_empty_result_field(self, mock_run):
        """Missing result field returns placeholder."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"other_field": "something"}),
            stderr=""
        )
        result = call_claude("test")
        assert result == "(empty response)"

    @patch("observers.morning_brief.subprocess.run")
    def test_model_argument_passed(self, mock_run):
        """Model argument is passed to Claude CLI."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "OK"}),
            stderr=""
        )
        call_claude("test", model="opus")
        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus"

    @patch("observers.morning_brief.subprocess.run")
    def test_unicode_in_prompt(self, mock_run):
        """Unicode in prompt is passed through to subprocess."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "OK"}),
            stderr=""
        )
        call_claude("Weather: 15\u00b0C, humidity 80%")
        cmd = mock_run.call_args[0][0]
        assert isinstance(cmd, list)
        assert any("\u00b0" in arg for arg in cmd)


# ---------------------------------------------------------------------------
# load_env
# ---------------------------------------------------------------------------

class TestLoadEnv:

    def test_basic_parsing(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env2"
        env_file.write_text("BOT_TOKEN=abc123\nCHAT_ID=999\n")
        monkeypatch.setattr("observers.morning_brief.ENV_PATH", env_file)
        result = load_env()
        assert result["BOT_TOKEN"] == "abc123"
        assert result["CHAT_ID"] == "999"

    def test_comments_and_blanks_ignored(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env2"
        env_file.write_text("# comment\n\nKEY=value\n\n# another comment\n")
        monkeypatch.setattr("observers.morning_brief.ENV_PATH", env_file)
        result = load_env()
        assert result["KEY"] == "value"
        assert len(result) == 1

    def test_equals_in_value(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env2"
        env_file.write_text("TOKEN=abc=def=ghi\n")
        monkeypatch.setattr("observers.morning_brief.ENV_PATH", env_file)
        result = load_env()
        assert result["TOKEN"] == "abc=def=ghi"


# ---------------------------------------------------------------------------
# decode_header
# ---------------------------------------------------------------------------

class TestDecodeHeader:

    def test_plain_ascii(self):
        assert decode_header("Hello World") == "Hello World"

    def test_none_returns_empty(self):
        assert decode_header(None) == ""

    def test_empty_returns_empty(self):
        assert decode_header("") == ""

    def test_utf8_encoded(self):
        raw = "=?UTF-8?Q?H=C3=A9llo?="
        result = decode_header(raw)
        assert "H\u00e9llo" in result
