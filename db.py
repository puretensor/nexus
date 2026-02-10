"""Unified SQLite database for NEXUS — sessions, scheduled tasks, drafts, observer state, email tracking."""

import asyncio
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH, AUTHORIZED_USER_ID, log


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect():
    return sqlite3.connect(DB_PATH)


# ---------------------------------------------------------------------------
# Schema — init_db
# ---------------------------------------------------------------------------

def init_db():
    con = _connect()
    # Check if old schema (chat_id PRIMARY KEY, no 'name' column)
    cols = [row[1] for row in con.execute("PRAGMA table_info(sessions)").fetchall()]

    if not cols:
        # Fresh DB — create v2 schema directly
        con.execute(
            """CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT 'default',
                session_id TEXT,
                model TEXT DEFAULT 'sonnet',
                message_count INTEGER DEFAULT 0,
                created_at TEXT,
                last_used TEXT,
                summary TEXT,
                archived_at TEXT,
                UNIQUE(chat_id, name)
            )"""
        )
    elif "name" not in cols:
        # Migrate from v1 -> v2
        log.info("Migrating sessions DB from v1 to v2 (named sessions)")
        con.execute("ALTER TABLE sessions ADD COLUMN name TEXT NOT NULL DEFAULT 'default'")
        con.execute("ALTER TABLE sessions ADD COLUMN last_used TEXT")
        con.execute("ALTER TABLE sessions ADD COLUMN summary TEXT")
        con.execute("ALTER TABLE sessions ADD COLUMN archived_at TEXT")
        # v1 had chat_id as PRIMARY KEY; v2 uses id + UNIQUE(chat_id, name).
        # SQLite can't drop PK or add AUTOINCREMENT to existing table, so
        # rebuild the table.
        con.execute(
            """CREATE TABLE sessions_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT 'default',
                session_id TEXT,
                model TEXT DEFAULT 'sonnet',
                message_count INTEGER DEFAULT 0,
                created_at TEXT,
                last_used TEXT,
                summary TEXT,
                archived_at TEXT,
                UNIQUE(chat_id, name)
            )"""
        )
        con.execute(
            """INSERT INTO sessions_v2 (chat_id, name, session_id, model, message_count, created_at)
               SELECT chat_id, 'default', session_id, model, message_count, created_at
               FROM sessions"""
        )
        con.execute("DROP TABLE sessions")
        con.execute("ALTER TABLE sessions_v2 RENAME TO sessions")
        log.info("Migration complete")

    # Scheduled tasks table
    task_cols = [row[1] for row in con.execute("PRAGMA table_info(scheduled_tasks)").fetchall()]
    if not task_cols:
        con.execute(
            """CREATE TABLE scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                trigger_time TEXT NOT NULL,
                prompt TEXT NOT NULL,
                recurrence TEXT,
                created_at TEXT,
                last_run TEXT,
                task_type TEXT NOT NULL DEFAULT 'schedule'
            )"""
        )
        log.info("Created scheduled_tasks table")
    elif "task_type" not in task_cols:
        con.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'schedule'"
        )
        log.info("Added task_type column to scheduled_tasks")

    # Drafts table (email reply drafts pending user approval)
    draft_cols = [row[1] for row in con.execute("PRAGMA table_info(drafts)").fetchall()]
    if not draft_cols:
        con.execute(
            """CREATE TABLE drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                email_from TEXT,
                email_subject TEXT,
                email_message_id TEXT,
                draft_body TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT,
                updated_at TEXT
            )"""
        )
        log.info("Created drafts table")

    # Observer state table (persistent state for observers between runs)
    obs_cols = [row[1] for row in con.execute("PRAGMA table_info(observer_state)").fetchall()]
    if not obs_cols:
        con.execute(
            """CREATE TABLE observer_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observer_name TEXT NOT NULL UNIQUE,
                last_run TEXT,
                state_json TEXT
            )"""
        )
        log.info("Created observer_state table")

    # Followups table (track emails awaiting responses)
    fu_cols = [row[1] for row in con.execute("PRAGMA table_info(followups)").fetchall()]
    if not fu_cols:
        con.execute(
            """CREATE TABLE followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                email_to TEXT NOT NULL,
                email_subject TEXT,
                email_message_id TEXT,
                sent_at TEXT,
                reminder_days INTEGER DEFAULT 3,
                last_reminded TEXT,
                resolved_at TEXT,
                created_at TEXT
            )"""
        )
        log.info("Created followups table")

    # Email seen table (dedup for email digest observer)
    email_cols = [row[1] for row in con.execute("PRAGMA table_info(email_seen)").fetchall()]
    if not email_cols:
        con.execute(
            """CREATE TABLE email_seen (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                account_name TEXT,
                seen_at TEXT
            )"""
        )
        log.info("Created email_seen table")

    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Active session helpers (backward-compatible with v1 callers)
