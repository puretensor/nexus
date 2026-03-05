#!/usr/bin/env python3
"""Git auto-sync observer — autonomous commit & push pipeline.

Runs every 4 hours from fox-n1. SSHes to TC, scans all tracked repos for
uncommitted changes, generates commit messages via Bedrock (Claude Sonnet),
commits, and pushes to all remotes (Gitea + GitHub).

Pre-commit security scan blocks any push containing leaked secrets.
Sends Telegram summary of all actions taken.
"""

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from observers.base import Observer, ObserverResult

log = logging.getLogger("nexus")

# SSH to tensor-core
TC_SSH = "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 puretensorai@localhost"

# Repos to auto-sync — path on TC, GitHub remote name, whether public
REPOS = [
    {"path": "nexus", "github_remote": "origin", "gitea_remote": "gitea", "public": True},
    {"path": "tensor-scripts", "github_remote": "github", "gitea_remote": "origin", "public": False},
    {"path": "voice-kb", "github_remote": "origin", "gitea_remote": "gitea", "public": False},
    {"path": "ecommerce-agent", "github_remote": "origin", "gitea_remote": "gitea", "public": False},
    {"path": "kalima", "github_remote": "origin", "gitea_remote": "gitea", "public": False},
    {"path": "arabic-qa", "github_remote": "github", "gitea_remote": "gitea", "public": False},
    {"path": "bookengine", "github_remote": "origin", "gitea_remote": None, "public": False},
    {"path": "echo-voicememo", "github_remote": "origin", "gitea_remote": "gitea", "public": False},
    {"path": "autopen", "github_remote": "origin", "gitea_remote": "gitea", "public": False},
]

# Minimum number of changed files to trigger a commit (avoid noise)
MIN_CHANGES = 1

# Secret patterns that BLOCK a commit (subset of git_security_audit patterns — high-confidence only)
BLOCK_PATTERNS = [
    r'sk-ant-api\d+-[A-Za-z0-9_-]{20,}',
    r'sk-proj-[A-Za-z0-9_-]{48,}',  # Real OpenAI keys are 48+ chars
    r'xai-[A-Za-z0-9_-]{40,}',       # Real xAI keys are 40+ chars
    r'AKIA[0-9A-Z]{16}',
    r'\d{10}:[A-Za-z0-9_-]{35}',     # Telegram bot token
    r'-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----',
]

BEDROCK_SYSTEM = (
    "You generate concise git commit messages for PureTensor infrastructure repos. "
    "Follow conventional commits format (feat:, fix:, chore:, refactor:, docs:). "
    "One line summary under 72 chars. If multiple areas changed, use the most "
    "significant change for the type. Return ONLY the commit message, nothing else."
)


