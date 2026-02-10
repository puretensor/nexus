"""Telegram command handlers — all /slash commands and the main message handler.

Ported from ~/claude-telegram/claude_telegram_bot.py.
"""

import asyncio
import io
import logging
from datetime import datetime

import aiohttp

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from dispatcher import (
    refresh_dispatch,
    extract_weather_location,
    extract_stations,
    handle_weather,
    handle_crypto,
    handle_trains,
    handle_gold,
    handle_status,
    handle_markets,
    handle_forex,
    handle_world,
)
from dispatcher.apis import DispatchError
from dispatcher.apis.infra import (
    NODES, ALLOWED_SERVICES,
    check_nodes, check_sites, restart_service,
    get_logs, get_disk, get_top, trigger_deploy,
)

from db import (
    get_session,
    upsert_session,
    update_model,
    delete_session,
    get_lock,
    authorized,
    list_sessions,
    switch_session,
    delete_session_by_name,
    archive_session,
    list_archived,
    restore_session,
    create_scheduled_task,
    list_scheduled_tasks,
    delete_scheduled_task,
)
from engine import call_streaming, split_message
from channels.telegram.streaming import StreamingEditor
from handlers.file_output import scan_and_send_outputs
from handlers.summaries import maybe_generate_summary
from handlers.keyboards import get_contextual_keyboard
from handlers.voice_tts import (
    is_voice_mode, set_voice_mode,
    get_voice_system_prompt_addition,
    text_to_voice_note,
)
from scheduler import parse_schedule_args
from config import TIMEOUT, WHISPER_URL, log

try:
    from memory import add_memory, remove_memory, list_memories, search_memories, memory_count
except ImportError:
    add_memory = remove_memory = list_memories = search_memories = memory_count = None


# ---------------------------------------------------------------------------
# Shared utility functions (used by handlers too)
# ---------------------------------------------------------------------------


def _build_reply_context(message) -> str:
    """If the user is replying to a previous message, extract that text as context."""
    reply = message.reply_to_message
    if not reply:
        return ""
    quoted = reply.text or reply.caption or ""
    if not quoted.strip():
        return ""
    # Truncate to 500 chars to avoid prompt bloat
    if len(quoted) > 500:
        quoted = quoted[:497] + "..."
    return f'[Replying to: "{quoted}"]\n\n'


async def _keep_typing(chat):
    """Continuously send typing action every 5 seconds."""
    try:
        while True:
            await asyncio.sleep(5)
            await chat.send_action(ChatAction.TYPING)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------


@authorized
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    model = session["model"] if session else "sonnet"
    name = context.args[0] if context.args else "default"
    current_name = session["name"] if session else None
    if current_name and current_name != name:
        # Different name: archive current session, then create/switch to new name
        archive_session(chat_id)
        switch_session(chat_id, name, model)
    else:
        # Same name (or no current session): hard-delete and recreate,
        # because UNIQUE(chat_id, name) prevents archiving + creating
        # a new row with the same name.
        delete_session(chat_id)
        update_model(chat_id, model)
    if name == "default":
        await update.message.reply_text("Session cleared. Next message starts a fresh conversation.")
    else:
        await update.message.reply_text(f"Session archived. Created new session: {name}")


@authorized
async def cmd_opus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    update_model(chat_id, "opus")
    await update.message.reply_text("Switched to Opus 4.6.")


@authorized
async def cmd_sonnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    update_model(chat_id, "sonnet")
    await update.message.reply_text("Switched to Sonnet.")


