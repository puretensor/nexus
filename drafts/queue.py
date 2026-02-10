"""Draft queue — manages email reply drafts with Telegram approval workflow.

Lifecycle: pending -> approved -> sent
                   -> rejected

Drafts are stored in SQLite (db.py). Sending uses gmail.py CLI.
"""

import logging
import subprocess
from pathlib import Path

from config import AUTHORIZED_USER_ID
from db import (
    create_draft,
    get_draft,
    list_drafts,
    update_draft_status,
    create_followup,
)

log = logging.getLogger("nexus")

GMAIL_SCRIPT = Path.home() / ".config" / "puretensor" / "gmail.py"
GMAIL_IDENTITY = "hal"  # sends as hal@puretensor.ai


async def create_email_draft(
    email_from: str,
    email_subject: str,
    email_message_id: str,
    draft_body: str,
    bot=None,
) -> int:
    """Create a draft and notify via Telegram with approval buttons.

    Returns draft ID.
    """
    chat_id = int(AUTHORIZED_USER_ID)
    draft_id = create_draft(
        chat_id=chat_id,
        email_from=email_from,
        email_subject=email_subject,
        email_message_id=email_message_id,
        draft_body=draft_body,
    )

    log.info("Created draft #%d for reply to %s", draft_id, email_from)

    if bot:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        preview = draft_body[:300] + "..." if len(draft_body) > 300 else draft_body
        text = (
            f"Draft Reply (#{draft_id})\n"
            f"To: {email_from}\n"
            f"Re: {email_subject}\n\n"
            f"{preview}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve", callback_data=f"draft:approve:{draft_id}"),
                InlineKeyboardButton("Reject", callback_data=f"draft:reject:{draft_id}"),
            ],
        ])

        try:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        except Exception as e:
            log.warning("Failed to send draft notification: %s", e)

    return draft_id


def send_draft(draft_id: int) -> tuple[bool, str]:
    """Send an approved draft via gmail.py. Returns (success, message)."""
    draft = get_draft(draft_id)
    if not draft:
        return False, f"Draft #{draft_id} not found"

    if draft["status"] != "approved":
        return False, f"Draft #{draft_id} is {draft['status']}, not approved"

    if not GMAIL_SCRIPT.exists():
        return False, f"gmail.py not found at {GMAIL_SCRIPT}"

    try:
        result = subprocess.run(
            [
                "python3", str(GMAIL_SCRIPT),
                GMAIL_IDENTITY, "reply",
                "--id", draft["email_message_id"],
                "--body", draft["draft_body"],
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            update_draft_status(draft_id, "sent")
            log.info("Sent draft #%d reply to %s", draft_id, draft["email_from"])

            # Auto-create a follow-up tracker
            try:
                create_followup(
                    chat_id=draft["chat_id"],
                    email_to=draft["email_from"],
                    email_subject=draft["email_subject"],
                    email_message_id=draft["email_message_id"],
                )
            except Exception as e:
                log.warning("Failed to create followup for draft #%d: %s", draft_id, e)

            return True, f"Reply sent to {draft['email_from']}"
        else:
            error = result.stderr[:300] or result.stdout[:300]
            log.warning("Failed to send draft #%d: %s", draft_id, error)
            return False, f"Send failed: {error}"

    except subprocess.TimeoutExpired:
        return False, "gmail.py timed out"
    except Exception as e:
        return False, f"Send error: {e}"


def approve_draft(draft_id: int) -> tuple[bool, str]:
    """Approve and send a draft."""
    draft = get_draft(draft_id)
    if not draft:
        return False, f"Draft #{draft_id} not found"
    if draft["status"] != "pending":
        return False, f"Draft #{draft_id} is already {draft['status']}"

    update_draft_status(draft_id, "approved")
    return send_draft(draft_id)


def reject_draft(draft_id: int) -> tuple[bool, str]:
    """Reject a draft."""
    draft = get_draft(draft_id)
    if not draft:
        return False, f"Draft #{draft_id} not found"
    if draft["status"] != "pending":
        return False, f"Draft #{draft_id} is already {draft['status']}"

    update_draft_status(draft_id, "rejected")
    return True, f"Draft #{draft_id} rejected"


def get_pending_drafts() -> list[dict]:
    """Get all pending drafts."""
    return list_drafts(int(AUTHORIZED_USER_ID), status="pending")
