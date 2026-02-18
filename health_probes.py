"""TC health probes — async background monitoring of tensor-core services.

Checks Whisper and TTS endpoints on tensor-core via Tailscale IP.
Services are marked offline after 3 consecutive failures, online after 1 success.
Used by Telegram voice handlers for graceful failover.
"""

import asyncio
import logging
import os
import time

import aiohttp

log = logging.getLogger("nexus")

# Probe config
_WHISPER_URL = os.environ.get("WHISPER_URL", "http://TC_TAILSCALE_IP:9000/transcribe")
_TTS_URL = os.environ.get("TTS_URL", "http://TC_TAILSCALE_IP:5580/tts")
_PROBE_INTERVAL = int(os.environ.get("HEALTH_PROBE_INTERVAL", "30"))
_FAILURE_THRESHOLD = 3


class TCHealthProbe:
    """Background health checker for tensor-core services."""

    def __init__(self):
        self._whisper_online = True
        self._tts_online = True
        self._whisper_failures = 0
        self._tts_failures = 0
        self._running = False

    @property
    def whisper_online(self) -> bool:
        return self._whisper_online

    @property
    def tts_online(self) -> bool:
        return self._tts_online

    async def _check_endpoint(self, url: str, name: str) -> bool:
        """HTTP GET to an endpoint, return True if reachable."""
        # Extract base URL (no path) for a lightweight check
        from urllib.parse import urlparse
        parsed = urlparse(url)
        check_url = f"{parsed.scheme}://{parsed.netloc}/"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    check_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    # Any response (even 404/405) means the service is running
                    return True
        except Exception:
            return False

    async def _probe_once(self):
        """Run one probe cycle for both services."""
        whisper_ok = await self._check_endpoint(_WHISPER_URL, "whisper")
        tts_ok = await self._check_endpoint(_TTS_URL, "tts")

        # Whisper
        if whisper_ok:
            if not self._whisper_online:
                log.info("TC Whisper is back ONLINE")
            self._whisper_online = True
            self._whisper_failures = 0
        else:
            self._whisper_failures += 1
            if self._whisper_failures >= _FAILURE_THRESHOLD and self._whisper_online:
                log.warning("TC Whisper marked OFFLINE after %d failures", self._whisper_failures)
                self._whisper_online = False

        # TTS
        if tts_ok:
            if not self._tts_online:
                log.info("TC TTS is back ONLINE")
            self._tts_online = True
            self._tts_failures = 0
        else:
            self._tts_failures += 1
            if self._tts_failures >= _FAILURE_THRESHOLD and self._tts_online:
                log.warning("TC TTS marked OFFLINE after %d failures", self._tts_failures)
                self._tts_online = False

    async def run_loop(self):
        """Main probe loop — runs until cancelled."""
        self._running = True
        log.info("TC health probe started (interval=%ds, threshold=%d)", _PROBE_INTERVAL, _FAILURE_THRESHOLD)

        while self._running:
            try:
                await self._probe_once()
            except Exception as e:
                log.warning("Health probe error: %s", e)
            await asyncio.sleep(_PROBE_INTERVAL)

    def stop(self):
        self._running = False


# Module-level singleton
_probe: TCHealthProbe | None = None


def get_probe() -> TCHealthProbe:
    """Get or create the singleton health probe."""
    global _probe
    if _probe is None:
        _probe = TCHealthProbe()
    return _probe


def is_tc_whisper_online() -> bool:
    """Check if TC Whisper is reachable. Returns True if probe hasn't started yet."""
    if _probe is None:
        return True  # Assume online until proven otherwise
    return _probe.whisper_online


def is_tc_tts_online() -> bool:
    """Check if TC TTS is reachable. Returns True if probe hasn't started yet."""
    if _probe is None:
        return True
    return _probe.tts_online