@authorized
async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /voice [on|off] — toggle voice response mode."""
    chat_id = update.effective_chat.id
    args = context.args or []

    if args:
        if args[0].lower() == "on":
            set_voice_mode(chat_id, True)
            await update.message.reply_text("Voice mode ON. Responses will include voice notes.")
            return
        elif args[0].lower() == "off":
            set_voice_mode(chat_id, False)
            await update.message.reply_text("Voice mode OFF.")
            return

    # Toggle
    current = is_voice_mode(chat_id)
    set_voice_mode(chat_id, not current)
    status = "ON" if not current else "OFF"
    await update.message.reply_text(f"Voice mode {status}.")


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if session is None or session["session_id"] is None:
        await update.message.reply_text("No active session. Send a message to start one.")
        return
    name = session.get("name", "default")
    summary = session.get("summary")
    msg = (
        f"Session: {session['session_id'][:12]}... (name: {name})\n"
        f"Model: {session['model']}\n"
        f"Messages: {session['message_count']}\n"
    )
    if summary:
        msg += f"Summary: {summary}\n"
    msg += f"Started: {session['created_at']}"
    await update.message.reply_text(msg)


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Session*\n"
        "/new \\[name] — Archive current & start fresh\n"
        "/session — List active sessions\n"
        "/session <name> — Switch to/create session\n"
        "/session delete <name> — Delete a session\n"
        "/history — List archived sessions\n"
        "/resume <n> — Restore archived session by number\n"
        "/opus — Switch to Opus 4.6\n"
        "/sonnet — Switch to Sonnet (default)\n"
        "/voice \\[on|off] — Toggle voice responses\n"
        "/status — Show current session info\n\n"
        "*Scheduled Tasks & Reminders*\n"
        "/remind <when> <message> — Set a reminder\n"
        "/schedule <when> <prompt> — Run Claude at a time\n"
        "  _5pm, tomorrow 9am, monday, 9 feb, daily 8am_\n"
        "/cancel <n> — Cancel a task or reminder\n\n"
        "*Memory*\n"
        "/remember <fact> — Store a persistent memory\n"
        "/forget <key|n> — Remove a memory\n"
        "/memories \\[category|query] — List or search memories\n\n"
        "*Data Cards*\n"
        "/weather \\[location] — Weather card\n"
        "/crypto — Crypto prices\n"
        "/markets \\[us|uk] — Stock market indices\n"
        "/gold — Gold & silver prices\n"
        "/forex — Forex rates\n"
        "/trains \\[from] \\[to] — Train departures\n"
        "/nodes — Infrastructure status\n\n"
        "*Quick Actions*\n"
        "/check \\[nodes|sites] — Health check\n"
        "/restart <service> \\[node] — Restart service\n"
        "/logs <service> \\[node] \\[n] — Tail logs\n"
        "/disk \\[node] — Disk usage\n"
        "/top \\[node] — System overview\n"
        "/deploy <site> — Trigger deploy\n\n"
        "/help — This message\n\n"
        "Send a location to get context-aware responses\n\n"
        "Any other text is sent to Claude.",
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized
async def cmd_weather_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /weather [location]."""
    location = " ".join(context.args) if context.args else None
    try:
        await handle_weather(location, update.effective_chat, context.bot)
    except DispatchError as e:
        await update.message.reply_text(f"\u26a0\ufe0f {e}")
    except Exception:
        log.exception("Weather command error")
        await update.message.reply_text("Failed to fetch weather data.")


@authorized
async def cmd_crypto_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /crypto."""
    try:
        await handle_crypto(update.effective_chat, context.bot)
    except DispatchError as e:
        await update.message.reply_text(f"\u26a0\ufe0f {e}")
    except Exception:
        log.exception("Crypto command error")
        await update.message.reply_text("Failed to fetch crypto data.")


@authorized
async def cmd_trains_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /trains [from] [to]."""
    args = context.args or []
    if len(args) >= 2:
        text = f"{args[0]} to {args[1]}"
        from_crs, to_crs = extract_stations(text)
    elif len(args) == 1:
        from dispatcher.apis.trains import resolve_station, DEFAULT_TO
        from_crs = resolve_station(args[0]) or args[0].upper()
        to_crs = DEFAULT_TO
    else:
        from dispatcher.apis.trains import DEFAULT_FROM, DEFAULT_TO
        from_crs, to_crs = DEFAULT_FROM, DEFAULT_TO
    try:
        await handle_trains(from_crs, to_crs, update.effective_chat, context.bot)
    except DispatchError as e:
        await update.message.reply_text(f"\u26a0\ufe0f {e}")
    except Exception:
        log.exception("Trains command error")
        await update.message.reply_text("Failed to fetch train data.")


