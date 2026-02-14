"""Tests for memory.py — persistent memory layer (Phase 5).

Tests cover:
- _slugify: spaces, special chars, truncation, unicode
- add_memory: basic add, duplicate update, categories
- remove_memory: existing and missing keys
- list_memories: all and by category, sort order
- search_memories: substring match on key and text
- get_memories_for_injection: formatted output, limit, empty
- memory_count: correct count
- _load: missing file, corrupt JSON
- _save: atomic write
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from memory import (
        add_memory,
        remove_memory,
        list_memories,
        search_memories,
        get_memories_for_injection,
        memory_count,
        _slugify,
        _load,
        _save,
        MEMORY_PATH,
    )


# ---------------------------------------------------------------------------
# Fixture — redirect MEMORY_PATH to tmp_path for every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def use_tmp_memory(tmp_path, monkeypatch):
    test_path = tmp_path / "memory.json"
    monkeypatch.setattr("memory.MEMORY_PATH", test_path)
    yield test_path


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------

class TestSlugify:

    def test_spaces(self):
        assert _slugify("hello world") == "hello-world"

    def test_special_chars(self):
        assert _slugify("user's #1 preference!") == "user-s-1-preference"

    def test_multiple_spaces_and_special(self):
        assert _slugify("  lots   of   spaces  ") == "lots-of-spaces"

    def test_truncation(self):
        long_text = "a" * 100
        result = _slugify(long_text)
        assert len(result) <= 60

    def test_unicode(self):
        result = _slugify("tensor-core runs Proxmox")
        assert result == "tensor-core-runs-proxmox"

    def test_unicode_non_ascii(self):
        # Non-ASCII chars should be replaced with hyphens
        result = _slugify("cafe\u0301 latte\u00e9")
        assert "-" in result or result.isalnum()
        assert len(result) <= 60

    def test_empty_string(self):
        assert _slugify("") == ""

    def test_only_special_chars(self):
        assert _slugify("!@#$%^&*()") == ""

    def test_leading_trailing_hyphens_stripped(self):
        assert _slugify("-hello-") == "hello"

    def test_collapse_multiple_hyphens(self):
        assert _slugify("foo---bar") == "foo-bar"


# ---------------------------------------------------------------------------
# add_memory
# ---------------------------------------------------------------------------

class TestAddMemory:

    def test_basic_add(self):
        key = add_memory("User prefers concise responses")
        assert key == "user-prefers-concise-responses"
        assert memory_count() == 1

    def test_returns_key(self):
        key = add_memory("tensor-core runs Proxmox", "infrastructure")
        assert isinstance(key, str)
        assert len(key) > 0

    def test_add_memory_duplicate_updates(self):
        key1 = add_memory("User prefers concise responses", "preferences")
        key2 = add_memory("User prefers concise responses", "general")
        assert key1 == key2
        assert memory_count() == 1
        # Category should be updated
        memories = list_memories()
        assert memories[0]["category"] == "general"

    def test_add_memory_valid_categories(self):
        for cat in ("preferences", "infrastructure", "people", "projects", "general"):
            key = add_memory(f"test {cat}", cat)
            memories = search_memories(f"test {cat}")
            assert memories[0]["category"] == cat

    def test_add_memory_invalid_category_defaults_to_general(self):
        key = add_memory("some fact", "invalid_category")
        memories = list_memories()
        assert memories[0]["category"] == "general"

    def test_add_memory_timestamps(self):
        key = add_memory("timestamped memory")
        memories = list_memories()
        mem = memories[0]
        assert "created_at" in mem
        assert "updated_at" in mem
        assert mem["created_at"] == mem["updated_at"]


# ---------------------------------------------------------------------------
# remove_memory
# ---------------------------------------------------------------------------

class TestRemoveMemory:

    def test_remove_existing(self):
        key = add_memory("to be removed")
        assert remove_memory(key) is True
        assert memory_count() == 0

    def test_remove_missing(self):
        assert remove_memory("nonexistent-key") is False

    def test_remove_then_readd(self):
        key = add_memory("ephemeral fact")
        remove_memory(key)
        key2 = add_memory("ephemeral fact")
        assert key == key2
        assert memory_count() == 1


# ---------------------------------------------------------------------------
# list_memories
# ---------------------------------------------------------------------------

class TestListMemories:

    def test_returns_all(self):
        add_memory("first")
        add_memory("second")
        add_memory("third")
        assert len(list_memories()) == 3

    def test_sorted_by_updated_at_desc(self):
        add_memory("old memory")
        # Force a different timestamp by updating
        add_memory("new memory")
        memories = list_memories()
        assert memories[0]["updated_at"] >= memories[-1]["updated_at"]

    def test_list_memories_by_category(self):
        add_memory("server config", "infrastructure")
        add_memory("user pref", "preferences")
        add_memory("project note", "projects")

        infra = list_memories("infrastructure")
        assert len(infra) == 1
        assert infra[0]["category"] == "infrastructure"

        prefs = list_memories("preferences")
        assert len(prefs) == 1
        assert prefs[0]["category"] == "preferences"

    def test_list_empty_category(self):
        add_memory("only general")
        assert list_memories("people") == []

    def test_list_no_memories(self):
        assert list_memories() == []


# ---------------------------------------------------------------------------
# search_memories
# ---------------------------------------------------------------------------

class TestSearchMemories:

    def test_search_by_text(self):
        add_memory("tensor-core runs Proxmox with 4 nodes", "infrastructure")
        add_memory("User prefers concise responses", "preferences")

        results = search_memories("proxmox")
        assert len(results) == 1
        assert "Proxmox" in results[0]["text"]

    def test_search_by_key(self):
        add_memory("User prefers concise responses")
        results = search_memories("concise")
        assert len(results) == 1

    def test_search_case_insensitive(self):
        add_memory("UPPERCASE MEMORY")
        results = search_memories("uppercase")
        assert len(results) == 1

    def test_search_no_match(self):
        add_memory("something else")
        results = search_memories("nonexistent")
        assert len(results) == 0

    def test_search_multiple_matches(self):
        add_memory("node health check", "infrastructure")
        add_memory("node exporter config", "infrastructure")
        add_memory("unrelated fact", "general")

        results = search_memories("node")
        assert len(results) == 2


# ---------------------------------------------------------------------------
# get_memories_for_injection
# ---------------------------------------------------------------------------

class TestGetMemoriesForInjection:

    def test_formatted_output(self):
        add_memory("User prefers concise responses", "preferences")
        add_memory("tensor-core runs Proxmox with 4 nodes", "infrastructure")

        output = get_memories_for_injection()
        assert output.startswith("[PureClaw Memory]")
        assert "- preferences: User prefers concise responses" in output
        assert "- infrastructure: tensor-core runs Proxmox with 4 nodes" in output

    def test_limit_respected(self):
        for i in range(20):
            add_memory(f"memory number {i}")

        output = get_memories_for_injection(limit=5)
        # Header + 5 memory lines
        lines = output.strip().split("\n")
        assert len(lines) == 6  # 1 header + 5 memories

    def test_empty_returns_empty_string(self):
        output = get_memories_for_injection()
        assert output == ""

    def test_default_limit(self):
        for i in range(15):
            add_memory(f"item {i}")

        output = get_memories_for_injection()
        lines = output.strip().split("\n")
        assert len(lines) == 11  # 1 header + 10 (default limit)


# ---------------------------------------------------------------------------
# memory_count
# ---------------------------------------------------------------------------

class TestMemoryCount:

    def test_empty(self):
        assert memory_count() == 0

    def test_after_adds(self):
        add_memory("one")
        add_memory("two")
        add_memory("three")
        assert memory_count() == 3

    def test_after_remove(self):
        key = add_memory("to remove")
        add_memory("to keep")
        remove_memory(key)
        assert memory_count() == 1


# ---------------------------------------------------------------------------
# _load edge cases
# ---------------------------------------------------------------------------

class TestLoadEdgeCases:

    def test_missing_file(self, use_tmp_memory):
        """_load returns empty structure when file doesn't exist."""
        assert not use_tmp_memory.exists()
        data = _load()
        assert data == {"version": 1, "memories": {}}

    def test_corrupt_json(self, use_tmp_memory):
        """_load returns empty structure on corrupt JSON (no crash)."""
        use_tmp_memory.parent.mkdir(parents=True, exist_ok=True)
        use_tmp_memory.write_text("{bad json content!!!}", encoding="utf-8")
        data = _load()
        assert data == {"version": 1, "memories": {}}

    def test_wrong_structure(self, use_tmp_memory):
        """_load returns empty structure if JSON lacks 'memories' key."""
        use_tmp_memory.parent.mkdir(parents=True, exist_ok=True)
        use_tmp_memory.write_text('{"version": 1}', encoding="utf-8")
        data = _load()
        assert data == {"version": 1, "memories": {}}

    def test_empty_file(self, use_tmp_memory):
        """_load handles empty file gracefully."""
        use_tmp_memory.parent.mkdir(parents=True, exist_ok=True)
        use_tmp_memory.write_text("", encoding="utf-8")
        data = _load()
        assert data == {"version": 1, "memories": {}}


# ---------------------------------------------------------------------------
# _save / atomic write
# ---------------------------------------------------------------------------

class TestAtomicWrite:

    def test_file_exists_after_save(self, use_tmp_memory):
        data = {"version": 1, "memories": {"test": {"key": "test", "text": "hello"}}}
        _save(data)
        assert use_tmp_memory.exists()

    def test_content_is_valid_json(self, use_tmp_memory):
        data = {"version": 1, "memories": {"test": {"key": "test", "text": "hello"}}}
        _save(data)
        loaded = json.loads(use_tmp_memory.read_text(encoding="utf-8"))
        assert loaded == data

    def test_tmp_file_cleaned_up(self, use_tmp_memory):
        """After atomic save, the .tmp file should not remain."""
        data = {"version": 1, "memories": {}}
        _save(data)
        tmp_file = use_tmp_memory.with_suffix(".tmp")
        assert not tmp_file.exists()

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        """_save creates parent directories if they don't exist."""
        deep_path = tmp_path / "a" / "b" / "c" / "memory.json"
        monkeypatch.setattr("memory.MEMORY_PATH", deep_path)
        data = {"version": 1, "memories": {}}
        _save(data)
        assert deep_path.exists()
