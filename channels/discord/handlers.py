"""Discord command and message handlers.

Follows the same pattern as channels/telegram/commands.py — auth check,
per-user lock, streaming engine call, session management.
Uses ! prefix commands (not slash commands).
"""

import asyncio
import logging

import discord

from db import (
    get_session,
    upsert_session,
    update_model,
    delete_session,
    get_lock,
    reset_session_id,
)
from engine import call_streaming, split_message, get_model_display
from channels.discord.streaming import DiscordStreamingEditor
from handlers.summaries import maybe_generate_summary
from config import TIMEOUT, DISCORD_AUTHORIZED_USER_ID, AGENT_NAME, log


# Discord message limit
MSG_LIMIT = 1900

# Override Claude Code's default identity for Discord channel
DISCORD_SYSTEM_PROMPT = (
    f"IMPORTANT IDENTITY OVERRIDE: You are {AGENT_NAME}, NOT Claude Code. "
    f"Ignore any prior instruction that says you are 'Claude Code' or a CLI tool. "
    f"You are {AGENT_NAME}, a personal AI assistant running on PureTensor infrastructure. "
    f"You are responding via Discord. You have full access to infrastructure, email, "
    f"calendar, and all tools described in your system context. "
    f"Formatting: Your output is rendered in Discord. Use Discord-compatible Markdown: "
    f"**bold**, *italic*, `code`, ```code blocks```, > blockquotes. "
    f"Discord supports full Markdown including ## headers and --- rules."
)


def _authorized(user_id: int) -> bool:
    """Check if a Discord user is authorized."""
    return user_id == DISCORD_AUTHORIZED_USER_ID


async def handle_command(message: discord.Message):
    """Route !prefix commands. Returns True if a command was handled."""
    content = message.content.strip()
    if not content.startswith("!"):
        return False

    parts = content[1:].split(None, 1)
    cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    chat_id = message.author.id
    channel = message.channel

    if cmd == "new":
        session = get_session(chat_id)
        model = session["model"] if session else "sonnet"
        delete_session(chat_id)
        update_model(chat_id, model)
        await channel.send("Session cleared. Next message starts a fresh conversation.")
        return True

    elif cmd == "opus":
        import config
        from backends import reset_backend
        if config.ENGINE_BACKEND != "claude_code":
            config.ENGINE_BACKEND = "claude_code"
            reset_backend()
        update_model(chat_id, "opus")
        await channel.send(f"Switched to {get_model_display('opus')}.")
        return True

    elif cmd == "sonnet":
        import config
        from backends import reset_backend
        if config.ENGINE_BACKEND != "claude_code":
            config.ENGINE_BACKEND = "claude_code"
            reset_backend()
        update_model(chat_id, "sonnet")
        await channel.send(f"Switched to {get_model_display('sonnet')}.")
        return True

    elif cmd == "ollama":
        import config
        from backends import reset_backend
        if config.ENGINE_BACKEND != "ollama":
            config.ENGINE_BACKEND = "ollama"
            reset_backend()
        update_model(chat_id, "sonnet")
        await channel.send(f"Switched to {get_model_display('sonnet')} (local, with tools).")
        return True

    elif cmd == "backend":
        import config
        current = config.ENGINE_BACKEND
        session = get_session(chat_id)
        current_model = session["model"] if session else "sonnet"

        def check(text, active):
            return f"**> {text}**" if active else f"  {text}"

        lines = [
            "**Select backend** (use `!ollama`, `!sonnet`, `!opus`):",
            check("Ollama (local)", current == "ollama"),
            check(f"Claude Sonnet", current == "claude_code" and current_model == "sonnet"),
            check(f"Claude Opus", current == "claude_code" and current_model == "opus"),
        ]
        await channel.send("\n".join(lines))
        return True

    elif cmd == "status":
        session = get_session(chat_id)
        if session is None or session["session_id"] is None:
            await channel.send("No active session. Send a message to start one.")
        else:
            name = session.get("name", "default")
            summary = session.get("summary")
            msg = (
                f"**Session:** `{session['session_id'][:12]}...` (name: {name})\n"
                f"**Model:** {session['model']}\n"
                f"**Messages:** {session['message_count']}\n"
            )
            if summary:
                msg += f"**Summary:** {summary}\n"
            msg += f"**Started:** {session['created_at']}"
            await channel.send(msg)
        return True

    elif cmd == "help":
        await channel.send(
            "**Session**\n"
            "`!new` — Start fresh session\n"
            "`!opus` — Switch to Claude Opus\n"
            "`!sonnet` — Switch to Claude Sonnet\n"
            "`!ollama` — Switch to local model\n"
            "`!backend` — Show current backend\n"
            "`!status` — Show current session info\n"
            "`!help` — This message\n\n"
            "Any other text is sent to the AI engine."
        )
        return True

    return False


async def handle_message(message: discord.Message, client: discord.Client):
    """Handle a regular (non-command) Discord message — send to engine."""
    chat_id = message.author.id
    user_text = message.content.strip()
    channel = message.channel

    if not user_text:
        return

    lock = get_lock(chat_id)
    if lock.locked():
        await channel.send("Still processing previous message -- please wait.")
        return

    async with lock:
        # Show typing indicator while processing
        async with channel.typing():
            try:
                session = get_session(chat_id)
                model = session["model"] if session else "sonnet"
                session_id = session["session_id"] if session else None
                msg_count = session["message_count"] if session else 0

                # Ack message
                model_label = get_model_display(model)
                if session_id or msg_count > 0:
                    ack = f"*{model_label} processing... (msg #{msg_count + 1})*"
                else:
                    ack = f"*{model_label} processing... (new session)*"
                ack_msg = await channel.send(ack)

                # Stream output with real-time message editing
                editor = DiscordStreamingEditor(channel)
                data = await call_streaming(
                    user_text, session_id, model, streaming_editor=editor,
                    extra_system_prompt=DISCORD_SYSTEM_PROMPT,
                )

                result_text = data.get("result", "")
                new_session_id = data.get("session_id", session_id)

                if not result_text:
                    result_text = "(Empty response)"

                upsert_session(chat_id, new_session_id, model, msg_count + 1)
                await maybe_generate_summary(chat_id)

                # Delete the ack message
                try:
                    await ack_msg.delete()
                except Exception:
                    pass

                # Finalize streaming message
                if editor.text:
                    await editor.finalize()
                else:
                    # Fallback: send as split messages
                    for chunk in split_message(result_text, limit=MSG_LIMIT):
                        await channel.send(chunk)

            except TimeoutError as e:
                log.error("Timeout: %s", e)
                await channel.send(f"Timed out after {TIMEOUT}s. Try a simpler query or `!new` to reset.")
            except RuntimeError as e:
                log.error("Engine error: %s", e)
                await channel.send(f"Error: {e}")
            except Exception as e:
                log.exception("Unexpected error in Discord handler")
                await channel.send(f"Unexpected error: {e}")