@authorized
async def cmd_gold_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /gold."""
    try:
        await handle_gold(update.effective_chat, context.bot)
    except DispatchError as e:
        await update.message.reply_text(f"\u26a0\ufe0f {e}")
    except Exception:
        log.exception("Gold command error")
        await update.message.reply_text("Failed to fetch gold data.")


@authorized
async def cmd_markets_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /markets [us|uk|world]."""
    args = context.args or []
    region = args[0].lower() if args else "world"
    if region not in ("us", "uk", "world"):
        region = "world"
    try:
        if region == "world":
            await handle_world(update.effective_chat, context.bot)
        else:
            await handle_markets(region, update.effective_chat, context.bot)
    except DispatchError as e:
        await update.message.reply_text(f"\u26a0\ufe0f {e}")
    except Exception:
        log.exception("Markets command error")
        await update.message.reply_text("Failed to fetch market data.")


@authorized
async def cmd_forex_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /forex."""
    try:
        await handle_forex(update.effective_chat, context.bot)
    except DispatchError as e:
        await update.message.reply_text(f"\u26a0\ufe0f {e}")
    except Exception:
        log.exception("Forex command error")
        await update.message.reply_text("Failed to fetch forex data.")


@authorized
async def cmd_nodes_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /nodes — infrastructure status."""
    try:
        await handle_status(update.effective_chat, context.bot)
    except DispatchError as e:
        await update.message.reply_text(f"\u26a0\ufe0f {e}")
    except Exception:
        log.exception("Nodes command error")
        await update.message.reply_text("Failed to fetch node status.")


@authorized
async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /session — list, switch, or delete sessions."""
    chat_id = update.effective_chat.id
    args = context.args or []

    # /session delete <name>
    if len(args) >= 2 and args[0] == "delete":
        name = args[1]
        # Don't allow deleting the current active session
        current = get_session(chat_id)
        if current and current.get("name") == name:
            await update.message.reply_text(f"Cannot delete the current active session: {name}")
            return
        deleted = delete_session_by_name(chat_id, name)
        if deleted:
            await update.message.reply_text(f"Deleted session: {name}")
        else:
            await update.message.reply_text(f"Session not found: {name}")
        return

    # /session <name> — switch to or create
    if len(args) == 1:
        name = args[0]
        current = get_session(chat_id)
        model = current["model"] if current else "sonnet"
        session = switch_session(chat_id, name, model)
        if session.get("message_count", 0) > 0 or session.get("session_id"):
            await update.message.reply_text(f"Switched to session: {name}")
        else:
            await update.message.reply_text(f"Created new session: {name}")
        return

    # /session (no args) — list all active sessions
    sessions = list_sessions(chat_id)
    if not sessions:
        await update.message.reply_text("No active sessions. Send a message to start one.")
        return

    current = get_session(chat_id)
    current_name = current["name"] if current else None

    lines = ["Sessions:"]
    for s in sessions:
        arrow = "\u2192 " if s["name"] == current_name else "  "
        model_label = "Opus" if s["model"] == "opus" else "Sonnet"
        msg_count = s["message_count"] or 0
        summary_part = ""
        if s.get("summary"):
            summary_part = f' \u2014 "{s["summary"]}"'
        elif msg_count == 0:
            summary_part = " (no messages)"
        lines.append(f"{arrow}{s['name']} ({model_label}, {msg_count} msgs){summary_part}")

    await update.message.reply_text("\n".join(lines))


@authorized
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history — list archived sessions."""
    chat_id = update.effective_chat.id
    archived = list_archived(chat_id)
    if not archived:
        await update.message.reply_text("No archived sessions.")
        return

    lines = ["Archived sessions:"]
    for i, s in enumerate(archived, 1):
        msg_count = s["message_count"] or 0
        # Format the archived_at date
        archived_at = s.get("archived_at", "")
        date_str = ""
        if archived_at:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(archived_at)
                date_str = f", archived {dt.strftime('%b %d')}"
            except (ValueError, TypeError):
                date_str = ""
        summary_part = ""
        if s.get("summary"):
            summary_part = f' \u2014 "{s["summary"]}"'
        lines.append(f"{i}. {s['name']} ({msg_count} msgs{date_str}){summary_part}")

    await update.message.reply_text("\n".join(lines))