class GitAutoSyncObserver(Observer):
    """Autonomous git commit and push for all tracked repos."""

    name = "git_auto_sync"
    schedule = "0 */4 * * *"  # Every 4 hours

    def _ssh_cmd(self, cmd: str, timeout: int = 60) -> tuple[int, str]:
        """Run a command on TC via SSH."""
        full_cmd = f'{TC_SSH} "{cmd}"'
        try:
            result = subprocess.run(
                full_cmd, shell=True, capture_output=True, text=True, timeout=timeout
            )
            return result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return -1, "SSH command timed out"
        except Exception as e:
            return -1, str(e)

    def _ssh_cmd_raw(self, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
        """Run command on TC, return (rc, stdout, stderr) separately."""
        full_cmd = f"{TC_SSH} {cmd}"
        try:
            result = subprocess.run(
                full_cmd, shell=True, capture_output=True, text=True, timeout=timeout
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "SSH command timed out"
        except Exception as e:
            return -1, "", str(e)

    def _get_dirty_repos(self) -> list[dict]:
        """Find repos with uncommitted changes."""
        dirty = []
        for repo in REPOS:
            repo_path = repo["path"]

            # Check repo exists
            rc, output = self._ssh_cmd(f"test -d ~/{repo_path}/.git && echo YES", timeout=5)
            if rc != 0 or "YES" not in output:
                continue

            # Get status
            rc, output = self._ssh_cmd(
                f"cd ~/{repo_path} && git status --porcelain 2>/dev/null",
                timeout=15,
            )
            if rc != 0:
                continue

            changes = [l.strip() for l in output.strip().split("\n") if l.strip()]
            if len(changes) >= MIN_CHANGES:
                dirty.append({
                    **repo,
                    "changes": changes,
                    "change_count": len(changes),
                })

        return dirty

    def _get_diff_summary(self, repo_path: str) -> str:
        """Get a compact diff summary for commit message generation."""
        rc, output = self._ssh_cmd(
            f"cd ~/{repo_path} && git diff --stat HEAD 2>/dev/null && echo '---UNTRACKED---' && git ls-files --others --exclude-standard 2>/dev/null",
            timeout=15,
        )
        if rc != 0:
            return ""

        # Also get a short content diff for context
        rc2, diff_content = self._ssh_cmd(
            f"cd ~/{repo_path} && git diff HEAD 2>/dev/null | head -200",
            timeout=15,
        )

        summary = output[:2000]
        if diff_content:
            summary += "\n\n--- Diff preview ---\n" + diff_content[:3000]

        return summary

    def _pre_commit_security_check(self, repo: dict) -> list[str]:
        """Scan staged changes for secrets. Returns list of violations."""
        repo_path = repo["path"]
        violations = []

        # Get the diff that would be committed
        rc, output = self._ssh_cmd(
            f"cd ~/{repo_path} && git diff HEAD 2>/dev/null && git diff --cached 2>/dev/null",
            timeout=30,
        )
        if rc != 0 or not output:
            return violations

        # Also check untracked files that will be added
        rc2, untracked = self._ssh_cmd(
            f"cd ~/{repo_path} && git ls-files --others --exclude-standard -z 2>/dev/null | xargs -0 cat 2>/dev/null | head -5000",
            timeout=30,
        )
        full_content = output + "\n" + (untracked or "")

        for pattern in BLOCK_PATTERNS:
            matches = re.findall(pattern, full_content)
            for match in matches:
                # Filter out obvious examples/placeholders
                if any(placeholder in match.lower() for placeholder in
                       ["replace_me", "your-key", "example", "...", "dummy", "xxx"]):
                    continue
                violations.append(f"Pattern: {pattern[:40]}... matched: {match[:50]}")

        return violations

    def _generate_commit_message(self, repo_path: str, diff_summary: str) -> str:
        """Generate a commit message using Bedrock, with fallback."""
        # Try Bedrock
        try:
            import boto3

            access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
            secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
            region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

            if access_key and secret_key:
                client = boto3.client(
                    "bedrock-runtime",
                    region_name=region,
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                )

                prompt = (
                    f"Repository: {repo_path}\n\n"
                    f"Changes:\n{diff_summary[:4000]}\n\n"
                    "Write a single-line conventional commit message (feat:/fix:/chore:/refactor:/docs:). "
                    "Max 72 chars. Return ONLY the message."
                )

                response = client.converse(
                    modelId="us.anthropic.claude-haiku-4-5-20251001",
                    system=[{"text": BEDROCK_SYSTEM}],
                    messages=[{"role": "user", "content": [{"text": prompt}]}],
                    inferenceConfig={"temperature": 0.3, "maxTokens": 100},
                )

                output = response.get("output", {})
                message = output.get("message", {})
                content_blocks = message.get("content", [])
                texts = [b["text"] for b in content_blocks if "text" in b]
                msg = " ".join(texts).strip().strip('"').strip("'")

                # Validate — should be a single line, not too long
                if msg and "\n" not in msg and len(msg) < 120:
                    return msg
                # Take first line if multi-line
                if msg:
                    return msg.split("\n")[0][:72]

        except Exception as e:
            log.warning("Bedrock commit message generation failed: %s", e)

        # Fallback: generate from change summary
        return self._fallback_commit_message(repo_path, diff_summary)

    def _fallback_commit_message(self, repo_path: str, diff_summary: str) -> str:
        """Generate a descriptive commit message without LLM."""
        lines = diff_summary.split("\n")
        file_changes = []
        for line in lines:
            line = line.strip()
            if line and "|" in line and not line.startswith("---"):
                # git diff --stat line: filename | N ++++---
                parts = line.split("|")
                if parts:
                    fname = parts[0].strip()
                    if fname and not fname.startswith("---"):
                        file_changes.append(fname)

        if not file_changes:
            return f"chore: update {repo_path}"

        # Determine type from file extensions/paths
        py_files = [f for f in file_changes if f.endswith(".py")]
        config_files = [f for f in file_changes if any(f.endswith(e) for e in
                        [".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".conf"])]
        doc_files = [f for f in file_changes if any(f.endswith(e) for e in [".md", ".txt", ".rst"])]

        if len(py_files) > len(config_files) and len(py_files) > len(doc_files):
            prefix = "feat" if len(py_files) > 3 else "chore"
        elif len(config_files) > 0:
            prefix = "chore"
        elif len(doc_files) > 0:
            prefix = "docs"
        else:
            prefix = "chore"

        # Summarize what changed
        if len(file_changes) == 1:
            return f"{prefix}: update {file_changes[0]}"
        elif len(file_changes) <= 3:
            names = ", ".join(os.path.basename(f) for f in file_changes)
            return f"{prefix}: update {names}"
        else:
            # Group by directory
            dirs = set(os.path.dirname(f) or "root" for f in file_changes)
            if len(dirs) == 1:
                return f"{prefix}: update {len(file_changes)} files in {list(dirs)[0]}"
            else:
                return f"{prefix}: update {len(file_changes)} files across {len(dirs)} directories"

    def _commit_and_push(self, repo: dict) -> dict:
        """Stage, commit, and push changes for a single repo."""
        repo_path = repo["path"]
        result = {
            "repo": repo_path,
            "committed": False,
            "pushed_gitea": False,
            "pushed_github": False,
            "message": "",
            "error": "",
            "files_changed": repo.get("change_count", 0),
        }

        # 1. Pre-commit security check
        violations = self._pre_commit_security_check(repo)
        if violations:
            result["error"] = f"BLOCKED — secrets detected: {'; '.join(violations[:3])}"
            log.warning("Security block on %s: %s", repo_path, violations)
            return result

        # 2. Get diff summary for commit message
        diff_summary = self._get_diff_summary(repo_path)

        # 3. Generate commit message
        commit_msg = self._generate_commit_message(repo_path, diff_summary)
        result["message"] = commit_msg

        # 4. Stage all changes (respecting .gitignore)
        rc, output = self._ssh_cmd(
            f"cd ~/{repo_path} && git add -A 2>&1",
            timeout=30,
        )
        if rc != 0:
            result["error"] = f"git add failed: {output[:200]}"
            return result

        # 5. Commit
        # Use heredoc-style to avoid shell escaping issues
        escaped_msg = commit_msg.replace('"', '\\"').replace("'", "'\\''")
        co_author = "Co-Authored-By: HAL <hal@example.com>"
        rc, output = self._ssh_cmd(
            f"cd ~/{repo_path} && git commit -m '{escaped_msg}' -m '{co_author}' 2>&1",
            timeout=30,
        )
        if rc != 0:
            if "nothing to commit" in output:
                result["message"] = "nothing to commit"
                return result
            result["error"] = f"git commit failed: {output[:200]}"
            return result

        result["committed"] = True

        # 6. Push to Gitea
        if repo.get("gitea_remote"):
            rc, output = self._ssh_cmd(
                f"cd ~/{repo_path} && git push {repo['gitea_remote']} HEAD 2>&1",
                timeout=60,
            )
            result["pushed_gitea"] = (rc == 0)
            if rc != 0:
                log.warning("Gitea push failed for %s: %s", repo_path, output[:200])

        # 7. Push to GitHub
        if repo.get("github_remote"):
            rc, output = self._ssh_cmd(
                f"cd ~/{repo_path} && git push {repo['github_remote']} HEAD 2>&1",
                timeout=60,
            )
            result["pushed_github"] = (rc == 0)
            if rc != 0:
                log.warning("GitHub push failed for %s: %s", repo_path, output[:200])

        return result

    # -- State tracking --------------------------------------------------------

    def _state_file(self) -> Path:
        state_dir = Path(os.environ.get("OBSERVER_STATE_DIR", "/data/state/observers"))
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "git_auto_sync_state.json"

    def _load_state(self) -> dict:
        sf = self._state_file()
        if sf.exists():
            try:
                return json.loads(sf.read_text())
            except Exception:
                pass
        return {"total_syncs": 0, "total_commits": 0, "total_pushes": 0}

    def _save_state(self, state: dict):
        self._state_file().write_text(json.dumps(state, indent=2))

    # -- Observer interface ----------------------------------------------------

    def run(self, ctx=None) -> ObserverResult:
        """Execute the git auto-sync pipeline."""
        now = self.now_utc()
        log.info("Git auto-sync starting at %s", now.strftime("%H:%M UTC"))

        state = self._load_state()

        # Test SSH connectivity
        rc, output = self._ssh_cmd("echo OK", timeout=10)
        if rc != 0 or "OK" not in output:
            log.warning("Cannot SSH to TC: %s", output[:200])
            return ObserverResult(
                success=False,
                error="SSH to TC failed — cannot run git sync",
            )

        # Find repos with uncommitted changes
        dirty_repos = self._get_dirty_repos()

        if not dirty_repos:
            log.info("Git auto-sync: all repos clean, nothing to do")
            state["last_run"] = now.isoformat()
            state["total_syncs"] = state.get("total_syncs", 0) + 1
            self._save_state(state)
            return ObserverResult(success=True)

        log.info("Found %d repos with uncommitted changes", len(dirty_repos))

        # Process each dirty repo
        results = []
        for repo in dirty_repos:
            log.info("Processing %s (%d changes)", repo["path"], repo["change_count"])
            result = self._commit_and_push(repo)
            results.append(result)
            # Small delay between repos to avoid SSH hammering
            time.sleep(2)

        # Summarize
        committed = [r for r in results if r["committed"]]
        blocked = [r for r in results if r.get("error") and "BLOCKED" in r["error"]]
        failed = [r for r in results if r.get("error") and "BLOCKED" not in r["error"] and not r["committed"]]

        # Update state
        state["last_run"] = now.isoformat()
        state["total_syncs"] = state.get("total_syncs", 0) + 1
        state["total_commits"] = state.get("total_commits", 0) + len(committed)
        state["total_pushes"] = state.get("total_pushes", 0) + sum(
            1 for r in committed if r["pushed_gitea"] or r["pushed_github"]
        )
        self._save_state(state)

        # Telegram notification
        lines = [f"GIT AUTO-SYNC — {now.strftime('%H:%M UTC')}", ""]

        if committed:
            for r in committed:
                push_status = []
                if r["pushed_gitea"]:
                    push_status.append("Gitea")
                if r["pushed_github"]:
                    push_status.append("GitHub")
                push_str = ", ".join(push_status) if push_status else "push failed"
                lines.append(f"  {r['repo']}: {r['message']}")
                lines.append(f"    {r['files_changed']} files -> {push_str}")

        if blocked:
            lines.append("")
            lines.append("BLOCKED (secrets detected):")
            for r in blocked:
                lines.append(f"  {r['repo']}: {r['error'][:80]}")

        if failed:
            lines.append("")
            lines.append("Failed:")
            for r in failed:
                lines.append(f"  {r['repo']}: {r['error'][:80]}")

        if not committed and not blocked and not failed:
            lines.append("  All repos clean — nothing to commit")

        self.send_telegram("\n".join(lines))

        total_committed = len(committed)
        total_pushed = sum(1 for r in committed if r["pushed_github"])

        return ObserverResult(
            success=len(blocked) == 0,  # Blocked = not fully successful
            message=f"Git sync: {total_committed} committed, {total_pushed} pushed to GitHub, {len(blocked)} blocked",
            data={
                "committed": total_committed,
                "pushed_github": total_pushed,
                "blocked": len(blocked),
                "failed": len(failed),
                "results": results,
            },
        )


# Standalone execution for testing
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import log as _  # noqa: F401

    observer = GitAutoSyncObserver()

    if "--dry-run" in sys.argv:
        print("DRY RUN — checking for dirty repos only")
        dirty = observer._get_dirty_repos()
        for repo in dirty:
            print(f"  {repo['path']}: {repo['change_count']} changes")
            diff = observer._get_diff_summary(repo["path"])
            msg = observer._generate_commit_message(repo["path"], diff)
            print(f"    Would commit: {msg}")
            violations = observer._pre_commit_security_check(repo)
            if violations:
                print(f"    BLOCKED: {violations}")
        sys.exit(0)

    result = observer.run()
    if result.success:
        print(f"Sync completed: {result.message}")
    else:
        print(f"Sync issues: {result.message}", file=sys.stderr)
        if result.data:
            for r in result.data.get("results", []):
                if r.get("error"):
                    print(f"  {r['repo']}: {r['error']}", file=sys.stderr)
