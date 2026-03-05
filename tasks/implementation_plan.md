# Nexus Quality Upgrade — Master Implementation Plan

**Date:** 2026-03-05
**Status:** Phase 1 COMPLETE, Phases 2-4 PLANNED
**Source:** Quality Gap Analysis (17 issues), 6 research analysts, 12 analysis vectors

---

## Architecture Overview

```
Phase 2 (Today)          Phase 3 (Today)              Phase 4 (Today)
├─ 2A: Ripgrep           ├─ 3A: Task Management       ├─ 4A: Hybrid Backend
├─ 2B: Bedrock Streaming ├─ 3B: Plan Mode             └─ 4B: Native TC Execution
└─ 2C: Context Compress  └─ 3C: Subagent Parallelism
```

All phases execute in parallel via independent agents. Dependencies noted below.

---

## PHASE 2 — Infrastructure Foundations

### 2A: Ripgrep Migration
**Agent:** Independent, no dependencies
**Complexity:** Trivial (~30 min)
**Files:**

| File | Change |
|------|--------|
| `Dockerfile` line 4 | Replace `grep` with `ripgrep` |
| `backends/tools.py:460` | `["grep", "-rn"]` → `["rg", "-n", "--no-ignore", "--no-messages"]` |
| `backends/tools.py:462` | `["--include", include]` → `["--glob", include]` |
| `backends/tools.py:470` | `timeout=15` → `timeout=30` |
| `backends/tools.py:483` | Error msg: `"15s"` → `"30s"` |
| `backends/tools.py` docstring | `"grep"` → `"ripgrep"` |
| `tests/test_tools.py` | Add `test_ripgrep_binary_available` |

**Verification:** `rg --version` in container, existing grep tests pass, functional search test.

---

### 2B: Bedrock Streaming Migration
**Agent:** Independent, no dependencies
**Complexity:** Medium (~2-3 hours)
**CRITICAL:** Current `BEDROCK_MAX_TOKENS=64000` exceeds the 21,333 non-streaming limit. Streaming is MANDATORY.

**Files:**

| File | Change |
|------|--------|
| `backends/bedrock_api.py` | New `_consume_stream()` method (~100 lines) |
| `backends/bedrock_api.py` | Modify `call_streaming()` to use `converse_stream()` |
| `backends/bedrock_api.py` | Add `_converse_stream()` method |
| `backends/bedrock_api.py` | Keep `call_sync()` on `converse()` with maxTokens capped at 21333 |
| `backends/tools.py` | Modify `run_tool_loop_async()` to support combined send+parse callback |

**Architecture:**

```
call_streaming() path (interactive):
  converse_stream() → EventStream → _consume_stream()
  ├── Text deltas → streaming_editor.add_text(delta)
  ├── Thinking deltas → accumulated, round-tripped
  ├── Tool use deltas → accumulated, parsed to ToolCall
  └── Returns (text, tool_calls, assistant_msg) for tool loop

call_sync() path (observers):
  converse() with maxTokens=21333 (safe non-streaming limit)
  Unchanged from current implementation
```

**Stream Event Processing:**
1. `messageStart` → begin accumulation
2. `contentBlockStart` → detect type (text/toolUse/reasoningContent)
3. `contentBlockDelta` → accumulate + stream text to editor
4. `contentBlockStop` → finalize block (parse tool JSON, capture thinking signature)
5. `messageStop` → extract stopReason
6. `metadata` → log usage for cost tracking

**Async-Sync Bridge (boto3 is sync-only):**
- Run `converse_stream()` in `run_in_executor()`
- Use `asyncio.run_coroutine_threadsafe()` from executor thread to call `streaming_editor.add_text()`
- OR: iterate stream events via async generator wrapping sync iterator

**Tool Loop Integration:**
Option: New `send_and_parse_stream` callback in `run_tool_loop_async()`:
```python
async def run_tool_loop_async(
    ...,
    send_and_parse_stream=None,  # NEW: combined send+parse for streaming
):
    if send_and_parse_stream:
        text, tool_calls, assistant_msg = await send_and_parse_stream(messages, streaming_editor)
    else:
        response = await send_request(messages)
        text, tool_calls, assistant_msg = parse_response(response)
```

