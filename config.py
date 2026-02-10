"""Unified configuration for NEXUS — loads .env, system prompts, sets up logging."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AUTHORIZED_USER_ID = int(os.environ["AUTHORIZED_USER_ID"])
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", "/home/puretensorai")
TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:9000/transcribe")

# Dispatcher
WEATHER_DEFAULT_LOCATION = os.environ.get("WEATHER_DEFAULT_LOCATION", "Windsor,UK")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://MON2_TAILSCALE_IP:9090")
DARWIN_API_TOKEN = os.environ.get("DARWIN_API_TOKEN", "")
GOLD_API_KEY = os.environ.get("GOLD_API_KEY", "")

# Gitea
GITEA_URL = os.environ.get("GITEA_URL", "http://MON1_TAILSCALE_IP:3002")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN", "REDACTED_GITEA_TOKEN")

# Daily snippet
SNIPPET_SMTP_HOST = os.environ.get("SNIPPET_SMTP_HOST", "")
SNIPPET_SMTP_PORT = int(os.environ.get("SNIPPET_SMTP_PORT", "587"))
SNIPPET_SMTP_USER = os.environ.get("SNIPPET_SMTP_USER", "")
SNIPPET_SMTP_PASS = os.environ.get("SNIPPET_SMTP_PASS", "")
SNIPPET_FROM = os.environ.get("SNIPPET_FROM", "")
SNIPPET_TO = os.environ.get("SNIPPET_TO", "")

# Paths
DB_PATH = Path(__file__).parent / "nexus.db"
SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "nexus_system_prompt.md"
CONTEXT_PATH = Path(__file__).parent / "prompts" / "hal_context.md"

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_system_prompt: str | None = None
if SYSTEM_PROMPT_PATH.exists():
    _system_prompt = SYSTEM_PROMPT_PATH.read_text().strip()

_context_prompt: str | None = None
if CONTEXT_PATH.exists():
    _context_prompt = CONTEXT_PATH.read_text().strip()

if _system_prompt and _context_prompt:
    _system_prompt = _system_prompt + "\n\n---\n\n" + _context_prompt
elif _context_prompt:
    _system_prompt = _context_prompt

SYSTEM_PROMPT = _system_prompt

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nexus")

if SYSTEM_PROMPT:
    log.info("Loaded system prompt (%d chars)", len(SYSTEM_PROMPT))
