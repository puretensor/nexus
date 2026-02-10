#!/usr/bin/env python3
"""Node health observer — checks Prometheus for down nodes.

Runs via cron. If any monitored node is down:
  1. Sends alert to Telegram
  2. Invokes Claude to investigate and attempt remediation
  3. Sends Claude's findings to Telegram

Cooldown prevents repeat alerts for the same node within 30 minutes.
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — all paths relative to this script
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
ENV_PATH = PROJECT_DIR / ".env"
STATE_DIR = SCRIPT_DIR / ".state"
COOLDOWN_SECONDS = 1800  # 30 min between alerts for the same node

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://MON2_TAILSCALE_IP:9090")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", str(Path.home()))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_env():
    """Parse .env file into a dict."""
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def send_telegram(token, chat_id, text, reply_markup=None):
    """Send a message via Telegram Bot API with optional inline keyboard."""
    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, 4000)
        if idx == -1:
            idx = 4000
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")

    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        # Only attach keyboard to the last chunk
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(reply_markup)
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=15)


def query_prometheus(query):
    """Run an instant PromQL query."""
    url = f"{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(query)}"
    resp = urllib.request.urlopen(url, timeout=10)
    return json.loads(resp.read())


def check_cooldown(node_key):
    """Return True if we're clear to alert (not in cooldown)."""
    STATE_DIR.mkdir(exist_ok=True)
    state_file = STATE_DIR / f"{node_key}.alert"
    if state_file.exists():
        age = time.time() - state_file.stat().st_mtime
        if age < COOLDOWN_SECONDS:
            return False
    return True


def set_cooldown(node_key):
    """Mark that we just alerted for this node."""
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"{node_key}.alert").touch()


def call_claude(message, model="sonnet"):
    """Invoke claude -p and return the result text."""
    cmd = [
        CLAUDE_BIN,
        "-p", message,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", model,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, cwd=CLAUDE_CWD
        )
    except subprocess.TimeoutExpired:
        return "Claude timed out after 300s"

    if result.returncode != 0:
        return f"Claude error (exit {result.returncode}): {result.stderr[:500]}"

    try:
        data = json.loads(result.stdout)
        return data.get("result", "(empty response)")
    except json.JSONDecodeError:
        return f"Failed to parse Claude output: {result.stdout[:500]}"


def get_remediation_commands(instance):
    """Return a list of suggested remediation commands for a down node."""
    ip = instance.split(":")[0]
    commands = [
        f"ping -c 3 {ip}",
        f"ssh {ip} 'systemctl status prometheus-node-exporter'",
        f"ssh {ip} 'systemctl restart prometheus-node-exporter'",
        f"ssh {ip} 'uptime'",
    ]
    return commands


def save_escalation_context(down_nodes, claude_response):
    """Save escalation context for the bot's callback handler."""
    context_file = STATE_DIR / "last_escalation.json"
    STATE_DIR.mkdir(exist_ok=True)
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "down_nodes": down_nodes,
        "investigation": claude_response[:2000],  # truncate for storage
    }
    with open(context_file, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    env = load_env()
    token = env["TELEGRAM_BOT_TOKEN"]
    chat_id = env["AUTHORIZED_USER_ID"]

    # Query Prometheus for down targets
    try:
        data = query_prometheus("up == 0")
    except Exception as e:
        print(f"Prometheus query failed: {e}", file=sys.stderr)
        sys.exit(1)

    results = data.get("data", {}).get("result", [])

    if not results:
        print(f"{datetime.now(timezone.utc).isoformat()} — All nodes up")
        sys.exit(0)

    # Check cooldowns
    down_nodes = []
    for r in results:
        instance = r["metric"].get("instance", "unknown")
        job = r["metric"].get("job", "unknown")
        node_key = f"{job}_{instance}".replace(":", "_").replace(".", "_")

        if check_cooldown(node_key):
            down_nodes.append({"instance": instance, "job": job, "key": node_key})

    if not down_nodes:
        print(f"{datetime.now(timezone.utc).isoformat()} — Down nodes in cooldown, skipping")
        sys.exit(0)

    # Build alert
    alert_lines = [f"  - {n['instance']} (job: {n['job']})" for n in down_nodes]
    alert_text = "NODES DOWN:\n" + "\n".join(alert_lines)
    print(alert_text)

    # Send raw alert to Telegram
    timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    send_telegram(token, chat_id, f"[{timestamp}] ALERT\n\n{alert_text}")

    # Invoke Claude to investigate
    prompt = (
        f"{alert_text}\n\n"
        "Investigate these down nodes. For each:\n"
        "1. Ping the IP (extract from instance)\n"
        "2. Try SSH if ping succeeds\n"
        "3. Determine if it's a node_exporter issue vs actual node failure\n"
        "4. Attempt remediation if possible (restart exporter, etc.)\n"
        "5. Summarise findings concisely\n\n"
        "This is an automated alert from the HAL Claude observer system."
    )

    print("Invoking Claude to investigate...")
    claude_response = call_claude(prompt)

    # Save escalation context for the bot's callback handler
    save_escalation_context(down_nodes, claude_response)

    # Build action buttons for each down node
    buttons = []
    for n in down_nodes:
        instance = n["instance"]
        node_name = instance.split(":")[0].replace("192.168.4.", "")
        buttons.append([
            {"text": f"\U0001f527 Auto-fix {instance}", "callback_data": f"escalation:fix:{instance}"},
            {"text": "\U0001f4cb Commands", "callback_data": f"escalation:commands:{instance}"},
        ])
    buttons.append([
        {"text": "\u23ed Ignore", "callback_data": "escalation:ignore"},
    ])

    keyboard = {"inline_keyboard": buttons}

    # Send Claude's analysis to Telegram with action buttons
    send_telegram(
        token, chat_id,
        f"[{timestamp}] INVESTIGATION\n\n{claude_response}",
        reply_markup=keyboard,
    )

    # Set cooldowns
    for n in down_nodes:
        set_cooldown(n["key"])

    print("Done")


if __name__ == "__main__":
    main()
