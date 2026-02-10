#!/usr/bin/env python3
"""Git push observer — Gitea webhook receiver.

Runs as a persistent HTTP server (not cron-scheduled). On push webhook:
  1. Receives Gitea webhook POST
  2. Extracts repo, branch, commits, diff URL
  3. Fetches the diff (via Gitea API)
  4. Feeds to Claude for review summary
  5. Sends summary to Telegram

The registry runs this in its own thread via `persistent = True`.
Standalone: python3 observers/git_push.py
"""

import hashlib
import hmac
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from observers.base import Observer, ObserverResult

log = logging.getLogger("nexus")


class GitPushObserver(Observer):
    """Gitea webhook receiver — persistent HTTP server, not cron-scheduled."""

    name = "git_push"
    schedule = ""          # Empty — not cron-driven
    persistent = True      # Signal to registry: run() blocks, needs own thread

    # -- Config (class-level with env fallbacks) --
    LISTEN_PORT = int(os.environ.get("GIT_PUSH_PORT", "9876"))
    GITEA_URL = os.environ.get("GITEA_URL", "")
    GITEA_TOKEN = os.environ.get("GITEA_TOKEN", "")
    WEBHOOK_SECRET = os.environ.get("GIT_PUSH_SECRET", "")
    MAX_DIFF_CHARS = 8000

    def __init__(self):
        # Import config values if env vars weren't set
        from config import GITEA_URL as CFG_GITEA_URL, GITEA_TOKEN as CFG_GITEA_TOKEN
        if not self.GITEA_URL:
            self.GITEA_URL = CFG_GITEA_URL
        if not self.GITEA_TOKEN:
            self.GITEA_TOKEN = CFG_GITEA_TOKEN

    # ------------------------------------------------------------------
    # run() — starts the HTTP server (blocks forever)
    # ------------------------------------------------------------------

    def run(self, ctx=None) -> ObserverResult:
        """Start the webhook HTTP server. Blocks until interrupted."""
        # Bind the observer instance into the handler via a closure class
        observer = self

        class BoundHandler(WebhookHandler):
            obs = observer

        server = HTTPServer(("0.0.0.0", self.LISTEN_PORT), BoundHandler)
        timestamp = self.now_utc().strftime("%H:%M UTC")

        log.info(
            "[%s] Git push observer starting on port %d "
            "(HMAC: %s, max_diff: %d)",
            timestamp, self.LISTEN_PORT,
            "enabled" if self.WEBHOOK_SECRET else "disabled",
            self.MAX_DIFF_CHARS,
        )

        try:
            server.serve_forever()
        except Exception as e:
            log.error("Git push server died: %s", e)
            return ObserverResult(success=False, error=str(e))
        finally:
            server.server_close()

        return ObserverResult(success=True, message="Server stopped")

    # ------------------------------------------------------------------
    # Diff fetching
    # ------------------------------------------------------------------

    def fetch_diff(self, repo_full_name: str, before: str, after: str) -> str:
        """Fetch the diff between two commits via the Gitea API.

        Tries the compare endpoint first, falls back to fetching the commit patch.
        Returns the diff as a string, truncated to MAX_DIFF_CHARS.
        """
        owner, repo = repo_full_name.split("/", 1)
        headers = {"Authorization": f"token {self.GITEA_TOKEN}"}

        # Try compare endpoint first
        compare_url = (
            f"{self.GITEA_URL}/api/v1/repos/{owner}/{repo}"
            f"/compare/{before}...{after}"
        )
        try:
            req = urllib.request.Request(compare_url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())

            diff_parts = []
            for file_entry in data.get("files", []):
                filename = file_entry.get("filename", "unknown")
                patch = file_entry.get("patch", "")
                if patch:
                    diff_parts.append(f"--- {filename} ---\n{patch}")

            if diff_parts:
                diff_text = "\n\n".join(diff_parts)
                if len(diff_text) > self.MAX_DIFF_CHARS:
                    diff_text = diff_text[:self.MAX_DIFF_CHARS] + "\n... [truncated]"
                return diff_text
        except Exception:
            pass  # Fall through to commit patch fallback

        # Fallback: fetch the commit patch directly
        patch_url = (
            f"{self.GITEA_URL}/api/v1/repos/{owner}/{repo}"
            f"/git/commits/{after}"
        )
        try:
            req = urllib.request.Request(patch_url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())

            diff_parts = []
            for f in data.get("files", []):
                filename = f.get("filename", "unknown")
                patch = f.get("patch", "")
                if patch:
                    diff_parts.append(f"--- {filename} ---\n{patch}")

            if diff_parts:
                diff_text = "\n\n".join(diff_parts)
                if len(diff_text) > self.MAX_DIFF_CHARS:
                    diff_text = diff_text[:self.MAX_DIFF_CHARS] + "\n... [truncated]"
                return diff_text
        except Exception as e:
            return f"(could not fetch diff: {e})"

        return "(no diff available)"

    # ------------------------------------------------------------------
    # Push processing
    # ------------------------------------------------------------------

    def process_push(self, payload: dict) -> str | None:
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
        repo_full_name = payload.get("repository", {}).get(
            "full_name", "unknown/unknown"
        )
        commits = payload.get("commits", [])
        commit_count = len(commits)
        pusher = payload.get("pusher", {}).get("login", "unknown")
        before = payload.get("before", "")
        after = payload.get("after", "")

        # Extract commit messages
        commit_lines = []
        for c in commits:
            sha_short = c.get("id", "")[:7]
            msg = c.get("message", "").split("\n")[0]
            commit_lines.append(f"  - {sha_short}: {msg}")

        commit_summary = "\n".join(commit_lines) if commit_lines else "(no commits)"

        # Fetch diff
        diff = ""
        if before and after and before != "0" * 40:
            diff = self.fetch_diff(repo_full_name, before, after)
        elif after:
            # New branch -- try to get just the latest commit
            diff = self.fetch_diff(repo_full_name, after + "~1", after)

        # Ask Claude for review
        prompt = (
            f"Review this git push to {repo_full_name} on branch {branch}.\n"
            f"{commit_count} commit(s) by {pusher}:\n"
            f"{commit_summary}\n\n"
            f"Diff:\n{diff}\n\n"
            "Provide a concise 2-3 sentence summary of what changed and why. "
            "Focus on the substance, not the mechanics. Plain text, no markdown."
        )

        claude_summary = self.call_claude(prompt)

        # Build Telegram message
        header = (
            f"\U0001f4e6 {repo_full_name} \u2192 {branch} "
            f"({commit_count} commit{'s' if commit_count != 1 else ''})"
        )
        commit_list = "\n".join(
            f"  - {c.get('id', '')[:7]}: {c.get('message', '').split(chr(10))[0]}"
            for c in commits
        )

        message_parts = [header, "", claude_summary]
        if commit_list:
            message_parts.extend(["", "Commits:", commit_list])

        message = "\n".join(message_parts)

        # Send to Telegram (uses base class helper)
        self.send_telegram(message)

        return message


