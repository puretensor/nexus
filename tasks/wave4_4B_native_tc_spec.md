# Wave 4 — 4B: Native TC Execution Implementation Spec

**Depends on:** 4A (Hybrid Backend) — must be deployed first
**Complexity:** Medium (~2 hours)
**Agent instructions:** Implement EXACTLY as specified. Read all referenced files first.

---

## Overview

When the hybrid backend routes to CLI for TC-native tasks, invoke Claude Code CLI on tensor-core via SSH instead of locally in the K8s pod. This gives the CLI direct access to TC's filesystem, GPUs, models, and all local tools.

## Architecture

```
Telegram message → HybridBackend._classify() → "cli"
    → ClaudeCodeBackend(remote_mode=True)
        → ssh tensor-core claude -p "message" --output-format stream-json ...
            → Executes on TC: full filesystem, GPU, /mnt/storage, ~/nexus, etc.
        → Stream results back via SSH pipe
    → StreamingEditor displays to user
```

## Why This Matters

The K8s pod on fox-n1 has no access to:
- `/mnt/storage/` (7.3TB RAID0 NVMe array)
- GPU resources (2x RTX PRO 6000 Blackwell)
- Local model files, voice-kb, scripts
- Direct network to 200G fabric nodes

With SSH-wrapped CLI on TC, HAL gets full tensor-core capabilities.

## Files to Modify

### 1. `backends/claude_code.py` — Add Remote Mode

Read the existing file first (168 lines). The key method is `call_streaming()` which spawns `claude` as a subprocess.

Add a `remote_mode` class attribute and modify the subprocess command:

```python
class ClaudeCodeBackend(BaseBackend):
    name = "claude_code"
    supports_streaming = True
    supports_tools = True
    supports_sessions = True

    def __init__(self):
        super().__init__()
        self._remote = os.environ.get("HYBRID_CLI_REMOTE", "").lower() in ("true", "1", "yes")
        self._tc_host = os.environ.get("HYBRID_TC_HOST", "tensor-core")
        if self._remote:
            log.info("ClaudeCodeBackend: remote mode enabled (host=%s)", self._tc_host)
```

In the `call_streaming()` method, modify the command construction:

```python
# Current (local execution):
cmd = [
    str(claude_bin), "-p", message,
    "--output-format", "stream-json",
    "--dangerously-skip-permissions",
    "--model", model,
    ...
]

# New (remote execution when self._remote is True):
if self._remote:
    # Build the claude command as a single string for SSH
    remote_cmd = (
        f"claude -p {shlex.quote(message)} "
        f"--output-format stream-json "
        f"--dangerously-skip-permissions "
        f"--model {model} "
        f"--verbose"
    )
    if session_id:
        remote_cmd += f" --resume {session_id}"
    if system_prompt:
        remote_cmd += f" --append-system-prompt {shlex.quote(system_prompt)}"

    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", self._tc_host, remote_cmd]
else:
    # Local execution (existing code)
    cmd = [str(claude_bin), "-p", message, ...]
```

**Important:** Use `shlex.quote()` for all user-provided strings to prevent shell injection.

### 2. `config.py` — Remote CLI Config

```python
# Remote CLI execution (Phase 4B)
HYBRID_CLI_REMOTE = os.environ.get("HYBRID_CLI_REMOTE", "false").lower() in ("true", "1", "yes")
HYBRID_TC_HOST = os.environ.get("HYBRID_TC_HOST", "tensor-core")
```

### 3. `k8s/configmap.yaml` — Enable Remote Mode

```yaml
HYBRID_CLI_REMOTE: "true"
HYBRID_TC_HOST: "tensor-core"
```

## SSH Connectivity (Already Verified)

The K8s pod has:
- SSH key at `/app/.ssh/id_ed25519` (copied from TC's key via K8s secret)
- SSH config at `/app/.ssh/config` (from `~/.ssh/config.puretensor`)
- `tensor-core` entry: `100.121.42.54` (Tailscale IP), user `puretensorai`
- `StrictHostKeyChecking accept-new`
- Already used by observers (git_push, git_auto_sync, daily_report)

## Session Management

- Session IDs from remote CLI are stored on TC's filesystem (`~/.claude/projects/...`)
- `--resume <session_id>` works because the claude binary on TC has its own session storage
- Session affinity in db.py tracks `backend="cli"` so subsequent messages route correctly
- The pod doesn't need to store CLI session files — TC manages them natively

## Stream Processing

The SSH pipe delivers the same `stream-json` output as local execution. The existing `_read_stream()` function in `engine.py` (lines 92-188) handles this format. No changes needed to stream parsing.

## Error Handling

```python
# SSH-specific errors to catch:
except asyncio.TimeoutError:
    log.error("Remote CLI timed out (SSH to %s)", self._tc_host)
    raise
except Exception as e:
    if "Connection refused" in str(e) or "No route to host" in str(e):
        log.error("SSH to %s failed: %s", self._tc_host, e)
        raise RuntimeError(f"Cannot reach {self._tc_host} via SSH") from e
    raise
```

The HybridBackend's fallback chain catches these and retries via API.

## Verification

1. Set `HYBRID_CLI_REMOTE=true` in configmap
2. Deploy and restart pod
3. Send a message that routes to CLI (e.g., "read /home/puretensorai/CLAUDE.md")
4. Verify the response contains TC-local file content (not pod-local)
5. Send "ls /mnt/storage/models/" via CLI — should show model files
6. Verify streaming works through SSH pipe (text appears incrementally)
7. Test session resumption: send follow-up message, verify `--resume` used
8. Test fallback: temporarily block SSH to TC, verify falls back to API
9. Check TC process list: `ps aux | grep claude` — should show claude process during active query

## Security Notes

- SSH key is the same key used by all pod→TC operations (not a new attack surface)
- Claude CLI on TC runs as `puretensorai` user (same as interactive sessions)
- `--dangerously-skip-permissions` is required for non-interactive execution
- The TC CLAUDE.md instructions apply to remote CLI sessions (same rules)
