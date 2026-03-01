# PureClaw

**Your AI agent. Your hardware. Your rules.**

PureClaw is a personal AI agent that lives in your Telegram app. It connects to whichever AI engine you choose — a local model running on your own GPU, or a cloud CLI like Claude, Codex, or Gemini. One bot, four engines, no lock-in.

[pureclaw.ai](https://pureclaw.ai) | [GitHub](https://github.com/puretensor/PureClaw)

---

## What It Does

You message your Telegram bot. PureClaw routes your message to whichever AI engine is active, streams the response back in real time, and gives the engine access to tools — it can run shell commands, read and write files, search the web, and more. It's like having Claude Code, Codex, or Gemini CLI in your pocket.

Beyond conversation, PureClaw runs background tasks (email monitoring, news briefs, infrastructure health checks), handles email drafts with approve/reject buttons, schedules reminders, and serves instant data cards for weather, markets, and trains — all from Telegram.

---

## Choose Your Engine

Four engines. Swap anytime with `/backend` in Telegram or one line in `.env`.

| Engine | What It Is | Tools | Cost |
|--------|-----------|-------|------|
| **Ollama** (default) | Any open model running locally via [Ollama](https://ollama.com) | 7 built-in tools | Free |
| **Claude Code** | Anthropic's [Claude Code](https://claude.ai/claude-code) CLI agent | Full agentic | Subscription |
| **Codex** | OpenAI's [Codex](https://openai.com/index/codex/) CLI agent | Full agentic | Subscription |
| **Gemini** | Google's [Gemini CLI](https://github.com/google-gemini/gemini-cli) agent | Full agentic | Subscription |

**Which should I pick?**

- **Just want to try it?** Start with **Ollama**. It's free, runs on your machine, and works out of the box with any GGUF model. Even a 7B parameter model on a laptop will work for basic conversation.
- **Want the best experience?** **Claude Code** is the most reliable for tool use, file operations, and multi-turn sessions. PureClaw is developed and tested against it.
- **Already pay for ChatGPT?** **Codex** gives you OpenAI's coding agent through the same Telegram interface.
- **Prefer Google?** **Gemini** connects to Google's CLI agent.

The CLI engines (Claude, Codex, Gemini) delegate all tool execution to their own binaries — they bring their own sandboxes, web search, and code execution. Ollama uses PureClaw's 7 built-in tools instead.

---

## Quick Start

### 1. Create your Telegram bot

Open Telegram, search for [@BotFather](https://t.me/BotFather), and send `/newbot`. Follow the prompts. You'll get a token like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`. Save it.

### 2. Find your Telegram user ID

Send a message to [@userinfobot](https://t.me/userinfobot) in Telegram. It will reply with your numeric user ID (e.g. `123456789`). This locks the bot to you — nobody else can use it.

### 3. Clone and install

```bash
git clone https://github.com/puretensor/PureClaw.git
cd PureClaw
pip install -r requirements.txt
```

### 4. Configure

```bash
cp .env.example .env
```

Open `.env` in a text editor and set these two required values:

```bash
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
AUTHORIZED_USER_ID=123456789
```

### 5. Set up your engine

Pick one (you can always add more later):

<details>
<summary><strong>Ollama (default, free, local)</strong></summary>

Install Ollama:
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Pull a model (pick one):
```bash
ollama pull llama3.2        # 3B, runs on any machine
ollama pull qwen3:8b        # 8B, good balance
ollama pull qwen3:32b       # 32B, needs ~20GB RAM
ollama pull qwen3:235b      # 235B MoE, needs serious GPU
```

Set it in `.env`:
```bash
ENGINE_BACKEND=ollama
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
```

**That's it.** Ollama is the default — if you don't set `ENGINE_BACKEND` at all, PureClaw uses Ollama.

</details>

<details>
<summary><strong>Claude Code (Anthropic, subscription)</strong></summary>

Install the Claude Code CLI:
```bash
npm install -g @anthropic-ai/claude-code
# or use the standalone installer:
curl -fsSL https://claude.ai/install.sh | sh
```

Authenticate (opens a browser):
```bash
claude login
```

Verify it works:
```bash
claude -p "Say hello"
```

Set it in `.env`:
```bash
ENGINE_BACKEND=claude_code
```

Claude Code uses your Anthropic subscription (Max plan). No API key needed — authentication is handled by the CLI.

</details>

<details>
<summary><strong>Codex (OpenAI, subscription or API credit)</strong></summary>

Install the Codex CLI:
```bash
npm install -g @openai/codex
```

Set your OpenAI API key as an environment variable:
```bash
export OPENAI_API_KEY=sk-proj-your-key-here
```

Verify it works:
```bash
codex exec "Say hello" --json --dangerously-bypass-approvals-and-sandbox
```

Set it in `.env`:
```bash
ENGINE_BACKEND=codex_cli
CODEX_MODEL=gpt-5.2-codex
OPENAI_API_KEY=sk-proj-your-key-here
```

</details>

<details>
<summary><strong>Gemini (Google, subscription)</strong></summary>

Install the Gemini CLI:
```bash
npm install -g @google/gemini-cli
```

Authenticate (opens a browser):
```bash
gemini auth
```

Verify it works:
```bash
gemini -p "Say hello" --output-format json --yolo
```

Set it in `.env`:
```bash
ENGINE_BACKEND=gemini_cli
```

</details>

### 6. Start

```bash
python3 nexus.py
```

Open your bot in Telegram and send a message. You should see a streaming response.

To run as a background service:
```bash
sudo cp nexus.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nexus.service
```

---

## Telegram Commands

### Conversation

| Command | What it does |
|---------|-------------|
| `/new [name]` | Archive current session, start fresh |
| `/session [name]` | List sessions, switch to one, or create new |
| `/session delete <name>` | Delete a named session |
| `/history` | List archived sessions |
| `/resume <n>` | Restore an archived session by number |
| `/status` | Show current engine, model, session info |

### Engine Switching

| Command | What it does |
|---------|-------------|
| `/backend` | Tap to select: Ollama, Claude Sonnet, Claude Opus, Codex, Gemini |
| `/sonnet` | Quick-switch to Claude Sonnet |
| `/opus` | Quick-switch to Claude Opus |
| `/ollama` | Quick-switch to local model |

### Memory

PureClaw remembers things across sessions. Memories persist to disk.

| Command | What it does |
|---------|-------------|
| `/remember <fact>` | Store a persistent memory (e.g. `/remember prefer dark mode`) |
| `/remember --infrastructure <fact>` | Store with a category (preferences, infrastructure, people, projects) |
| `/forget <key or number>` | Remove a memory |
| `/memories` | List all memories |
| `/memories infrastructure` | Filter by category |

### Scheduling

| Command | What it does |
|---------|-------------|
| `/schedule 5pm generate a status report` | Run a full AI prompt at 5pm |
| `/schedule daily 8am check my emails` | Recurring daily prompt |
| `/remind tomorrow 9am call the dentist` | Simple notification (no AI, just a ping) |
| `/cancel <n>` | Cancel a scheduled task by number |

Time formats: `5pm`, `9:30am`, `17:00`, `tomorrow 9am`, `monday`, `9 feb`, `in 30 minutes`, `daily 8am`, `weekly monday 9am`.

### Data Cards

Instant structured responses that bypass the AI engine for speed. No tokens used.

| Command | What it does |
|---------|-------------|
| `/weather [location]` | Weather with 3-day forecast (defaults to your configured location) |
| `/markets` | Stock indices, crypto, commodities, and FX — all in one card |
| `/trains [from] [to]` | UK train departures |
| `/nodes` | Infrastructure node status (requires Prometheus) |

### Voice

| Command | What it does |
|---------|-------------|
| `/voice on` | Enable voice responses (AI replies include audio) |
| `/voice off` | Disable voice responses |
| Send a voice note | Transcribed via Whisper, then processed by your AI engine |

### Email & Follow-ups

| Command | What it does |
|---------|-------------|
| `/drafts` | View pending email drafts with Approve / Reject buttons |
| `/followups` | List emails waiting for a reply |
| `/followups resolve <n>` | Mark a follow-up as resolved |

### Infrastructure

| Command | What it does |
|---------|-------------|
| `/check nodes` | Quick health check |
| `/restart <service> [node]` | Restart a service (with confirmation) |
| `/logs <service> [node] [n]` | Tail service logs |
| `/disk [node]` | Disk usage |
| `/top [node]` | System overview |
| `/deploy <site>` | Trigger deploy webhook (with confirmation) |
| `/calendar [today\|week]` | Google Calendar events |

---

## Features

### Streaming Responses

Responses stream in real time — you see the text appear word by word in Telegram, just like ChatGPT's interface. Tool usage (file reads, shell commands, web searches) shows live status updates.

### Tool Use

When using **Ollama**, PureClaw provides 7 built-in tools that the model can call:

| Tool | What it does |
|------|-------------|
| `bash` | Execute shell commands |
| `read_file` | Read file contents |
| `write_file` | Create or overwrite files |
| `edit_file` | Find-and-replace within files |
| `glob` | Find files by pattern |
| `grep` | Search file contents with regex |
| `web_search` | Search the web (SearXNG or DuckDuckGo) |

The CLI engines (Claude Code, Codex, Gemini) bring their own tools — they handle file operations, code execution, and web search through their own sandboxed environments.

### Email Draft Queue

PureClaw can monitor your email and draft replies. The workflow is human-in-the-loop:

```
Incoming email
  -> AI classifies and drafts a reply
  -> You get a Telegram notification with [Approve] [Reject] buttons
  -> Approve: sends the reply and creates a follow-up tracker
  -> Reject: discards the draft
```

You always have the final say. Nothing sends without your tap.

### Observers

Background tasks that run on schedules inside the PureClaw process. No external cron needed.

| Observer | Schedule | What it does |
|----------|----------|-------------|
| Email Digest | Every 30 min | Summarizes unread emails |
| Morning Brief | 7:30 AM weekdays | Combined email + weather + calendar briefing |
| Node Health | Every 5 min | Checks infrastructure via Prometheus |
| Daily Snippet | 8:00 AM weekdays | Geopolitical news brief from RSS feeds |
| Content Review | Every 2 hours | Reviews scheduled blog posts |
| Follow-up Reminder | 9:00 AM weekdays | Nags about unanswered outbound emails |
| Git Push | Always on | Webhook listener for git push event summaries |
| Email Responder | Every 5 min | Monitors inbox, generates draft replies for approval |

Observers are optional — they run if configured but won't break anything if their dependencies aren't set up.

### Agent Identity

Give your agent a name and personality:

```bash
AGENT_NAME=HAL
AGENT_PERSONALITY=You speak like HAL 9000. Calm, measured, precise.
```

The name and personality are injected into the system prompt for all engines.

---

## Architecture

```
nexus.py (entry point)
  |
  +-- Engine (backends/)
  |     +-- ollama             Local models with 7 built-in tools (default)
  |     +-- claude_code        Claude Code CLI
  |     +-- codex_cli          Codex CLI
  |     +-- gemini_cli         Gemini CLI
  |     +-- tools.py           Shared tool schemas + execution loop
  |
  +-- Channels
  |     +-- Telegram           Streaming, keyboards, voice, photos, documents
  |     +-- Email Input        IMAP polling, classification, draft generation
  |
  +-- Observers                Background tasks on cron schedules
  +-- Dispatcher               Instant data cards (weather, markets, trains, nodes)
  +-- Draft Queue              Email drafts with Telegram approve/reject
  +-- Scheduler                User-defined tasks and reminders
  +-- Memory                   Persistent key-value memory across sessions
```

---

## Configuration Reference

All configuration is in `.env`. Only two values are required.

### Required

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Your bot token from [@BotFather](https://t.me/BotFather) |
| `AUTHORIZED_USER_ID` | Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot)) |

### Engine Selection

| Variable | Default | Description |
|----------|---------|-------------|
| `ENGINE_BACKEND` | `ollama` | Which engine to use: `ollama`, `claude_code`, `codex_cli`, or `gemini_cli` |

### Ollama

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server address |
| `OLLAMA_MODEL` | `qwen3:235b` | Model name (must be pulled first) |
| `OLLAMA_TOOLS_ENABLED` | `true` | Enable/disable tool use |
| `OLLAMA_NUM_PREDICT` | `8192` | Max tokens per response |

### Claude Code

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_BIN` | auto-detected | Path to `claude` binary |
| `CLAUDE_CWD` | `/home/user` | Working directory for Claude |
| `CLAUDE_TIMEOUT` | `1800` | Timeout in seconds |

### Codex

| Variable | Default | Description |
|----------|---------|-------------|
| `CODEX_BIN` | auto-detected | Path to `codex` binary |
| `CODEX_MODEL` | (Codex default) | Model name (e.g. `gpt-5.2-codex`, `o3`) |
| `CODEX_CWD` | `/home/user` | Working directory |
| `OPENAI_API_KEY` | (none) | Your OpenAI API key |

### Gemini

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_BIN` | auto-detected | Path to `gemini` binary |
| `GEMINI_CLI_MODEL` | (Gemini default) | Model name (e.g. `gemini-2.5-flash`) |

### Optional

| Variable | Description |
|----------|-------------|
| `AGENT_NAME` | Your agent's name (shown in prompts) |
| `AGENT_PERSONALITY` | Personality injected into system prompt |
| `SEARXNG_URL` | Self-hosted SearXNG URL for private web search |
| `WEATHER_DEFAULT_LOCATION` | Default location for `/weather` |
| `PROMETHEUS_URL` | Prometheus server for `/nodes` and health monitoring |
| `WHISPER_URL` | Whisper API endpoint for voice transcription |

---

## Project Structure

```
PureClaw/
+-- nexus.py                    Entry point
+-- config.py                   Environment loading, system prompt
+-- db.py                       SQLite: sessions, drafts, follow-ups, tasks
+-- engine.py                   Engine abstraction (sync + streaming)
+-- memory.py                   Persistent memory system
+-- scheduler.py                Task scheduler (/schedule, /remind)
|
+-- backends/
|     +-- base.py               Backend Protocol definition
|     +-- ollama.py             Ollama backend with tool loop
|     +-- claude_code.py        Claude Code CLI backend
|     +-- codex_cli.py          Codex CLI backend
|     +-- gemini_cli.py         Gemini CLI backend
|     +-- tools.py              7 tools + shared execution loop
|     +-- __init__.py           Backend factory
|
+-- channels/
|     +-- telegram/             Bot setup, commands, callbacks, streaming
|     +-- email_in.py           IMAP polling email input
|
+-- dispatcher/                 Data cards (weather, markets, trains, nodes)
+-- drafts/                     Email draft queue with approve/reject
+-- observers/                  Background tasks (email, news, health, git)
+-- handlers/                   Telegram handlers (voice, photo, document, location)
+-- prompts/                    System prompts
+-- tests/                      Test suite (860+ tests)
```

---

## Testing

```bash
python3 -m pytest tests/ -v
```

---

## Requirements

- Python 3.11+
- A Telegram bot token
- At least one engine: Ollama installed, or a CLI tool (claude / codex / gemini) authenticated

### Python Dependencies

```
python-telegram-bot>=21.0
python-dotenv>=1.0.0
aiohttp>=3.9.0
Pillow>=10.0.0
edge-tts>=6.1.0
```

### System Dependencies (optional)

- **ffmpeg** — Required for voice output
- **Node.js 18+** — Required for CLI engines (claude, codex, gemini)
- **Ollama** — Required for the local engine

---

## Security

- **Single-user only** — locked to one Telegram user ID
- **No telemetry** — no analytics, no tracking, no phoning home
- **No cloud dependency** — Ollama keeps everything local; CLI engines use your own subscriptions
- **Human-in-the-loop email** — drafts require explicit approval before sending
- **Minimal dependencies** — stdlib HTTP, no bloated SDK chains
- **Your hardware** — not a managed platform, not a SaaS product

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

MIT License. See [LICENSE](LICENSE) for details.