# ---------------------------------------------------------------------------
# Signature verification (standalone function — used by handler)
# ---------------------------------------------------------------------------


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify the Gitea HMAC-SHA256 signature if a secret is configured."""
    if not secret:
        return True  # No secret configured, skip verification
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Webhook HTTP handler
# ---------------------------------------------------------------------------


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Gitea webhook POSTs.

    The class attribute `obs` is bound to a GitPushObserver instance
    by the BoundHandler closure in run().
    """

    obs: GitPushObserver  # Set by BoundHandler subclass

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
        if not verify_signature(body, signature, self.obs.WEBHOOK_SECRET):
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
            result = self.obs.process_push(payload)
            if result:
                log.info("Push processed: %s", result[:80])
            else:
                log.info("Push skipped (not a branch push)")
        except Exception as e:
            log.error("Error processing push: %s", e, exc_info=True)

    def log_message(self, fmt, *args):
        """Route HTTP server logs through the nexus logger."""
        log.debug(fmt, *args)

    log_error = log_message


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import sys
    # Allow running standalone without the full NEXUS stack
    # Ensure the project root is on sys.path for config/observers imports
    from pathlib import Path
    project_root = Path(__file__).parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from config import log as _  # noqa: F401 — triggers logging setup

    observer = GitPushObserver()
    print(f"Starting git push observer on port {observer.LISTEN_PORT}...")
    print(f"  Gitea URL: {observer.GITEA_URL}")
    print(f"  HMAC verification: {'enabled' if observer.WEBHOOK_SECRET else 'disabled'}")
    print(f"  Max diff chars: {observer.MAX_DIFF_CHARS}")

    try:
        observer.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