**Thinking Block Round-Trip in Streaming:**
- `reasoningContent` text deltas accumulated per contentBlockIndex
- Signature captured at contentBlockStop
- Stored as `{"type": "thinking", "thinking": text, "signature": sig}` in Anthropic format
- `redactedContent` (binary) preserved as `{"type": "redacted_thinking", "data": ...}`
- Must be passed back unmodified in subsequent tool loop iterations

**Error Handling:**
- Stream can contain `internalServerException`, `modelStreamErrorException`, `throttlingException`, `validationException`
- Catch and convert to Python exceptions
- On stream error mid-response: return accumulated text so far + error suffix

**Verification:**
1. Send message, verify real-time text appearing in Telegram (not all-at-once)
2. Trigger tool use, verify tool status shown then text streams after
3. Test thinking-heavy response, verify thinking blocks round-trip in tool loop
4. Test observer sync path still works (capped at 21333 tokens)
5. Test maxTokens > 21333 actually produces full output (was previously silently truncated)

---

### 2C: Context Compression
**Agent:** Independent, no dependencies
**Complexity:** Medium (~2-3 hours)

**Three-tier strategy:**

#### Tier 1: Tool Result Truncation (Zero-Cost, Inline)
**File:** `db.py`

New function `_compress_tool_results(messages)`:
- Keep last 6 tool results verbatim (`TOOL_RESULT_FULL_WINDOW = 6`)
- Truncate older tool results to 200 chars (`TOOL_RESULT_SUMMARY_CHARS = 200`)
- Called in `save_conversation_history()` before `_trim_history()`
- Expected compression: 60-80% for tool-heavy sessions
- Zero cost, zero latency, zero risk

#### Tier 2: LLM-Based Summarization (Triggered)
**Files:**

| File | Change |
|------|--------|
| `context_compression.py` | **NEW FILE** (~150 lines): `_estimate_tokens()`, `compress_history()`, `_build_summary_prompt()`, `_call_summarizer()` |
| `backends/bedrock_api.py:391` | Integrate at read-time in `call_sync()` |
| `backends/bedrock_api.py:457` | Integrate at read-time in `call_streaming()` |
| `config.py` | Add: `COMPRESS_TRIGGER_TOKENS=100000`, `PRESERVE_RECENT_MESSAGES=40`, `SUMMARY_MODEL="haiku"` |
| `db.py` | Add `context_summaries` table in `init_db()` |

**Algorithm:**
```
Load history from DB
→ _compress_tool_results() [Tier 1, always]
→ _estimate_tokens() [chars / 3.5]
→ If > COMPRESS_TRIGGER_TOKENS:
    Split: old_messages | recent_messages (last 40)
    Summarize old_messages via Haiku (~$0.015)
    Replace old messages with [summary_msg, ack_msg]
    Save compressed history back to DB
→ Pass to API
```

**Summary injection format:**
```
[CONVERSATION CONTEXT SUMMARY — Earlier messages were compressed]
• TASK STATE: ...
• KEY DECISIONS: ...
• FILES MODIFIED: ...
• CURRENT OBJECTIVE: ...
[END SUMMARY — Recent conversation continues below]
```

**Emergency fallback:** If Bedrock returns ValidationException (token limit exceeded), trigger immediate compression and retry.

#### Tier 3: Archival (Future, not this sprint)
- `context_summaries` table for audit trail
- Chained summaries for ultra-long sessions

**Verification:**
1. Start a tool-heavy session (10+ tool calls)
2. Verify old tool results truncated in DB after save
3. Push conversation past 100K estimated tokens
4. Verify summary generated and injected
5. Verify agent maintains context of original task after compression
6. Test emergency compression on token limit exceeded

---

## PHASE 3 — Cognitive Capabilities

### 3A: Task Management
**Agent:** Independent, no dependencies
**Complexity:** Low (~1 hour)

**Files:**

| File | Change |
|------|--------|
| `backends/tools.py` | 3 tool schemas (`create_task`, `update_task`, `list_tasks`) |
| `backends/tools.py` | 3 executor functions |
| `backends/tools.py` | 3 entries in `_EXECUTORS` dict |
| `backends/tools.py` | 3 entries in `_format_tool_status()` |
| `db.py` | `tasks` table in `init_db()` |
| `db.py` | `db_create_task()`, `db_update_task()`, `db_list_tasks()` |
| `tests/test_tools.py` | Update tool count and name set |

**SQLite Schema:**
```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'medium',
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);
```

**Task statuses:** pending, in_progress, done, cancelled
**Priorities:** low, medium, high, critical

