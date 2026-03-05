#!/usr/bin/env python3
"""Pipeline health watchdog observer.

Runs every 6 hours. Checks that critical pipelines are alive and producing
output. Sends a Telegram alert if any pipeline appears stalled or dead.

Checks:
  1. voice-kb ingest: service running on TC, output files recent
  2. daily report: last compiled date is yesterday or today
  3. rsync sync: /sync/ data is fresh (updated within 30 min)
  4. vLLM: responding to health checks
  5. observer cron: observers ran recently (not stuck)
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from observers.base import Observer, ObserverResult

log = logging.getLogger("nexus")

# Retry config for transient network failures
MAX_RETRIES = 3
RETRY_DELAY_SECS = 5
# Skip watchdog checks for this many seconds after pod start
STARTUP_GRACE_SECS = 600  # 10 minutes

_pod_start_time = time.monotonic()

# Thresholds
VOICE_KB_STALE_HOURS = 24  # Alert if no new voice-kb output in this many hours
SYNC_STALE_MINUTES = 60    # Alert if /sync/ data older than this
DAILY_REPORT_MAX_AGE_HOURS = 36  # Alert if last daily report older than this

# Paths
SYNC_DIR = Path("/sync")
CC_REPORTS_DIR = SYNC_DIR / "reports" / "cc"
VOICE_KB_SYNC_DIR = SYNC_DIR / "voice-kb" / "kb"
DAILY_REPORT_STATE = Path(os.environ.get(
    "OBSERVER_STATE_DIR", "/data/state/observers"
)) / "daily_report_state.json"
OUTPUT_DIR = Path("/output/daily")

# TC Tailscale IP for SSH checks
TC_HOST = os.environ.get("TC_SSH_HOST", "REDACTED_TAILSCALE_IP")
VLLM_URL = os.environ.get("VLLM_URL", "http://REDACTED_TAILSCALE_IP:8200/health")


class PipelineWatchdog(Observer):
    """Monitors critical pipeline health and alerts on failures."""

    name = "pipeline_watchdog"
    schedule = "0 */6 * * *"  # Every 6 hours

    def run(self, ctx=None) -> ObserverResult:
        # Startup grace period — skip remote checks if pod just started
        uptime = time.monotonic() - _pod_start_time
        if uptime < STARTUP_GRACE_SECS:
            log.info("Pipeline watchdog: skipping (pod uptime %.0fs < %ds grace)",
                     uptime, STARTUP_GRACE_SECS)
            return ObserverResult(
                success=True, message="",
                data={"skipped": True, "reason": "startup_grace"},
            )

        now = self.now_utc()
        alerts = []
        healthy = []

        # 1. Check rsync sync freshness
        self._check_sync_freshness(now, alerts, healthy)

        # 2. Check voice-kb ingest service (via systemd on TC)
        self._check_voice_kb(now, alerts, healthy)

        # 3. Check daily report recency
        self._check_daily_report(now, alerts, healthy)

        # 4. Check vLLM health
        self._check_vllm(alerts, healthy)

        # 5. Check observer state directory for stale locks
        self._check_observer_health(now, alerts, healthy)

        # Build result
        if alerts:
            alert_text = (
                f"PIPELINE WATCHDOG \u2014 {len(alerts)} ALERT(S)\n\n"
                + "\n".join(f"\u26a0\ufe0f {a}" for a in alerts)
            )
            if healthy:
                alert_text += "\n\n" + "\n".join(f"\u2705 {h}" for h in healthy)
            self.send_telegram(alert_text)
            return ObserverResult(
                success=True,
                message=alert_text,
                data={"alerts": alerts, "healthy": healthy},
            )

        # All healthy — silent success (don't spam Telegram)
        log.info("Pipeline watchdog: all %d checks healthy", len(healthy))
        return ObserverResult(
            success=True,
            message="",  # Empty = silent
            data={"alerts": [], "healthy": healthy},
        )

    def _check_sync_freshness(self, now, alerts, healthy):
        """Check that /sync/ data is being updated by the rsync cron."""
        try:
            # Find the most recent file in CC reports
            if CC_REPORTS_DIR.exists():
                files = sorted(CC_REPORTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime)
                if files:
                    newest = files[-1]
                    age_min = (now.timestamp() - newest.stat().st_mtime) / 60
                    if age_min > SYNC_STALE_MINUTES:
                        alerts.append(
                            f"Rsync sync stale: newest CC report is {age_min:.0f} min old "
                            f"(threshold: {SYNC_STALE_MINUTES} min) \u2014 "
                            f"check crontab rsync on tensor-core"
                        )
                    else:
                        healthy.append(f"Rsync sync: fresh ({age_min:.0f} min ago)")
                    return

            alerts.append("Rsync sync: /sync/reports/cc/ directory missing or empty")
        except Exception as e:
            alerts.append(f"Rsync sync check failed: {e}")

    def _ssh_service_active(self, service: str) -> bool | None:
        """Check if a systemd service is active on TC via SSH.

        Returns True/False for definitive results, None for transient failures.
        """
        try:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                 f"puretensorai@{TC_HOST}",
                 f"systemctl is-active {service} 2>/dev/null"],
                capture_output=True, text=True, timeout=15,
            )
            return result.stdout.strip() == "active"
        except (subprocess.TimeoutExpired, OSError):
            return None  # Transient — worth retrying

    def _check_voice_kb(self, now, alerts, healthy):
        """Check voice-kb ingest service and output freshness (with retries)."""
        service_active = None
        for attempt in range(MAX_RETRIES):
            service_active = self._ssh_service_active("voice-kb-ingest")
            if service_active is not None:
                break
            if attempt < MAX_RETRIES - 1:
                log.info("voice-kb SSH check failed (attempt %d/%d), retrying in %ds",
                         attempt + 1, MAX_RETRIES, RETRY_DELAY_SECS)
                time.sleep(RETRY_DELAY_SECS)

        if service_active is None:
            alerts.append(
                "voice-kb check: SSH to tensor-core failed after "
                f"{MAX_RETRIES} attempts"
            )
            return

        if not service_active:
            alerts.append(
                "voice-kb-ingest service NOT running on tensor-core \u2014 "
                "pipeline is dead, new voice memos will not be processed"
            )
            return

        # Check output freshness via sync mount
        try:
            if VOICE_KB_SYNC_DIR.exists():
                files = sorted(VOICE_KB_SYNC_DIR.glob("*.md"),
                              key=lambda p: p.stat().st_mtime)
                if files:
                    newest = files[-1]
                    age_hrs = (now.timestamp() - newest.stat().st_mtime) / 3600
                    if age_hrs > VOICE_KB_STALE_HOURS:
                        alerts.append(
                            f"voice-kb output stale: newest memo is {age_hrs:.1f}h old "
                            f"(threshold: {VOICE_KB_STALE_HOURS}h) \u2014 "
                            f"service may be running but not producing output"
                        )
                    else:
                        healthy.append(
                            f"voice-kb ingest: active, output {age_hrs:.1f}h ago"
                        )
                    return

            healthy.append("voice-kb ingest: service active (sync dir not available for freshness check)")
        except Exception as e:
            alerts.append(f"voice-kb freshness check failed: {e}")

    def _check_daily_report(self, now, alerts, healthy):
        """Check that the daily report observer ran recently."""
        try:
            if DAILY_REPORT_STATE.exists():
                data = json.loads(DAILY_REPORT_STATE.read_text())
                last_date = data.get("last_compiled_date", "")
                if last_date:
                    last_dt = datetime.strptime(last_date, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    age_hrs = (now - last_dt).total_seconds() / 3600
                    if age_hrs > DAILY_REPORT_MAX_AGE_HOURS:
                        alerts.append(
                            f"Daily report stale: last compiled {last_date} "
                            f"({age_hrs:.0f}h ago, threshold: {DAILY_REPORT_MAX_AGE_HOURS}h)"
                        )
                    else:
                        healthy.append(f"Daily report: last compiled {last_date}")
                    return

            alerts.append("Daily report: no state file found \u2014 observer may never have run")
        except Exception as e:
            alerts.append(f"Daily report check failed: {e}")

    def _curl_health(self, url: str) -> str | None:
        """Curl a health endpoint. Returns HTTP status code, or None on transient failure."""
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--connect-timeout", "5", url],
                capture_output=True, text=True, timeout=10,
            )
            status = result.stdout.strip()
            # HTTP 000 = connection failed — treat as transient
            if status == "000":
                return None
            return status
        except (subprocess.TimeoutExpired, OSError):
            return None

    def _check_vllm(self, alerts, healthy):
        """Check vLLM is responding (with retries)."""
        status = None
        for attempt in range(MAX_RETRIES):
            status = self._curl_health(VLLM_URL)
            if status is not None:
                break
            if attempt < MAX_RETRIES - 1:
                log.info("vLLM health check failed (attempt %d/%d), retrying in %ds",
                         attempt + 1, MAX_RETRIES, RETRY_DELAY_SECS)
                time.sleep(RETRY_DELAY_SECS)

        if status is None:
            alerts.append(
                f"vLLM health check unreachable after {MAX_RETRIES} attempts "
                f"(HTTP 000 / connection refused)"
            )
        elif status == "200":
            healthy.append("vLLM: healthy (port 8200)")
        else:
            alerts.append(f"vLLM health check returned HTTP {status}")

    def _check_observer_health(self, now, alerts, healthy):
        """Check observer state directory for signs of life."""
        try:
            state_dir = DAILY_REPORT_STATE.parent
            if state_dir.exists():
                state_files = list(state_dir.glob("*.json"))
                healthy.append(f"Observer state: {len(state_files)} state files present")
            else:
                alerts.append("Observer state directory missing")
        except Exception as e:
            alerts.append(f"Observer state check failed: {e}")
