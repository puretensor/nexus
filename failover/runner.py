#!/usr/bin/env python3
"""Nexus Failover Runner — runs on fox-n1 when tensor-core is offline.

Fired by systemd timer at :05 past each hour. Steps:
  1. Health-check TC Ollama (if reachable → TC is handling it → exit)
  2. GCP dedup check (if briefing already exists for this hour → exit)
  3. Run observers with OLLAMA_URL="" (forces Gemini API fallback)
  4. Send one-time Telegram notification when failover activates

Env vars loaded from /opt/nexus-failover/.env (or .env in script dir).
"""

import json
import logging
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
NEXUS_DIR = SCRIPT_DIR.parent  # /opt/nexus-failover/nexus/ on fox-n1

# Load .env
for env_path in [SCRIPT_DIR / ".env", SCRIPT_DIR.parent / ".env"]:
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        break

# Ensure nexus is on sys.path for observer imports
if str(NEXUS_DIR) not in sys.path:
    sys.path.insert(0, str(NEXUS_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nexus-failover")

# Config
TC_OLLAMA_URL = os.environ.get("TC_OLLAMA_URL", "http://100.121.42.54:11434")
GCP_HOST = os.environ.get("GCP_SSH_HOST", "puretensorai@100.116.141.107")
STATE_DIR = Path(os.environ.get("OBSERVER_STATE_DIR", str(SCRIPT_DIR / "state")))
CYBER_WEBROOT = "/var/www/cyber.puretensor.ai"
INTEL_WEBROOT = "/var/www/intel.puretensor.ai"

# Telegram alert bot
ALERT_BOT_TOKEN = os.environ.get("ALERT_BOT_TOKEN", "")
ALERT_CHAT_ID = os.environ.get("AUTHORIZED_USER_ID", "22276981")


def send_telegram(text: str) -> None:
    """Send a Telegram notification."""
    token = ALERT_BOT_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.warning("No Telegram bot token — skipping notification")
        return
    try:
        import urllib.parse
        data = urllib.parse.urlencode({"chat_id": ALERT_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


# --------------------------------------------------------------------------
# Health check: is TC Ollama reachable?
# --------------------------------------------------------------------------

def tc_ollama_reachable() -> bool:
    """Check if tensor-core Ollama is responding."""
    try:
        req = urllib.request.Request(TC_OLLAMA_URL)
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# GCP dedup: has a briefing already been published this hour?
# --------------------------------------------------------------------------

def briefing_exists_on_gcp(webroot: str, pattern: str) -> bool:
    """Check if a file matching pattern exists on GCP."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
             GCP_HOST, f"ls {webroot}/briefings/{pattern} 2>/dev/null"],
            capture_output=True, text=True, timeout=15,
        )
        return bool(result.stdout.strip())
    except Exception as e:
        log.warning("GCP check failed for %s: %s", pattern, e)
        return False  # If we can't check, proceed with the run


def cyber_briefing_exists() -> bool:
    """Check if a cyber briefing for the current hour already exists."""
    now = datetime.now(timezone.utc)
    pattern = f"{now.strftime('%Y-%m-%d_%H')}*.html"
    return briefing_exists_on_gcp(CYBER_WEBROOT, pattern)


def intel_briefing_exists() -> bool:
    """Check if an intel briefing for today already exists (runs every 4h)."""
    now = datetime.now(timezone.utc)
    pattern = f"{now.strftime('%Y-%m-%d')}*.html"
    return briefing_exists_on_gcp(INTEL_WEBROOT, pattern)


def should_run_intel() -> bool:
    """Intel briefing only runs at hours 0, 4, 8, 12, 16, 20."""
    return datetime.now(timezone.utc).hour % 4 == 0


# --------------------------------------------------------------------------
# Run observers
# --------------------------------------------------------------------------

def run_cyber_observer() -> bool:
    """Run cyber_threat_feed observer with Gemini fallback."""
    log.info("Running cyber_threat_feed observer...")
    try:
        from observers.cyber_threat_feed import CyberThreatFeedObserver
        from observers.base import ObserverContext

        ctx = ObserverContext(state_dir=STATE_DIR)
        observer = CyberThreatFeedObserver()
        result = observer.run(ctx)

        if result.success:
            log.info("cyber_threat_feed: SUCCESS — %s", result.data.get("briefing_filename", ""))
            return True
        else:
            log.error("cyber_threat_feed: FAILED — %s", result.error)
            return False
    except Exception as e:
        log.error("cyber_threat_feed: EXCEPTION — %s", e, exc_info=True)
        return False


def run_intel_observer() -> bool:
    """Run intel_briefing observer with Gemini fallback."""
    log.info("Running intel_briefing observer...")
    try:
        from observers.intel_briefing import IntelBriefingObserver
        from observers.base import ObserverContext

        ctx = ObserverContext(state_dir=STATE_DIR)
        observer = IntelBriefingObserver()
        result = observer.run(ctx)

        if result.success:
            log.info("intel_briefing: SUCCESS — %s", result.data.get("title", ""))
            return True
        else:
            log.error("intel_briefing: FAILED — %s", result.error)
            return False
    except Exception as e:
        log.error("intel_briefing: EXCEPTION — %s", e, exc_info=True)
        return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    now = datetime.now(timezone.utc)
    log.info("Nexus failover runner — %s", now.strftime("%Y-%m-%d %H:%M UTC"))

    # 1. Health check: is TC handling things?
    if tc_ollama_reachable():
        log.info("TC Ollama is reachable — TC is handling observers, exiting")
        return 0

    log.info("TC Ollama unreachable — activating failover")

    # Force Gemini path by clearing Ollama URL
    os.environ["OLLAMA_URL"] = ""

    # Ensure state dir exists
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    ran_anything = False
    results = []

    # 2. Cyber threat feed (every hour)
    if cyber_briefing_exists():
        log.info("Cyber briefing already exists for this hour — skipping")
    else:
        ran_anything = True
        ok = run_cyber_observer()
        results.append(("cyber_threat_feed", ok))

    # 3. Intel briefing (every 4 hours)
    if should_run_intel():
        if intel_briefing_exists():
            log.info("Intel briefing already exists for today — skipping")
        else:
            ran_anything = True
            ok = run_intel_observer()
            results.append(("intel_briefing", ok))

    # 4. Telegram notification
    if ran_anything:
        status_parts = []
        for name, ok in results:
            status_parts.append(f"  {name}: {'OK' if ok else 'FAILED'}")
        status_text = "\n".join(status_parts)

        send_telegram(
            f"[NEXUS FAILOVER] TC offline, running via Gemini Flash\n"
            f"{now.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"{status_text}"
        )

        # Return non-zero if any observer failed
        if any(not ok for _, ok in results):
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
