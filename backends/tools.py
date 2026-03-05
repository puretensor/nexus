"""Tool registry and shared tool loop for all backends.

Provides eight core tools matching Claude Code's toolset:
bash, read_file, write_file, edit_file, glob, grep, web_search, web_fetch.

Each tool has an OpenAI-compatible JSON schema and a Python executor
that returns (result_string, written_files_list).

The shared run_tool_loop_sync / run_tool_loop_async functions implement
a callback-driven tool loop that works with any API backend.  Each backend
supplies three thin adapter functions (send_request, parse_response,
format_tool_result) and the loop handles iteration, timeouts, and file
tracking.
"""

import asyncio
import html as html_mod
import json
import logging
import os
import pathlib
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("nexus")

import threading

# Thread-local context for plan mode (safe for concurrent conversations)
_tool_context = threading.local()

def is_plan_mode() -> bool:
    """Check if current thread is in plan mode."""
    return getattr(_tool_context, "plan_mode", False)

def set_plan_mode(active: bool):
    """Set plan mode for current thread."""
    _tool_context.plan_mode = active

# Tools that are blocked in plan mode
_WRITE_TOOLS = frozenset({"bash", "write_file", "edit_file"})

# Max chars returned from any single tool execution
MAX_OUTPUT_CHARS = 32000

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
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo. Returns titles, URLs, and snippets for the top results. Use this when you need current information, facts, or anything not available locally.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5, max: 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch the content of a URL and return it as readable text. HTML is automatically converted to plain text. Use for reading documentation, verifying deployments, checking API responses, or scraping pages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch (http or https)",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Optional HTTP headers as key-value pairs",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enter_plan_mode",
            "description": (
                "Switch to plan mode for read-only exploration. In plan mode you can "
                "read files, search, and reason — but CANNOT run bash, write, or edit. "
                "Use before non-trivial tasks to design your approach. Call exit_plan_mode "
                "when ready to implement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why you are entering plan mode",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exit_plan_mode",
            "description": "Exit plan mode and return to full execution mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_summary": {
                        "type": "string",
                        "description": "Brief summary of your plan (1-3 sentences)",
                    },
                },
                "required": ["plan_summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_phone_call",
            "description": (
                "Make an outbound phone call via HAL. Use for booking/cancelling "
                "appointments, calling businesses for information, or routine phone "
                "tasks. HAL introduces himself as Heimir's personal assistant. "
                "Returns the call transcript and outcome when the call completes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "Phone number in E.164 format (+44...)",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "What the call is about: book_appointment, cancel_appointment, inquiry, or test",
                    },
                    "context": {
                        "type": "string",
                        "description": "Key details for the call: names, dates, preferences, account numbers, specific questions to ask",
                    },
                    "voice": {
                        "type": "string",
                        "description": "Voice to use: 'hal' (default) or 'heimir'",
                    },
                },
                "required": ["phone_number", "purpose"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "einherjar_dispatch",
            "description": (
                "Dispatch a task to the EINHERJAR specialist agent swarm. "
                "EINHERJAR agents are expert domain specialists — use them for complex "
                "legal (UK/US corporate law, contracts, governance), financial (audit, "
                "compliance), or engineering tasks requiring specialist knowledge. "
                "Each agent runs a 3-model council (Primary + Grounding + Critic) for "
                "rigorous, cross-verified answers. Returns the final synthesised response "
                "plus optional council breakdown. "
                "Available agents: odin (researcher), bragi (creative), mimir (data analyst), "
                "sigyn (executor), hermod (communicator), idunn (infra guardian), "
                "forseti (strategist), tyr (UK law), domar (US law), runa (UK counsel), "
                "eira (US counsel), var (UK audit), snotra (US audit)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task or question to dispatch. Be specific and complete.",
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "Optional: codename of the specific agent to use "
                            "(odin, bragi, mimir, sigyn, hermod, idunn, forseti, "
                            "tyr, domar, runa, eira, var, snotra). "
                            "Omit for automatic routing by keyword matching."
                        ),
                    },
                },
                "required": ["task"],
            },
        },
    },
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
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a tracked task for multi-step work. Tasks persist across sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Brief actionable title"},
                    "description": {"type": "string", "description": "Detailed description and acceptance criteria"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"], "description": "Task priority (default: medium)"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "Update a task's status or append notes. Use to track progress.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "Task ID to update"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "done", "cancelled"], "description": "New status"},
                    "notes": {"type": "string", "description": "Notes to append (progress, blockers, results)"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "List tracked tasks, optionally filtered by status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["pending", "in_progress", "done", "cancelled", "active", "all"], "description": "Filter (default: active = pending + in_progress)"},
                },
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


def _exec_bash(args: dict, *, timeout: int = 120, cwd: str | None = None) -> tuple[str, list[str]]:
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
        matches = sorted(base.glob(pattern))[:1000]
        if not matches:
            return f"No files matching '{pattern}' in {search_path}", []
        result = "\n".join(str(m) for m in matches)
        return _truncate(result), []
    except Exception as e:
        return f"Error in glob: {e}", []


def _exec_grep(args: dict, *, cwd: str | None = None, **_kwargs) -> tuple[str, list[str]]:
    """Search file contents with ripgrep. Returns (matching lines, [])."""
    pattern = args.get("pattern", "")
    if not pattern:
        return "Error: no pattern provided", []

    search_path = args.get("path") or cwd or os.getcwd()
    include = args.get("include")

    cmd = ["rg", "-n", "--no-ignore", "--no-messages"]
    if include:
        cmd.extend(["--glob", include])
    cmd.extend(["--", pattern, search_path])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
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
        return "Error: search timed out after 30s", []
    except Exception as e:
        return f"Error in grep: {e}", []


def _exec_web_search(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Search the web. Uses SearXNG if configured, DuckDuckGo otherwise."""
    query = args.get("query", "")
    if not query:
        return "Error: no query provided", []

    num_results = min(args.get("num_results") or 5, 10)

    # Prefer SearXNG (self-hosted, private, better results)
    searxng_url = os.environ.get("SEARXNG_URL", "")
    if searxng_url:
        return _search_searxng(query, num_results, searxng_url)
    return _search_ddg(query, num_results)


def _search_searxng(query: str, num_results: int, base_url: str) -> tuple[str, list[str]]:
    """Search via SearXNG JSON API."""
    log.info("Tool web_search (SearXNG): %s (n=%d)", query[:80], num_results)

    try:
        params = urllib.parse.urlencode({"q": query, "format": "json"})
        url = f"{base_url}?{params}" if "?" not in base_url else f"{base_url}&{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        log.warning("SearXNG failed, falling back to DuckDuckGo: %s", e)
        return _search_ddg(query, num_results)

    results = data.get("results", [])[:num_results]
    if not results:
        return f"No results found for: {query}", []

    output_lines = [f"Web search: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        snippet = r.get("content", "")
        url = r.get("url", "")
        engines = ", ".join(r.get("engines", []))
        output_lines.append(f"{i}. {title}")
        output_lines.append(f"   {url}")
        if snippet:
            output_lines.append(f"   {snippet}")
        if engines:
            output_lines.append(f"   [via {engines}]")
        output_lines.append("")

    return _truncate("\n".join(output_lines)), []


def _search_ddg(query: str, num_results: int) -> tuple[str, list[str]]:
    """Search via DuckDuckGo HTML (zero config fallback)."""
    log.info("Tool web_search (DuckDuckGo): %s (n=%d)", query[:80], num_results)

    try:
        url = "https://html.duckduckgo.com/html/"
        form_data = urllib.parse.urlencode({"q": query}).encode()
        req = urllib.request.Request(
            url,
            data=form_data,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"Error: web search failed: {e}", []

    results = _parse_ddg_html(raw_html, num_results)
    if not results:
        return f"No results found for: {query}", []

    output_lines = [f"Web search: {query}\n"]
    for i, r in enumerate(results, 1):
        output_lines.append(f"{i}. {r['title']}")
        output_lines.append(f"   {r['url']}")
        if r["snippet"]:
            output_lines.append(f"   {r['snippet']}")
        output_lines.append("")

    return _truncate("\n".join(output_lines)), []


def _parse_ddg_html(raw_html: str, max_results: int) -> list[dict]:
    """Extract search results from DuckDuckGo HTML lite response."""
    results = []

    result_blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]*)"[^>]*>(.*?)</a>'
        r'.*?'
        r'(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)?',
        raw_html,
        re.DOTALL,
    )

    for href, title_html, snippet_html in result_blocks:
        if len(results) >= max_results:
            break

        title = html_mod.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
        snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snippet_html)).strip() if snippet_html else ""

        if "uddg=" in href:
            match = re.search(r"uddg=([^&]+)", href)
            if match:
                href = urllib.parse.unquote(match.group(1))

        if title and href and not href.startswith("/"):
            results.append({"title": title, "url": href, "snippet": snippet})

    return results


def _html_to_text(html: str) -> str:
    """Convert HTML to readable plain text via regex stripping."""
    # Remove script, style, nav, footer blocks entirely
    text = re.sub(r"<(script|style|nav|footer)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Convert block elements to newlines
    text = re.sub(r"<(br|hr|/p|/div|/h[1-6]|/li|/tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Convert list items to bullet points
    text = re.sub(r"<li[^>]*>", "\n• ", text, flags=re.IGNORECASE)
    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    text = html_mod.unescape(text)
    # Collapse whitespace: multiple blank lines to two, trailing spaces
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _exec_web_fetch(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Fetch a URL and return content as text. HTML is stripped to plain text."""
    url = args.get("url", "").strip()
    if not url:
        return "Error: no url provided", []

    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://", []

    custom_headers = args.get("headers") or {}
    max_bytes = MAX_OUTPUT_CHARS * 2  # 64KB raw — room for HTML stripping

    log.info("Tool web_fetch: %s", url[:120])

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        }
        headers.update(custom_headers)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(max_bytes)
            content_type = resp.headers.get("Content-Type", "")

            # Detect charset
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()

            text = raw.decode(charset, errors="replace")

            # Strip HTML to plain text if content is HTML
            if "html" in content_type.lower():
                text = _html_to_text(text)

            return _truncate(text or "(empty response)"), []
    except urllib.error.HTTPError as e:
        return f"Error: HTTP {e.code} {e.reason} fetching {url}", []
    except urllib.error.URLError as e:
        return f"Error: could not reach {url}: {e.reason}", []
    except Exception as e:
        return f"Error fetching {url}: {e}", []


def _exec_einherjar_dispatch(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Dispatch a task to the EINHERJAR specialist agent swarm."""
    task = args.get("task", "").strip()
    if not task:
        return "Error: no task provided", []

    agent = args.get("agent")
    einherjar_url = os.environ.get("EINHERJAR_URL", "http://einherjar.einherjar.svc.cluster.local:8080")
    log.info("Tool einherjar_dispatch: agent=%s task=%s", agent or "auto", task[:80])

    payload = json.dumps({"task": task, "agent": agent})
    try:
        req = urllib.request.Request(
            f"{einherjar_url}/dispatch",
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        return f"Error: EINHERJAR service unreachable at {einherjar_url}: {e}", []
    except Exception as e:
        return f"Error dispatching to EINHERJAR: {e}", []

    agent_name = result.get("agent", "unknown")
    elapsed = result.get("elapsed_seconds", 0)
    errors = result.get("errors", [])
    final = result.get("final_response", "")

    lines = [f"[EINHERJAR/{agent_name.upper()}] ({elapsed:.1f}s)"]
    if errors:
        lines.append(f"Warnings: {'; '.join(errors)}")
    lines.append("")
    lines.append(final)

    return _truncate("\n".join(lines)), []


def _exec_make_phone_call(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Make an outbound phone call via HAL Phone service on fox-n0."""
    phone_number = args.get("phone_number", "").strip()
    purpose = args.get("purpose", "inquiry")
    context = args.get("context", "")
    voice = args.get("voice", "hal")

    if not phone_number:
        return "Error: no phone_number provided", []
    if not phone_number.startswith("+"):
        return "Error: phone_number must be in E.164 format (+44...)", []

    hal_phone_url = os.environ.get("HAL_PHONE_URL", "http://localhost:5590")
    log.info("Tool make_phone_call: %s (purpose=%s)", phone_number, purpose)

    # Initiate call with wait=true (blocks until call completes, up to 5 min)
    payload = json.dumps({
        "phone_number": phone_number,
        "purpose": purpose,
        "context": context,
        "voice": voice,
        "wait": True,
    })

    try:
        req = urllib.request.Request(
            f"{hal_phone_url}/call",
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=330) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        return f"Error: HAL Phone service unreachable: {e}", []
    except Exception as e:
        return f"Error making phone call: {e}", []

    # Format the result
    status = result.get("status", "unknown")
    call_id = result.get("call_id", "?")
    outcome = result.get("outcome", "unknown")
    duration = result.get("duration_secs", 0)
    transcript = result.get("transcript", [])

    output_lines = [
        f"Call {call_id}: {status}",
        f"Outcome: {outcome}",
        f"Duration: {duration}s",
        "",
        "Transcript:",
    ]
    if isinstance(transcript, list):
        for msg in transcript:
            role = msg.get("role", "?").upper()
            content = msg.get("content", "")
            output_lines.append(f"  [{role}] {content}")
    elif isinstance(transcript, str):
        output_lines.append(f"  {transcript}")
    else:
        output_lines.append("  (no transcript)")

    return _truncate("\n".join(output_lines)), []


def _exec_enter_plan_mode(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Switch to plan mode (read-only tools)."""
    reason = args.get("reason", "")
    set_plan_mode(True)
    return (
        f"Plan mode activated. Reason: {reason}\n\n"
        "READ-ONLY mode: read_file, glob, grep, web_search, web_fetch available.\n"
        "BLOCKED: bash, write_file, edit_file.\n\n"
        "Design your plan, then call exit_plan_mode to implement.",
        [],
    )


def _exec_exit_plan_mode(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Exit plan mode, restore full execution."""
    plan_summary = args.get("plan_summary", "")
    set_plan_mode(False)
    return (
        f"Execution mode restored. Plan: {plan_summary}\n\n"
        "All tools now available. Proceed with implementation.",
        [],
    )


# ---------------------------------------------------------------------------
# Subagent executor
# ---------------------------------------------------------------------------

_SUBAGENT_DEFAULT_TOOLS = ["read_file", "glob", "grep", "web_search", "web_fetch"]
_SUBAGENT_MAX_ITERATIONS = int(os.environ.get("SUBAGENT_MAX_ITER", "15"))
_SUBAGENT_TIMEOUT = int(os.environ.get("SUBAGENT_TIMEOUT", "180"))


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
    """Run a subagent conversation synchronously via Bedrock."""
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
        # Override backend tools with filtered set
        from backends.bedrock_api import _bedrock_tools as _orig_bedrock_tools
        filtered_bedrock_tools = []
        for t in filtered_schemas:
            fn = t.get("function", {})
            params = fn.get("parameters", {"type": "object", "properties": {}})
            filtered_bedrock_tools.append({
                "toolSpec": {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "inputSchema": {"json": params},
                }
            })

        # Temporarily swap tools on the backend
        original_tools = backend._tools
        original_enabled = backend._tools_enabled
        backend._tools = filtered_bedrock_tools if filtered_bedrock_tools else None
        backend._tools_enabled = bool(filtered_bedrock_tools)

        try:
            result = backend.call_sync(
                prompt=task,
                model=model or "sonnet",
                system_prompt=system_prompt,
                timeout=_SUBAGENT_TIMEOUT,
            )
            return result.get("result", "(no result)")
        finally:
            backend._tools = original_tools
            backend._tools_enabled = original_enabled
    finally:
        _tool_context.is_subagent = False


# ---------------------------------------------------------------------------
# Task management executors
# ---------------------------------------------------------------------------


def _exec_create_task(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Create a tracked task."""
    from db import db_create_task
    title = args.get("title", "").strip()
    if not title:
        return "Error: no title provided", []
    description = args.get("description", "")
    priority = args.get("priority", "medium")
    if priority not in ("low", "medium", "high", "critical"):
        priority = "medium"
    task_id = db_create_task(title, description, priority)
    return f"Task #{task_id} created: {title} [{priority}]", []


def _exec_update_task(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """Update a task's status or notes."""
    from db import db_update_task
    task_id = args.get("task_id")
    if task_id is None:
        return "Error: no task_id provided", []
    status = args.get("status")
    notes = args.get("notes")
    if status is None and notes is None:
        return "Error: provide 'status' or 'notes' to update", []
    ok, msg = db_update_task(int(task_id), status=status, notes=notes)
    if not ok:
        return f"Error: {msg}", []
    return msg, []


def _exec_list_tasks(args: dict, **_kwargs) -> tuple[str, list[str]]:
    """List tracked tasks."""
    from db import db_list_tasks
    status_filter = args.get("status", "active")
    tasks = db_list_tasks(status_filter)
    if not tasks:
        return f"No tasks found (filter: {status_filter})", []
    lines = [f"Tasks ({status_filter}):"]
    for t in tasks:
        icon = {"critical": "!!!", "high": "!!", "medium": "!", "low": "."}.get(t["priority"], "")
        lines.append(f"  #{t['id']} [{t['status']}] {icon} {t['title']}")
        if t.get("notes"):
            last = t["notes"].strip().split("\n")[-1]
            if len(last) > 80:
                last = last[:77] + "..."
            lines.append(f"      {last}")
    return "\n".join(lines), []


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
    "web_search": _exec_web_search,
    "web_fetch": _exec_web_fetch,
    "make_phone_call": _exec_make_phone_call,
    "einherjar_dispatch": _exec_einherjar_dispatch,
    "enter_plan_mode": _exec_enter_plan_mode,
    "exit_plan_mode": _exec_exit_plan_mode,
    "spawn_subagent": _exec_spawn_subagent,
    "create_task": _exec_create_task,
    "update_task": _exec_update_task,
    "list_tasks": _exec_list_tasks,
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
    # Plan mode enforcement
    if is_plan_mode() and name in _WRITE_TOOLS:
        return (
            f"Error: '{name}' is blocked in plan mode. "
            "Use read-only tools or call exit_plan_mode first.",
            [],
        )

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
    elif tool_name in ("WebSearch", "web_search"):
        return f"Searching web: {tool_input.get('query', '?')}"
    elif tool_name == "web_fetch":
        return f"Fetching: {tool_input.get('url', '?')}"
    elif tool_name == "make_phone_call":
        return f"Calling: {tool_input.get('phone_number', '?')}"
    elif tool_name == "einherjar_dispatch":
        agent = tool_input.get("agent") or "auto"
        task = tool_input.get("task", "?")[:60]
        return f"EINHERJAR [{agent}]: {task}"
    elif tool_name == "enter_plan_mode":
        return f"Planning: {tool_input.get('reason', '?')}"
    elif tool_name == "exit_plan_mode":
        return "Exiting plan mode"
    elif tool_name == "spawn_subagent":
        task = tool_input.get("task", "?")[:60]
        return f"Subagent: {task}"
    elif tool_name == "create_task":
        return f"Creating task: {tool_input.get('title', '?')}"
    elif tool_name == "update_task":
        return f"Updating task #{tool_input.get('task_id', '?')}"
    elif tool_name == "list_tasks":
        return f"Listing tasks ({tool_input.get('status', 'active')})"
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
    send_and_parse_stream=None,
) -> dict:
    """Generic async tool loop with tool status updates.

    Same interface as run_tool_loop_sync but awaits send_request and runs
    tool execution in a thread pool.  Sends status updates via
    streaming_editor or on_progress callbacks.

    If send_and_parse_stream is provided, it is called instead of the
    separate send_request + parse_response pair.  Signature:
        send_and_parse_stream(messages, streaming_editor) -> (text, tool_calls, assistant_msg)
    This enables streaming responses where send and parse are fused
    (e.g. Bedrock converse_stream).
    """
    written_files: list[str] = []
    last_text = ""
    start = time.time()

    for iteration in range(max_iterations):
        if time.time() - start > total_timeout:
            log.warning("Tool loop async: total timeout after %d iterations", iteration)
            break

        if send_and_parse_stream:
            text, tool_calls, assistant_msg = await send_and_parse_stream(
                messages, streaming_editor,
            )
        else:
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

        # Parallel execution when all calls are subagents
        if len(tool_calls) > 1 and all(tc.name == "spawn_subagent" for tc in tool_calls):
            # Show status for all subagents
            for tc in tool_calls:
                status = _format_tool_status(tc.name, tc.arguments)
                if streaming_editor:
                    await streaming_editor.add_tool_status(status)
                elif on_progress:
                    await on_progress(status)
                log.info("Tool call [async-parallel]: %s(%s)", tc.name, str(tc.arguments)[:100])

            # Run all subagents concurrently in thread pool
            loop = asyncio.get_event_loop()
            tasks = [
                loop.run_in_executor(
                    None,
                    lambda n=tc.name, a=tc.arguments: execute_tool(
                        n, a, timeout=_SUBAGENT_TIMEOUT, cwd=cwd,
                    ),
                )
                for tc in tool_calls
            ]
            results = await asyncio.gather(*tasks)
            for tc, (result_str, new_files) in zip(tool_calls, results):
                written_files.extend(new_files)
                messages.append(format_tool_result(tc.name, tc.id, result_str))
        else:
            # Sequential execution (standard behavior)
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
