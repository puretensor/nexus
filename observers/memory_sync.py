#!/usr/bin/env python3
"""Memory sync observer — bidirectional context sharing between PureClaw and HAL.

Runs every 10 minutes via the Observer registry:
  Pull: Reads PureClaw's MEMORY.md from tensor-core via SSH, stores locally
  Push: Composes a digest of HAL's recent sessions/memories, writes to tensor-core

SSH is one-way (pod → TC only), so this observer drives both directions.
"""

import hashlib
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from observers.base import Observer, ObserverContext, ObserverResult

log = logging.getLogger("nexus")

# Paths
SHARED_CONTEXT_PATH = Path(os.environ.get(
    "SHARED_CONTEXT_PATH", "/data/sync/pureclaw_memory.md"
))
TC_SSH_HOST = os.environ.get("TC_SSH_ALIAS", "tensor-core")
TC_MEMORY_PATH = "~/.claude/projects/-home-puretensorai/memory/MEMORY.md"
TC_DIGEST_PATH = "~/.claude/projects/-home-puretensorai/memory/hal_digest.md"

# SSH options for non-interactive use
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
]


def _ssh_cmd(host: str, remote_cmd: str, timeout: int = 15) -> str:
    """Run a command on a remote host via SSH. Returns stdout or raises."""
    cmd = ["ssh"] + SSH_OPTS + [host, remote_cmd]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH failed (rc={result.returncode}): {result.stderr.strip()}")
    return result.stdout


def _ssh_write(host: str, remote_path: str, content: str, timeout: int = 15) -> None:
    """Write content to a remote file via SSH stdin pipe."""
    cmd = ["ssh"] + SSH_OPTS + [host, f"cat > {remote_path}"]
    result = subprocess.run(
        cmd, input=content, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH write failed (rc={result.returncode}): {result.stderr.strip()}")


def _content_hash(text: str) -> str:
    """SHA-256 hash of text content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MemorySyncObserver(Observer):
    """Bidirectional memory sync between PureClaw (tensor-core) and HAL (K3s)."""

    name = "memory_sync"
    schedule = "*/10 * * * *"  # Every 10 minutes

    def run(self, ctx: ObserverContext) -> ObserverResult:
        """Execute bidirectional sync. Each direction is independent."""
        from db import get_observer_state, set_observer_state

        # Load persisted state
        raw_state = get_observer_state(self.name)
        state = {}
        if raw_state and raw_state.get("state_json"):
            try:
                state = json.loads(raw_state["state_json"])
            except (json.JSONDecodeError, TypeError):
                state = {}

        pull_ok = self._pull_pureclaw_memory(state)
        push_ok = self._push_hal_digest(state)

        # Persist updated state
        set_observer_state(self.name, json.dumps(state))

        if pull_ok and push_ok:
            return ObserverResult(success=True)
        elif pull_ok or push_ok:
            failed = []
            if not pull_ok:
                failed.append("pull")
            if not push_ok:
                failed.append("push")
            return ObserverResult(
                success=True,
                data={"partial_failure": failed},
            )
        else:
            return ObserverResult(success=False, error="Both pull and push failed")

    def _pull_pureclaw_memory(self, state: dict) -> bool:
        """Pull PureClaw's MEMORY.md from tensor-core and store locally."""
        try:
            content = _ssh_cmd(TC_SSH_HOST, f"cat {TC_MEMORY_PATH}")
        except Exception as exc:
            log.warning("[memory_sync] Pull failed: %s", exc)
            return False

        content = content.strip()
        if not content:
            log.debug("[memory_sync] Pull: MEMORY.md is empty, skipping")
            return True

        # Check if content changed
        new_hash = _content_hash(content)
        if new_hash == state.get("pull_hash"):
            log.debug("[memory_sync] Pull: content unchanged")
            return True

        # Write to local sync directory
        SHARED_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SHARED_CONTEXT_PATH.write_text(content, encoding="utf-8")

        state["pull_hash"] = new_hash
        state["pull_time"] = datetime.now(timezone.utc).isoformat()
        log.info("[memory_sync] Pull: updated PureClaw memory (%d bytes)", len(content))
        return True

    def _push_hal_digest(self, state: dict) -> bool:
        """Compose HAL's activity digest and push to tensor-core."""
        from config import AUTHORIZED_USER_ID
        from db import list_sessions
        from memory import list_memories

        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%d %H:%M UTC")

        # Build digest content
        lines = [
            "# Recent HAL Activity (auto-synced)",
            f"_Last updated: {now_str}_",
            "",
        ]

        # Active sessions (last 24h with activity)
        try:
            sessions = list_sessions(AUTHORIZED_USER_ID)
            active = []
            for s in sessions:
                if s.get("message_count", 0) > 0:
                    model = s.get("model", "?")
                    name = s.get("name", "default")
                    count = s.get("message_count", 0)
                    summary = s.get("summary", "")
                    entry = f"- {name} ({count} msgs, {model})"
                    if summary:
                        entry += f": {summary}"
                    active.append(entry)

            if active:
                lines.append("## Active Sessions")
                lines.extend(active[:10])  # Cap at 10
                lines.append("")
        except Exception as exc:
            log.warning("[memory_sync] Push: failed to query sessions: %s", exc)

        # HAL memories
        try:
            memories = list_memories()
            if memories:
                lines.append("## HAL Memories")
                for mem in memories[:15]:  # Cap at 15
                    cat = mem.get("category", "general")
                    text = mem.get("text", "")
                    lines.append(f"- {cat}: {text}")
                lines.append("")
        except Exception as exc:
            log.warning("[memory_sync] Push: failed to query memories: %s", exc)

        digest = "\n".join(lines).strip() + "\n"

        # Check if content changed
        new_hash = _content_hash(digest)
        if new_hash == state.get("push_hash"):
            log.debug("[memory_sync] Push: digest unchanged")
            return True

        # Write to tensor-core
        try:
            _ssh_write(TC_SSH_HOST, TC_DIGEST_PATH, digest)
        except Exception as exc:
            log.warning("[memory_sync] Push failed: %s", exc)
            return False

        state["push_hash"] = new_hash
        state["push_time"] = now.isoformat()
        log.info("[memory_sync] Push: updated HAL digest on tensor-core (%d bytes)", len(digest))
        return True


# ---------------------------------------------------------------------------
# Standalone testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    project_dir = Path(__file__).parent.parent
    env_path = project_dir / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

    sys.path.insert(0, str(project_dir))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    observer = MemorySyncObserver()
    ctx = ObserverContext()
    result = observer.run(ctx)

    if result.success:
        print(f"SUCCESS: {result.data}")
    else:
        print(f"FAILED: {result.error}", file=sys.stderr)
        sys.exit(1)
