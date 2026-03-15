"""MessageRouter — resolves WhatsApp JID → response mode from routing config."""

import json
import logging
from enum import Enum
from pathlib import Path

log = logging.getLogger("nexus")


class Mode(str, Enum):
    SILENT = "silent"         # Log only, no action
    NOTIFY = "notify"         # Forward to Telegram
    SUGGEST = "suggest"       # Draft reply, send to Telegram for approval
    AUTONOMOUS = "autonomous" # Draft reply, send automatically (rate-limited)


# Default routing config path
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "data" / "wa_routing.json"


class MessageRouter:
    """Resolve per-contact/group response mode from config file.

    Config structure (wa_routing.json):
    {
        "default_mode": "notify",
        "contacts": {
            "44xxxxxxxxxx@s.whatsapp.net": {"mode": "suggest", "label": "Person Name"},
            ...
        },
        "groups": {
            "xxxxxxxxxx-xxxxxxxxxx@g.us": {"mode": "silent", "label": "Group Name"},
            ...
        }
    }
    """

    def __init__(self, config_path: str | Path | None = None):
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._config: dict = {}
        self._load()

    def _load(self):
        """Load or reload routing config from disk."""
        if self._config_path.exists():
            try:
                self._config = json.loads(self._config_path.read_text())
                log.info("WA routing config loaded: %d contacts, %d groups",
                         len(self._config.get("contacts", {})),
                         len(self._config.get("groups", {})))
            except Exception as e:
                log.warning("Failed to load WA routing config: %s", e)
                self._config = {}
        else:
            log.info("No WA routing config at %s — using defaults", self._config_path)
            self._config = {}

    def reload(self):
        """Reload config from disk (called via /wa command)."""
        self._load()

    @property
    def default_mode(self) -> Mode:
        return Mode(self._config.get("default_mode", "notify"))

    def resolve(self, jid: str, is_group: bool = False) -> Mode:
        """Resolve the response mode for a JID."""
        section = "groups" if is_group else "contacts"
        entry = self._config.get(section, {}).get(jid)
        if entry:
            return Mode(entry.get("mode", self.default_mode.value))
        return self.default_mode

    def get_label(self, jid: str, is_group: bool = False) -> str | None:
        """Get the human-readable label for a JID, if configured."""
        section = "groups" if is_group else "contacts"
        entry = self._config.get(section, {}).get(jid)
        return entry.get("label") if entry else None

    def set_mode(self, jid: str, mode: Mode, is_group: bool = False,
                 label: str | None = None):
        """Update the mode for a JID and persist to disk."""
        section = "groups" if is_group else "contacts"
        if section not in self._config:
            self._config[section] = {}

        entry = self._config[section].get(jid, {})
        entry["mode"] = mode.value
        if label:
            entry["label"] = label
        self._config[section][jid] = entry

        self._save()

    def _save(self):
        """Persist routing config to disk."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(self._config, indent=2) + "\n")

    def list_all(self) -> dict:
        """Return all configured routes for display."""
        result = {"default": self.default_mode.value, "contacts": {}, "groups": {}}
        for jid, entry in self._config.get("contacts", {}).items():
            result["contacts"][jid] = {
                "mode": entry.get("mode", self.default_mode.value),
                "label": entry.get("label", ""),
            }
        for jid, entry in self._config.get("groups", {}).items():
            result["groups"][jid] = {
                "mode": entry.get("mode", self.default_mode.value),
                "label": entry.get("label", ""),
            }
        return result