@authorized
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resume <n> — restore archived session by number."""
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        await update.message.reply_text("Usage: /resume <number> (from /history list)")
        return

    try:
        n = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid number. Use /history to see archived sessions.")
        return

    archived = list_archived(chat_id)
    if n < 1 or n > len(archived):
        await update.message.reply_text(f"Invalid number. Use /history to see archived sessions (1-{len(archived)}).")
        return

    session = archived[n - 1]
    restored = restore_session(chat_id, session["id"])
    if restored:
        await update.message.reply_text(f"Restored session: {restored['name']}")
    else:
        await update.message.reply_text("Failed to restore session.")


@authorized
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /schedule — list tasks or create a new scheduled task."""
    chat_id = update.effective_chat.id
    args = context.args or []

    # No args: list all tasks and reminders
    if not args:
        tasks = list_scheduled_tasks(chat_id)
        if not tasks:
            await update.message.reply_text(
                "No scheduled tasks or reminders.\n\n"
                "/schedule 5pm <prompt> — run Claude\n"
                "/remind 5pm <message> — simple ping"
            )
            return

        lines = ["Scheduled tasks & reminders:"]
        for i, t in enumerate(tasks, 1):
            try:
                dt = datetime.fromisoformat(t["trigger_time"])
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                time_str = t["trigger_time"]

            task_type = t.get("task_type", "schedule")
            type_tag = "remind" if task_type == "remind" else "task"

            if t.get("recurrence"):
                lines.append(f"{i}. [{type_tag}] [{t['recurrence']} {dt.strftime('%H:%M') if dt else time_str}] {t['prompt']}")
            else:
                lines.append(f"{i}. [{type_tag}] [{time_str}] {t['prompt']}")

        await update.message.reply_text("\n".join(lines))
        return

    # Args provided: create a new task
    try:
        trigger_iso, prompt, recurrence = parse_schedule_args(args)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    task_id = create_scheduled_task(chat_id, trigger_iso, prompt, recurrence)

    # Format confirmation
    try:
        dt = datetime.fromisoformat(trigger_iso)
        time_str = dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        time_str = trigger_iso

    if recurrence:
        await update.message.reply_text(
            f"Scheduled ({recurrence}): {prompt}\n"
            f"First run: {time_str}\n"
            f"Task ID: {task_id}"
        )
    else:
        await update.message.reply_text(
            f"Scheduled: {prompt}\n"
            f"Run at: {time_str}\n"
            f"Task ID: {task_id}"
        )


@authorized
async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remind — set a lightweight reminder (no Claude, just a ping)."""
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        await update.message.reply_text(
            "Usage: /remind <when> <message>\n\n"
            "Examples:\n"
            "/remind me in 30 minutes check the build\n"
            "/remind in 2 hours call Alan\n"
            "/remind me 5pm check the deployment\n"
            "/remind me tomorrow 9am to review PR\n"
            "/remind monday standup\n"
            "/remind me 9 feb project deadline"
        )
        return

    # Strip "me" if first arg — "/remind me in 5 min ..." → "/remind in 5 min ..."
    if args[0].lower() == "me":
        args = args[1:]

    if len(args) < 2:
        await update.message.reply_text(
            "Need a time and a message.\n"
            "Example: /remind me 5pm check the deployment"
        )
        return

    try:
        trigger_iso, prompt, recurrence = parse_schedule_args(args)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    # Strip leading "to" / "that" from prompt — "remind me at 5pm to check X" → "check X"
    for prefix in ("to ", "that "):
        if prompt.lower().startswith(prefix):
            prompt = prompt[len(prefix):]
            break

    task_id = create_scheduled_task(
        chat_id, trigger_iso, prompt, recurrence, task_type="remind"
    )

    try:
        dt = datetime.fromisoformat(trigger_iso)
        time_str = dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        time_str = trigger_iso

    if recurrence:
        await update.message.reply_text(
            f"Reminder set ({recurrence}): {prompt}\n"
            f"First at: {time_str}"
        )
    else:
        await update.message.reply_text(
            f"Reminder set: {prompt}\n"
            f"At: {time_str}"
        )


@authorized
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel <n> — delete a scheduled task by its list number."""
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        await update.message.reply_text("Usage: /cancel <number> (from /schedule list)")
        return

    try:
        n = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid number. Use /schedule to see your tasks.")
        return

    tasks = list_scheduled_tasks(chat_id)
    if n < 1 or n > len(tasks):
        if not tasks:
            await update.message.reply_text("No scheduled tasks to cancel.")
        else:
            await update.message.reply_text(
                f"Invalid number. Use /schedule to see tasks (1-{len(tasks)})."
            )
        return

    task = tasks[n - 1]
    deleted = delete_scheduled_task(chat_id, task["id"])
    if deleted:
        await update.message.reply_text(f"Cancelled: {task['prompt']}")
    else:
        await update.message.reply_text("Failed to cancel task.")


