#!/usr/bin/env python3
"""GitHub Activity Observer — generates genuine commits to keep the contribution graph green.

Runs on a frequent cron schedule but uses probabilistic execution to achieve
2-5 commits per week at random times. Each execution generates real content
(infrastructure snapshots, security trends, voice-kb stats) and commits it.

Complements GitAutoSyncObserver which commits *dirty* changes — this observer
*generates new content* then commits it.
"""

import json
import logging
import os
import random
import re
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from observers.base import Observer, ObserverResult

log = logging.getLogger("nexus")

# SSH to tensor-core (reused from git_auto_sync)
TC_SSH = "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 puretensorai@localhost"

# Prometheus on mon2
PROMETHEUS_URL = "http://100.80.213.1:9090/api/v1/query"

# Target repos — path on TC, remote names
REPOS = {
    "tensor-scripts": {"github_remote": "github", "gitea_remote": "origin"},
    "voice-kb": {"github_remote": "origin", "gitea_remote": "gitea"},
}

# Weekly budget
MIN_COMMITS_PER_WEEK = 2
MAX_COMMITS_PER_WEEK = 5
BASE_PROBABILITY = 0.10  # ~10% per tick

# Secret patterns (from git_auto_sync)
BLOCK_PATTERNS = [
    r'sk-ant-api\d+-[A-Za-z0-9_-]{20,}',
    r'sk-proj-[A-Za-z0-9_-]{48,}',
    r'xai-[A-Za-z0-9_-]{40,}',
    r'AKIA[0-9A-Z]{16}',
    r'\d{10}:[A-Za-z0-9_-]{35}',
    r'-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----',
]


