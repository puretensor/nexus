#!/usr/bin/env python3
"""Git security audit observer.

Runs every 2 hours from fox-n1. SSHes to TC and scans all tracked repos
for leaked secrets, credentials, API keys, and sensitive data in both
the working tree and git history.

On detection: immediate Telegram alert with repo, file, line number.
Tracks known false positives in state file to avoid re-alerting.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from observers.base import Observer, ObserverResult

log = logging.getLogger("nexus")

# SSH to tensor-core
_TC_HOST = os.environ.get("TC_SSH_HOST", "localhost")
TC_SSH = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 puretensorai@{_TC_HOST}"

# Repos to audit — path on TC, whether PUBLIC
REPOS = [
    {"path": "nexus", "public": True, "github": "puretensor/PureClaw"},
    {"path": "tensor-scripts", "public": False, "github": "puretensor/tensor-scripts"},
    {"path": "voice-kb", "public": False, "github": "puretensor/voice-kb"},
    {"path": "ecommerce-agent", "public": False, "github": "puretensor/ecommerce-agent"},
    {"path": "kalima", "public": False, "github": "puretensor/kalima"},
    {"path": "arabic-qa", "public": False, "github": "puretensor/arabic-qa"},
    {"path": "bookengine", "public": False, "github": "puretensor/bookengine"},
    {"path": "echo-voicememo", "public": False, "github": "puretensor/Echo-VoiceMemo"},
    {"path": "autopen", "public": False, "github": "puretensor/autopen"},
]

# Secret detection patterns — pattern: description
# Ordered by severity (most dangerous first)
SECRET_PATTERNS = {
    # API Keys — specific formats
    r'sk-ant-api\d+-[A-Za-z0-9_-]{20,}': 'Anthropic API key',
    r'sk-proj-[A-Za-z0-9_-]{20,}': 'OpenAI API key',
    r'xai-[A-Za-z0-9_-]{20,}': 'xAI API key',
    r'AKIA[0-9A-Z]{16}': 'AWS Access Key ID',
    r'AIza[0-9A-Za-z_-]{35}': 'Google API key',
    r'ghp_[A-Za-z0-9]{36}': 'GitHub Personal Access Token',
    r'gho_[A-Za-z0-9]{36}': 'GitHub OAuth Token',
    r'glpat-[A-Za-z0-9_-]{20}': 'GitLab Personal Access Token',

    # Telegram bot tokens
    r'\d{9,10}:[A-Za-z0-9_-]{35}': 'Telegram bot token',

    # Private keys
    r'-----BEGIN (RSA |EC |DSA |ED25519 |OPENSSH )?PRIVATE KEY-----': 'Private key',
    r'-----BEGIN PGP PRIVATE KEY BLOCK-----': 'PGP private key',

    # Database connection strings
    r'(mysql|postgres|postgresql|mongodb|redis)://[^\s:]+:[^\s@]+@[^\s]+': 'Database connection string',

    # Generic secret assignments (in code/config)
    r'(?i)(password|passwd|secret|api_key|apikey|api[-_]?secret|private[-_]?key|access[-_]?token)\s*[=:]\s*["\'][^\s"\']{8,}["\']': 'Hardcoded secret in code',

    # Cloudflare tokens
    r'[A-Za-z0-9_]{37,40}(?=.*cloudflare)': 'Possible Cloudflare token',

    # JWT tokens
    r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}': 'JWT token',

    # AWS Secret Access Key (40 char base64-like after an access key)
    r'(?i)aws.{0,20}secret.{0,20}[A-Za-z0-9/+=]{40}': 'AWS Secret Access Key',
}

# Files/paths to ALWAYS skip (false positive heavy)
SKIP_PATHS = {
    '.git/',
    'node_modules/',
    '__pycache__/',
    '.pytest_cache/',
    'package-lock.json',
    'yarn.lock',
    'poetry.lock',
    '.claude/',
    'venv/',
    '.venv/',
}

# File extensions to skip (binary/generated)
SKIP_EXTENSIONS = {
    '.pyc', '.pyo', '.so', '.dylib', '.dll', '.exe',
    '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg',
    '.woff', '.woff2', '.ttf', '.eot',
    '.pdf', '.zip', '.tar', '.gz', '.bz2',
    '.db', '.sqlite', '.sqlite3',
}


class GitSecurityAuditObserver(Observer):
    """Scans all tracked repos for leaked secrets."""

    name = "git_security_audit"
    schedule = "0 */2 * * *"  # Every 2 hours

    def _state_file(self) -> Path:
        state_dir = Path(os.environ.get("OBSERVER_STATE_DIR", "/data/state/observers"))
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "git_security_audit_state.json"

    def _load_state(self) -> dict:
        sf = self._state_file()
        if sf.exists():
            try:
                return json.loads(sf.read_text())
            except Exception:
                pass
        return {"known_findings": [], "last_run": "", "total_scans": 0}

    def _save_state(self, state: dict):
        self._state_file().write_text(json.dumps(state, indent=2))

    def _finding_hash(self, repo: str, filepath: str, pattern_desc: str, match: str) -> str:
        """Stable hash for a finding to track false positives."""
        key = f"{repo}:{filepath}:{pattern_desc}:{match[:50]}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _ssh_cmd(self, cmd: str, timeout: int = 60) -> tuple[int, str]:
        """Run a command on TC via SSH. Returns (returncode, output)."""
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

    def _scan_working_tree(self, repo: dict) -> list[dict]:
        """Scan the current working tree of a repo for secrets."""
        findings = []
        repo_path = repo["path"]

        # Get list of tracked + untracked files
        rc, output = self._ssh_cmd(
            f"cd ~/{repo_path} && git ls-files && git ls-files --others --exclude-standard",
            timeout=30,
        )
        if rc != 0:
            log.warning("Failed to list files in %s: %s", repo_path, output[:200])
            return findings

        files = [f.strip() for f in output.strip().split("\n") if f.strip()]

        for filepath in files:
            # Skip binary/irrelevant files
            if any(skip in filepath for skip in SKIP_PATHS):
                continue
            ext = os.path.splitext(filepath)[1].lower()
            if ext in SKIP_EXTENSIONS:
                continue

            # Read file contents
            rc, content = self._ssh_cmd(
                f"cd ~/{repo_path} && cat '{filepath}' 2>/dev/null | head -500",
                timeout=15,
            )
            if rc != 0 or not content:
                continue

            # Scan against patterns
            for pattern, desc in SECRET_PATTERNS.items():
                for match in re.finditer(pattern, content):
                    matched_text = match.group(0)
                    # Find line number
                    line_num = content[:match.start()].count('\n') + 1
                    findings.append({
                        "repo": repo_path,
                        "file": filepath,
                        "line": line_num,
                        "pattern": desc,
                        "match": matched_text[:80],  # Truncate for safety
                        "public": repo.get("public", False),
                        "hash": self._finding_hash(repo_path, filepath, desc, matched_text),
                    })

        return findings

    def _scan_git_history(self, repo: dict) -> list[dict]:
        """Scan recent git history for secrets (last 50 commits)."""
        findings = []
        repo_path = repo["path"]

        # Get diffs from recent commits
        rc, output = self._ssh_cmd(
            f"cd ~/{repo_path} && git log --all -50 --diff-filter=A --name-only --format='COMMIT:%H' 2>/dev/null | head -500",
            timeout=30,
        )
        if rc != 0:
            return findings

        # Check files that were added in recent commits
        current_commit = ""
        for line in output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("COMMIT:"):
                current_commit = line[7:]
                continue
            filepath = line
            if any(skip in filepath for skip in SKIP_PATHS):
                continue
            ext = os.path.splitext(filepath)[1].lower()
            if ext in SKIP_EXTENSIONS:
                continue

            # Check the file content at that commit
            rc, content = self._ssh_cmd(
                f"cd ~/{repo_path} && git show {current_commit}:{filepath} 2>/dev/null | head -300",
                timeout=10,
            )
            if rc != 0 or not content:
                continue

            for pattern, desc in SECRET_PATTERNS.items():
                for match in re.finditer(pattern, content):
                    matched_text = match.group(0)
                    findings.append({
                        "repo": repo_path,
                        "file": f"{filepath} (commit {current_commit[:8]})",
                        "line": 0,
                        "pattern": desc,
                        "match": matched_text[:80],
                        "public": repo.get("public", False),
                        "hash": self._finding_hash(repo_path, filepath, desc, matched_text),
                        "in_history": True,
                    })

        return findings

    def run(self, ctx=None) -> ObserverResult:
        """Execute the security audit."""
        now = self.now_utc()
        log.info("Git security audit starting at %s", now.strftime("%H:%M UTC"))

        state = self._load_state()
        known_hashes = set(state.get("known_findings", []))

        # Test SSH connectivity
        rc, output = self._ssh_cmd("echo OK", timeout=10)
        if rc != 0 or "OK" not in output:
            log.warning("Cannot SSH to TC: %s", output[:200])
            return ObserverResult(
                success=False,
                error="SSH to TC failed — cannot run security audit",
            )

        all_findings = []
        new_findings = []
        repos_scanned = 0

        for repo in REPOS:
            repo_path = repo["path"]

            # Check repo exists
            rc, _ = self._ssh_cmd(f"test -d ~/{repo_path}/.git && echo YES", timeout=5)
            if rc != 0:
                log.info("Repo %s not found on TC, skipping", repo_path)
                continue

            repos_scanned += 1

            # Scan working tree
            tree_findings = self._scan_working_tree(repo)
            all_findings.extend(tree_findings)

            # Scan git history (only for public repos or every 6th run)
            scan_count = state.get("total_scans", 0)
            if repo.get("public") or scan_count % 6 == 0:
                history_findings = self._scan_git_history(repo)
                all_findings.extend(history_findings)

        # Deduplicate and filter known findings
        seen_hashes = set()
        for f in all_findings:
            h = f["hash"]
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            if h not in known_hashes:
                new_findings.append(f)

        # Update state
        state["last_run"] = now.isoformat()
        state["total_scans"] = state.get("total_scans", 0) + 1
        state["repos_scanned"] = repos_scanned
        state["total_findings"] = len(all_findings)
        state["new_findings_count"] = len(new_findings)

        # Add new findings to known list (so we don't re-alert)
        for f in new_findings:
            if f["hash"] not in known_hashes:
                known_hashes.add(f["hash"])
        state["known_findings"] = list(known_hashes)

        self._save_state(state)

        # Alert on new findings
        if new_findings:
            # Separate critical (public repo) from warnings (private)
            critical = [f for f in new_findings if f.get("public")]
            warnings = [f for f in new_findings if not f.get("public")]

            if critical:
                lines = [
                    "SECURITY ALERT — SECRETS IN PUBLIC REPO",
                    "",
                ]
                for f in critical[:10]:
                    lines.append(f"  {f['repo']}/{f['file']}:{f.get('line', '?')}")
                    lines.append(f"    {f['pattern']}: {f['match'][:40]}...")
                    lines.append("")
                lines.append("IMMEDIATE ACTION REQUIRED — these are visible to the public.")
                self.send_telegram("\n".join(lines))

            if warnings:
                lines = [
                    f"Git Security Scan — {len(warnings)} new finding(s) in private repos",
                    "",
                ]
                for f in warnings[:15]:
                    history_tag = " [HISTORY]" if f.get("in_history") else ""
                    lines.append(f"  {f['repo']}/{f['file']}:{f.get('line', '?')}{history_tag}")
                    lines.append(f"    {f['pattern']}")
                lines.append("")
                lines.append(f"Repos scanned: {repos_scanned}")
                self.send_telegram("\n".join(lines))

            log.info("Security audit: %d new findings (%d critical, %d warnings)",
                     len(new_findings), len(critical), len(warnings))
        else:
            log.info("Security audit clean: %d repos scanned, 0 new findings", repos_scanned)

        return ObserverResult(
            success=True,
            message=f"Security audit: {repos_scanned} repos, {len(new_findings)} new findings",
            data={
                "repos_scanned": repos_scanned,
                "total_findings": len(all_findings),
                "new_findings": len(new_findings),
            },
        )


# Standalone execution for testing
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import log as _  # noqa: F401

    observer = GitSecurityAuditObserver()

    # Reset state for fresh scan
    if "--fresh" in sys.argv:
        sf = observer._state_file()
        if sf.exists():
            sf.unlink()
            print("State reset — fresh scan")

    result = observer.run()
    if result.success:
        print(f"Audit completed: {result.message}")
        if result.data:
            print(f"  Repos: {result.data.get('repos_scanned', 0)}")
            print(f"  Total findings: {result.data.get('total_findings', 0)}")
            print(f"  New findings: {result.data.get('new_findings', 0)}")
    else:
        print(f"Audit failed: {result.error}", file=sys.stderr)
        sys.exit(1)
