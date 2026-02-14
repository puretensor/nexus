"""Tool registry and shared tool loop for all backends.

Provides six core tools matching Claude Code's toolset:
bash, read_file, write_file, edit_file, glob, grep.

Each tool has an OpenAI-compatible JSON schema and a Python executor
that returns (result_string, written_files_list).

The shared run_tool_loop_sync / run_tool_loop_async functions implement
a callback-driven tool loop that works with any API backend.  Each backend
supplies three thin adapter functions (send_request, parse_response,
format_tool_result) and the loop handles iteration, timeouts, and file
tracking.
"""

import asyncio
import json
import logging
import os
import pathlib
import subprocess
import time
from dataclasses import dataclass, field

log = logging.getLogger("nexus")

# Max chars returned from any single tool execution
MAX_OUTPUT_CHARS = 8000

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function calling format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command and return stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Returns the file text with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start from (1-indexed, optional)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to read (optional)",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file (creates or overwrites).",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific string in a file. The old_string must appear exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to find",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The replacement text",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern. Returns matching file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g. '**/*.py')",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: cwd)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a regex pattern. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search (default: cwd)",
                    },
                    "include": {
                        "type": "string",
                        "description": "File glob filter (e.g. '*.py')",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]


def _truncate(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate output to stay within context budget."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated, {len(text)} total chars)"


# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------


def _exec_bash(args: dict, *, timeout: int = 30, cwd: str | None = None) -> tuple[str, list[str]]:
    """Execute a shell command. Returns (output, written_files)."""
    command = args.get("command", "")
    if not command:
        return "Error: no command provided", []

    effective_cwd = cwd or os.getcwd()
    log.info("Tool bash: %s (cwd=%s, timeout=%ds)", command[:100], effective_cwd, timeout)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=timeout,
            cwd=effective_cwd,
            text=True,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            if output:
                output += "\n"
            output += result.stderr
        if result.returncode != 0:
            output += f"\n(exit code: {result.returncode})"
        return _truncate(output or "(no output)"), []
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s", []
    except Exception as e:
        return f"Error executing command: {e}", []


def _exec_read_file(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Read a file with optional offset/limit. Returns (content, [])."""
    file_path = args.get("file_path", "")
    if not file_path:
        return "Error: no file_path provided", []

    offset = args.get("offset", 1)
    if offset is None:
        offset = 1
    limit = args.get("limit")

    try:
        with open(file_path, "r", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return f"Error: file not found: {file_path}", []
    except PermissionError:
        return f"Error: permission denied: {file_path}", []
    except Exception as e:
        return f"Error reading file: {e}", []

    # Apply offset (1-indexed)
    start = max(0, offset - 1)
    if limit:
        end = start + limit
        lines = lines[start:end]
    else:
        lines = lines[start:]

    # Format with line numbers
    numbered = []
    for i, line in enumerate(lines, start=start + 1):
        numbered.append(f"{i:>6}\t{line.rstrip()}")

    result = "\n".join(numbered)
    if not result:
        return "(empty file)", []
    return _truncate(result), []


def _exec_write_file(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Write content to a file. Returns (confirmation, [file_path])."""
    file_path = args.get("file_path", "")
    content = args.get("content", "")
    if not file_path:
        return "Error: no file_path provided", []

    try:
        # Create parent directories if needed
        parent = pathlib.Path(file_path).parent
        parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w") as f:
            f.write(content)
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"Wrote {lines} lines to {file_path}", [file_path]
    except PermissionError:
        return f"Error: permission denied: {file_path}", []
    except Exception as e:
        return f"Error writing file: {e}", []


def _exec_edit_file(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Replace old_string with new_string in a file. Returns (confirmation, [file_path])."""
    file_path = args.get("file_path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")

    if not file_path:
        return "Error: no file_path provided", []
    if not old_string:
        return "Error: no old_string provided", []

    try:
        with open(file_path, "r") as f:
            content = f.read()
    except FileNotFoundError:
        return f"Error: file not found: {file_path}", []
    except Exception as e:
        return f"Error reading file: {e}", []

    count = content.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {file_path}", []
    if count > 1:
        return f"Error: old_string appears {count} times in {file_path} (must be unique)", []

    new_content = content.replace(old_string, new_string, 1)
    try:
        with open(file_path, "w") as f:
            f.write(new_content)
        return f"Edited {file_path} (replaced 1 occurrence)", [file_path]
    except Exception as e:
        return f"Error writing file: {e}", []


def _exec_glob(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Find files matching a glob pattern. Returns (paths, [])."""
    pattern = args.get("pattern", "")
    if not pattern:
        return "Error: no pattern provided", []

    search_path = args.get("path") or os.getcwd()

    try:
        base = pathlib.Path(search_path)
        matches = sorted(base.glob(pattern))[:100]
        if not matches:
            return f"No files matching '{pattern}' in {search_path}", []
        result = "\n".join(str(m) for m in matches)
        return _truncate(result), []
    except Exception as e:
        return f"Error in glob: {e}", []


def _exec_grep(args: dict, *, cwd: str | None = None, **_kwargs) -> tuple[str, list[str]]:
    """Search file contents with grep. Returns (matching lines, [])."""
    pattern = args.get("pattern", "")
    if not pattern:
        return "Error: no pattern provided", []

    search_path = args.get("path") or cwd or os.getcwd()
    include = args.get("include")

    cmd = ["grep", "-rn"]
    if include:
        cmd.extend(["--include", include])
    cmd.extend(["--", pattern, search_path])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stdout
        if not output:
            return f"No matches for '{pattern}' in {search_path}", []

        # Limit output lines
        lines = output.split("\n")
        if len(lines) > 200:
            output = "\n".join(lines[:200]) + f"\n... ({len(lines)} total matches)"

        return _truncate(output), []
    except subprocess.TimeoutExpired:
        return "Error: grep timed out after 15s", []
    except Exception as e:
        return f"Error in grep: {e}", []


# ---------------------------------------------------------------------------
# Executor dispatch
# ---------------------------------------------------------------------------

_EXECUTORS = {
    "bash": _exec_bash,
    "read_file": _exec_read_file,
    "write_file": _exec_write_file,
    "edit_file": _exec_edit_file,
    "glob": _exec_glob,
    "grep": _exec_grep,
}


def execute_tool(
    name: str,
    args: dict,
    *,
    timeout: int = 30,
    cwd: str | None = None,
) -> tuple[str, list[str]]:
    """Execute a tool by name. Returns (result_string, written_files).

    Args:
        name: Tool name (bash, read_file, write_file, edit_file, glob, grep)
        args: Tool arguments dict
        timeout: Timeout for bash commands (seconds)
        cwd: Working directory for bash/grep
    """
    executor = _EXECUTORS.get(name)
    if executor is None:
        return f"Error: unknown tool '{name}'", []

    try:
        return executor(args, timeout=timeout, cwd=cwd)
    except Exception as e:
        log.error("Tool %s execution error: %s", name, e)
        return f"Error executing {name}: {e}", []


# ---------------------------------------------------------------------------
# Shared tool loop for API backends
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """Normalised tool call — backend-agnostic."""
    id: str            # tool_use_id (Anthropic), generated UUID (Gemini), call ID (OpenAI)
    name: str          # function name
    arguments: dict    # parsed arguments


def _format_tool_status(tool_name: str, tool_input: dict) -> str:
    """Map a tool call to a human-readable status line (local copy to avoid circular import)."""
    if tool_name in ("Bash", "bash"):
        cmd = tool_input.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"Running: {cmd}"
    elif tool_name in ("Read", "read_file"):
        return f"Reading: {tool_input.get('file_path', '?')}"
    elif tool_name in ("Edit", "edit_file"):
        return f"Editing: {tool_input.get('file_path', '?')}"
    elif tool_name in ("Write", "write_file"):
        return f"Writing: {tool_input.get('file_path', '?')}"
    elif tool_name in ("Glob", "glob"):
        return f"Searching files: {tool_input.get('pattern', '?')}"
    elif tool_name in ("Grep", "grep"):
        return f"Searching content: {tool_input.get('pattern', '?')}"
    return f"Tool: {tool_name}"


def run_tool_loop_sync(
    messages: list,
    send_request,
    parse_response,
    format_tool_result,
    *,
    max_iterations: int = 25,
    tool_timeout: int = 30,
    total_timeout: int = 300,
    cwd: str | None = None,
) -> dict:
    """Generic synchronous tool loop.

    Args:
        messages: Mutable conversation list — modified in place.
        send_request(messages) -> raw_response: Send messages to the API.
        parse_response(raw_response) -> (text, list[ToolCall], assistant_msg):
            Extract text, tool calls, and the assistant message to append.
        format_tool_result(tool_name, call_id, result_str) -> dict:
            Format a tool result as a message dict for the API.
        max_iterations: Max tool-loop rounds.
        tool_timeout: Per-tool bash timeout in seconds.
        total_timeout: Wall-clock limit for the entire loop.
        cwd: Working directory for bash/grep tools.

    Returns:
        {"result": str, "session_id": None, "written_files": list[str]}
    """
    written_files: list[str] = []
    last_text = ""
    start = time.time()

    for iteration in range(max_iterations):
        if time.time() - start > total_timeout:
            log.warning("Tool loop: total timeout after %d iterations", iteration)
            break

        response = send_request(messages)
        text, tool_calls, assistant_msg = parse_response(response)
        messages.append(assistant_msg)

        if text:
            last_text = text

        if not tool_calls:
            return {
                "result": last_text or "(empty response)",
                "session_id": None,
                "written_files": written_files,
            }

        for tc in tool_calls:
            log.info("Tool call [sync]: %s(%s)", tc.name, str(tc.arguments)[:100])
            result_str, new_files = execute_tool(
                tc.name, tc.arguments, timeout=tool_timeout, cwd=cwd,
            )
            written_files.extend(new_files)
            messages.append(format_tool_result(tc.name, tc.id, result_str))

    return {
        "result": last_text or "(max tool iterations reached)",
        "session_id": None,
        "written_files": written_files,
    }


async def run_tool_loop_async(
    messages: list,
    send_request,
    parse_response,
    format_tool_result,
    *,
    max_iterations: int = 25,
    tool_timeout: int = 30,
    total_timeout: int = 300,
    cwd: str | None = None,
    streaming_editor=None,
    on_progress=None,
) -> dict:
    """Generic async tool loop with tool status updates.

    Same interface as run_tool_loop_sync but awaits send_request and runs
    tool execution in a thread pool.  Sends status updates via
    streaming_editor or on_progress callbacks.
    """
    written_files: list[str] = []
    last_text = ""
    start = time.time()

    for iteration in range(max_iterations):
        if time.time() - start > total_timeout:
            log.warning("Tool loop async: total timeout after %d iterations", iteration)
            break

        response = await send_request(messages)
        text, tool_calls, assistant_msg = parse_response(response)
        messages.append(assistant_msg)

        if text:
            last_text = text

        if not tool_calls:
            return {
                "result": last_text or "(empty response)",
                "session_id": None,
                "written_files": written_files,
            }

        for tc in tool_calls:
            # Show tool status
            status = _format_tool_status(tc.name, tc.arguments)
            if streaming_editor:
                await streaming_editor.add_tool_status(status)
            elif on_progress:
                await on_progress(status)

            log.info("Tool call [async]: %s(%s)", tc.name, str(tc.arguments)[:100])

            # Execute in thread pool to avoid blocking
            result_str, new_files = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda n=tc.name, a=tc.arguments: execute_tool(
                    n, a, timeout=tool_timeout, cwd=cwd,
                ),
            )
            written_files.extend(new_files)
            messages.append(format_tool_result(tc.name, tc.id, result_str))

    return {
        "result": last_text or "(max tool iterations reached)",
        "session_id": None,
        "written_files": written_files,
    }
