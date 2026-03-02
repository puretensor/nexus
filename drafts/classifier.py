"""Rule-based email classifier for incoming messages.

Classifies emails into:
  - ignore:     spam, marketing, newsletters, no-reply senders
  - notify:     important but no reply needed (receipts, alerts, confirmations)
  - auto_reply: needs a response — Claude drafts a reply for approval
  - followup:   track for follow-up (e.g., waiting for someone's response)
"""

import os
import re
import logging

log = logging.getLogger("nexus")

# Senders to always ignore (case-insensitive substring match)
IGNORE_SENDERS = [
    "noreply@", "no-reply@", "donotreply@",
    "notifications@github.com",
    "notification@",
    "newsletter@",
    "marketing@",
    "promotions@",
    "mailer-daemon@",
    "postmaster@",
    "updates@",
    "digest@",
]

# Subject patterns to ignore
IGNORE_SUBJECTS = [
    r"unsubscribe",
    r"^out of office",
    r"^automatic reply",
    r"delivery status notification",
]

# Senders that indicate notify-only (receipts, alerts)
NOTIFY_SENDERS = [
    "stripe.com",
    "paypal.com",
    "google.com/accounts",
    "security@",
    "alert@",
    "billing@",
    "receipt@",
    "invoice@",
    "order-confirmation@",
]

# Subject patterns for notify-only
NOTIFY_SUBJECTS = [
    r"receipt",
    r"invoice",
    r"payment.*confirm",
    r"order.*confirm",
    r"security alert",
    r"sign-in.*detected",
    r"password.*reset",
    r"two-factor",
    r"2fa",
    r"verification code",
]

# VIP senders that always get auto_reply treatment (loaded from env or defaults)
VIP_SENDERS = [
    s.strip() for s in
    os.environ.get("VIP_SENDERS", "ops@example.com").split(",")
    if s.strip()
]


def classify_email(from_addr: str, subject: str, to_addr: str = "") -> str:
    """Classify an email into: ignore, notify, auto_reply, or followup.

    Args:
        from_addr: Sender email address (may include display name)
        subject: Email subject line
        to_addr: Recipient address (for context)

    Returns:
        One of: "ignore", "notify", "auto_reply", "followup"
    """
    from_lower = from_addr.lower()
    subject_lower = subject.lower()

    # Never reply to ourselves (prevents self-reply loops)
    if "hal@example.com" in from_lower:
        return "ignore"

    # Check ignore rules FIRST (noreply, mailer-daemon, etc.)
    for pattern in IGNORE_SENDERS:
        if pattern in from_lower:
            return "ignore"

    for pattern in IGNORE_SUBJECTS:
        if re.search(pattern, subject_lower):
            return "ignore"

    # VIP senders (whitelisted domains) get drafted replies via Claude
    for vip in VIP_SENDERS:
        if vip in from_lower:
            return "auto_reply"

    # Check notify rules
    for pattern in NOTIFY_SENDERS:
        if pattern in from_lower:
            return "notify"

    for pattern in NOTIFY_SUBJECTS:
        if re.search(pattern, subject_lower):
            return "notify"

    # Emails to hal@ from non-whitelisted senders: notify only (no LLM).
    if to_addr and "hal@" in to_addr.lower():
        return "notify"

    # Default: notify (safe — user sees it but no draft created)
    return "notify"
