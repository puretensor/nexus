"""Tests for backends.tools — tool schemas, executors, and dispatch."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from backends.tools import (
        TOOL_SCHEMAS,
        execute_tool,
        _exec_bash,
        _exec_read_file,
        _exec_write_file,
        _exec_edit_file,
        _exec_glob,
        _exec_grep,
        _truncate,
        MAX_OUTPUT_CHARS,
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestToolSchemas:

    def test_seven_tools_defined(self):
        assert len(TOOL_SCHEMAS) == 7

    def test_all_have_function_format(self):
        for schema in TOOL_SCHEMAS:
            assert schema["type"] == "function"
            assert "function" in schema
            assert "name" in schema["function"]
            assert "description" in schema["function"]
            assert "parameters" in schema["function"]

    def test_tool_names(self):
        names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        assert names == {"bash", "read_file", "write_file", "edit_file", "glob", "grep", "web_search"}

    def test_required_params(self):
        """Each tool has required params matching the spec."""
        expected = {
            "bash": ["command"],
            "read_file": ["file_path"],
            "write_file": ["file_path", "content"],
            "edit_file": ["file_path", "old_string", "new_string"],
            "glob": ["pattern"],
            "grep": ["pattern"],
            "web_search": ["query"],
        }
        for schema in TOOL_SCHEMAS:
            name = schema["function"]["name"]
            required = schema["function"]["parameters"].get("required", [])
            assert set(required) == set(expected[name]), f"{name} required mismatch"


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

class TestTruncation:

    def test_short_text_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_long_text_truncated(self):
        text = "x" * (MAX_OUTPUT_CHARS + 100)
        result = _truncate(text)
        assert len(result) < len(text)
        assert "truncated" in result
        assert str(len(text)) in result


# ---------------------------------------------------------------------------
# Bash executor
# ---------------------------------------------------------------------------

class TestBash:

    def test_simple_command(self):
        result, files = _exec_bash({"command": "echo hello"})
        assert "hello" in result
        assert files == []

    def test_empty_command(self):
        result, files = _exec_bash({"command": ""})
        assert "error" in result.lower()

    def test_no_command(self):
        result, files = _exec_bash({})
        assert "error" in result.lower()

    def test_stderr_captured(self):
        result, files = _exec_bash({"command": "echo err >&2"})
        assert "err" in result

    def test_exit_code_shown(self):
        result, files = _exec_bash({"command": "exit 42"})
        assert "42" in result

    def test_timeout(self):
        result, files = _exec_bash({"command": "sleep 10"}, timeout=1)
        assert "timed out" in result.lower()

    def test_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = _exec_bash({"command": "pwd"}, cwd=tmpdir)
            assert tmpdir in result


# ---------------------------------------------------------------------------
# Read file executor
# ---------------------------------------------------------------------------

class TestReadFile:

    def test_read_existing_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("line one\nline two\nline three\n")
            f.flush()
            path = f.name
        try:
            result, files = _exec_read_file({"file_path": path})
            assert "line one" in result
            assert "line two" in result
            assert "line three" in result
            assert files == []
            # Check line numbers
            assert "1\t" in result
        finally:
            os.unlink(path)

    def test_read_nonexistent_file(self):
        result, files = _exec_read_file({"file_path": "/nonexistent/path.txt"})
        assert "not found" in result.lower()

    def test_read_with_offset(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("a\nb\nc\nd\ne\n")
            path = f.name
        try:
            result, _ = _exec_read_file({"file_path": path, "offset": 3})
            assert "c" in result
            assert "a" not in result.split("\t")[0]  # Line 'a' shouldn't be there
        finally:
            os.unlink(path)

    def test_read_with_limit(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("a\nb\nc\nd\ne\n")
            path = f.name
        try:
            result, _ = _exec_read_file({"file_path": path, "limit": 2})
            lines = [l for l in result.strip().split("\n") if l.strip()]
            assert len(lines) == 2
        finally:
            os.unlink(path)

    def test_read_empty_path(self):
        result, _ = _exec_read_file({"file_path": ""})
        assert "error" in result.lower()

    def test_read_no_path(self):
        result, _ = _exec_read_file({})
        assert "error" in result.lower()


# ---------------------------------------------------------------------------
# Write file executor
# ---------------------------------------------------------------------------

class TestWriteFile:

    def test_write_new_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.txt")
            result, files = _exec_write_file({"file_path": path, "content": "hello\nworld\n"})
            assert "Wrote" in result
            assert path in files
            assert os.path.exists(path)
            with open(path) as f:
                assert f.read() == "hello\nworld\n"

    def test_write_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "a", "b", "test.txt")
            result, files = _exec_write_file({"file_path": path, "content": "deep"})
            assert "Wrote" in result
            assert os.path.exists(path)

    def test_write_overwrites(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("old content")
            path = f.name
        try:
            result, files = _exec_write_file({"file_path": path, "content": "new content"})
            with open(path) as f:
                assert f.read() == "new content"
        finally:
            os.unlink(path)

    def test_write_empty_path(self):
        result, files = _exec_write_file({"file_path": "", "content": "x"})
        assert "error" in result.lower()
        assert files == []


# ---------------------------------------------------------------------------
# Edit file executor
# ---------------------------------------------------------------------------

class TestEditFile:

    def test_edit_replaces_string(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world\nfoo bar\n")
            path = f.name
        try:
            result, files = _exec_edit_file({
                "file_path": path,
                "old_string": "foo bar",
                "new_string": "baz qux",
            })
            assert "Edited" in result
            assert path in files
            with open(path) as f:
                content = f.read()
            assert "baz qux" in content
            assert "foo bar" not in content
        finally:
            os.unlink(path)

    def test_edit_string_not_found(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world\n")
            path = f.name
        try:
            result, files = _exec_edit_file({
                "file_path": path,
                "old_string": "nonexistent",
                "new_string": "replacement",
            })
            assert "not found" in result.lower()
            assert files == []
        finally:
            os.unlink(path)

    def test_edit_multiple_occurrences_rejected(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("foo foo foo\n")
            path = f.name
        try:
            result, files = _exec_edit_file({
                "file_path": path,
                "old_string": "foo",
                "new_string": "bar",
            })
            assert "3 times" in result
            assert files == []
        finally:
            os.unlink(path)

    def test_edit_nonexistent_file(self):
        result, files = _exec_edit_file({
            "file_path": "/nonexistent/path.txt",
            "old_string": "x",
            "new_string": "y",
        })
        assert "not found" in result.lower()

    def test_edit_empty_old_string(self):
        result, _ = _exec_edit_file({
            "file_path": "/tmp/test.txt",
            "old_string": "",
            "new_string": "y",
        })
        assert "error" in result.lower()


# ---------------------------------------------------------------------------
# Glob executor
# ---------------------------------------------------------------------------

class TestGlob:

    def test_find_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some files
            Path(tmpdir, "a.py").touch()
            Path(tmpdir, "b.py").touch()
            Path(tmpdir, "c.txt").touch()

            result, files = _exec_glob({"pattern": "*.py", "path": tmpdir})
            assert "a.py" in result
            assert "b.py" in result
            assert "c.txt" not in result
            assert files == []

    def test_no_matches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = _exec_glob({"pattern": "*.xyz", "path": tmpdir})
            assert "No files" in result

    def test_empty_pattern(self):
        result, _ = _exec_glob({"pattern": ""})
        assert "error" in result.lower()

    def test_recursive_glob(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sub = Path(tmpdir, "sub")
            sub.mkdir()
            Path(sub, "deep.py").touch()

            result, _ = _exec_glob({"pattern": "**/*.py", "path": tmpdir})
            assert "deep.py" in result


# ---------------------------------------------------------------------------
# Grep executor
# ---------------------------------------------------------------------------

class TestGrep:

    def test_find_pattern(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "test.txt").write_text("hello world\nfoo bar\n")
            result, files = _exec_grep({"pattern": "hello", "path": tmpdir})
            assert "hello" in result
            assert files == []

    def test_no_matches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "test.txt").write_text("hello world\n")
            result, _ = _exec_grep({"pattern": "nonexistent", "path": tmpdir})
            assert "No matches" in result

    def test_include_filter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "a.py").write_text("target\n")
            Path(tmpdir, "b.txt").write_text("target\n")
            result, _ = _exec_grep({"pattern": "target", "path": tmpdir, "include": "*.py"})
            assert "a.py" in result
            # b.txt should be excluded
            assert "b.txt" not in result

    def test_empty_pattern(self):
        result, _ = _exec_grep({"pattern": ""})
        assert "error" in result.lower()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class TestExecuteTool:

    def test_known_tool(self):
        result, files = execute_tool("bash", {"command": "echo dispatch_test"})
        assert "dispatch_test" in result

    def test_unknown_tool(self):
        result, files = execute_tool("unknown_tool", {"arg": "val"})
        assert "unknown tool" in result.lower()
        assert files == []

    def test_cwd_passed_through(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = execute_tool("bash", {"command": "pwd"}, cwd=tmpdir)
            assert tmpdir in result

    def test_timeout_passed_through(self):
        result, _ = execute_tool("bash", {"command": "sleep 10"}, timeout=1)
        assert "timed out" in result.lower()
