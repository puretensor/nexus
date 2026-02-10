# NEXUS

**Multi-channel agentic service powered by Claude Code.**

NEXUS is a personal AI operations platform that bridges multiple communication channels to [Claude Code](https://docs.anthropic.com/en/docs/claude-code). It accepts input from Telegram and email, runs autonomous background observers on cron schedules, manages an email draft approval queue, and provides real-time streaming responses — all from a single systemd service.

Built for infrastructure operators who want an always-on AI assistant that monitors systems, triages email, delivers intelligence briefs, and takes action on their behalf.

---

## Architecture

```
nexus.py (entry point)
  ├── Telegram Channel       — python-telegram-bot, streaming responses, inline keyboards
  ├── Email Input Channel    — IMAP polling → classify → Claude draft → Telegram approval
  ├── Observer Registry      — internal cron scheduler, 7 observers
  │   ├── EmailDigest        — summarise unread emails across 3 IMAP accounts
  │   ├── MorningBrief       — combined email + infra + weather + calendar briefing
  │   ├── NodeHealth         — Prometheus-based cluster health checks with escalation
  │   ├── DailySnippet       — Claude-powered geopolitical intelligence brief
  │   ├── BretalonReview     — content review pipeline with email delivery
  │   ├── FollowupReminder   — nag about unanswered outbound emails
  │   └── GitPush            — persistent HTTP webhook for Gitea push summaries
  ├── Draft Queue            — email drafts with Telegram Approve/Reject buttons
  ├── Dispatcher             — instant data cards (weather, crypto, markets, trains, infra)
  ├── Task Scheduler         — user-defined scheduled tasks and reminders
  └── Engine                 — Claude Code CLI wrapper (sync, async, streaming)
```

## Features

### Channels

- **Telegram** — Full conversational interface with streaming responses, real-time tool status updates, session continuity, model switching (`/opus`, `/sonnet`), voice input (Whisper transcription), photo/document analysis, and inline keyboards
- **Email Input** — Polls IMAP for incoming messages, classifies them (ignore / notify / auto-reply / follow-up), and generates Claude-drafted replies that require explicit Telegram approval before sending

### Observers

Autonomous background tasks running on cron schedules inside a single process. No external cron entries needed.

| Observer | Schedule | Description |
|----------|----------|-------------|
| `email_digest` | Every 30 min | Summarises unread emails from 3 accounts via Claude |
| `morning_brief` | 07:30 weekdays | Combined email + infrastructure + weather + calendar briefing |
| `node_health` | Every 5 min | Queries Prometheus for cluster health, escalates to Telegram |
| `daily_snippet` | 08:00 weekdays | RSS-powered geopolitical intelligence brief via Claude |
| `bretalon_review` | Every 2 hours | Content review pipeline with editorial email delivery |
| `followup_reminder` | 09:00 weekdays | Reminds about outbound emails that haven't received a reply |
| `git_push` | Persistent | HTTP webhook server (port 9876) for Gitea push event summaries |

### Draft Queue

Outbound email lifecycle with human-in-the-loop approval:

```
Incoming email → Classify → Claude generates draft reply
  → Telegram notification with [Approve] [Reject] buttons
    → Approve → Send via Gmail API → Auto-create follow-up tracker
    → Reject → Draft discarded
```

### Dispatcher (Data Cards)

Instant structured responses that bypass Claude for speed:

- Weather (OpenWeatherMap)
- Cryptocurrency prices (CoinGecko)
- Gold/silver spot prices
- Stock market indices
- Forex rates
- UK train departures (Darwin API)
- Infrastructure status (Prometheus)

### Telegram Commands

| Command | Description |
|---------|-------------|
| `/new` | Start a fresh Claude session |
| `/opus` | Switch to Opus model |
| `/sonnet` | Switch to Sonnet model |
| `/status` | Show system status and session info |
| `/schedule` | Schedule a task (e.g. `/schedule 2h remind me to check logs`) |
| `/remind` | Set a reminder |
| `/drafts` | List pending email drafts with approve/reject buttons |
| `/calendar` | Show today's calendar events (supports `week`, `upcoming`) |
| `/followups` | List active email follow-up trackers |
| `/help` | Show command reference |

---

## Project Structure

```
nexus/
├── nexus.py                    # Entry point — starts all subsystems
├── config.py                   # Environment loading, logging, constants
├── db.py                       # SQLite: sessions, drafts, followups, tasks, observer state
├── engine.py                   # Claude CLI wrapper (sync + async + streaming)
├── memory.py                   # Persistent memory injection
├── scheduler.py                # User task scheduler (/schedule, /remind)
│
├── channels/
│   ├── base.py                 # Channel ABC
│   ├── email_in.py             # IMAP polling email input
│   └── telegram/
│       ├── __init__.py         # TelegramChannel — bot setup, handler registration
│       ├── commands.py         # All /command handlers
│       ├── callbacks.py        # Inline keyboard callback handlers
│       └── streaming.py        # StreamingEditor for real-time message updates
│
├── dispatcher/
│   ├── router.py               # Pattern matching for data card triggers
│   ├── cards.py                # Card formatting and rendering
│   └── apis/                   # Weather, crypto, gold, markets, forex, trains, infra, status
│
├── drafts/
│   ├── classifier.py           # Rule-based email classification
│   └── queue.py                # Draft lifecycle (create, approve, reject, send)
│
├── observers/
│   ├── base.py                 # Observer ABC, ObserverContext, ObserverResult
│   ├── registry.py             # Cron-expression scheduler loop
│   ├── email_digest.py         # Email summary observer
│   ├── morning_brief.py        # Morning briefing (email + infra + weather + calendar)
│   ├── node_health.py          # Prometheus cluster health
│   ├── daily_snippet.py        # Geopolitical intelligence brief
│   ├── bretalon_review.py      # Content review pipeline
│   ├── followup_reminder.py    # Unanswered email reminders
│   └── git_push.py             # Gitea webhook handler (persistent)
│
├── handlers/                   # Telegram message handlers
│   ├── voice_tts.py            # Voice input (Whisper) + TTS output (edge-tts)
│   ├── photo.py                # Photo/image analysis
│   ├── document.py             # Document processing
│   ├── file_output.py          # File delivery to Telegram
│   ├── location.py             # Location-based responses
│   ├── keyboards.py            # Inline keyboard builders
│   └── summaries.py            # Conversation summarisation
│
├── prompts/                    # System prompts and context
├── tests/                      # 764 tests
├── nexus.service               # systemd unit file
└── requirements.txt
```

---

## Requirements

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Telegram bot token (via [@BotFather](https://t.me/BotFather))

### Python Dependencies

```
python-telegram-bot>=21.0
python-dotenv>=1.0.0
aiohttp>=3.9.0
Pillow>=10.0.0
edge-tts>=6.1.0
```

---

## Setup

1. **Clone the repository**

```bash
git clone https://github.com/puretensor/nexus.git
cd nexus
```

2. **Install dependencies**

```bash
pip install -r requirements.txt
```

3. **Configure environment**

```bash
cp .env.example .env
# Edit .env with your Telegram bot token and user ID
```

4. **Run directly**

```bash
python3 nexus.py
```

5. **Or deploy as a systemd service**

```bash
sudo cp nexus.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nexus.service
```

---

## Configuration

All configuration is via `.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token from BotFather |
| `AUTHORIZED_USER_ID` | Yes | Your Telegram user ID (single-user auth) |
| `CLAUDE_TIMEOUT` | No | Claude CLI timeout in seconds (default: 1800) |
| `CLAUDE_BIN` | No | Path to Claude CLI binary (default: `/usr/bin/claude`) |
| `CLAUDE_CWD` | No | Working directory for Claude sessions |
| `WEATHER_DEFAULT_LOCATION` | No | Default location for weather cards |
| `PROMETHEUS_URL` | No | Prometheus URL for infrastructure monitoring |

---

## Testing

```bash
python3 -m pytest tests/ -v
```

764 tests covering all observers, commands, handlers, the draft queue, email classifier, dispatcher, streaming engine, and Telegram callbacks.

---

## How It Works

NEXUS wraps the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) as its AI engine. When a message arrives (from Telegram or email), it's routed through the dispatcher for instant data cards or forwarded to Claude Code with full tool access — Claude can read/write files, run commands, search the web, and manage infrastructure through the same tools available in the CLI.

Responses stream back in real-time with live tool status updates (e.g., "Reading: /etc/hosts", "Running: kubectl get pods"), so you can see exactly what Claude is doing.

Observers run autonomously in a thread pool on cron schedules, using the same Claude engine for analysis and summarisation. The observer registry handles scheduling, cooldowns, and state persistence — no external cron jobs needed.

The email draft queue ensures Claude never sends email without explicit human approval. Drafts appear as Telegram messages with inline Approve/Reject buttons. Approved drafts are sent via the Gmail API and automatically tracked for follow-up.

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
    <td align="center">
      <a href="https://www.anthropic.com/claude">
        <img src="https://upload.wikimedia.org/wikipedia/commons/7/78/Anthropic_logo.svg" width="100px;" alt="Claude"/>
        <br />
        <sub><b>Claude</b></sub>
      </a>
      <br />
      Implementation, testing, documentation
    </td>
  </tr>
</table>

Built with [Claude Code](https://claude.com/claude-code) by [Anthropic](https://www.anthropic.com).

---

## License

This project is provided as-is for reference and educational purposes. See the repository for details.