# ---------------------------------------------------------------------------

def get_session(chat_id: int) -> dict | None:
    """Get the active (non-archived) session for a chat. Returns the most
    recently used session if multiple exist."""
    con = _connect()
    row = con.execute(
        """SELECT session_id, model, message_count, created_at, name, summary, last_used
           FROM sessions
           WHERE chat_id = ? AND archived_at IS NULL
           ORDER BY last_used DESC NULLS LAST, id DESC
           LIMIT 1""",
        (chat_id,),
    ).fetchone()
    con.close()
    if row is None:
        return None
    return {
        "session_id": row[0],
        "model": row[1],
        "message_count": row[2],
        "created_at": row[3],
        "name": row[4],
        "summary": row[5],
        "last_used": row[6],
    }


def upsert_session(chat_id: int, session_id: str, model: str, message_count: int):
    """Update the active session for a chat. Creates 'default' if none exists."""
    now = _now()
    con = _connect()
    # Find the active session
    row = con.execute(
        """SELECT id, name FROM sessions
           WHERE chat_id = ? AND archived_at IS NULL
           ORDER BY last_used DESC NULLS LAST, id DESC
           LIMIT 1""",
        (chat_id,),
    ).fetchone()

    if row:
        con.execute(
            """UPDATE sessions SET session_id = ?, model = ?, message_count = ?, last_used = ?
               WHERE id = ?""",
            (session_id, model, message_count, now, row[0]),
        )
    else:
        con.execute(
            """INSERT INTO sessions (chat_id, name, session_id, model, message_count, created_at, last_used)
               VALUES (?, 'default', ?, ?, ?, ?, ?)""",
            (chat_id, session_id, model, message_count, now, now),
        )
    con.commit()
    con.close()


def update_model(chat_id: int, model: str):
    """Update the model for the active session. Creates 'default' if none exists."""
    now = _now()
    con = _connect()
    row = con.execute(
        """SELECT id FROM sessions
           WHERE chat_id = ? AND archived_at IS NULL
           ORDER BY last_used DESC NULLS LAST, id DESC
           LIMIT 1""",
        (chat_id,),
    ).fetchone()

    if row:
        con.execute("UPDATE sessions SET model = ?, last_used = ? WHERE id = ?", (model, now, row[0]))
    else:
        con.execute(
            """INSERT INTO sessions (chat_id, name, session_id, model, message_count, created_at, last_used)
               VALUES (?, 'default', NULL, ?, 0, ?, ?)""",
            (chat_id, model, now, now),
        )
    con.commit()
    con.close()


def delete_session(chat_id: int):
    """Delete the active session for a chat (hard delete, backward compat)."""
    con = _connect()
    row = con.execute(
        """SELECT id FROM sessions
           WHERE chat_id = ? AND archived_at IS NULL
           ORDER BY last_used DESC NULLS LAST, id DESC
           LIMIT 1""",
        (chat_id,),
    ).fetchone()
    if row:
        con.execute("DELETE FROM sessions WHERE id = ?", (row[0],))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Named sessions
# ---------------------------------------------------------------------------

def get_session_by_name(chat_id: int, name: str) -> dict | None:
    """Get a specific named session (active or archived)."""
    con = _connect()
    row = con.execute(
        """SELECT id, session_id, model, message_count, created_at, name, summary, last_used, archived_at
           FROM sessions WHERE chat_id = ? AND name = ?""",
        (chat_id, name),
    ).fetchone()
    con.close()
    if row is None:
        return None
    return {
        "id": row[0],
        "session_id": row[1],
        "model": row[2],
        "message_count": row[3],
        "created_at": row[4],
        "name": row[5],
        "summary": row[6],
        "last_used": row[7],
        "archived_at": row[8],
    }


