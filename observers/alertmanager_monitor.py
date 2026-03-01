"""Alertmanager monitor — forwards firing/resolved alerts to Telegram.

Polls Alertmanager every 5 minutes for active alerts. Tracks seen alert
fingerprints so only new firing and newly resolved alerts are reported.
"""

import json
import logging
import os
import urllib.request
from pathlib import Path

from observers.base import Observer, ObserverContext, ObserverResult
import config

log = logging.getLogger("nexus")

ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "")


class AlertmanagerMonitorObserver(Observer):
    """Poll Alertmanager for firing alerts and send to Telegram."""

    name = "alertmanager_monitor"
    schedule = "*/5 * * * *"

    STATE_DIR = Path(__file__).parent / ".state"
    SEEN_FILE = STATE_DIR / "alertmanager_seen.json"

    def _load_seen(self) -> dict:
        """Load previously seen alert fingerprints with their status.

        Returns {fingerprint: {"name": str, "status": str, "labels": dict}}
        """
        if self.SEEN_FILE.exists():
            try:
                return json.loads(self.SEEN_FILE.read_text())
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def _save_seen(self, seen: dict) -> None:
        self.STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.SEEN_FILE.write_text(json.dumps(seen, indent=2))

    def _fetch_alerts(self) -> list[dict]:
        """GET /api/v2/alerts from Alertmanager."""
        url = f"{ALERTMANAGER_URL}/api/v2/alerts?active=true&silenced=false&inhibited=false"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            log.warning("Alertmanager fetch failed: %s", e)
            return []

    def _format_alert(self, alert: dict, resolved: bool = False) -> str:
        """Format a single alert for Telegram."""
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})

        name = labels.get("alertname", "Unknown")
        severity = labels.get("severity", "unknown")
        instance = labels.get("instance", "")
        job = labels.get("job", "")
        summary = annotations.get("summary", annotations.get("description", ""))

        if resolved:
            prefix = "RESOLVED"
        else:
            prefix = "FIRING" if severity != "critical" else "CRITICAL"

        parts = [f"[{prefix}] {name}"]
        if instance:
            parts.append(f"Instance: {instance}")
        if job:
            parts.append(f"Job: {job}")
        if summary:
            parts.append(summary[:200])

        return "\n".join(parts)

    def run(self, ctx: ObserverContext = None) -> ObserverResult:
        """Check for new or resolved alerts."""
        alerts = self._fetch_alerts()
        seen = self._load_seen()

        # Current firing fingerprints
        current_fps = {}
        for alert in alerts:
            fp = alert.get("fingerprint", "")
            if not fp:
                continue
            current_fps[fp] = alert

        # Find new alerts (not previously seen or previously resolved)
        new_alerts = []
        for fp, alert in current_fps.items():
            if fp not in seen or seen[fp].get("status") == "resolved":
                new_alerts.append(alert)

        # Find resolved alerts (previously firing, now gone)
        resolved_alerts = []
        for fp, info in seen.items():
            if info.get("status") == "firing" and fp not in current_fps:
                resolved_alerts.append(info)

        # Send notifications
        messages = []
        for alert in new_alerts:
            messages.append(self._format_alert(alert, resolved=False))
        for info in resolved_alerts:
            messages.append(self._format_alert(info, resolved=True))

        if messages:
            text = "\n\n---\n\n".join(messages)
            self.send_telegram(f"[ALERTMANAGER]\n\n{text}", token=config.ALERT_BOT_TOKEN)

        # Update seen state
        new_seen = {}
        for fp, alert in current_fps.items():
            new_seen[fp] = {
                "name": alert.get("labels", {}).get("alertname", "Unknown"),
                "status": "firing",
                "labels": alert.get("labels", {}),
                "annotations": alert.get("annotations", {}),
            }
        # Mark previously firing alerts as resolved (keep for one cycle)
        for fp, info in seen.items():
            if fp not in current_fps and info.get("status") == "firing":
                info["status"] = "resolved"
                new_seen[fp] = info
            # Drop alerts that were already resolved last cycle (cleanup)

        self._save_seen(new_seen)

        total_new = len(new_alerts) + len(resolved_alerts)
        if total_new > 0:
            log.info("Alertmanager: %d new, %d resolved", len(new_alerts), len(resolved_alerts))
            return ObserverResult(
                success=True,
                message=f"Sent {total_new} alert notifications",
                data={"new": len(new_alerts), "resolved": len(resolved_alerts)},
            )

        return ObserverResult(
            success=True,
            data={"firing": len(current_fps), "new": 0, "resolved": 0},
        )


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    observer = AlertmanagerMonitorObserver()
    result = observer.run()
    if result.message:
        print(f"OK: {result.message}")
    else:
        print(f"OK: {result.data}")