**Verification:** Create task via Telegram, list tasks, update status, verify persistence across sessions.

---

### 3B: Plan Mode
**Agent:** Independent, no dependencies
**Complexity:** Low-Medium (~1.5 hours)

**Files:**

| File | Change |
|------|--------|
| `backends/tools.py` | 2 tool schemas (`enter_plan_mode`, `exit_plan_mode`) |
| `backends/tools.py` | 2 executor functions |
| `backends/tools.py` | Thread-local state: `_tool_context = threading.local()` |
| `backends/tools.py` | Plan mode gate in `execute_tool()` |
| `backends/tools.py` | 2 entries in `_EXECUTORS` and `_format_tool_status()` |
| `tests/test_tools.py` | Update tool count and name set, add plan mode enforcement test |

**Enforcement mechanism:**
```python
_WRITE_TOOLS = {"bash", "write_file", "edit_file"}

def execute_tool(name, args, ...):
    if is_plan_mode() and name in _WRITE_TOOLS:
        return f"Error: '{name}' not available in plan mode. Call exit_plan_mode first.", []
    # ... normal dispatch
```

**Thread-local state (safe for concurrent conversations):**
```python
import threading
_tool_context = threading.local()

def is_plan_mode() -> bool:
    return getattr(_tool_context, "plan_mode", False)
```

**Verification:** Enter plan mode, try bash command (should fail), read file (should work), exit plan mode, bash (should work).

---

### 3C: Subagent Parallelism
**Agent:** Depends on 3A (task management) and 3B (plan mode) being complete first
**Complexity:** High (~3-4 hours)

**Files:**

| File | Change |
|------|--------|
| `backends/tools.py` | 1 tool schema (`spawn_subagent`) |
| `backends/tools.py` | `_exec_spawn_subagent()` executor |
| `backends/tools.py` | `_run_subagent()` helper (creates own Bedrock conversation) |
| `backends/tools.py` | Parallel execution in `run_tool_loop_async()` |
| `backends/tools.py` | Anti-nesting guard via `_tool_context.is_subagent` |
| `config.py` | `SUBAGENT_MODEL`, `SUBAGENT_MAX_ITER=15`, `SUBAGENT_TIMEOUT=180` |

**Key design decisions:**
- Subagents get read-only tools by default (can add write tools explicitly)
- Subagents CANNOT spawn further subagents (anti-nesting)
- Multiple spawn_subagent calls in same turn execute in parallel via `asyncio.gather`
- Each subagent creates its own `BedrockAPIBackend` instance + tool loop
- Subagents inherit plan mode restrictions if parent is in plan mode
- Use Haiku by default for cost efficiency (~$0.01/subagent)

**Parallel execution in tool loop:**
```python
# When all tool calls in a turn are spawn_subagent:
if all(tc.name == "spawn_subagent" for tc in tool_calls):
    tasks = [run_in_executor(execute_tool, tc) for tc in tool_calls]
    results = await asyncio.gather(*tasks)
else:
    # Sequential (existing behavior)
```

**Cost:** ~$0.01-0.05 per subagent (Haiku/Sonnet). Budget 5-10 subagents per complex task = $0.05-$0.50.

**Verification:** Ask HAL to research two topics in parallel, verify both subagent results returned, verify no nesting allowed.

---

## PHASE 4 — Architecture Evolution

### 4A: Hybrid Backend
**Agent:** Depends on Phase 2B (streaming) and Phase 3 being complete
**Complexity:** Medium-High (~3 hours)

**Files:**

| File | Change |
|------|--------|
| `backends/hybrid.py` | **NEW FILE** (~200 lines): `HybridBackend` class |
| `backends/__init__.py` | Add `"hybrid"` to backend registry |
| `config.py` | Add `HYBRID_DEFAULT`, `HYBRID_CLI_TIMEOUT`, `HYBRID_API_TIMEOUT` |
| `db.py` | Add `backend` column to `sessions` table |
| `channels/telegram/commands.py` | Update `/opus`/`/sonnet` for hybrid mode |
| `k8s/configmap.yaml` | Set `ENGINE_BACKEND: hybrid` |

**Routing Logic:**
```
Message → ComplexityClassifier
├── CLI triggers: deploy, restart, fix, update, multi-step, file operations
├── API triggers: simple questions, web search, status checks, short messages
├── Explicit: !cli / !api prefix
├── Session affinity: keep session on same backend
└── Default: API (fast path)
```

