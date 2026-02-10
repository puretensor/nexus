"""Contextual inline keyboards — attach quick-action buttons after Claude responses."""

import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def get_contextual_keyboard(response_text: str) -> InlineKeyboardMarkup | None:
    """Analyze Claude's response and return appropriate quick-action buttons.

    Returns None if no buttons are appropriate (most responses).
    Only shows buttons when they're clearly useful.
    """
    buttons = []

    # Infrastructure-related responses
    if _is_infra_response(response_text):
        buttons.append([
            InlineKeyboardButton("Retry", callback_data="action:retry"),
            InlineKeyboardButton("Details", callback_data="action:details"),
        ])

    # Code change responses
    elif _is_code_response(response_text):
        buttons.append([
            InlineKeyboardButton("Commit", callback_data="action:commit"),
            InlineKeyboardButton("Diff", callback_data="action:diff"),
        ])

    # Long responses (over 2000 chars)
    elif len(response_text) > 2000:
        buttons.append([
            InlineKeyboardButton("Summarize", callback_data="action:summarize"),
        ])

    if not buttons:
        return None

    return InlineKeyboardMarkup(buttons)


def _is_infra_response(text: str) -> bool:
    """Check if response is infrastructure-related."""
    infra_patterns = [
        r'\b(?:node|server|service|container|docker|systemctl|nginx)\b.*\b(?:down|failed|error|restart|stopped|unreachable|timeout)\b',
        r'\b(?:ssh|ping|curl)\b.*\b(?:fail|error|timeout|refused|unreachable)\b',
        r'\b(?:disk|cpu|memory|load)\b.*\b(?:high|full|critical|warning|alert)\b',
        r'\b(?:restart|restarted|stopped|started|deployed|deployed)\b.*\b(?:service|container|process)\b',
        r'\bsystemctl\s+(?:restart|stop|start|status)\b',
        r'\b(?:node_exporter|prometheus|grafana|alertmanager)\b',
    ]
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in infra_patterns)


def _is_code_response(text: str) -> bool:
    """Check if response involves code changes."""
    code_patterns = [
        r'\b(?:wrote|written|created|modified|edited|updated)\b.*\b(?:file|\.py|\.js|\.ts|\.go|\.rs)\b',
        r'\b(?:git\s+(?:add|commit|diff|status))\b',
        r'(?:edit|write)\s+tool',
        r'\bchanges?\s+(?:to|in)\s+\S+\.\w{1,4}\b',
        r'\bmodified\s+\d+\s+file',
    ]
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in code_patterns)
