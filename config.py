"""Unified configuration for NEXUS — loads .env, system prompts, sets up logging."""

import logging
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AUTHORIZED_USER_ID = int(os.environ["AUTHORIZED_USER_ID"])
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "/usr/bin/claude"
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", "/home/puretensorai")
TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "1800"))
WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:9000/transcribe")

# Engine backend selection (ollama, claude_code, codex_cli, gemini_cli)
ENGINE_BACKEND = os.environ.get("ENGINE_BACKEND", "ollama")

# Ollama — local models (default)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:235b")
OLLAMA_TOOLS_ENABLED = os.environ.get("OLLAMA_TOOLS_ENABLED", "true").lower() == "true"
OLLAMA_TOOL_MAX_ITER = int(os.environ.get("OLLAMA_TOOL_MAX_ITER", "25"))
OLLAMA_TOOL_TIMEOUT = int(os.environ.get("OLLAMA_TOOL_TIMEOUT", "30"))
OLLAMA_NUM_PREDICT = int(os.environ.get("OLLAMA_NUM_PREDICT", "8192"))  # thinking models need headroom

# Web search — SearXNG preferred (self-hosted, private), DuckDuckGo fallback (zero config)
SEARXNG_URL = os.environ.get("SEARXNG_URL", "")  # e.g. http://GCP_MEDIUM_TAILSCALE_IP:8080/search

# Claude Code CLI
# (uses claude binary + CLAUDE_BIN/CLAUDE_CWD above)

# Codex CLI (OpenAI)
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "/usr/bin/codex"
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")
CODEX_CWD = os.environ.get("CODEX_CWD", "/home/puretensorai")

# Gemini CLI (Google)
GEMINI_BIN = os.environ.get("GEMINI_BIN") or shutil.which("gemini") or "/usr/bin/gemini"
GEMINI_CLI_MODEL = os.environ.get("GEMINI_CLI_MODEL", "")  # empty = use Gemini CLI's own default
ALERT_BOT_TOKEN = os.environ.get("ALERT_BOT_TOKEN", BOT_TOKEN)  # fallback to main bot

# Discord
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_AUTHORIZED_USER_ID = int(os.environ.get("DISCORD_AUTHORIZED_USER_ID", "0"))

# Agent identity — configurable name and personality
AGENT_NAME = os.environ.get("AGENT_NAME", "HAL")
AGENT_PERSONALITY = os.environ.get("AGENT_PERSONALITY", "")

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
CONTEXT_PATH = Path(__file__).parent / "prompts" / "pureclaw_context.md"

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

# Apply agent identity template substitution
if _system_prompt:
    _system_prompt = _system_prompt.replace("{agent_name}", AGENT_NAME)
    if AGENT_PERSONALITY:
        _system_prompt = _system_prompt.replace(
            "{agent_personality_block}",
            f"\nPersonality: {AGENT_PERSONALITY}\n"
        )
    else:
        _system_prompt = _system_prompt.replace("{agent_personality_block}", "")

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