@authorized
async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remember <fact> — store a persistent memory."""
    if add_memory is None:
        await update.message.reply_text("Memory module not available.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /remember <fact>\n\n"
            "Examples:\n"
            "/remember prefer dark mode for all UIs\n"
            "/remember tensor-core IP is 192.168.4.129\n"
            "/remember --people Alan is CTO of Bretalon\n\n"
            "Categories: --preferences, --infrastructure, --people, --projects"
        )
        return

    # Check for category flag
    category = "general"
    text_args = list(args)
    if text_args[0].startswith("--"):
        cat_flag = text_args.pop(0).lstrip("-").lower()
        valid_cats = ("preferences", "infrastructure", "people", "projects", "general")
        if cat_flag in valid_cats:
            category = cat_flag

    if not text_args:
        await update.message.reply_text("Need a fact to remember.")
        return

    text = " ".join(text_args)
    key = add_memory(text, category)
    count = memory_count()
    await update.message.reply_text(f"Remembered [{category}]: {text}\nKey: {key} ({count} total memories)")


@authorized
async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /forget <key-or-number> — remove a memory."""
    if remove_memory is None:
        await update.message.reply_text("Memory module not available.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /forget <key>\n"
            "Use /memories to see keys."
        )
        return

    key = args[0]

    # Support forgetting by number from the /memories list
    try:
        n = int(key)
        mems = list_memories()
        if 1 <= n <= len(mems):
            key = mems[n - 1]["key"]
        else:
            await update.message.reply_text(f"Invalid number. Use /memories to see list (1-{len(mems)}).")
            return
    except ValueError:
        pass  # Not a number, treat as key

    removed = remove_memory(key)
    if removed:
        await update.message.reply_text(f"Forgot: {key}")
    else:
        await update.message.reply_text(f"Memory not found: {key}")