def list_sessions(chat_id: int) -> list[dict]:
    """List all active (non-archived) sessions for a chat."""
    con = _connect()
    rows = con.execute(
        """SELECT id, session_id, model, message_count, created_at, name, summary, last_used
           FROM sessions
           WHERE chat_id = ? AND archived_at IS NULL
           ORDER BY last_used DESC NULLS LAST""",
        (chat_id,),
    ).fetchall()
    con.close()
    return [
        {
            "id": r[0],
            "session_id": r[1],
            "model": r[2],
            "message_count": r[3],
            "created_at": r[4],
            "name": r[5],
            "summary": r[6],
            "last_used": r[7],
        }
        for r in rows
    ]


def switch_session(chat_id: int, name: str, model: str = "sonnet") -> dict:
    """Switch to a named session. Creates it if it doesn't exist.
    Returns the session dict."""
    now = _now()
    con = _connect()
    row = con.execute(
        """SELECT id, session_id, model, message_count, created_at, summary, last_used, archived_at
           FROM sessions WHERE chat_id = ? AND name = ?""",
        (chat_id, name),
    ).fetchone()

    if row:
        sid = row[0]
        # If it was archived, unarchive it
        if row[7] is not None:
            con.execute("UPDATE sessions SET archived_at = NULL, last_used = ? WHERE id = ?", (now, sid))
        else:
            con.execute("UPDATE sessions SET last_used = ? WHERE id = ?", (now, sid))
        con.commit()
        result = {
            "id": sid,
            "session_id": row[1],
            "model": row[2],
            "message_count": row[3],
            "created_at": row[4],
            "name": name,
            "summary": row[5],
            "last_used": now,
        }
    else:
        con.execute(
            """INSERT INTO sessions (chat_id, name, session_id, model, message_count, created_at, last_used)
               VALUES (?, ?, NULL, ?, 0, ?, ?)""",
            (chat_id, name, model, now, now),
        )
        result = {
            "id": con.execute("SELECT last_insert_rowid()").fetchone()[0],
            "session_id": None,
            "model": model,
            "message_count": 0,
            "created_at": now,
            "name": name,
            "summary": None,
            "last_used": now,
        }

    con.commit()
    con.close()
    return result


def delete_session_by_name(chat_id: int, name: str) -> bool:
    """Delete a named session. Returns True if a session was deleted."""
    con = _connect()
    cursor = con.execute(
        "DELETE FROM sessions WHERE chat_id = ? AND name = ?",
        (chat_id, name),
    )
    con.commit()
    con.close()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Session history & archive
# ---------------------------------------------------------------------------

def archive_session(chat_id: int, name: str | None = None) -> bool:
    """Archive a session (soft delete). If name is None, archives the active session.
    Returns True if a session was archived."""
    now = _now()
    con = _connect()
    if name:
        cursor = con.execute(
            "UPDATE sessions SET archived_at = ? WHERE chat_id = ? AND name = ? AND archived_at IS NULL",
            (now, chat_id, name),
        )
    else:
        # Archive the most recently used active session
        row = con.execute(
            """SELECT id FROM sessions
               WHERE chat_id = ? AND archived_at IS NULL
               ORDER BY last_used DESC NULLS LAST, id DESC
               LIMIT 1""",
            (chat_id,),
        ).fetchone()
        if row:
            cursor = con.execute("UPDATE sessions SET archived_at = ? WHERE id = ?", (now, row[0]))
        else:
            con.close()
            return False
    con.commit()
    result = cursor.rowcount > 0
    con.close()
    return result


