# Wave 2 — 3C: Subagent Parallelism Implementation Spec

**Depends on:** 3A (Task Management) + 3B (Plan Mode) — must be deployed first
**Complexity:** High (~3-4 hours)
**Agent instructions:** Implement EXACTLY as specified. Read all referenced files first.

---

## Overview

Add a `spawn_subagent` tool that creates independent Bedrock API conversations with their own message history and tool access. Multiple subagents in the same turn execute in parallel.

## Files to Modify

### 1. `backends/tools.py` — Tool Schema

Add after the `exit_plan_mode` schema in `TOOL_SCHEMAS`:

```python
{
    "type": "function",
    "function": {
        "name": "spawn_subagent",
        "description": (
            "Spawn a parallel subagent to handle a focused subtask. The subagent "
            "gets its own conversation context and tool access. Use this to "
            "parallelize independent research tasks, delegate focused analysis, "
            "or run concurrent operations. The subagent cannot spawn further "
            "subagents. Returns the subagent's final text response."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Clear, complete description of what the subagent should do",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: restrict subagent to specific tools. "
                        "Default: all read-only tools (read_file, glob, grep, "
                        "web_search, web_fetch). Add 'bash', 'write_file', 'edit_file' "
                        "explicitly if the subagent needs write access."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional: model to use. Default: same as parent. "
                        "Use 'haiku' for cheaper research tasks."
                    ),
                },
            },
            "required": ["task"],
        },
    },
},
```

### 2. `backends/tools.py` — Executor

Add after the `_exec_exit_plan_mode` function:

```python
_SUBAGENT_DEFAULT_TOOLS = ["read_file", "glob", "grep", "web_search", "web_fetch"]
_SUBAGENT_MAX_ITERATIONS = 15
_SUBAGENT_TIMEOUT = 180

def _exec_spawn_subagent(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Spawn a subagent with its own Bedrock conversation."""
    task = args.get("task", "").strip()
    if not task:
        return "Error: no task provided", []

    if getattr(_tool_context, "is_subagent", False):
        return "Error: subagents cannot spawn further subagents", []

    requested_tools = args.get("tools") or _SUBAGENT_DEFAULT_TOOLS
    model = args.get("model") or None

    log.info("Spawning subagent: %s (tools=%s, model=%s)", task[:80], requested_tools, model)

    try:
        result = _run_subagent(task, requested_tools, model)
        return _truncate(result), []
    except Exception as e:
        log.error("Subagent failed: %s", e)
        return f"Subagent error: {e}", []


def _run_subagent(task: str, tool_names: list[str], model: str | None) -> str:
    """Run a subagent conversation synchronously."""
    from backends.bedrock_api import BedrockAPIBackend

    backend = BedrockAPIBackend()

    # Filter tool schemas — exclude spawn_subagent to prevent nesting
    allowed = set(tool_names)
    filtered_schemas = [
        t for t in TOOL_SCHEMAS
        if t["function"]["name"] in allowed and t["function"]["name"] != "spawn_subagent"
    ]

    system_prompt = (
        "You are a focused subagent. Complete the assigned task thoroughly and concisely. "
        "You cannot spawn further subagents. When done, provide your final answer as text."
    )

    # Mark thread as subagent context
    _tool_context.is_subagent = True

    try:
        # Build subagent conversation
        messages = [{"role": "user", "content": task}]

        # Use the backend's sync call with filtered tools
        # The subagent gets its own tool loop with reduced iterations
        result = backend.call_sync(
            message=task,
            system_prompt=system_prompt,
            model=model or "sonnet",
            tools=filtered_schemas,
            max_iterations=_SUBAGENT_MAX_ITERATIONS,
        )
        return result.get("result", "(no result)")
    finally:
        _tool_context.is_subagent = False
```

**NOTE:** The `call_sync` method of BedrockAPIBackend may need a `tools` parameter override. Check the current signature at implementation time. If `call_sync` doesn't accept custom tools, add a `tool_schemas_override` parameter.

### 3. `backends/tools.py` — Parallel Execution in `run_tool_loop_async()`

In `run_tool_loop_async()`, replace the sequential tool execution block with:

```python
for tc in tool_calls:
    # ... existing code

# ADD: parallel execution when all calls are subagents
if len(tool_calls) > 1 and all(tc.name == "spawn_subagent" for tc in tool_calls):
    # All calls are subagents — run in parallel
    import asyncio
    tasks = [
        asyncio.get_event_loop().run_in_executor(
            None,
            lambda n=tc.name, a=tc.arguments: execute_tool(n, a, timeout=180, cwd=cwd),
        )
        for tc in tool_calls
    ]
    results = await asyncio.gather(*tasks)
    for tc, (result_str, new_files) in zip(tool_calls, results):
        written_files.extend(new_files)
        messages.append(format_tool_result(tc.name, tc.id, result_str))
else:
    # Sequential (existing behavior)
    for tc in tool_calls:
        ...
```

### 4. `backends/tools.py` — Registration

Add to `_EXECUTORS`:
```python
"spawn_subagent": _exec_spawn_subagent,
```

Add to `_format_tool_status()`:
```python
elif tool_name == "spawn_subagent":
    task = tool_input.get("task", "?")[:60]
    return f"Subagent: {task}"
```

### 5. `config.py` — Constants

```python
SUBAGENT_MODEL = os.environ.get("SUBAGENT_MODEL", "sonnet")
SUBAGENT_MAX_ITER = int(os.environ.get("SUBAGENT_MAX_ITER", "15"))
SUBAGENT_TIMEOUT = int(os.environ.get("SUBAGENT_TIMEOUT", "180"))
```

### 6. `tests/test_tools.py`

Update tool count and name set to include `spawn_subagent`. Add test for anti-nesting guard.

## Verification

1. Ask HAL: "Research X and Y in parallel" — should spawn 2 subagents
2. Verify both results returned in the response
3. Verify subagent cannot spawn sub-subagents
4. Verify plan mode inherited (subagent in plan mode parent can't write)
5. Check logs for subagent cost (input/output tokens)