@authorized
async def cmd_memories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /memories [category|search term] — list stored memories."""
    if list_memories is None:
        await update.message.reply_text("Memory module not available.")
        return

    args = context.args or []

    valid_cats = ("preferences", "infrastructure", "people", "projects", "general")

    if args and args[0].lower() in valid_cats:
        # Filter by category
        category = args[0].lower()
        mems = list_memories(category=category)
        header = f"Memories [{category}]:"
    elif args:
        # Search
        query = " ".join(args)
        mems = search_memories(query)
        header = f"Memories matching \"{query}\":"
    else:
        mems = list_memories()
        header = "All memories:"

    if not mems:
        await update.message.reply_text("No memories found.")
        return

    lines = [header]
    for i, m in enumerate(mems, 1):
        cat_tag = f"[{m['category']}]" if m.get('category') else ""
        lines.append(f"{i}. {cat_tag} {m['text']} ({m['key']})")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3997] + "..."
    await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# Infrastructure quick-action commands
# ---------------------------------------------------------------------------


@authorized
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /check [nodes|sites] — quick health check."""
    args = context.args or []
    target = args[0].lower() if args else "nodes"

    await update.message.reply_text(f"Checking {target}...")

    try:
        if target == "sites":
            result = await check_sites()
        else:
            result = await check_nodes()
        await update.message.reply_text(f"```\n{result}\n```", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Check failed: {e}")


@authorized
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /restart <service> [node] — restart a service (with confirmation)."""
    args = context.args or []
    if not args:
        services = ", ".join(sorted(ALLOWED_SERVICES))
        await update.message.reply_text(
            f"Usage: /restart <service> [node]\n\n"
            f"Allowed services:\n{services}\n\n"
            f"Default node: tensor-core"
        )
        return

    service = args[0]
    node = args[1] if len(args) > 1 else "tensor-core"

    if service not in ALLOWED_SERVICES:
        await update.message.reply_text(f"Service not in whitelist: {service}")
        return
    if node not in NODES:
        await update.message.reply_text(f"Unknown node: {node}")
        return

    # Confirmation button
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm restart", callback_data=f"infra:restart:{node}:{service}"),
        InlineKeyboardButton("Cancel", callback_data="infra:cancel"),
    ]])
    await update.message.reply_text(
        f"Restart {service} on {node}?",
        reply_markup=keyboard,
    )


@authorized
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /logs <service> [node] [lines] — tail service logs."""
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /logs <service> [node] [lines]")
        return

    service = args[0]
    node = "tensor-core"
    lines = 50

    for arg in args[1:]:
        if arg in NODES:
            node = arg
        elif arg.isdigit():
            lines = min(int(arg), 200)

    await update.message.reply_text(f"Fetching logs for {service} on {node}...")
    try:
        result = await get_logs(node, service, lines)
        # Send as code block, might need splitting
        if len(result) > 4000:
            result = result[-3997:] + "..."
        await update.message.reply_text(f"```\n{result}\n```", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


@authorized
async def cmd_disk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /disk [node] — disk usage."""
    args = context.args or []
    node = args[0] if args and args[0] in NODES else None

    try:
        result = await get_disk(node)
        await update.message.reply_text(f"```\n{result}\n```", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


@authorized
async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /top [node] — system overview."""
    args = context.args or []
    node = args[0] if args and args[0] in NODES else None

    await update.message.reply_text("Gathering system info...")
    try:
        result = await get_top(node)
        await update.message.reply_text(f"```\n{result}\n```", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


@authorized
async def cmd_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /deploy <site> — trigger deploy webhook (with confirmation)."""
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /deploy <site-repo-name>")
        return

    site = args[0]
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm deploy", callback_data=f"infra:deploy:{site}"),
        InlineKeyboardButton("Cancel", callback_data="infra:cancel"),
    ]])
    await update.message.reply_text(f"Deploy {site}?", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text

    if not user_text or not user_text.strip():
        return

    lock = get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Still processing previous message — please wait.")
        return

    async with lock:
        # Send typing indicator
        await update.effective_chat.send_action(ChatAction.TYPING)

        # Keep sending typing every 5s while claude runs
        typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

        try:
            session = get_session(chat_id)
            model = session["model"] if session else "sonnet"
            session_id = session["session_id"] if session else None
            msg_count = session["message_count"] if session else 0

            # Prepend reply-to context if replying to a message
            reply_ctx = _build_reply_context(update.message)
            prompt = reply_ctx + user_text

            # Acknowledgment message
            model_label = "Opus 4.6" if model == "opus" else "Sonnet"
            if session_id:
                ack = f"Processing... ({model_label}, msg #{msg_count + 1})"
            else:
                ack = f"Starting new session... ({model_label})"
            await update.message.reply_text(ack)

            # Build extra system prompt for voice mode
            extra_sp = get_voice_system_prompt_addition() if is_voice_mode(chat_id) else None

            # Stream Claude output with real-time message editing
            editor = StreamingEditor(update.effective_chat)
            data = await call_streaming(
                prompt, session_id, model, streaming_editor=editor,
                extra_system_prompt=extra_sp,
            )

            result_text = data.get("result", "")
            new_session_id = data.get("session_id", session_id)

            if not result_text:
                result_text = "(Empty response from Claude)"

            upsert_session(chat_id, new_session_id, model, msg_count + 1)
            await maybe_generate_summary(chat_id)

            # Finalize streaming message (applies Markdown formatting)
            if editor.text:
                await editor.finalize()
            else:
                for chunk in split_message(result_text):
                    try:
                        await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        await update.message.reply_text(chunk)

            # Send voice note if voice mode is enabled
            if is_voice_mode(chat_id):
                voice_data = await text_to_voice_note(result_text)
                if voice_data:
                    await update.effective_chat.send_voice(voice=voice_data)

            # Attach contextual quick-action keyboard if appropriate
            keyboard = get_contextual_keyboard(result_text)
            if keyboard and editor.sent_messages:
                try:
                    last_msg = editor.sent_messages[-1]
                    await last_msg.edit_reply_markup(reply_markup=keyboard)
                except Exception:
                    pass  # Don't fail if we can't add buttons

            # Send any files Claude wrote back to Telegram
            written_files = data.get("written_files", [])
            if written_files:
                await scan_and_send_outputs(update.effective_chat, written_files)

        except TimeoutError as e:
            log.error("Timeout: %s", e)
            await update.message.reply_text(f"Timed out after {TIMEOUT}s. Try a simpler query or /new to reset.")
        except RuntimeError as e:
            log.error("Claude error: %s", e)
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Unexpected error")
            await update.message.reply_text(f"Unexpected error: {e}")
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Voice message handler
# ---------------------------------------------------------------------------


async def transcribe_voice(voice_bytes: bytes) -> str:
    """Send OGG audio to the Whisper server and return transcription text."""
    form = aiohttp.FormData()
    form.add_field("audio", voice_bytes, filename="voice.ogg", content_type="audio/ogg")
    async with aiohttp.ClientSession() as session:
        async with session.post(WHISPER_URL, data=form, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Whisper returned {resp.status}: {body[:300]}")
            data = await resp.json()
            return data.get("text", "").strip()


@authorized
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    voice = update.message.voice or update.message.audio

    if voice is None:
        return

    lock = get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Still processing previous message — please wait.")
        return

    async with lock:
        await update.effective_chat.send_action(ChatAction.TYPING)
        typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

        try:
            # Download voice file from Telegram
            file = await context.bot.get_file(voice.file_id)
            buf = io.BytesIO()
            await file.download_to_memory(buf)
            voice_bytes = buf.getvalue()
            log.info("Downloaded voice message: %d bytes", len(voice_bytes))

            # Transcribe via Whisper
            transcript = await transcribe_voice(voice_bytes)
            if not transcript:
                await update.message.reply_text("(Could not transcribe voice message — empty result)")
                return

            log.info("Transcription: %s", transcript[:100])
            await update.message.reply_text(f"[Transcribed]: {transcript}")

            # Feed transcription to Claude
            session = get_session(chat_id)
            model = session["model"] if session else "sonnet"
            session_id = session["session_id"] if session else None
            msg_count = session["message_count"] if session else 0

            # Prepend reply-to context if replying to a message
            reply_ctx = _build_reply_context(update.message)
            prompt = reply_ctx + transcript

            model_label = "Opus 4.6" if model == "opus" else "Sonnet"
            await update.message.reply_text(f"Processing with {model_label}...")

            # Build extra system prompt for voice mode
            extra_sp = get_voice_system_prompt_addition() if is_voice_mode(chat_id) else None

            editor = StreamingEditor(update.effective_chat)
            data = await call_streaming(
                prompt, session_id, model, streaming_editor=editor,
                extra_system_prompt=extra_sp,
            )

            result_text = data.get("result", "")
            new_session_id = data.get("session_id", session_id)

            if not result_text:
                result_text = "(Empty response from Claude)"

            upsert_session(chat_id, new_session_id, model, msg_count + 1)
            await maybe_generate_summary(chat_id)

            if editor.text:
                await editor.finalize()
            else:
                for chunk in split_message(result_text):
                    try:
                        await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        await update.message.reply_text(chunk)

            # Send voice note if voice mode is enabled
            if is_voice_mode(chat_id):
                voice_data = await text_to_voice_note(result_text)
                if voice_data:
                    await update.effective_chat.send_voice(voice=voice_data)

            # Attach contextual quick-action keyboard if appropriate
            keyboard = get_contextual_keyboard(result_text)
            if keyboard and editor.sent_messages:
                try:
                    last_msg = editor.sent_messages[-1]
                    await last_msg.edit_reply_markup(reply_markup=keyboard)
                except Exception:
                    pass  # Don't fail if we can't add buttons

            # Send any files Claude wrote back to Telegram
            written_files = data.get("written_files", [])
            if written_files:
                await scan_and_send_outputs(update.effective_chat, written_files)

        except TimeoutError as e:
            log.error("Timeout: %s", e)
            await update.message.reply_text(f"Timed out after {TIMEOUT}s. Try a simpler query or /new to reset.")
        except RuntimeError as e:
            log.error("Voice/Claude error: %s", e)
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Unexpected error in voice handler")
            await update.message.reply_text(f"Unexpected error: {e}")
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
