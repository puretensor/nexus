# Wave 3 — 4A: Hybrid Backend Implementation Spec

**Depends on:** 2B (Bedrock Streaming) — must be deployed first
**Complexity:** Medium-High (~3 hours)
**Agent instructions:** Implement EXACTLY as specified. Read all referenced files first.

---

## Overview

Create a `HybridBackend` class that wraps both `BedrockAPIBackend` (fast path for simple queries) and `ClaudeCodeBackend` (power path for complex tasks). Routes requests based on message complexity analysis.

## Architecture

```
HybridBackend
├── _api_backend: BedrockAPIBackend      (simple queries, web search, status)
├── _cli_backend: ClaudeCodeBackend      (complex tasks, deployments, reports)
├── _classify(message, session) → "api" | "cli"
├── call_sync() → delegates to chosen backend
├── call_streaming() → delegates to chosen backend
└── Fallback: CLI failure → API retry
```

## Files to Create/Modify

### 1. `backends/hybrid.py` — NEW FILE (~200 lines)

```python
"""Hybrid backend — routes between Bedrock API (fast) and Claude Code CLI (power)."""

import logging
import os
import re

from backends.base import BaseBackend

log = logging.getLogger("nexus")

_CLI_PATTERNS = re.compile(
    r'\b(deploy|restart|fix|update|install|configure|commit|push|'
    r'write.*report|create.*file|edit.*file|merge|migrate|'
    r'build|compile|test.*suite|debug|diagnose|audit|refactor)\b',
    re.IGNORECASE,
)

_MULTI_STEP = re.compile(
    r'\b(then|after that|next step|also|finally|first.*then)\b',
    re.IGNORECASE,
)


class HybridBackend(BaseBackend):
    name = "hybrid"
    supports_streaming = True
    supports_tools = True
    supports_sessions = True

    def __init__(self):
        from backends.bedrock_api import BedrockAPIBackend
        from backends.claude_code import ClaudeCodeBackend

        self._api = BedrockAPIBackend()
        self._cli = ClaudeCodeBackend()
        self._default = os.environ.get("HYBRID_DEFAULT", "api")
        log.info("HybridBackend initialized (default=%s)", self._default)

    def _classify(self, message: str, session: dict | None = None) -> str:
        """Route message to 'api' or 'cli' backend."""
        # Session affinity — keep session on same backend
        if session and session.get("backend") == "cli":
            return "cli"

        # Explicit routing prefixes
        if message.startswith("!cli "):
            return "cli"
        if message.startswith("!api "):
            return "api"

        # Pattern matching for complex tasks
        if _CLI_PATTERNS.search(message):
            return "cli"
        if _MULTI_STEP.search(message):
            return "cli"

        # Long messages with action intent
        if len(message) > 500 and any(w in message.lower() for w in ("implement", "create", "write", "deploy")):
            return "cli"

        return self._default

    def call_sync(self, message, **kwargs):
        """Sync call — always use API (observers, background tasks)."""
        return self._api.call_sync(message, **kwargs)

    async def call_streaming(self, message, **kwargs):
        """Streaming call — route based on complexity."""
        session = kwargs.get("session")
        target = self._classify(message, session)

        # Strip routing prefix if present
        if message.startswith("!cli "):
            message = message[5:]
            kwargs["message"] = message
        elif message.startswith("!api "):
            message = message[5:]
            kwargs["message"] = message

        log.info("Hybrid routing: %s → %s", message[:60], target)

        if target == "cli":
            try:
                result = await self._cli.call_streaming(message, **kwargs)
                return result
            except Exception as e:
                log.warning("CLI backend failed (%s), falling back to API", e)

        return await self._api.call_streaming(message, **kwargs)
```

### 2. `backends/__init__.py` — Register Hybrid

Add to the backend registry dict:
```python
"hybrid": ("backends.hybrid", "HybridBackend"),
```

### 3. `db.py` — Session Backend Tracking

Add `backend` column to sessions table. In `init_db()`, add migration:
```python
session_cols = [row[1] for row in con.execute("PRAGMA table_info(sessions)").fetchall()]
if "backend" not in session_cols:
    con.execute("ALTER TABLE sessions ADD COLUMN backend TEXT DEFAULT 'api'")
    log.info("Added 'backend' column to sessions table")
```

Update `create_session()` and `update_session()` to accept/store backend name.

### 4. `config.py` — Hybrid Config

```python
# Hybrid backend
HYBRID_DEFAULT = os.environ.get("HYBRID_DEFAULT", "api")
HYBRID_CLI_TIMEOUT = int(os.environ.get("HYBRID_CLI_TIMEOUT", "1800"))
HYBRID_API_TIMEOUT = int(os.environ.get("HYBRID_API_TIMEOUT", "300"))
```

### 5. `channels/telegram/commands.py` — Model Switching

Update `/opus` and `/sonnet` commands to work with hybrid mode. Currently they force-switch to `bedrock_api` backend. With hybrid mode, they should set the model without changing the backend:

```python
# Instead of:
if config.ENGINE_BACKEND not in ("bedrock_api", "anthropic_api"):
    config.ENGINE_BACKEND = "bedrock_api"
    reset_backend()

# Use:
if config.ENGINE_BACKEND not in ("bedrock_api", "anthropic_api", "hybrid"):
    config.ENGINE_BACKEND = "bedrock_api"
    reset_backend()
```

### 6. `k8s/configmap.yaml` — Set Default Backend

```yaml
ENGINE_BACKEND: "hybrid"
```

## Cost Insight

Max subscription ($200/month) makes CLI free for complex tasks. API costs per-token ($3/$15 per M in/out for Sonnet). Route expensive agentic tasks (3+ tool calls expected) to CLI for massive cost savings.

## Verification

1. Set `ENGINE_BACKEND=hybrid` in `.env`
2. Send simple question → verify routes to API (check logs for "Hybrid routing: ... → api")
3. Send "deploy X to production" → verify routes to CLI (logs show "→ cli")
4. Send "!cli status" → verify explicit CLI routing
5. Send "!api deploy something" → verify explicit API routing
6. Kill Claude CLI binary temporarily → verify fallback to API works
7. Verify `/opus` and `/sonnet` work with hybrid backend
8. Verify session affinity (CLI session stays on CLI)
