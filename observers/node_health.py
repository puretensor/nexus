#!/usr/bin/env python3
"""Node health observer — checks Prometheus for down nodes.

Runs every 5 minutes via the observer registry. If any monitored node is down:
  1. Sends alert to Telegram
  2. Invokes Claude to investigate and attempt remediation
  3. Sends Claude's findings to Telegram with action buttons

Cooldown prevents repeat alerts for the same node within 30 minutes.
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from observers.base import Observer, ObserverResult
from config import PROMETHEUS_URL

log = logging.getLogger("nexus")


class NodeHealthObserver(Observer):
    """Monitor Prometheus targets and alert on down nodes."""

    name = "node_health"
    schedule = "*/5 * * * *"

    # -- Class attributes --

    PROMETHEUS_URL = PROMETHEUS_URL
    COOLDOWN_SECONDS = 1800  # 30 min between alerts for the same node
    STATE_DIR = Path(__file__).parent / ".state"

    # -- Prometheus query --

    def query_prometheus(self, query: str) -> dict:
        """Run an instant PromQL query."""
        url = f"{self.PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(query)}"
        resp = urllib.request.urlopen(url, timeout=10)
        return json.loads(resp.read())

    # -- Cooldown management --

    def check_cooldown(self, node_key: str) -> bool:
        """Return True if we're clear to alert (not in cooldown)."""
        self.STATE_DIR.mkdir(exist_ok=True)
        state_file = self.STATE_DIR / f"{node_key}.alert"
        if state_file.exists():
            age = time.time() - state_file.stat().st_mtime
            if age < self.COOLDOWN_SECONDS:
                return False
        return True

    def set_cooldown(self, node_key: str) -> None:
        """Mark that we just alerted for this node."""
        self.STATE_DIR.mkdir(exist_ok=True)
        (self.STATE_DIR / f"{node_key}.alert").touch()

    # -- Remediation helpers --

    def get_remediation_commands(self, instance: str) -> list[str]:
        """Return a list of suggested remediation commands for a down node."""
        ip = instance.split(":")[0]
        return [
            f"ping -c 3 {ip}",
            f"ssh {ip} 'systemctl status prometheus-node-exporter'",
            f"ssh {ip} 'systemctl restart prometheus-node-exporter'",
            f"ssh {ip} 'uptime'",
        ]

    def save_escalation_context(self, down_nodes: list[dict], claude_response: str) -> None:
        """Save escalation context for the bot's callback handler."""
        context_file = self.STATE_DIR / "last_escalation.json"
        self.STATE_DIR.mkdir(exist_ok=True)
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "down_nodes": down_nodes,
            "investigation": claude_response[:2000],  # truncate for storage
        }
        with open(context_file, "w") as f:
            json.dump(data, f)

    # -- Main observer logic --

    def run(self, ctx=None) -> ObserverResult:
        """Check Prometheus for down targets, alert and investigate."""

        # Query Prometheus for down targets
        try:
            data = self.query_prometheus("up == 0")
        except Exception as e:
            log.error("Prometheus query failed: %s", e)
            return ObserverResult(
                success=False,
                error=f"Prometheus query failed: {e}",
            )

        results = data.get("data", {}).get("result", [])

        if not results:
            log.info("All nodes up")
            return ObserverResult(success=True)

        # Check cooldowns — only alert for nodes not in cooldown
        down_nodes = []
        for r in results:
            instance = r["metric"].get("instance", "unknown")
            job = r["metric"].get("job", "unknown")
            node_key = f"{job}_{instance}".replace(":", "_").replace(".", "_")

            if self.check_cooldown(node_key):
                down_nodes.append({"instance": instance, "job": job, "key": node_key})

        if not down_nodes:
            log.info("Down nodes in cooldown, skipping")
            return ObserverResult(success=True)

        # Build alert
        alert_lines = [f"  - {n['instance']} (job: {n['job']})" for n in down_nodes]
        alert_text = "NODES DOWN:\n" + "\n".join(alert_lines)
        log.warning(alert_text)

        # Send raw alert to Telegram
        timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
        self.send_telegram(f"[{timestamp}] ALERT\n\n{alert_text}")

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

        log.info("Invoking Claude to investigate %d down nodes...", len(down_nodes))
        claude_response = self.call_claude(prompt)

        # Save escalation context for the bot's callback handler
        self.save_escalation_context(down_nodes, claude_response)

        # Build action buttons for each down node
        buttons = []
        for n in down_nodes:
            instance = n["instance"]
            buttons.append([
                {"text": "\U0001f527 Auto-fix " + instance, "callback_data": f"escalation:fix:{instance}"},
                {"text": "\U0001f4cb Commands", "callback_data": f"escalation:commands:{instance}"},
            ])
        buttons.append([
            {"text": "\u23ed Ignore", "callback_data": "escalation:ignore"},
        ])

        keyboard = {"inline_keyboard": buttons}

        # Send Claude's analysis to Telegram with action buttons
        # Use the raw Telegram API for reply_markup support
        from config import BOT_TOKEN, AUTHORIZED_USER_ID

        payload = {
            "chat_id": str(AUTHORIZED_USER_ID),
            "text": f"[{timestamp}] INVESTIGATION\n\n{claude_response}"[:4000],
            "reply_markup": json.dumps(keyboard),
        }
        req_data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=req_data
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            log.warning("Failed to send investigation with buttons: %s", e)

        # Set cooldowns
        for n in down_nodes:
            self.set_cooldown(n["key"])

        return ObserverResult(
            success=True,
            message=alert_text,
            data={"down_nodes": down_nodes},
        )


# ---------------------------------------------------------------------------
# Standalone execution for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    observer = NodeHealthObserver()
    result = observer.run()

    if result.success:
        if result.message:
            print(result.message)
        else:
            print(f"{datetime.now(timezone.utc).isoformat()} — All nodes up")
    else:
        print(f"ERROR: {result.error}", file=sys.stderr)
        sys.exit(1)