def list_archived(chat_id: int, limit: int = 10) -> list[dict]:
    """List archived sessions, most recently archived first."""
    con = _connect()
    rows = con.execute(
        """SELECT id, session_id, model, message_count, created_at, name, summary, last_used, archived_at
           FROM sessions
           WHERE chat_id = ? AND archived_at IS NOT NULL
           ORDER BY archived_at DESC
           LIMIT ?""",
        (chat_id, limit),
    ).fetchall()
    con.close()
    return [
        {
            "id": r[0],
            "session_id": r[1],
            "model": r[2],
            "message_count": r[3],
            "created_at": r[4],
            "name": r[5],
            "summary": r[6],
            "last_used": r[7],
            "archived_at": r[8],
        }
        for r in rows
    ]


def restore_session(chat_id: int, session_db_id: int) -> dict | None:
    """Unarchive a session by its database ID. Returns the session dict or None."""
    now = _now()
    con = _connect()
    row = con.execute(
        """SELECT id, session_id, model, message_count, created_at, name, summary
           FROM sessions WHERE id = ? AND chat_id = ? AND archived_at IS NOT NULL""",
        (session_db_id, chat_id),
    ).fetchone()
    if row is None:
        con.close()
        return None
    con.execute("UPDATE sessions SET archived_at = NULL, last_used = ? WHERE id = ?", (now, row[0]))
    con.commit()
    con.close()
    return {
        "id": row[0],
        "session_id": row[1],
        "model": row[2],
        "message_count": row[3],
        "created_at": row[4],
        "name": row[5],
        "summary": row[6],
        "last_used": now,
    }


# ---------------------------------------------------------------------------
# Session summaries
# ---------------------------------------------------------------------------

def update_summary(chat_id: int, summary: str, name: str | None = None):
    """Update the summary for a session. If name is None, updates the active session."""
    con = _connect()
    if name:
        con.execute(
            "UPDATE sessions SET summary = ? WHERE chat_id = ? AND name = ? AND archived_at IS NULL",
            (summary, chat_id, name),
        )
    else:
        row = con.execute(
            """SELECT id FROM sessions
               WHERE chat_id = ? AND archived_at IS NULL
               ORDER BY last_used DESC NULLS LAST, id DESC
               LIMIT 1""",
            (chat_id,),
        ).fetchone()
        if row:
            con.execute("UPDATE sessions SET summary = ? WHERE id = ?", (summary, row[0]))
    con.commit()
    con.close()


def get_summary(chat_id: int, name: str | None = None) -> str | None:
    """Get the summary for a session."""
    con = _connect()
    if name:
        row = con.execute(
            "SELECT summary FROM sessions WHERE chat_id = ? AND name = ? AND archived_at IS NULL",
            (chat_id, name),
        ).fetchone()
    else:
        row = con.execute(
            """SELECT summary FROM sessions
               WHERE chat_id = ? AND archived_at IS NULL
               ORDER BY last_used DESC NULLS LAST, id DESC
               LIMIT 1""",
            (chat_id,),
        ).fetchone()
    con.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------

