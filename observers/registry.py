"""Observer registry — runs observers on cron schedules.

Uses a simple asyncio loop that checks every 30 seconds whether any observer
is due. Observers run in a thread pool (they use sync I/O).
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from observers.base import ALERT_BOT_TOKEN, Observer, ObserverContext, ObserverResult

log = logging.getLogger("nexus")

# Thread pool for running observers (sync I/O in threads)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="observer")


def _match_cron_field(field_expr: str, value: int, max_val: int) -> bool:
    """Check if a single cron field matches the given value.

    Supports: * (any), N (exact), */N (step), N-M (range), N-M/S (range+step),
    and comma-separated lists of any of the above.
    """
    for part in field_expr.split(","):
        part = part.strip()
        if part == "*":
            return True

        # Step: */N or N-M/S
        if "/" in part:
            range_part, step_str = part.split("/", 1)
            step = int(step_str)
            if range_part == "*":
                if value % step == 0:
                    return True
            elif "-" in range_part:
                lo, hi = range_part.split("-", 1)
                if int(lo) <= value <= int(hi) and (value - int(lo)) % step == 0:
                    return True
            continue

        # Range: N-M
        if "-" in part:
            lo, hi = part.split("-", 1)
            if int(lo) <= value <= int(hi):
                return True
            continue

        # Exact match
        if int(part) == value:
            return True

    return False


def matches_cron(cron_expr: str, dt: datetime) -> bool:
    """Check if a datetime matches a 5-field cron expression.

    Fields: minute hour day-of-month month day-of-week
    Day-of-week: 0=Monday ... 6=Sunday (Python convention)
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        log.warning("Invalid cron expression (need 5 fields): %s", cron_expr)
        return False

    minute, hour, dom, month, dow = fields
    return (
        _match_cron_field(minute, dt.minute, 59)
        and _match_cron_field(hour, dt.hour, 23)
        and _match_cron_field(dom, dt.day, 31)
        and _match_cron_field(month, dt.month, 12)
        and _match_cron_field(dow, dt.weekday(), 6)
    )


class ObserverRegistry:
    """Manages and schedules all observers."""

    def __init__(self):
        self.observers: list[Observer] = []
        self._persistent: list[Observer] = []
        self._last_run: dict[str, float] = {}  # observer_name -> unix timestamp

    def register(self, observer: Observer) -> None:
        """Register an observer."""
        if getattr(observer, "persistent", False):
            self._persistent.append(observer)
            log.info("Registered persistent observer: %s", observer.name)
        else:
            self.observers.append(observer)
            log.info("Registered observer: %s [%s]", observer.name, observer.schedule)

    def _is_due(self, observer: Observer, now: datetime) -> bool:
        """Check if an observer should run now."""
        if not observer.schedule:
            return False

        if not matches_cron(observer.schedule, now):
            return False

        # Prevent running multiple times in the same minute
        last = self._last_run.get(observer.name, 0)
        minute_start = now.replace(second=0, microsecond=0).timestamp()
        return last < minute_start

    def _run_observer(self, observer: Observer) -> ObserverResult:
        """Run a single observer (in thread pool). Catches all exceptions."""
        try:
            ctx = ObserverContext()
            return observer.run(ctx)
        except Exception as e:
            log.exception("Observer %s crashed: %s", observer.name, e)
            return ObserverResult(
                success=False,
                error=f"Observer {observer.name} crashed: {e}",
            )

    async def tick(self) -> None:
        """Check all observers and run any that are due."""
        now = datetime.now(timezone.utc)
        loop = asyncio.get_event_loop()

        for observer in self.observers:
            if not self._is_due(observer, now):
                continue

            log.info("Running observer: %s", observer.name)
            self._last_run[observer.name] = time.time()

            result = await loop.run_in_executor(_executor, self._run_observer, observer)

            if result.success:
                if result.message:
                    log.info("Observer %s: sending result to Telegram", observer.name)
                else:
                    log.debug("Observer %s: silent success", observer.name)
            else:
                log.warning("Observer %s failed: %s", observer.name, result.error)
                # Send error notification
                try:
                    observer.send_telegram(f"[{observer.name}] ERROR: {result.error}", token=ALERT_BOT_TOKEN)
                except Exception:
                    pass

    def _start_persistent(self) -> None:
        """Start persistent observers in dedicated daemon threads."""
        import threading
        for observer in self._persistent:
            t = threading.Thread(
                target=self._run_observer,
                args=(observer,),
                name=f"observer-{observer.name}",
                daemon=True,
            )
            t.start()
            log.info("Started persistent observer: %s (thread %s)", observer.name, t.name)

    async def run_loop(self, interval: int = 30) -> None:
        """Main loop — checks observers every `interval` seconds."""
        total = len(self.observers) + len(self._persistent)
        log.info("Observer registry started with %d observers (%d cron, %d persistent)",
                 total, len(self.observers), len(self._persistent))

        # Start persistent observers in background threads
        self._start_persistent()

        while True:
            try:
                await self.tick()
            except Exception as e:
                log.exception("Observer registry tick failed: %s", e)
            await asyncio.sleep(interval)
