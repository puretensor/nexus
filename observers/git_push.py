#!/usr/bin/env python3
"""Git push observer — Gitea webhook receiver.

Runs as a persistent HTTP server. On push webhook:
  1. Receives Gitea webhook POST
  2. Extracts repo, branch, commits, diff URL
  3. Fetches the diff (via Gitea API)
  4. Feeds to Claude for review summary
  5. Sends summary to Telegram

# Run: python3 observers/git_push.py
# Or as systemd service: git-push-observer.service
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
ENV_PATH = PROJECT_DIR / ".env"

LISTEN_PORT = int(os.environ.get("GIT_PUSH_PORT", "9876"))
GITEA_URL = os.environ.get("GITEA_URL", "http://MON1_TAILSCALE_IP:3002")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN", "REDACTED_GITEA_TOKEN")
WEBHOOK_SECRET = os.environ.get("GIT_PUSH_SECRET", "")  # optional HMAC verification
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", str(Path.home()))
MAX_DIFF_CHARS = 8000  # Truncate large diffs before sending to Claude


# ---------------------------------------------------------------------------
# Helpers (same pattern as other observers — no dependencies)
# ---------------------------------------------------------------------------


def load_env():
    """Parse .env file into a dict."""
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def send_telegram(token, chat_id, text, parse_mode=None):
    """Send a message via Telegram Bot API."""
    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, 4000)
        if idx == -1:
            idx = 4000
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")

    for chunk in chunks:
        params = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            params["parse_mode"] = parse_mode
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=15)


def call_claude(message, model="sonnet"):
    """Invoke claude -p and return the result text."""
    cmd = [
        CLAUDE_BIN, "-p", message,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", model,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, cwd=CLAUDE_CWD
        )
    except subprocess.TimeoutExpired:
        return "Claude timed out after 300s"

    if result.returncode != 0:
        return f"Claude error (exit {result.returncode}): {result.stderr[:500]}"

    try:
        data = json.loads(result.stdout)
        return data.get("result", "(empty response)")
    except json.JSONDecodeError:
        return f"Failed to parse Claude output: {result.stdout[:500]}"


# ---------------------------------------------------------------------------
# Diff fetching
# ---------------------------------------------------------------------------


def fetch_diff(repo_full_name, before, after):
    """Fetch the diff between two commits via the Gitea API.

    Tries the compare endpoint first, falls back to fetching the commit patch.
    Returns the diff as a string, truncated to MAX_DIFF_CHARS.
    """
    owner, repo = repo_full_name.split("/", 1)

    # Try compare endpoint first
    compare_url = (
        f"{GITEA_URL}/api/v1/repos/{owner}/{repo}/compare/{before}...{after}"
    )
    headers = {"Authorization": f"token {GITEA_TOKEN}"}

    try:
        req = urllib.request.Request(compare_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())

        # Extract diff from the compare response
        diff_parts = []
        for file_entry in data.get("files", []):
            filename = file_entry.get("filename", "unknown")
            patch = file_entry.get("patch", "")
            if patch:
                diff_parts.append(f"--- {filename} ---\n{patch}")

        if diff_parts:
            diff_text = "\n\n".join(diff_parts)
            if len(diff_text) > MAX_DIFF_CHARS:
                diff_text = diff_text[:MAX_DIFF_CHARS] + "\n... [truncated]"
            return diff_text
    except Exception:
        pass  # Fall through to commit patch fallback

    # Fallback: fetch the commit patch directly
    patch_url = f"{GITEA_URL}/api/v1/repos/{owner}/{repo}/git/commits/{after}"
    try:
        req = urllib.request.Request(patch_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())

        # Build a summary from the commit files
        diff_parts = []
        for f in data.get("files", []):
            filename = f.get("filename", "unknown")
            patch = f.get("patch", "")
            if patch:
                diff_parts.append(f"--- {filename} ---\n{patch}")

        if diff_parts:
            diff_text = "\n\n".join(diff_parts)
            if len(diff_text) > MAX_DIFF_CHARS:
                diff_text = diff_text[:MAX_DIFF_CHARS] + "\n... [truncated]"
            return diff_text
    except Exception as e:
        return f"(could not fetch diff: {e})"

    return "(no diff available)"


# ---------------------------------------------------------------------------
# Push processing
# ---------------------------------------------------------------------------


def process_push(payload):
    """Process a Gitea push webhook payload.

    Extracts repo, branch, commits, fetches the diff, asks Claude for a
    summary, and sends the result to Telegram.

    Returns the Telegram message text (for testing), or None if skipped.
    """
    ref = payload.get("ref", "")

    # Only process branch pushes, not tags
    if not ref.startswith("refs/heads/"):
        return None

    branch = ref.replace("refs/heads/", "")
    repo_full_name = payload.get("repository", {}).get("full_name", "unknown/unknown")
    commits = payload.get("commits", [])
    commit_count = len(commits)
    pusher = payload.get("pusher", {}).get("login", "unknown")
    before = payload.get("before", "")
    after = payload.get("after", "")

    # Extract commit messages
    commit_lines = []
    for c in commits:
        sha_short = c.get("id", "")[:7]
        msg = c.get("message", "").split("\n")[0]  # first line only
        commit_lines.append(f"  - {sha_short}: {msg}")

    commit_summary = "\n".join(commit_lines) if commit_lines else "(no commits)"

    # Fetch diff
    diff = ""
    if before and after and before != "0" * 40:
        diff = fetch_diff(repo_full_name, before, after)
    elif after:
        # New branch — try to get just the latest commit
        diff = fetch_diff(repo_full_name, after + "~1", after)

    # Ask Claude for review
    prompt = (
        f"Review this git push to {repo_full_name} on branch {branch}.\n"
        f"{commit_count} commit(s) by {pusher}:\n"
        f"{commit_summary}\n\n"
        f"Diff:\n{diff}\n\n"
        "Provide a concise 2-3 sentence summary of what changed and why. "
        "Focus on the substance, not the mechanics. Plain text, no markdown."
    )

    claude_summary = call_claude(prompt)

    # Build Telegram message
    header = f"\U0001f4e6 {repo_full_name} \u2192 {branch} ({commit_count} commit{'s' if commit_count != 1 else ''})"
    commit_list = "\n".join(
        f"  - {c.get('id', '')[:7]}: {c.get('message', '').split(chr(10))[0]}"
        for c in commits
    )

    message_parts = [header, "", claude_summary]
    if commit_list:
        message_parts.extend(["", "Commits:", commit_list])

    message = "\n".join(message_parts)

    # Send to Telegram
    env = load_env()
    token = env["TELEGRAM_BOT_TOKEN"]
    chat_id = env["AUTHORIZED_USER_ID"]
    send_telegram(token, chat_id, message)

    return message


# ---------------------------------------------------------------------------
# Webhook HTTP handler
# ---------------------------------------------------------------------------


def verify_signature(body, signature):
    """Verify the Gitea HMAC-SHA256 signature if a secret is configured."""
    if not WEBHOOK_SECRET:
        return True  # No secret configured, skip verification
    if not signature:
        return False
    expected = hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Gitea webhook POSTs."""

    def do_GET(self):
        """Health check endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_POST(self):
        """Handle Gitea webhook POST."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Empty body")
            return

        body = self.rfile.read(content_length)

        # Verify HMAC signature if configured
        signature = self.headers.get("X-Gitea-Signature", "")
        if not verify_signature(body, signature):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Invalid signature")
            return

        # Parse JSON
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        # Respond immediately, process in the handler thread
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Accepted")

        # Process the push
        try:
            result = process_push(payload)
            if result:
                self.log_message("Push processed: %s", result[:80])
            else:
                self.log_message("Push skipped (not a branch push)")
        except Exception as e:
            self.log_error("Error processing push: %s", str(e))

    def log_message(self, fmt, *args):
        """Override to add timestamp."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sys.stderr.write(f"[{timestamp}] {fmt % args}\n")

    log_error = log_message


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    load_env()  # Validate .env is readable at startup

    server = HTTPServer(("0.0.0.0", LISTEN_PORT), WebhookHandler)
    timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")

    print(f"[{timestamp}] Git push observer starting on port {LISTEN_PORT}")
    print(f"  Gitea URL: {GITEA_URL}")
    print(f"  HMAC verification: {'enabled' if WEBHOOK_SECRET else 'disabled'}")
    print(f"  Max diff chars: {MAX_DIFF_CHARS}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
