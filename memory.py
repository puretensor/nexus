"""Persistent memory layer for PureClaw — file-based JSON store at ~/.hal/memory.json."""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from config import log, AGENT_NAME

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MEMORY_PATH = Path(os.environ.get("MEMORY_PATH", os.path.expanduser("~/.hal/memory.json")))

VALID_CATEGORIES = {"preferences", "infrastructure", "people", "projects", "general"}

_EMPTY_STORE = {"version": 1, "memories": {}}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load() -> dict:
    """Read and parse the JSON memory file.

    Returns the empty structure if the file doesn't exist or contains
    corrupt JSON (logs a warning in the latter case).
    """
    if not MEMORY_PATH.exists():
        return json.loads(json.dumps(_EMPTY_STORE))  # deep copy
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "memories" not in data:
            log.warning("memory.json has unexpected structure — resetting")
            return json.loads(json.dumps(_EMPTY_STORE))
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("Corrupt memory.json (%s) — returning empty store", exc)
        return json.loads(json.dumps(_EMPTY_STORE))


def _save(data: dict) -> None:
    """Write the JSON file atomically (write to .tmp then rename)."""
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = MEMORY_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(MEMORY_PATH)


def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug key.

    Lowercase, replace spaces/special chars with hyphens, strip leading/
    trailing hyphens, collapse multiple hyphens.  Truncate to 60 chars.
    """
    slug = text.lower()
    # Replace any non-alphanumeric character (except hyphens) with a hyphen
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)
    # Collapse multiple hyphens
    slug = re.sub(r"-{2,}", "-", slug)
    # Strip leading/trailing hyphens
    slug = slug.strip("-")
    # Truncate to 60 chars (don't break mid-hyphen)
    if len(slug) > 60:
        slug = slug[:60].rstrip("-")
    return slug


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_memory(text: str, category: str = "general") -> str:
    """Add a memory (or update if the same slug already exists).

    Returns the key.  Invalid categories silently default to "general".
    """
    if category not in VALID_CATEGORIES:
        category = "general"

    key = _slugify(text)
    if not key:
        key = "unnamed"

    data = _load()
    now = _now()

    if key in data["memories"]:
        data["memories"][key]["text"] = text
        data["memories"][key]["category"] = category
        data["memories"][key]["updated_at"] = now
    else:
        data["memories"][key] = {
            "key": key,
            "text": text,
            "category": category,
            "created_at": now,
            "updated_at": now,
        }

    _save(data)
    return key


def remove_memory(key: str) -> bool:
    """Remove a memory by key.  Returns True if it existed."""
    data = _load()
    if key in data["memories"]:
        del data["memories"][key]
        _save(data)
        return True
    return False


def list_memories(category: str | None = None) -> list[dict]:
    """List all memories, optionally filtered by category.

    Returns a list sorted by updated_at descending.
    """
    data = _load()
    memories = list(data["memories"].values())

    if category is not None:
        memories = [m for m in memories if m.get("category") == category]

    memories.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    return memories


def search_memories(query: str) -> list[dict]:
    """Case-insensitive substring search across key and text fields."""
    data = _load()
    q = query.lower()
    results = []
    for mem in data["memories"].values():
        if q in mem.get("key", "").lower() or q in mem.get("text", "").lower():
            results.append(mem)
    results.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    return results


def get_memories_for_injection(limit: int = 10) -> str:
    """Return a formatted string of the most recent N memories for prompt injection.

    Format:
        [PureClaw Memory]
        - category: text
        - category: text
        ...

    Returns empty string if no memories.
    """
    memories = list_memories()[:limit]
    if not memories:
        return ""

    lines = [f"[{AGENT_NAME} Memory]"]
    for mem in memories:
        lines.append(f"- {mem['category']}: {mem['text']}")
    return "\n".join(lines)


def memory_count() -> int:
    """Return total number of stored memories."""
    data = _load()
    return len(data["memories"])
