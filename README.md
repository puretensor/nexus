# PureClaw

**Pure, Simple, and Secure.**
Your AI agent, your hardware, your rules.

[pureclaw.ai](https://pureclaw.ai) | [GitHub](https://github.com/puretensor/PureClaw)

---

## Choose Your Engine

PureClaw is a multi-engine agentic framework. Swap between any backend with a single environment variable or a `/backend` tap in Telegram. No lock-in, no bloat, no telemetry.

### Engine Table

| Backend | Type | Provider | Tool Use | Streaming | Cost |
|---------|------|----------|----------|-----------|------|
| `ollama` | Local | Any GGUF model | 7 tools (incl. web search) | Yes | Free |
| `anthropic_api` | API | Anthropic | 7 tools (incl. web search) | Yes | Per-token |
| `gemini_api` | API | Google Gemini | 7 tools (incl. web search) | Yes | Per-token |
| `openai_compat` | API | ChatGPT, DeepSeek, Kimi 2.5, GLM 5, Grok, Mistral, vLLM | 7 tools (incl. web search) | Yes | Per-token |
| `claude_code` | CLI | Claude Code | Full agentic | Yes | Subscription |
| `gemini_cli` | CLI | Gemini CLI | Full agentic | Yes | Subscription |
| `codex_cli` | CLI | Codex CLI | Full agentic | Yes | Subscription |

**Three tiers:**

- **Tier 1 -- Local-First:** Ollama runs on your hardware. Free, private, with 7 tools included (bash, read, write, edit, glob, grep, web search). Your data never leaves your machine.
- **Tier 2 -- API:** Anthropic, Gemini, and any OpenAI-compatible provider. Pay-per-token, no subscription, 7 built-in tools (same as Tier 1). Pure HTTP -- stdlib `urllib` for sync, `aiohttp` for async streaming.
- **Tier 3 -- CLI:** Claude Code, Gemini CLI, Codex CLI. Subscription-based, full agentic tool use delegated to the CLI binary.

### Recommended Backends

For the best agentic experience:

- **Claude Code (Sonnet/Opus)** -- Most reliable tool use, web search, file operations, and session continuity. The reference backend that PureClaw is developed against.
- **Ollama (local)** -- Best for privacy and cost. Runs entirely on your hardware with 7 built-in tools. Models like Qwen 3, Llama 4, and DeepSeek R2 all work well.
- **Anthropic API / Gemini API / OpenAI-compatible** -- Good middle ground. 7 built-in tools, pay-per-token, no subscription.

**Note on Gemini CLI and Codex CLI:** These are third-party CLI tools with their own tool execution sandboxes. Their agentic capabilities (web search, code execution) are less mature than Claude Code and may produce errors during tool use. They work for basic conversation but are not recommended for tasks requiring reliable tool execution.

### Switching Engines

One env var:

```bash
ENGINE_BACKEND=gemini_api
```

Or tap `/backend` in Telegram to get an inline keyboard:

```
[Sonnet 4.5]  [Opus 4.6]       <- claude_code
[Gemini CLI]                     <- gemini_cli
[Codex CLI]                      <- codex_cli
[Anthropic API]                  <- anthropic_api
[Gemini API]                     <- gemini_api
[Qwen 3 235B (local)]           <- ollama
```

---

## Security

- **Single-user auth** -- one authorized Telegram user ID, no shared access
- **No cloud dependency** -- Ollama keeps data local; API backends use direct HTTP calls
- **No telemetry** -- phones home to nobody, no analytics, no tracking
- **Human-in-the-loop email** -- agent drafts replies, you approve or reject via Telegram
- **Minimal dependencies** -- stdlib HTTP for API calls, no bloated SDK chains
- **Your hardware, not a managed platform** -- run on your own server, your own GPU

---

## Architecture

```
nexus.py (entry point)
  |
  +-- Engine (backends/)
  |     +-- claude_code      Claude Code CLI (Tier 3)
  |     +-- anthropic_api    Anthropic Messages API (Tier 2)
  |     +-- gemini_api       Google Gemini REST API (Tier 2)
  |     +-- openai_compat    ChatGPT / DeepSeek / Grok / Mistral / vLLM (Tier 2)
  |     +-- ollama           Local models with tool use (Tier 1)
  |     +-- gemini_cli       Gemini CLI (Tier 3)
  |     +-- codex_cli        Codex CLI (Tier 3)
  |
  +-- Channels
  |     +-- Telegram         Streaming responses, inline keyboards, voice input
  |     +-- Email Input      IMAP polling -> classify -> draft -> approve/reject
  |
  +-- Observers              Internal cron scheduler, 7 autonomous tasks
  |     +-- EmailDigest      Summarise unread emails (every 30 min)
  |     +-- MorningBrief     Email + infra + weather + calendar (07:30 weekdays)
  |     +-- NodeHealth       Prometheus cluster health (every 5 min)
  |     +-- DailySnippet     Geopolitical intelligence brief (08:00 weekdays)
  |     +-- BretalonReview   Content review pipeline (every 2 hours)
  |     +-- FollowupReminder Nag about unanswered emails (09:00 weekdays)
  |     +-- GitPush          Webhook for Gitea push summaries (persistent)
  |
  +-- Draft Queue            Email drafts with Telegram Approve/Reject buttons
  +-- Dispatcher             Instant data cards (weather, markets, trains, infra)
  +-- Task Scheduler         User-defined scheduled tasks and reminders
```

---

## Features

### Channels

- **Telegram** -- Full conversational interface with streaming responses, real-time tool status updates, session continuity, model switching, voice input (Whisper transcription), photo/document analysis, and inline keyboards
- **Email Input** -- Polls IMAP for incoming messages, classifies them, and generates Claude-drafted replies that require explicit Telegram approval before sending

### Observers

Autonomous background tasks running on cron schedules inside a single process. No external cron entries needed.

| Observer | Schedule | Description |
|----------|----------|-------------|
| `email_digest` | Every 30 min | Summarises unread emails from 3 accounts |
| `morning_brief` | 07:30 weekdays | Combined email + infrastructure + weather + calendar briefing |
| `node_health` | Every 5 min | Queries Prometheus for cluster health, escalates to Telegram |
| `daily_snippet` | 08:00 weekdays | RSS-powered geopolitical intelligence brief |
| `bretalon_review` | Every 2 hours | Content review pipeline with editorial email delivery |
| `followup_reminder` | 09:00 weekdays | Reminds about outbound emails that haven't received a reply |
| `git_push` | Persistent | HTTP webhook server for Gitea push event summaries |

### Draft Queue

Outbound email lifecycle with human-in-the-loop approval:

```
Incoming email -> Classify -> Engine generates draft reply
  -> Telegram notification with [Approve] [Reject] buttons
    -> Approve -> Send via Gmail API -> Auto-create follow-up tracker
    -> Reject -> Draft discarded
```

### Dispatcher (Data Cards)

Instant structured responses that bypass the engine for speed:

- Weather (OpenWeatherMap)
- Cryptocurrency prices (CoinGecko)
- Gold/silver spot prices
- Stock market indices / Forex rates
- UK train departures (Darwin API)
- Infrastructure status (Prometheus)

---

## Quick Start

```bash
git clone https://github.com/puretensor/PureClaw.git
cd PureClaw
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set TELEGRAM_BOT_TOKEN, AUTHORIZED_USER_ID, and choose your engine
python3 nexus.py
```

Or deploy as a systemd service:

```bash
sudo cp nexus.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nexus.service
```

---

## Project Structure

```
nexus/
+-- nexus.py                    # Entry point
+-- config.py                   # Environment loading, logging
+-- db.py                       # SQLite: sessions, drafts, followups, tasks, observer state
+-- engine.py                   # Engine abstraction (sync + async + streaming)
+-- memory.py                   # Persistent memory injection
+-- scheduler.py                # User task scheduler (/schedule, /remind)
|
+-- backends/
|     +-- base.py               # Backend Protocol (structural subtyping)
|     +-- claude_code.py        # Claude Code CLI backend
|     +-- anthropic_api.py      # Anthropic Messages API backend
|     +-- gemini_api.py         # Google Gemini REST API backend
|     +-- openai_compat.py      # OpenAI-compatible API backend
|     +-- ollama.py             # Ollama backend with tool use
|     +-- gemini_cli.py         # Gemini CLI backend
|     +-- codex_cli.py          # Codex CLI backend
|     +-- tools.py              # 7 tools (bash, read, write, edit, glob, grep, web_search) + shared tool loop
|     +-- __init__.py           # Backend factory (lazy singleton)
|
+-- channels/
|     +-- base.py               # Channel ABC
|     +-- email_in.py           # IMAP polling email input
|     +-- telegram/
|           +-- __init__.py     # TelegramChannel -- bot setup, handler registration
|           +-- commands.py     # All /command handlers
|           +-- callbacks.py    # Inline keyboard callback handlers
|           +-- streaming.py    # StreamingEditor for real-time message updates
|
+-- dispatcher/
|     +-- router.py             # Pattern matching for data card triggers
|     +-- cards.py              # Card formatting and rendering
|     +-- apis/                 # Weather, crypto, gold, markets, forex, trains, infra
|
+-- drafts/
|     +-- classifier.py         # Rule-based email classification
|     +-- queue.py              # Draft lifecycle (create, approve, reject, send)
|
+-- observers/                  # 7 autonomous background observers
+-- handlers/                   # Telegram message handlers (voice, photo, document, etc.)
+-- prompts/                    # System prompts and context
+-- tests/                      # Test suite
+-- nexus.service               # systemd unit file
+-- requirements.txt
```

---

## Configuration

All configuration is via `.env`. See `.env.example` for the full reference with all engine options, tiered by type.

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token from BotFather |
| `AUTHORIZED_USER_ID` | Yes | Your Telegram user ID (single-user auth) |
| `ENGINE_BACKEND` | No | Engine backend (default: `claude_code`) |
| `ANTHROPIC_API_KEY` | If using `anthropic_api` | Anthropic API key |
| `GEMINI_API_KEY` | If using `gemini_api` | Google Gemini API key |
| `GEMINI_API_MODEL` | No | Gemini model (default: `gemini-2.5-flash`) |
| `OLLAMA_URL` | No | Ollama server URL (default: `http://localhost:11434`) |
| `OLLAMA_MODEL` | No | Ollama model (default: `qwen3:235b`) |
| `OPENAI_COMPAT_URL` | If using `openai_compat` | OpenAI-compatible endpoint URL |
| `OPENAI_COMPAT_KEY` | No | API key for the OpenAI-compatible provider |
| `OPENAI_COMPAT_MODEL` | No | Model name (default: `gpt-4o`) |
| `CLAUDE_TIMEOUT` | No | Claude CLI timeout in seconds (default: `1800`) |

---

## Testing

```bash
python3 -m pytest tests/ -v
```

---

## Requirements

- Python 3.11+
- A Telegram bot token (via [@BotFather](https://t.me/BotFather))
- At least one engine backend configured

### Python Dependencies

```
python-telegram-bot>=21.0
python-dotenv>=1.0.0
aiohttp>=3.9.0
Pillow>=10.0.0
edge-tts>=6.1.0
```

---

## Contributors

<table>
  <tr>
    <td align="center">
      <a href="https://github.com/puretensor">
        <img src="https://github.com/puretensor.png" width="100px;" alt="PureTensor"/>
        <br />
        <sub><b>PureTensor</b></sub>
      </a>
      <br />
      Architecture, design, infrastructure
    </td>
  </tr>
</table>

---

## License

This project is provided as-is for reference and educational purposes. See the repository for details.
