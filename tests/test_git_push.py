"""Tests for git_push.py observer -- Gitea webhook receiver.

Tests cover:
- process_push: basic flow, tag skipping, branch extraction, commit extraction
- fetch_diff: truncation, API fallback
- WebhookHandler: POST processing, GET health check
- Error handling: Claude failure, missing fields
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "observers"))

# Patch config before importing observer classes
with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from observers.git_push import (
        GitPushObserver,
        WebhookHandler,
        verify_signature,
    )


# ---------------------------------------------------------------------------
# Sample payload
# ---------------------------------------------------------------------------

SAMPLE_PUSH_PAYLOAD = {
    "ref": "refs/heads/master",
    "before": "abc123",
    "after": "def456",
    "compare_url": "http://gitea/repo/compare/abc123...def456",
    "commits": [
        {
            "id": "def456abcdef1234567890",
            "message": "Fix authentication bug",
            "author": {"name": "Heimir"},
            "timestamp": "2026-02-06T12:00:00Z",
        }
    ],
    "repository": {
        "full_name": "puretensor/hal-claude",
        "name": "hal-claude",
    },
    "pusher": {
        "login": "puretensor",
        "full_name": "Heimir",
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def obs():
    """Create a GitPushObserver instance."""
    with patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "fake:token",
        "AUTHORIZED_USER_ID": "12345",
    }):
        observer = GitPushObserver()
    return observer


# ---------------------------------------------------------------------------
# process_push (now a method on GitPushObserver)
# ---------------------------------------------------------------------------

class TestProcessPush:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = GitPushObserver()

    @patch.object(GitPushObserver, "send_telegram")
    @patch.object(GitPushObserver, "call_claude")
    @patch.object(GitPushObserver, "fetch_diff")
    def test_basic_push(self, mock_diff, mock_claude, mock_tg):
        """Basic push: diff fetched, Claude called, Telegram message sent."""
        mock_diff.return_value = "--- file.py ---\n+new line"
        mock_claude.return_value = "Fixed an auth bug in the login module."

        result = self.obs.process_push(SAMPLE_PUSH_PAYLOAD)

        assert result is not None
        assert "puretensor/hal-claude" in result
        assert "master" in result
        assert "1 commit" in result
        assert "Fixed an auth bug" in result
        assert "def456a" in result  # short SHA
        assert "Fix authentication bug" in result

        mock_diff.assert_called_once_with("puretensor/hal-claude", "abc123", "def456")
        mock_claude.assert_called_once()
        mock_tg.assert_called_once()

    @patch.object(GitPushObserver, "send_telegram")
    @patch.object(GitPushObserver, "call_claude")
    @patch.object(GitPushObserver, "fetch_diff")
    def test_skips_tags(self, mock_diff, mock_claude, mock_tg):
        """Tag pushes (refs/tags/*) should be skipped entirely."""
        payload = dict(SAMPLE_PUSH_PAYLOAD, ref="refs/tags/v1.0")
        result = self.obs.process_push(payload)

        assert result is None
        mock_diff.assert_not_called()
        mock_claude.assert_not_called()
        mock_tg.assert_not_called()

    @patch.object(GitPushObserver, "send_telegram")
    @patch.object(GitPushObserver, "call_claude")
    @patch.object(GitPushObserver, "fetch_diff")
    def test_extracts_branch(self, mock_diff, mock_claude, mock_tg):
        """Branch name correctly extracted from refs/heads/feature-xyz."""
        mock_diff.return_value = "(no diff)"
        mock_claude.return_value = "Summary"

        payload = dict(SAMPLE_PUSH_PAYLOAD, ref="refs/heads/feature/new-login")
        result = self.obs.process_push(payload)

        assert "feature/new-login" in result

    @patch.object(GitPushObserver, "send_telegram")
    @patch.object(GitPushObserver, "call_claude")
    @patch.object(GitPushObserver, "fetch_diff")
    def test_extracts_commits(self, mock_diff, mock_claude, mock_tg):
        """Commit messages are correctly extracted and included."""
        mock_diff.return_value = "(no diff)"
        mock_claude.return_value = "Summary"

        payload = dict(SAMPLE_PUSH_PAYLOAD, commits=[
            {
                "id": "aaa111bbbccc",
                "message": "First commit\n\nDetails here",
                "author": {"name": "Alice"},
                "timestamp": "2026-02-06T11:00:00Z",
            },
            {
                "id": "bbb222cccddd",
                "message": "Second commit",
                "author": {"name": "Bob"},
                "timestamp": "2026-02-06T12:00:00Z",
            },
        ])

        result = self.obs.process_push(payload)

        assert "aaa111b" in result  # short SHA
        assert "First commit" in result  # first line only
        assert "bbb222c" in result
        assert "Second commit" in result
        assert "Details here" not in result  # body stripped
        assert "2 commits" in result

    @patch.object(GitPushObserver, "send_telegram")
    @patch.object(GitPushObserver, "call_claude")
    @patch.object(GitPushObserver, "fetch_diff")
    def test_multiple_commits_plural(self, mock_diff, mock_claude, mock_tg):
        """Multiple commits show plural 'commits' not 'commit'."""
        mock_diff.return_value = "(no diff)"
        mock_claude.return_value = "Summary"

        payload = dict(SAMPLE_PUSH_PAYLOAD, commits=[
            {"id": "aaa111", "message": "A", "author": {"name": "X"}, "timestamp": ""},
            {"id": "bbb222", "message": "B", "author": {"name": "X"}, "timestamp": ""},
            {"id": "ccc333", "message": "C", "author": {"name": "X"}, "timestamp": ""},
        ])

        result = self.obs.process_push(payload)
        assert "3 commits" in result

    @patch.object(GitPushObserver, "send_telegram")
    @patch.object(GitPushObserver, "call_claude")
    @patch.object(GitPushObserver, "fetch_diff")
    def test_single_commit_singular(self, mock_diff, mock_claude, mock_tg):
        """Single commit shows singular 'commit' not 'commits'."""
        mock_diff.return_value = "(no diff)"
        mock_claude.return_value = "Summary"

        result = self.obs.process_push(SAMPLE_PUSH_PAYLOAD)
        assert "1 commit)" in result

    @patch.object(GitPushObserver, "send_telegram")
    @patch.object(GitPushObserver, "call_claude")
    @patch.object(GitPushObserver, "fetch_diff")
    def test_handles_claude_failure(self, mock_diff, mock_claude, mock_tg):
        """Claude failure still sends a partial message to Telegram."""
        mock_diff.return_value = "--- file.py ---\n+change"
        mock_claude.return_value = "Claude error (exit 1): API rate limit"

        result = self.obs.process_push(SAMPLE_PUSH_PAYLOAD)

        assert result is not None
        assert "puretensor/hal-claude" in result
        assert "Claude error" in result
        mock_tg.assert_called_once()

    @patch.object(GitPushObserver, "send_telegram")
    @patch.object(GitPushObserver, "call_claude")
    @patch.object(GitPushObserver, "fetch_diff")
    def test_skips_non_branch_ref(self, mock_diff, mock_claude, mock_tg):
        """Non-branch refs like refs/notes/* are skipped."""
        payload = dict(SAMPLE_PUSH_PAYLOAD, ref="refs/notes/commits")
        result = self.obs.process_push(payload)
        assert result is None

    @patch.object(GitPushObserver, "send_telegram")
    @patch.object(GitPushObserver, "call_claude")
    @patch.object(GitPushObserver, "fetch_diff")
    def test_new_branch_push(self, mock_diff, mock_claude, mock_tg):
        """New branch (before=0000...) still fetches diff for latest commit."""
        mock_diff.return_value = "(no diff)"
        mock_claude.return_value = "New branch created"

        payload = dict(SAMPLE_PUSH_PAYLOAD, before="0" * 40)
        result = self.obs.process_push(payload)

        assert result is not None
        # Should call fetch_diff with after~1 as fallback
        mock_diff.assert_called_once()

    @patch.object(GitPushObserver, "send_telegram")
    @patch.object(GitPushObserver, "call_claude")
    @patch.object(GitPushObserver, "fetch_diff")
    def test_empty_commits_list(self, mock_diff, mock_claude, mock_tg):
        """Empty commits list still processes the push."""
        mock_diff.return_value = "(no diff)"
        mock_claude.return_value = "Empty push"

        payload = dict(SAMPLE_PUSH_PAYLOAD, commits=[])
        result = self.obs.process_push(payload)

        assert result is not None
        assert "0 commits" in result


# ---------------------------------------------------------------------------
# fetch_diff (now a method on GitPushObserver)
# ---------------------------------------------------------------------------

class TestFetchDiff:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = GitPushObserver()

    @patch("observers.git_push.urllib.request.urlopen")
    def test_truncation(self, mock_urlopen):
        """Diffs exceeding MAX_DIFF_CHARS are truncated."""
        huge_patch = "x" * (self.obs.MAX_DIFF_CHARS + 5000)
        response_data = {
            "files": [{"filename": "big.py", "patch": huge_patch}]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp

        result = self.obs.fetch_diff("puretensor/hal-claude", "abc", "def")

        assert len(result) <= self.obs.MAX_DIFF_CHARS + 200  # Allow for header + truncation marker
        assert "[truncated]" in result

    @patch("observers.git_push.urllib.request.urlopen")
    def test_compare_success(self, mock_urlopen):
        """Successful compare API returns formatted diff."""
        response_data = {
            "files": [
                {"filename": "auth.py", "patch": "+    check_token()\n-    pass"},
                {"filename": "readme.md", "patch": "+Updated docs"},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp

        result = self.obs.fetch_diff("puretensor/hal-claude", "abc", "def")

        assert "auth.py" in result
        assert "check_token" in result
        assert "readme.md" in result
        assert "Updated docs" in result

    @patch("observers.git_push.urllib.request.urlopen")
    def test_compare_fails_falls_back(self, mock_urlopen):
        """When compare endpoint fails, falls back to commit endpoint."""
        # First call (compare) fails, second call (commit) succeeds
        fallback_data = {
            "files": [{"filename": "fallback.py", "patch": "+fallback line"}]
        }
        mock_resp_ok = MagicMock()
        mock_resp_ok.read.return_value = json.dumps(fallback_data).encode()

        mock_urlopen.side_effect = [
            Exception("404 Not Found"),
            mock_resp_ok,
        ]

        result = self.obs.fetch_diff("puretensor/hal-claude", "abc", "def")

        assert "fallback.py" in result
        assert "fallback line" in result

    @patch("observers.git_push.urllib.request.urlopen")
    def test_both_endpoints_fail(self, mock_urlopen):
        """When both endpoints fail, returns error message."""
        mock_urlopen.side_effect = Exception("Connection refused")

        result = self.obs.fetch_diff("puretensor/hal-claude", "abc", "def")

        assert "could not fetch diff" in result

    @patch("observers.git_push.urllib.request.urlopen")
    def test_empty_files(self, mock_urlopen):
        """Empty files list from compare endpoint falls through to fallback."""
        # Compare returns empty files
        compare_data = {"files": []}
        commit_data = {"files": [{"filename": "x.py", "patch": "+x"}]}

        mock_resp1 = MagicMock()
        mock_resp1.read.return_value = json.dumps(compare_data).encode()
        mock_resp2 = MagicMock()
        mock_resp2.read.return_value = json.dumps(commit_data).encode()

        mock_urlopen.side_effect = [mock_resp1, mock_resp2]

        result = self.obs.fetch_diff("puretensor/hal-claude", "abc", "def")
        assert "x.py" in result

    @patch("observers.git_push.urllib.request.urlopen")
    def test_no_patches_in_files(self, mock_urlopen):
        """Files with no patch field fall through to fallback."""
        compare_data = {"files": [{"filename": "binary.png"}]}
        commit_data = {"files": [{"filename": "code.py", "patch": "+hello"}]}

        mock_resp1 = MagicMock()
        mock_resp1.read.return_value = json.dumps(compare_data).encode()
        mock_resp2 = MagicMock()
        mock_resp2.read.return_value = json.dumps(commit_data).encode()

        mock_urlopen.side_effect = [mock_resp1, mock_resp2]

        result = self.obs.fetch_diff("puretensor/hal-claude", "abc", "def")
        assert "code.py" in result


# ---------------------------------------------------------------------------
# WebhookHandler
# ---------------------------------------------------------------------------

class TestWebhookHandler:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = GitPushObserver()

    def _make_handler(self, method, path, body=None, headers=None):
        """Create a mock WebhookHandler for testing."""
        handler = MagicMock(spec=WebhookHandler)
        handler.headers = headers or {}
        handler.path = path
        handler.command = method
        # Bind the observer instance
        handler.obs = self.obs

        # Mock response methods
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = io.BytesIO()
        handler.log_message = MagicMock()
        handler.log_error = MagicMock()

        if body:
            body_bytes = json.dumps(body).encode() if isinstance(body, dict) else body
            handler.rfile = io.BytesIO(body_bytes)
            handler.headers = {
                "Content-Length": str(len(body_bytes)),
                "Content-Type": "application/json",
                **(headers or {}),
            }
        else:
            handler.rfile = io.BytesIO(b"")
            handler.headers = {"Content-Length": "0", **(headers or {})}

        return handler

    def test_get_health_check(self):
        """GET request returns 200 OK health check."""
        handler = self._make_handler("GET", "/")
        WebhookHandler.do_GET(handler)

        handler.send_response.assert_called_with(200)
        output = handler.wfile.getvalue()
        assert output == b"OK"

    @patch.object(GitPushObserver, "process_push")
    def test_post_calls_process_push(self, mock_process):
        """Valid POST calls process_push with parsed payload."""
        mock_process.return_value = "Message sent"

        payload = SAMPLE_PUSH_PAYLOAD
        body_bytes = json.dumps(payload).encode()
        handler = self._make_handler("POST", "/", headers={
            "Content-Length": str(len(body_bytes)),
            "Content-Type": "application/json",
        })
        handler.rfile = io.BytesIO(body_bytes)

        WebhookHandler.do_POST(handler)

        handler.send_response.assert_called_with(200)
        mock_process.assert_called_once()
        # Verify the payload was parsed correctly
        call_payload = mock_process.call_args[0][0]
        assert call_payload["ref"] == "refs/heads/master"

    def test_post_empty_body(self):
        """POST with empty body returns 400."""
        handler = self._make_handler("POST", "/", headers={
            "Content-Length": "0",
        })

        WebhookHandler.do_POST(handler)

        handler.send_response.assert_called_with(400)

    @patch.object(GitPushObserver, "process_push")
    def test_post_invalid_json(self, mock_process):
        """POST with invalid JSON returns 400."""
        body_bytes = b"not valid json{{"
        handler = self._make_handler("POST", "/", headers={
            "Content-Length": str(len(body_bytes)),
        })
        handler.rfile = io.BytesIO(body_bytes)

        WebhookHandler.do_POST(handler)

        handler.send_response.assert_called_with(400)
        mock_process.assert_not_called()

    @patch.object(GitPushObserver, "process_push")
    def test_post_invalid_signature(self, mock_process):
        """POST with wrong HMAC signature returns 403."""
        self.obs.WEBHOOK_SECRET = "my-secret"
        body_bytes = json.dumps(SAMPLE_PUSH_PAYLOAD).encode()
        handler = self._make_handler("POST", "/", headers={
            "Content-Length": str(len(body_bytes)),
            "X-Gitea-Signature": "bad-signature",
        })
        handler.rfile = io.BytesIO(body_bytes)

        WebhookHandler.do_POST(handler)

        handler.send_response.assert_called_with(403)
        mock_process.assert_not_called()

    @patch.object(GitPushObserver, "process_push")
    def test_post_process_push_exception(self, mock_process):
        """Exception in process_push doesn't crash the handler."""
        mock_process.side_effect = RuntimeError("unexpected error")

        body_bytes = json.dumps(SAMPLE_PUSH_PAYLOAD).encode()
        handler = self._make_handler("POST", "/", headers={
            "Content-Length": str(len(body_bytes)),
            "Content-Type": "application/json",
        })
        handler.rfile = io.BytesIO(body_bytes)

        # Should not raise
        WebhookHandler.do_POST(handler)

        handler.send_response.assert_called_with(200)  # Already responded


# ---------------------------------------------------------------------------
# verify_signature (standalone function, not a method)
# ---------------------------------------------------------------------------

class TestVerifySignature:

    def test_no_secret_configured(self):
        """No secret configured -- always passes."""
        assert verify_signature(b"anything", "", "") is True
        assert verify_signature(b"anything", "whatever", "") is True

    def test_valid_signature(self):
        """Correct HMAC signature passes."""
        import hashlib
        import hmac as hmac_mod

        secret = "test-secret"
        body = b'{"ref":"refs/heads/main"}'
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()

        assert verify_signature(body, sig, secret) is True

    def test_invalid_signature(self):
        """Wrong HMAC signature fails."""
        assert verify_signature(b"body", "wrong-hex", "real-secret") is False

    def test_missing_signature_with_secret(self):
        """Missing signature when secret is configured fails."""
        assert verify_signature(b"body", "", "secret") is False


# ---------------------------------------------------------------------------
# send_telegram (via base class)
# ---------------------------------------------------------------------------

class TestSendTelegram:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = GitPushObserver()

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_short_message(self, mock_req, mock_urlopen):
        """Short message sends as single request."""
        self.obs.send_telegram("Hello")
        assert mock_req.call_count == 1

    @patch("observers.base.urllib.request.urlopen")
    @patch("observers.base.urllib.request.Request")
    def test_long_message_splits(self, mock_req, mock_urlopen):
        """Long message splits into multiple chunks."""
        msg = "x" * 10000
        self.obs.send_telegram(msg)
        assert mock_req.call_count == 3


# ---------------------------------------------------------------------------
# call_claude (via base class)
# ---------------------------------------------------------------------------

class TestCallClaude:

    @pytest.fixture(autouse=True)
    def make_observer(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "AUTHORIZED_USER_ID": "12345",
        }):
            self.obs = GitPushObserver()

    @patch("engine.call_sync")
    def test_successful_call(self, mock_call_sync):
        """Successful Claude call returns result text."""
        mock_call_sync.return_value = {"result": "This push fixes an auth bug."}
        result = self.obs.call_claude("test prompt")
        assert result == "This push fixes an auth bug."

    @patch("engine.call_sync")
    def test_empty_result(self, mock_call_sync):
        """Missing result key returns empty string."""
        mock_call_sync.return_value = {}
        result = self.obs.call_claude("test")
        assert result == ""