class GitHubActivityObserver(Observer):
    """Generates genuine commits 2-5 times per week at random intervals."""

    name = "github_activity"
    schedule = "0 */3 9-21 * *"  # Every 3h during 09:00-21:00 UTC

    # -- SSH helper -----------------------------------------------------------

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

    # -- State management -----------------------------------------------------

    def _state_file(self) -> Path:
        default = str(Path(__file__).parent / ".state")
        state_dir = Path(os.environ.get("OBSERVER_STATE_DIR", default))
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "github_activity_state.json"

    def _load_state(self) -> dict:
        sf = self._state_file()
        if sf.exists():
            try:
                return json.loads(sf.read_text())
            except Exception:
                pass
        return {
            "week_start": None,
            "commits_this_week": 0,
            "last_commit": None,
            "history": [],
        }

    def _save_state(self, state: dict):
        # Keep history trimmed to last 50 entries
        state["history"] = state.get("history", [])[-50:]
        self._state_file().write_text(json.dumps(state, indent=2))

    def _get_week_start(self, now: datetime) -> str:
        """ISO date of the Monday of the current week."""
        monday = now - timedelta(days=now.weekday())
        return monday.strftime("%Y-%m-%d")

    # -- Probabilistic execution ----------------------------------------------

    def _should_run(self, state: dict, now: datetime, force: bool = False) -> bool:
        """Decide whether to execute this tick based on weekly budget and probability."""
        if force:
            return True

        week_start = self._get_week_start(now)

        # Reset weekly counter if new week
        if state.get("week_start") != week_start:
            state["week_start"] = week_start
            state["commits_this_week"] = 0

        commits = state["commits_this_week"]

        # Already hit max for the week
        if commits >= MAX_COMMITS_PER_WEEK:
            log.info("github_activity: weekly max (%d) reached, skipping", MAX_COMMITS_PER_WEEK)
            return False

        # Calculate probability — increase if approaching end of week with low commits
        prob = BASE_PROBABILITY
        day_of_week = now.weekday()  # 0=Mon, 6=Sun

        if commits < MIN_COMMITS_PER_WEEK:
            # Ramp up probability as we approach Friday (day 4)
            if day_of_week >= 3:  # Wed+
                remaining_ticks = max(1, (6 - day_of_week) * 5)  # ~5 ticks per day
                needed = MIN_COMMITS_PER_WEEK - commits
                prob = max(prob, needed / remaining_ticks + 0.1)
            elif day_of_week >= 4:  # Thu+
                prob = max(prob, 0.4)

        # Cap at 80% to maintain some randomness
        prob = min(prob, 0.80)

        roll = random.random()
        log.info(
            "github_activity: week=%s commits=%d prob=%.2f roll=%.2f %s",
            week_start, commits, prob, roll, "RUN" if roll < prob else "SKIP",
        )
        return roll < prob

    # -- Content generators ---------------------------------------------------

    def _query_prometheus(self, query: str) -> dict | None:
        """Query Prometheus and return the result."""
        import urllib.parse
        import urllib.request

        url = f"{PROMETHEUS_URL}?query={urllib.parse.quote(query)}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if data.get("status") == "success":
                    return data.get("data", {}).get("result", [])
        except Exception as e:
            log.warning("Prometheus query failed (%s): %s", query[:40], e)
        return None

    def _generate_infra_snapshot(self, now: datetime) -> tuple[str, str, str, str] | None:
        """Generate infrastructure health snapshot → tensor-scripts."""
        snapshot = {
            "timestamp": now.isoformat(),
            "generated_by": "github_activity_observer",
        }

        queries = {
            "nodes_up": 'up{job="node-exporter"}',
            "cpu_usage_percent": '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
            "memory_usage_percent": '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
            "disk_free_bytes": 'node_filesystem_avail_bytes{mountpoint="/"}',
            "ceph_health": "ceph_health_status",
        }

        for key, query in queries.items():
            result = self._query_prometheus(query)
            if result is not None:
                snapshot[key] = [
                    {
                        "instance": r.get("metric", {}).get("instance", "unknown"),
                        "value": r.get("value", [None, None])[1],
                    }
                    for r in result
                ]

        if len(snapshot) <= 2:
            log.warning("github_activity: no Prometheus data, skipping infra snapshot")
            return None

        ts = now.strftime("%Y-%m-%d_%H%M%S")
        content = json.dumps(snapshot, indent=2)
        file_path = f"monitoring/snapshots/{ts}.json"
        commit_msg = f"chore(monitoring): fleet health snapshot {now.strftime('%Y-%m-%d')}"

        return "tensor-scripts", file_path, content, commit_msg

    def _generate_security_trend(self, now: datetime) -> tuple[str, str, str, str] | None:
        """Generate security trend summary from .prom files → tensor-scripts."""
        # Read .prom files from TC
        rc, output = self._ssh_cmd(
            "cat ~/tensor-scripts/security/output/metrics/*.prom 2>/dev/null",
            timeout=15,
        )
        if rc != 0 or not output.strip():
            log.warning("github_activity: no .prom files found, skipping security trend")
            return None

        # Parse HELP/TYPE/metric lines
        findings = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
        total_metrics = 0
        sources = set()

        for line in output.strip().split("\n"):
            line = line.strip()
            if line.startswith("#"):
                continue
            if not line:
                continue

            total_metrics += 1

            # Extract source from metric name
            parts = line.split("{")
            if parts:
                metric_name = parts[0].strip()
                source = metric_name.split("_")[0] if "_" in metric_name else metric_name
                sources.add(source)

            # Look for severity labels
            lower = line.lower()
            for sev in findings:
                if sev in lower:
                    # Try to get the value
                    val_parts = line.split()
                    if val_parts:
                        try:
                            val = float(val_parts[-1])
                            findings[sev] += int(val)
                        except ValueError:
                            pass

        date_str = now.strftime("%Y-%m-%d")
        md_lines = [
            f"# Security Trend Summary — {date_str}",
            "",
            f"Generated: {now.isoformat()}",
            f"Sources: {', '.join(sorted(sources)) or 'N/A'}",
            f"Total metrics parsed: {total_metrics}",
            "",
            "## Findings by Severity",
            "",
            "| Severity | Count |",
            "|----------|-------|",
        ]
        for sev, count in findings.items():
            md_lines.append(f"| {sev.capitalize()} | {count} |")

        md_lines.extend([
            "",
            "---",
            f"*Auto-generated by GitHubActivityObserver at {now.strftime('%H:%M UTC')}*",
        ])

        content = "\n".join(md_lines) + "\n"
        file_path = f"security/output/reports/trend_{date_str}.md"
        commit_msg = f"docs(security): trend summary {date_str}"

        return "tensor-scripts", file_path, content, commit_msg

    def _generate_voicekb_stats(self, now: datetime) -> tuple[str, str, str, str] | None:
        """Generate voice-kb statistics → voice-kb."""
        # Count entries and sample frontmatter
        rc, output = self._ssh_cmd(
            "ls ~/voice-kb/kb/ 2>/dev/null | wc -l",
            timeout=10,
        )
        if rc != 0:
            log.warning("github_activity: cannot access voice-kb, skipping stats")
            return None

        entry_count = int(output.strip()) if output.strip().isdigit() else 0
        if entry_count == 0:
            return None

        # Get date range
        rc, first_file = self._ssh_cmd(
            "ls ~/voice-kb/kb/ 2>/dev/null | sort | head -1",
            timeout=10,
        )
        rc, last_file = self._ssh_cmd(
            "ls ~/voice-kb/kb/ 2>/dev/null | sort | tail -1",
            timeout=10,
        )

        # Sample topics from frontmatter
        rc, topics_raw = self._ssh_cmd(
            "grep -h '^topics:' ~/voice-kb/kb/*.md 2>/dev/null | head -30",
            timeout=15,
        )

        topic_counts: dict[str, int] = {}
        if topics_raw:
            for line in topics_raw.strip().split("\n"):
                # topics: [foo, bar, baz]
                match = re.search(r'\[(.+?)\]', line)
                if match:
                    for topic in match.group(1).split(","):
                        t = topic.strip().strip("'\"")
                        if t:
                            topic_counts[t] = topic_counts.get(t, 0) + 1

        month_key = now.strftime("%Y-%m")
        stats = {
            "month": month_key,
            "generated": now.isoformat(),
            "entry_count": entry_count,
            "first_entry": first_file.strip() if first_file else None,
            "last_entry": last_file.strip() if last_file else None,
            "topic_frequencies": dict(sorted(topic_counts.items(), key=lambda x: -x[1])[:30]),
        }

        content = json.dumps(stats, indent=2) + "\n"
        file_path = f"stats/{month_key}.json"
        commit_msg = f"docs(stats): voice-kb statistics update"

        return "voice-kb", file_path, content, commit_msg

    # -- Git operations -------------------------------------------------------

    def _security_scan(self, content: str) -> list[str]:
        """Scan content for leaked secrets."""
        violations = []
        for pattern in BLOCK_PATTERNS:
            matches = re.findall(pattern, content)
            for match in matches:
                if any(p in match.lower() for p in ["replace_me", "your-key", "example", "dummy"]):
                    continue
                violations.append(f"Pattern matched: {match[:50]}")
        return violations

    def _write_and_commit(
        self, repo_name: str, file_path: str, content: str, commit_msg: str, dry_run: bool = False
    ) -> dict:
        """Write a file to a repo on TC, commit, and push."""
        result = {
            "repo": repo_name,
            "file": file_path,
            "committed": False,
            "pushed_github": False,
            "pushed_gitea": False,
            "message": commit_msg,
            "error": "",
        }

        repo = REPOS.get(repo_name)
        if not repo:
            result["error"] = f"Unknown repo: {repo_name}"
            return result

        # Security scan
        violations = self._security_scan(content)
        if violations:
            result["error"] = f"BLOCKED — secrets detected: {'; '.join(violations[:3])}"
            return result

        if dry_run:
            result["message"] = f"[DRY RUN] Would write {file_path} and commit: {commit_msg}"
            return result

        # Ensure directory exists
        dir_path = os.path.dirname(file_path)
        if dir_path:
            rc, _ = self._ssh_cmd(f"mkdir -p ~/{repo_name}/{dir_path}", timeout=10)

        # Write file via heredoc
        # Escape content for shell
        escaped = content.replace("\\", "\\\\").replace("'", "'\\''")
        rc, output = self._ssh_cmd(
            f"cat > ~/{repo_name}/{file_path} << 'GITHUB_ACTIVITY_EOF'\n{content}\nGITHUB_ACTIVITY_EOF",
            timeout=15,
        )

        # Fallback: use printf if heredoc in SSH fails
        if rc != 0:
            # Write via python on TC
            py_cmd = (
                f"python3 -c \"import pathlib; "
                f"p = pathlib.Path.home() / '{repo_name}' / '{file_path}'; "
                f"p.parent.mkdir(parents=True, exist_ok=True); "
                f"p.write_text(open('/dev/stdin').read())\" "
            )
            try:
                full_cmd = f"{TC_SSH} \"{py_cmd}\""
                proc = subprocess.run(
                    full_cmd, shell=True, input=content,
                    capture_output=True, text=True, timeout=15,
                )
                if proc.returncode != 0:
                    result["error"] = f"Write failed: {proc.stderr[:200]}"
                    return result
            except Exception as e:
                result["error"] = f"Write failed: {e}"
                return result

        # Stage and commit
        rc, output = self._ssh_cmd(
            f"cd ~/{repo_name} && git add {file_path} && "
            f"git commit -m '{commit_msg.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}' 2>&1",
            timeout=30,
        )
        if rc != 0:
            if "nothing to commit" in output:
                result["message"] = "nothing to commit (content unchanged)"
                return result
            result["error"] = f"Commit failed: {output[:200]}"
            return result

        result["committed"] = True

        # Push to Gitea
        if repo.get("gitea_remote"):
            rc, output = self._ssh_cmd(
                f"cd ~/{repo_name} && git push {repo['gitea_remote']} HEAD 2>&1",
                timeout=60,
            )
            result["pushed_gitea"] = (rc == 0)
            if rc != 0:
                log.warning("Gitea push failed for %s: %s", repo_name, output[:200])

        # Push to GitHub
        if repo.get("github_remote"):
            rc, output = self._ssh_cmd(
                f"cd ~/{repo_name} && git push {repo['github_remote']} HEAD 2>&1",
                timeout=60,
            )
            result["pushed_github"] = (rc == 0)
            if rc != 0:
                log.warning("GitHub push failed for %s: %s", repo_name, output[:200])

        return result

    # -- Main observer interface -----------------------------------------------

    def run(self, ctx=None, force: bool = False, dry_run: bool = False) -> ObserverResult:
        """Execute the GitHub activity observer."""
        now = self.now_utc()
        state = self._load_state()

        if not self._should_run(state, now, force=force):
            return ObserverResult(success=True)  # Silent skip

        # Random jitter: 0-30 minutes (skip in dry-run/force)
        if not dry_run and not force:
            jitter = random.randint(0, 1800)
            log.info("github_activity: sleeping %d seconds for jitter", jitter)
            time.sleep(jitter)

        # Test SSH connectivity
        rc, output = self._ssh_cmd("echo OK", timeout=10)
        if rc != 0 or "OK" not in output:
            return ObserverResult(
                success=False,
                error="SSH to TC failed — cannot generate activity",
            )

        # Pick a random generator
        generators = [
            self._generate_infra_snapshot,
            self._generate_security_trend,
            self._generate_voicekb_stats,
        ]
        random.shuffle(generators)

        # Try generators until one succeeds
        gen_result = None
        generator_name = None
        for gen in generators:
            try:
                gen_result = gen(now)
                if gen_result is not None:
                    generator_name = gen.__name__
                    break
            except Exception as e:
                log.warning("github_activity: generator %s failed: %s", gen.__name__, e)

        if gen_result is None:
            log.info("github_activity: all generators returned None, skipping")
            return ObserverResult(success=True, message="No content generated")

        repo_name, file_path, content, commit_msg = gen_result
        log.info("github_activity: using %s → %s/%s", generator_name, repo_name, file_path)

        # Write, commit, push
        result = self._write_and_commit(repo_name, file_path, content, commit_msg, dry_run=dry_run)

        # Update state
        if result["committed"]:
            week_start = self._get_week_start(now)
            if state.get("week_start") != week_start:
                state["week_start"] = week_start
                state["commits_this_week"] = 0
            state["commits_this_week"] = state.get("commits_this_week", 0) + 1
            state["last_commit"] = now.isoformat()
            state["history"] = state.get("history", [])
            state["history"].append({
                "time": now.isoformat(),
                "repo": repo_name,
                "file": file_path,
                "msg": commit_msg,
            })
            self._save_state(state)

            # Telegram notification
            push_targets = []
            if result["pushed_gitea"]:
                push_targets.append("Gitea")
            if result["pushed_github"]:
                push_targets.append("GitHub")
            push_str = ", ".join(push_targets) if push_targets else "push pending"

            self.send_telegram(
                f"GITHUB ACTIVITY — {now.strftime('%H:%M UTC')}\n\n"
                f"  {repo_name}: {commit_msg}\n"
                f"  → {push_str}\n"
                f"  Week: {state['commits_this_week']}/{MAX_COMMITS_PER_WEEK}"
            )
        elif result.get("error"):
            self._save_state(state)

        return ObserverResult(
            success=not result.get("error"),
            message=result.get("message") or result.get("error", ""),
            data=result,
        )


# Standalone execution for testing
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import log as _  # noqa: F401

    observer = GitHubActivityObserver()

    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv or dry_run  # dry-run implies force

    if dry_run:
        print("DRY RUN — will generate content but not commit")
    elif force:
        print("FORCE — bypassing probability check")

    result = observer.run(force=force, dry_run=dry_run)

    if result.data:
        data = result.data
        print(f"\nRepo: {data.get('repo', 'N/A')}")
        print(f"File: {data.get('file', 'N/A')}")
        print(f"Message: {data.get('message', 'N/A')}")
        if data.get("committed"):
            print(f"Pushed GitHub: {data.get('pushed_github')}")
            print(f"Pushed Gitea: {data.get('pushed_gitea')}")
        if data.get("error"):
            print(f"Error: {data['error']}", file=sys.stderr)
    else:
        print(result.message or "Skipped (probability check)")
