"""Observer base class — ABC for all periodic observers.

Each observer implements run(ctx) which returns an ObserverResult.
The registry calls run() on schedule and delivers results to Telegram.
"""

import logging
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from config import BOT_TOKEN, AUTHORIZED_USER_ID

log = logging.getLogger("nexus")


@dataclass
class ObserverContext:
    """Runtime context passed to every observer invocation."""

    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    state_dir: Path = field(
        default_factory=lambda: Path(__file__).parent / ".state"
    )

    def __post_init__(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class ObserverResult:
    """Result from an observer run."""
    success: bool = True
    message: str = ""          # Text to send to Telegram (empty = silent success)
    error: str = ""            # Error message if success=False
    data: dict = field(default_factory=dict)  # Arbitrary structured data


class Observer(ABC):
    """Base class for all NEXUS observers.

    Subclasses must implement:
        name: str            — unique identifier (e.g. "email_digest")
        schedule: str        — cron expression (e.g. "*/30 * * * *")
        run(ctx) -> ObserverResult
    """

    name: str = ""
    schedule: str = ""  # 5-field cron: min hour dom month dow

    @abstractmethod
    def run(self, ctx: ObserverContext) -> ObserverResult:
        """Execute the observer's task. Runs in a thread pool (sync I/O ok)."""
        ...

    # -- Shared helpers (replaces duplicated code in standalone scripts) --

    def send_telegram(self, text: str, token: str = "", chat_id: str = "") -> None:
        """Send a message to Telegram. Uses bot token from config by default."""
        token = token or BOT_TOKEN
        chat_id = chat_id or str(AUTHORIZED_USER_ID)

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

        for chunk in chunks:
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data
            )
            try:
                urllib.request.urlopen(req, timeout=15)
            except Exception as e:
                log.warning("Telegram send failed for %s: %s", self.name, e)

    def send_telegram_html(self, text: str, token: str = "", chat_id: str = "") -> None:
        """Send HTML-formatted message to Telegram."""
        token = token or BOT_TOKEN
        chat_id = chat_id or str(AUTHORIZED_USER_ID)

        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            log.warning("Telegram HTML send failed for %s: %s", self.name, e)

    def call_llm(self, prompt: str, model: str = "sonnet", timeout: int = 300) -> str:
        """Invoke the configured LLM backend synchronously. Returns result text."""
        from engine import call_sync
        result = call_sync(prompt, model=model, timeout=timeout)
        return result.get("result", "")

    # Backward-compatible alias
    call_claude = call_llm

    def now_utc(self) -> datetime:
        """Current UTC datetime."""
        return datetime.now(timezone.utc)