def create_scheduled_task(chat_id: int, trigger_time: str, prompt: str,
                          recurrence: str | None = None,
                          task_type: str = "schedule") -> int:
    """Create a scheduled task. Returns the task ID.

    task_type: "schedule" (run Claude) or "remind" (send message directly).
    """
    now = _now()
    con = _connect()
    con.execute(
        """INSERT INTO scheduled_tasks
           (chat_id, trigger_time, prompt, recurrence, created_at, task_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (chat_id, trigger_time, prompt, recurrence, now, task_type),
    )
    task_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit()
    con.close()
    return task_id


def list_scheduled_tasks(chat_id: int) -> list[dict]:
    """List all scheduled tasks for a chat."""
    con = _connect()
    rows = con.execute(
        """SELECT id, trigger_time, prompt, recurrence, created_at, last_run, task_type
           FROM scheduled_tasks
           WHERE chat_id = ?
           ORDER BY trigger_time ASC""",
        (chat_id,),
    ).fetchall()
    con.close()
    return [
        {
            "id": r[0],
            "trigger_time": r[1],
            "prompt": r[2],
            "recurrence": r[3],
            "created_at": r[4],
            "last_run": r[5],
            "task_type": r[6],
        }
        for r in rows
    ]


def delete_scheduled_task(chat_id: int, task_id: int) -> bool:
    """Delete a scheduled task. Returns True if deleted."""
    con = _connect()
    cursor = con.execute(
        "DELETE FROM scheduled_tasks WHERE id = ? AND chat_id = ?",
        (task_id, chat_id),
    )
    con.commit()
    con.close()
    return cursor.rowcount > 0


def get_due_tasks() -> list[dict]:
    """Get all tasks where trigger_time <= now and (last_run is NULL or stale for recurring)."""
    now = _now()
    con = _connect()
    rows = con.execute(
        """SELECT id, chat_id, trigger_time, prompt, recurrence, last_run, task_type
           FROM scheduled_tasks
           WHERE trigger_time <= ?
             AND (last_run IS NULL OR recurrence IS NOT NULL)
           ORDER BY trigger_time ASC""",
        (now,),
    ).fetchall()
    con.close()
    return [
        {
            "id": r[0],
            "chat_id": r[1],
            "trigger_time": r[2],
            "prompt": r[3],
            "recurrence": r[4],
            "last_run": r[5],
            "task_type": r[6],
        }
        for r in rows
    ]


def mark_task_run(task_id: int):
    """Mark a task as having been run now."""
    now = _now()
    con = _connect()
    con.execute("UPDATE scheduled_tasks SET last_run = ? WHERE id = ?", (now, task_id))
    con.commit()
    con.close()


def advance_recurring_task(task_id: int, next_trigger: str):
    """Update a recurring task's trigger_time to the next occurrence."""
    con = _connect()
    con.execute(
        "UPDATE scheduled_tasks SET trigger_time = ? WHERE id = ?",
        (next_trigger, task_id),
    )
    con.commit()
    con.close()


def delete_task_by_id(task_id: int):
    """Delete a task by ID (no chat_id check — for internal use after one-shot tasks)."""
    con = _connect()
    con.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Per-chat locks (one claude invocation at a time per chat)
# ---------------------------------------------------------------------------

_chat_locks: dict[int, asyncio.Lock] = {}


def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------

def authorized(func):
    from telegram import Update
    from telegram.ext import ContextTypes

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != AUTHORIZED_USER_ID:
            log.warning("Unauthorized access attempt from user %s", update.effective_user.id)
            msg = update.message or (update.callback_query.message if update.callback_query else None)
            if msg:
                await msg.reply_text("Unauthorized.")
            return
        return await func(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Drafts (email reply drafts pending user approval)
# ---------------------------------------------------------------------------

def create_draft(chat_id: int, email_from: str, email_subject: str,
                 email_message_id: str, draft_body: str) -> int:
    """Create a draft email reply pending approval. Returns the draft ID."""
    now = _now()
    con = _connect()
    con.execute(
        """INSERT INTO drafts (chat_id, email_from, email_subject, email_message_id,
                               draft_body, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (chat_id, email_from, email_subject, email_message_id, draft_body, now, now),
    )
    draft_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit()
    con.close()
    return draft_id


def get_draft(draft_id: int) -> dict | None:
    """Get a draft by ID."""
    con = _connect()
    row = con.execute(
        """SELECT id, chat_id, email_from, email_subject, email_message_id,
                  draft_body, status, created_at, updated_at
           FROM drafts WHERE id = ?""",
        (draft_id,),
    ).fetchone()
    con.close()
    if row is None:
        return None
    return {
        "id": row[0],
        "chat_id": row[1],
        "email_from": row[2],
        "email_subject": row[3],
        "email_message_id": row[4],
        "draft_body": row[5],
        "status": row[6],
        "created_at": row[7],
        "updated_at": row[8],
    }


def list_drafts(chat_id: int, status: str = "pending") -> list[dict]:
    """List drafts for a chat, filtered by status."""
    con = _connect()
    rows = con.execute(
        """SELECT id, chat_id, email_from, email_subject, email_message_id,
                  draft_body, status, created_at, updated_at
           FROM drafts WHERE chat_id = ? AND status = ?
           ORDER BY created_at DESC""",
        (chat_id, status),
    ).fetchall()
    con.close()
    return [
        {
            "id": r[0],
            "chat_id": r[1],
            "email_from": r[2],
            "email_subject": r[3],
            "email_message_id": r[4],
            "draft_body": r[5],
            "status": r[6],
            "created_at": r[7],
            "updated_at": r[8],
        }
        for r in rows
    ]


def update_draft_status(draft_id: int, status: str) -> bool:
    """Update the status of a draft. Returns True if updated."""
    now = _now()
    con = _connect()
    cursor = con.execute(
        "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, draft_id),
    )
    con.commit()
    con.close()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Observer state (persistent state between observer runs)
# ---------------------------------------------------------------------------

def get_observer_state(observer_name: str) -> dict | None:
    """Get the persisted state for an observer."""
    con = _connect()
    row = con.execute(
        "SELECT observer_name, last_run, state_json FROM observer_state WHERE observer_name = ?",
        (observer_name,),
    ).fetchone()
    con.close()
    if row is None:
        return None
    return {
        "observer_name": row[0],
        "last_run": row[1],
        "state_json": row[2],
    }


def set_observer_state(observer_name: str, state_json: str) -> None:
    """Upsert the persisted state for an observer."""
    now = _now()
    con = _connect()
    con.execute(
        """INSERT INTO observer_state (observer_name, last_run, state_json)
           VALUES (?, ?, ?)
           ON CONFLICT(observer_name) DO UPDATE SET last_run = ?, state_json = ?""",
        (observer_name, now, state_json, now, state_json),
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Followups (track emails awaiting responses)
# ---------------------------------------------------------------------------

def create_followup(chat_id: int, email_to: str, email_subject: str,
                    email_message_id: str, reminder_days: int = 3) -> int:
    """Create a follow-up tracker for a sent email. Returns followup ID."""
    now = _now()
    con = _connect()
    con.execute(
        """INSERT INTO followups (chat_id, email_to, email_subject, email_message_id,
                                  sent_at, reminder_days, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (chat_id, email_to, email_subject, email_message_id, now, reminder_days, now),
    )
    fid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit()
    con.close()
    return fid


def list_active_followups(chat_id: int | None = None) -> list[dict]:
    """List all unresolved followups. If chat_id is None, returns all."""
    con = _connect()
    if chat_id is not None:
        rows = con.execute(
            """SELECT id, chat_id, email_to, email_subject, email_message_id,
                      sent_at, reminder_days, last_reminded, created_at
               FROM followups WHERE chat_id = ? AND resolved_at IS NULL
               ORDER BY sent_at ASC""",
            (chat_id,),
        ).fetchall()
    else:
        rows = con.execute(
            """SELECT id, chat_id, email_to, email_subject, email_message_id,
                      sent_at, reminder_days, last_reminded, created_at
               FROM followups WHERE resolved_at IS NULL
               ORDER BY sent_at ASC""",
        ).fetchall()
    con.close()
    return [
        {
            "id": r[0], "chat_id": r[1], "email_to": r[2],
            "email_subject": r[3], "email_message_id": r[4],
            "sent_at": r[5], "reminder_days": r[6],
            "last_reminded": r[7], "created_at": r[8],
        }
        for r in rows
    ]


def resolve_followup(followup_id: int) -> bool:
    """Mark a followup as resolved. Returns True if updated."""
    now = _now()
    con = _connect()
    cursor = con.execute(
        "UPDATE followups SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL",
        (now, followup_id),
    )
    con.commit()
    con.close()
    return cursor.rowcount > 0


def update_followup_reminded(followup_id: int) -> None:
    """Update last_reminded timestamp for a followup."""
    now = _now()
    con = _connect()
    con.execute(
        "UPDATE followups SET last_reminded = ? WHERE id = ?",
        (now, followup_id),
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Email seen (dedup for email digest observer)
# ---------------------------------------------------------------------------

def mark_email_seen(message_id: str, account_name: str) -> None:
    """Mark an email message ID as seen."""
    now = _now()
    con = _connect()
    con.execute(
        """INSERT OR IGNORE INTO email_seen (message_id, account_name, seen_at)
           VALUES (?, ?, ?)""",
        (message_id, account_name, now),
    )
    con.commit()
    con.close()


def is_email_seen(message_id: str) -> bool:
    """Check if an email message ID has been seen."""
    con = _connect()
    row = con.execute(
        "SELECT 1 FROM email_seen WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    con.close()
    return row is not None
