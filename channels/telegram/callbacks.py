"""Telegram callback query handler — inline keyboard button actions.

Ported from ~/claude-telegram/claude_telegram_bot.py (handle_callback).
"""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from db import authorized, get_session
from engine import call_streaming
from channels.telegram.streaming import StreamingEditor
from dispatcher import refresh_dispatch
from dispatcher.apis import DispatchError
from dispatcher.apis.infra import restart_service, trigger_deploy

log = logging.getLogger("nexus")


@authorized
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callback queries (e.g., Refresh buttons)."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data.startswith("escalation:"):
        parts = data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        param = parts[2] if len(parts) > 2 else ""

        if action == "ignore":
            await query.message.edit_text(
                query.message.text + "\n\n\u2705 Acknowledged \u2014 no action taken."
            )
            return

        if action == "commands":
            # Show remediation commands for the instance
            ip = param.split(":")[0]
            commands = [
                f"ping -c 3 {ip}",
                f"ssh {ip} 'systemctl status prometheus-node-exporter'",
                f"ssh {ip} 'systemctl restart prometheus-node-exporter'",
                f"ssh {ip} 'uptime'",
            ]
            cmd_text = "\n".join(f"  {c}" for c in commands)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Suggested commands for {param}:\n\n{cmd_text}",
            )
            return

        if action == "fix":
            # Auto-fix: restart node_exporter on the target
            ip = param.split(":")[0]
            await query.message.edit_text(
                query.message.text + f"\n\n\U0001f527 Attempting auto-fix for {param}..."
            )

            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh", "-o", "ConnectTimeout=10", ip,
                    "systemctl restart prometheus-node-exporter",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

                if proc.returncode == 0:
                    result = f"\u2705 Restarted node_exporter on {ip}"
                else:
                    err = stderr.decode().strip()[:200]
                    result = f"\u274c Failed (exit {proc.returncode}): {err}"
            except asyncio.TimeoutError:
                result = f"\u274c SSH to {ip} timed out"
            except Exception as e:
                result = f"\u274c Error: {e}"

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=result,
            )

            # Log the action
            log.info("Escalation auto-fix for %s: %s", param, result)
            return

        return

    if data.startswith("backend:"):
        parts = data.split(":")
        if len(parts) >= 3:
            backend_name = parts[1]
            model_name = parts[2]

            import config
            from backends import reset_backend
            from db import update_model
            from engine import get_model_display

            chat_id = update.effective_chat.id

            # Switch backend if needed
            if config.ENGINE_BACKEND != backend_name:
                config.ENGINE_BACKEND = backend_name
                reset_backend()

            # Update model in session
            update_model(chat_id, model_name)

            display = get_model_display(model_name)
            if backend_name == "ollama":
                label = f"{display} (local, with tools)"
            else:
                label = display

            await query.message.edit_text(f"Switched to {label}.")
        return

    if data.startswith("infra:"):
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "cancel":
            await query.message.edit_text("Cancelled.")
            return

        if action == "restart" and len(parts) >= 4:
            node, service = parts[2], parts[3]
            await query.message.edit_text(f"Restarting {service} on {node}...")
            try:
                result = await restart_service(node, service)
                await query.message.edit_text(f"Result:\n{result}")
            except Exception as e:
                await query.message.edit_text(f"Failed: {e}")
            return

        if action == "deploy" and len(parts) >= 3:
            site = parts[2]
            await query.message.edit_text(f"Deploying {site}...")
            try:
                result = await trigger_deploy(site)
                await query.message.edit_text(f"Deploy result:\n{result}")
            except Exception as e:
                await query.message.edit_text(f"Deploy failed: {e}")
            return

        return

    if data.startswith("action:"):
        action = data.split(":", 1)[1] if ":" in data else ""
        chat_id = update.effective_chat.id

        if action == "retry":
            # Re-send the last user message to Claude
            await query.message.reply_text("Retrying last request...")
            session = get_session(chat_id)
            if session and session.get("session_id"):
                editor = StreamingEditor(update.effective_chat)
                data_result = await call_streaming(
                    "Please retry what you just attempted. If it was a command or check, run it again.",
                    session["session_id"],
                    session["model"],
                    streaming_editor=editor,
                )
                if editor.text:
                    await editor.finalize()
                elif data_result.get("result"):
                    await query.message.reply_text(data_result["result"][:4000])
            return

        if action == "details":
            session = get_session(chat_id)
            if session and session.get("session_id"):
                editor = StreamingEditor(update.effective_chat)
                data_result = await call_streaming(
                    "Give me more details about what you just reported. Be thorough.",
                    session["session_id"],
                    session["model"],
                    streaming_editor=editor,
                )
                if editor.text:
                    await editor.finalize()
                elif data_result.get("result"):
                    await query.message.reply_text(data_result["result"][:4000])
            return

        if action == "commit":
            session = get_session(chat_id)
            if session and session.get("session_id"):
                editor = StreamingEditor(update.effective_chat)
                data_result = await call_streaming(
                    "Please commit the changes you just made with an appropriate commit message.",
                    session["session_id"],
                    session["model"],
                    streaming_editor=editor,
                )
                if editor.text:
                    await editor.finalize()
                elif data_result.get("result"):
                    await query.message.reply_text(data_result["result"][:4000])
            return

        if action == "diff":
            session = get_session(chat_id)
            if session and session.get("session_id"):
                editor = StreamingEditor(update.effective_chat)
                data_result = await call_streaming(
                    "Show me the diff of the changes you just made.",
                    session["session_id"],
                    session["model"],
                    streaming_editor=editor,
                )
                if editor.text:
                    await editor.finalize()
                elif data_result.get("result"):
                    await query.message.reply_text(data_result["result"][:4000])
            return

        if action == "summarize":
            session = get_session(chat_id)
            if session and session.get("session_id"):
                editor = StreamingEditor(update.effective_chat)
                data_result = await call_streaming(
                    "Please summarize your last response in 2-3 concise bullet points.",
                    session["session_id"],
                    session["model"],
                    streaming_editor=editor,
                )
                if editor.text:
                    await editor.finalize()
                elif data_result.get("result"):
                    await query.message.reply_text(data_result["result"][:4000])
            return

        return

    if data.startswith("draft:"):
        parts = data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        draft_id_str = parts[2] if len(parts) > 2 else ""

        try:
            draft_id = int(draft_id_str)
        except (ValueError, TypeError):
            await query.message.edit_text("Invalid draft ID.")
            return

        from drafts.queue import approve_draft, reject_draft

        if action == "approve":
            success, msg = approve_draft(draft_id)
            status_icon = "\u2705" if success else "\u274c"
            await query.message.edit_text(
                query.message.text + f"\n\n{status_icon} {msg}"
            )
            return

        if action == "reject":
            success, msg = reject_draft(draft_id)
            status_icon = "\u2705" if success else "\u274c"
            await query.message.edit_text(
                query.message.text + f"\n\n{status_icon} {msg}"
            )
            return

        return

    if not data.startswith("refresh:"):
        return

    parts = data.split(":", 2)
    if len(parts) < 3:
        return

    category = parts[1]
    params = parts[2]

    try:
        await refresh_dispatch(category, params, update.effective_chat, context.bot)
    except DispatchError as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"\u26a0\ufe0f {e}",
        )
    except Exception:
        log.exception("Refresh callback error")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Refresh failed \u2014 try again.",
        )