**Fallback chain:** CLI failure → API retry (API is always the safety net)

**Cost insight:** Max subscription ($200/month) makes CLI free for complex tasks. API costs per-token. Route expensive agentic tasks to CLI.

**Verification:** Send simple question (routes to API, fast), send "deploy X" (routes to CLI), verify session affinity across messages.

---

### 4B: Native TC Execution
**Agent:** Depends on 4A (hybrid backend)
**Complexity:** Medium (~2 hours)

**Architecture:** When hybrid backend routes to CLI for TC-native tasks, invoke Claude Code CLI on tensor-core via SSH instead of locally in the pod.

```python
# In modified ClaudeCodeBackend:
if self._remote_mode:
    cmd = ["ssh", "tensor-core", "claude", "-p", message, "--output-format", "stream-json", ...]
else:
    cmd = ["claude", "-p", message, "--output-format", "stream-json", ...]
```

**Files:**

| File | Change |
|------|--------|
| `backends/claude_code.py` | Add `remote_mode` parameter, SSH-wrapped CLI invocation |
| `config.py` | Add `HYBRID_CLI_REMOTE=true`, `HYBRID_TC_HOST=tensor-core` |

**Benefits:**
- CLI runs natively on TC: full filesystem, GPU, models, all local tools
- CLAUDE.md and project context native to TC
- Claude CLI on TC uses its own OAuth tokens (already present)
- SSH from pod to TC already operational (verified by health probes, observers)

**Verification:** Route a task to CLI via hybrid backend, verify it executes on TC filesystem, verify file operations target TC paths.

---

## DEPENDENCY GRAPH

```
Phase 2A (Ripgrep)           ──────────────────────────> Deploy
Phase 2B (Streaming)         ──────────────────────────> Deploy ──> Phase 4A
Phase 2C (Compression)       ──────────────────────────> Deploy

Phase 3A (Tasks)             ──────────────────────────> Deploy ──> Phase 3C
Phase 3B (Plan Mode)         ──────────────────────────> Deploy ──> Phase 3C
Phase 3C (Subagents)         ─── waits for 3A + 3B ───> Deploy

Phase 4A (Hybrid Backend)    ─── waits for 2B ─────────> Deploy ──> Phase 4B
Phase 4B (Native TC Exec)    ─── waits for 4A ─────────> Deploy
```

**Parallel execution plan (8 agents):**

| Wave | Agents Running | Work |
|------|---------------|------|
| Wave 1 | 5 parallel | 2A, 2B, 2C, 3A, 3B |
| Wave 2 | 1 | 3C (after 3A + 3B complete) |
| Wave 3 | 1 | 4A (after 2B complete) |
| Wave 4 | 1 | 4B (after 4A complete) |

Wave 1 handles 5 of 8 work items simultaneously. Wave 2-4 are sequential due to dependencies.

---

## DEPLOYMENT STRATEGY

Each completed work item gets:
1. Local syntax check (`python3 -c "import py_compile; ..."`)
2. Unit tests (`python3 -m pytest tests/test_tools.py -v`)
3. Integration into single deploy
4. `bash k8s/deploy.sh` — single deploy for all Phase 2+3A+3B changes
5. Telegram smoke tests per feature
6. Git commit

**Single deploy for Wave 1:** All 5 Wave 1 items merge into one deploy since they touch different files with no conflicts:
- 2A: Dockerfile + tools.py grep function
- 2B: bedrock_api.py + tools.py tool loop
- 2C: NEW context_compression.py + db.py + config.py
- 3A: tools.py schemas/executors + db.py tasks table
- 3B: tools.py plan mode + threading

---

## TOTAL SCOPE SUMMARY

| Metric | Value |
|--------|-------|
| New files | 2 (context_compression.py, backends/hybrid.py) |
| Modified files | 8 (tools.py, bedrock_api.py, db.py, config.py, Dockerfile, __init__.py, claude_code.py, commands.py) |
| New lines of code | ~1,200-1,500 |
| New tools | 8 (web_fetch done, + enter_plan_mode, exit_plan_mode, create_task, update_task, list_tasks, spawn_subagent) |
| New DB tables | 2 (tasks, context_summaries) |
| Gap analysis items resolved | 14 of 17 (#1-8, #10-11, #14-16 + partial #3, #12) |
| Remaining after all phases | #9 partial (K8s mismatch mitigated by 4B), #13 (memory, future), #17 (formatting, low priority) |
