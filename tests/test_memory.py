"""Tests for memory.py — markdown-based persistent memory layer.

Tests cover:
- save_memory: basic append, topic files, size cap warning
- update_memory: find-and-replace in MEMORY.md and topic files
- remove_memory: by text match and by index
- list_memories: bullet parsing
- list_topic_files: enumeration
- read_topic_file: content retrieval
- search_memories: cross-file search
- get_memories_for_injection: formatted output, line cap
- memory_count: bullet count
- add_memory: backward compat shim
- migration: JSON → markdown
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from memory import (
        save_memory,
        update_memory,
        remove_memory,
        list_memories,
        list_topic_files,
        read_topic_file,
        search_memories,
        get_memories_for_injection,
        memory_count,
        add_memory,
        _migrate_from_json,
        _bullet_lines,
        _sanitize_topic,
        MEMORY_MD,
        MEMORY_DIR,
        CONTEXT_MD,
        LESSONS_MD,
        MAX_MEMORY_LINES,
    )
    from config import AGENT_NAME


# ---------------------------------------------------------------------------
# Fixture — redirect MEMORY_DIR and MEMORY_MD to tmp_path for every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def use_tmp_memory(tmp_path, monkeypatch):
    mem_dir = tmp_path / "memory"
    mem_md = mem_dir / "MEMORY.md"
    ctx_md = mem_dir / "CONTEXT.md"
    les_md = mem_dir / "LESSONS.md"
    monkeypatch.setattr("memory.MEMORY_DIR", mem_dir)
    monkeypatch.setattr("memory.MEMORY_MD", mem_md)
    monkeypatch.setattr("memory.CONTEXT_MD", ctx_md)
    monkeypatch.setattr("memory.LESSONS_MD", les_md)
    yield mem_dir


# ---------------------------------------------------------------------------
# _sanitize_topic
# ---------------------------------------------------------------------------

class TestSanitizeTopic:

    def test_basic(self):
        assert _sanitize_topic("infrastructure") == "infrastructure.md"

    def test_uppercase(self):
        assert _sanitize_topic("LESSONS") == "lessons.md"

    def test_special_chars(self):
        assert _sanitize_topic("my topic!") == "mytopic.md"

    def test_already_has_extension(self):
        assert _sanitize_topic("lessons.md") == "lessonsmd.md"

    def test_empty_defaults_to_general(self):
        assert _sanitize_topic("") == "general.md"
        assert _sanitize_topic("!!!") == "general.md"


# ---------------------------------------------------------------------------
# save_memory
# ---------------------------------------------------------------------------

class TestSaveMemory:

    def test_basic_save(self, use_tmp_memory):
        save_memory("User prefers dark mode")
        mem_md = use_tmp_memory / "MEMORY.md"
        assert mem_md.exists()
        content = mem_md.read_text()
        assert "- User prefers dark mode" in content

    def test_creates_header(self, use_tmp_memory):
        save_memory("first entry")
        content = (use_tmp_memory / "MEMORY.md").read_text()
        assert content.startswith("# HAL Memory")

    def test_appends_to_existing(self, use_tmp_memory):
        save_memory("first")
        save_memory("second")
        content = (use_tmp_memory / "MEMORY.md").read_text()
        assert "- first" in content
        assert "- second" in content
        assert content.index("- first") < content.index("- second")

    def test_empty_text_returns_empty(self):
        result = save_memory("")
        assert result == ""

    def test_strips_whitespace(self, use_tmp_memory):
        save_memory("  padded text  ")
        content = (use_tmp_memory / "MEMORY.md").read_text()
        assert "- padded text" in content

    def test_returns_saved_text(self):
        result = save_memory("hello world")
        assert result == "hello world"


class TestSaveMemoryTopic:

    def test_creates_topic_file(self, use_tmp_memory):
        save_memory("arx1 has 3 HDDs", topic="infrastructure")
        topic_path = use_tmp_memory / "infrastructure.md"
        assert topic_path.exists()
        content = topic_path.read_text()
        assert "- arx1 has 3 HDDs" in content
        assert "# Infrastructure" in content

    def test_appends_to_existing_topic(self, use_tmp_memory):
        save_memory("fact one", topic="infra")
        save_memory("fact two", topic="infra")
        content = (use_tmp_memory / "infra.md").read_text()
        assert "- fact one" in content
        assert "- fact two" in content


# ---------------------------------------------------------------------------
# update_memory
# ---------------------------------------------------------------------------

class TestUpdateMemory:

    def test_basic_update(self, use_tmp_memory):
        save_memory("old fact")
        assert update_memory("old fact", "new fact") is True
        content = (use_tmp_memory / "MEMORY.md").read_text()
        assert "new fact" in content
        assert "old fact" not in content

    def test_update_in_topic(self, use_tmp_memory):
        save_memory("wrong info", topic="infra")
        assert update_memory("wrong info", "correct info", topic="infra") is True
        content = (use_tmp_memory / "infra.md").read_text()
        assert "correct info" in content

    def test_update_not_found(self):
        save_memory("existing")
        assert update_memory("nonexistent", "replacement") is False

    def test_update_empty_strings(self):
        assert update_memory("", "new") is False
        assert update_memory("old", "") is False


# ---------------------------------------------------------------------------
# remove_memory
# ---------------------------------------------------------------------------

class TestRemoveMemory:

    def test_remove_by_text(self, use_tmp_memory):
        save_memory("keep this")
        save_memory("remove this")
        assert remove_memory("remove this") is True
        content = (use_tmp_memory / "MEMORY.md").read_text()
        assert "keep this" in content
        assert "remove this" not in content

    def test_remove_by_index(self, use_tmp_memory):
        save_memory("first")
        save_memory("second")
        save_memory("third")
        assert remove_memory(2) is True  # removes "second"
        content = (use_tmp_memory / "MEMORY.md").read_text()
        assert "first" in content
        assert "second" not in content
        assert "third" in content

    def test_remove_missing_text(self):
        save_memory("exists")
        assert remove_memory("nonexistent") is False

    def test_remove_invalid_index(self):
        save_memory("only one")
        assert remove_memory(5) is False
        assert remove_memory(0) is False

    def test_remove_from_empty(self):
        assert remove_memory("anything") is False

    def test_remove_then_readd(self, use_tmp_memory):
        save_memory("ephemeral")
        remove_memory("ephemeral")
        save_memory("ephemeral")
        assert memory_count() == 1


# ---------------------------------------------------------------------------
# list_memories
# ---------------------------------------------------------------------------

class TestListMemories:

    def test_returns_all_bullets(self):
        save_memory("one")
        save_memory("two")
        save_memory("three")
        mems = list_memories()
        assert len(mems) == 3

    def test_dict_format(self):
        save_memory("test entry")
        mems = list_memories()
        assert "text" in mems[0]
        assert "line_num" in mems[0]
        assert mems[0]["text"] == "test entry"

    def test_empty(self):
        assert list_memories() == []

    def test_preserves_order(self):
        save_memory("alpha")
        save_memory("beta")
        save_memory("gamma")
        mems = list_memories()
        texts = [m["text"] for m in mems]
        assert texts == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# list_topic_files
# ---------------------------------------------------------------------------

class TestListTopicFiles:

    def test_empty(self):
        assert list_topic_files() == []

    def test_lists_topics(self, use_tmp_memory):
        save_memory("infra fact", topic="infrastructure")
        save_memory("lesson", topic="lessons")
        topics = list_topic_files()
        names = [t["name"] for t in topics]
        assert "infrastructure" in names
        assert "lessons" in names

    def test_excludes_system_files(self, use_tmp_memory):
        save_memory("main entry")  # creates MEMORY.md
        save_memory("topic entry", topic="test")
        # Create system files
        use_tmp_memory.mkdir(parents=True, exist_ok=True)
        (use_tmp_memory / "CONTEXT.md").write_text("# Context\n")
        (use_tmp_memory / "LESSONS.md").write_text("# Lessons\n")
        topics = list_topic_files()
        names = [t["name"] for t in topics]
        assert "MEMORY" not in names
        assert "CONTEXT" not in names
        assert "LESSONS" not in names
        assert "test" in names

    def test_has_size(self, use_tmp_memory):
        save_memory("some content", topic="sized")
        topics = list_topic_files()
        assert topics[0]["size"] > 0


# ---------------------------------------------------------------------------
# read_topic_file
# ---------------------------------------------------------------------------

class TestReadTopicFile:

    def test_read_existing(self, use_tmp_memory):
        save_memory("test content", topic="mytopic")
        content = read_topic_file("mytopic")
        assert "test content" in content

    def test_read_nonexistent(self):
        assert read_topic_file("nonexistent") == ""


# ---------------------------------------------------------------------------
# search_memories
# ---------------------------------------------------------------------------

class TestSearchMemories:

    def test_search_memory_md(self, use_tmp_memory):
        save_memory("tensor-core has 512GB RAM")
        save_memory("fox-n1 runs K3s")
        results = search_memories("tensor")
        assert len(results) == 1
        assert "512GB" in results[0]["text"]
        assert results[0]["source"] == "MEMORY.md"

    def test_search_topic_files(self, use_tmp_memory):
        save_memory("arx1 has HDDs", topic="infrastructure")
        results = search_memories("arx1")
        assert len(results) == 1
        assert results[0]["source"] == "infrastructure"

    def test_case_insensitive(self, use_tmp_memory):
        save_memory("UPPERCASE FACT")
        results = search_memories("uppercase")
        assert len(results) == 1

    def test_no_match(self, use_tmp_memory):
        save_memory("something")
        assert search_memories("nonexistent") == []

    def test_cross_file(self, use_tmp_memory):
        save_memory("node fact in main")
        save_memory("node fact in infra", topic="infrastructure")
        results = search_memories("node")
        assert len(results) == 2

    def test_empty_query(self):
        assert search_memories("") == []


# ---------------------------------------------------------------------------
# get_memories_for_injection
# ---------------------------------------------------------------------------

class TestGetMemoriesForInjection:

    def test_formatted_output(self, use_tmp_memory):
        save_memory("first fact")
        save_memory("second fact")
        output = get_memories_for_injection()
        assert f"[{AGENT_NAME} Memory]" in output
        assert "- first fact" in output
        assert "- second fact" in output

    def test_empty_returns_empty_string(self):
        assert get_memories_for_injection() == ""

    def test_includes_header(self, use_tmp_memory):
        save_memory("test")
        output = get_memories_for_injection()
        assert "# HAL Memory" in output

    def test_includes_context(self, use_tmp_memory):
        use_tmp_memory.mkdir(parents=True, exist_ok=True)
        (use_tmp_memory / "CONTEXT.md").write_text("# Context\n\nFleet topology here\n")
        output = get_memories_for_injection()
        assert f"[{AGENT_NAME} Context]" in output
        assert "Fleet topology here" in output

    def test_includes_lessons(self, use_tmp_memory):
        use_tmp_memory.mkdir(parents=True, exist_ok=True)
        (use_tmp_memory / "LESSONS.md").write_text("# Lessons\n\n- Never do X\n")
        output = get_memories_for_injection()
        assert f"[{AGENT_NAME} Lessons]" in output
        assert "Never do X" in output

    def test_all_three_sections(self, use_tmp_memory):
        use_tmp_memory.mkdir(parents=True, exist_ok=True)
        (use_tmp_memory / "CONTEXT.md").write_text("# Context\n\nFleet info\n")
        (use_tmp_memory / "LESSONS.md").write_text("# Lessons\n\n- Lesson 1\n")
        save_memory("runtime fact")
        output = get_memories_for_injection()
        assert f"[{AGENT_NAME} Context]" in output
        assert f"[{AGENT_NAME} Lessons]" in output
        assert f"[{AGENT_NAME} Memory]" in output
        # Verify ordering: Context before Lessons before Memory
        ctx_pos = output.index(f"[{AGENT_NAME} Context]")
        les_pos = output.index(f"[{AGENT_NAME} Lessons]")
        mem_pos = output.index(f"[{AGENT_NAME} Memory]")
        assert ctx_pos < les_pos < mem_pos


# ---------------------------------------------------------------------------
# memory_count
# ---------------------------------------------------------------------------

class TestMemoryCount:

    def test_empty(self):
        assert memory_count() == 0

    def test_after_adds(self):
        save_memory("one")
        save_memory("two")
        save_memory("three")
        assert memory_count() == 3

    def test_after_remove(self):
        save_memory("to remove")
        save_memory("to keep")
        remove_memory("to remove")
        assert memory_count() == 1


# ---------------------------------------------------------------------------
# Line cap warning
# ---------------------------------------------------------------------------

class TestLineCap:

    def test_warning_on_overflow(self, use_tmp_memory, caplog):
        import logging
        # Create a MEMORY.md with > 200 lines
        lines = ["# HAL Memory", ""]
        for i in range(MAX_MEMORY_LINES):
            lines.append(f"- entry {i}")
        (use_tmp_memory / "MEMORY.md").parent.mkdir(parents=True, exist_ok=True)
        (use_tmp_memory / "MEMORY.md").write_text("\n".join(lines) + "\n")

        with caplog.at_level(logging.WARNING, logger="nexus"):
            save_memory("one more entry")

        assert any("lines" in r.message and str(MAX_MEMORY_LINES) in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompat:

    def test_add_memory_shim(self, use_tmp_memory):
        result = add_memory("shim test", "general")
        assert result == "shim test"
        assert memory_count() == 1

    def test_add_memory_with_category_creates_topic(self, use_tmp_memory):
        add_memory("infra fact", "infrastructure")
        topics = list_topic_files()
        names = [t["name"] for t in topics]
        assert "infrastructure" in names

    def test_add_memory_general_goes_to_main(self, use_tmp_memory):
        add_memory("general fact", "general")
        content = (use_tmp_memory / "MEMORY.md").read_text()
        assert "general fact" in content


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

class TestMigration:

    def test_json_to_markdown(self, tmp_path, monkeypatch):
        mem_dir = tmp_path / "new_memory"
        mem_md = mem_dir / "MEMORY.md"
        legacy = tmp_path / "old_memory.json"

        monkeypatch.setattr("memory.MEMORY_DIR", mem_dir)
        monkeypatch.setattr("memory.MEMORY_MD", mem_md)
        monkeypatch.setattr("memory._LEGACY_JSON_PATH", legacy)

        # Create legacy JSON
        data = {
            "version": 1,
            "memories": {
                "key1": {"text": "first fact", "category": "general"},
                "key2": {"text": "second fact", "category": "infrastructure"},
            },
        }
        legacy.write_text(json.dumps(data), encoding="utf-8")

        _migrate_from_json()

        assert mem_md.exists()
        content = mem_md.read_text()
        assert "first fact" in content
        assert "second fact" in content
        assert not legacy.exists()
        assert (tmp_path / "old_memory.json.migrated").exists()

    def test_skip_if_already_migrated(self, tmp_path, monkeypatch):
        mem_dir = tmp_path / "memory"
        mem_md = mem_dir / "MEMORY.md"
        legacy = tmp_path / "old.json"

        monkeypatch.setattr("memory.MEMORY_DIR", mem_dir)
        monkeypatch.setattr("memory.MEMORY_MD", mem_md)
        monkeypatch.setattr("memory._LEGACY_JSON_PATH", legacy)

        mem_dir.mkdir(parents=True)
        mem_md.write_text("# HAL Memory\n\n- existing\n")
        legacy.write_text('{"version":1,"memories":{"k":{"text":"old"}}}')

        _migrate_from_json()

        # MEMORY.md unchanged, legacy not touched
        assert "existing" in mem_md.read_text()
        assert legacy.exists()

    def test_no_legacy_no_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr("memory._LEGACY_JSON_PATH", tmp_path / "nonexistent.json")
        _migrate_from_json()  # should not raise


# ---------------------------------------------------------------------------
# _bullet_lines
# ---------------------------------------------------------------------------

class TestBulletLines:

    def test_extracts_bullets(self):
        content = "# Header\n\n- first\n- second\nNot a bullet\n- third\n"
        bullets = _bullet_lines(content)
        assert len(bullets) == 3
        assert bullets[0] == "- first"

    def test_empty(self):
        assert _bullet_lines("") == []
