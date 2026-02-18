#!/usr/bin/env python3
"""Darwin real-time train data consumer — Kafka feed from RDM.

Connects to the Rail Data Marketplace (raildata.org.uk) Confluent Cloud
Kafka topic for Darwin Push Port data. Maintains an in-memory state of
live train services, indexed by station CRS code.

The registry runs this in its own thread via `persistent = True`.
Standalone: python3 observers/darwin_consumer.py
"""

import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from observers.base import Observer, ObserverResult

log = logging.getLogger("nexus")

# Module-level global state — read by dispatcher/apis/darwin.py
_darwin_state: "DarwinState | None" = None


def get_darwin_state() -> "DarwinState | None":
    """Return the current DarwinState instance (or None if consumer not running)."""
    return _darwin_state


# ---------------------------------------------------------------------------
# Darwin XML namespaces
# ---------------------------------------------------------------------------

NS = {
    "": "http://www.thalesgroup.com/rtti/PushPort/v16",
    "ns2": "http://www.thalesgroup.com/rtti/PushPort/v16",
    "ns3": "http://www.thalesgroup.com/rtti/PushPort/Forecasts/v3",
    "ns4": "http://www.thalesgroup.com/rtti/PushPort/Formations/v2",
    "ns5": "http://www.thalesgroup.com/rtti/PushPort/v12",
    "ns6": "http://www.thalesgroup.com/rtti/PushPort/Alarms/v1",
    "ct": "http://www.thalesgroup.com/rtti/PushPort/CommonTypes/v3",
}

# Darwin message types within the Pport envelope
SCHEDULE_TAG = "uR"  # Update/Replace schedule
DEACTIVATED_TAG = "deactivated"
TRAIN_STATUS_TAG = "TS"  # Train Status
STATION_MSG_TAG = "OW"  # Station message
ALARM_TAG = "alarm"


# ---------------------------------------------------------------------------
# TIPLOC→CRS mapping
# ---------------------------------------------------------------------------

def _load_tiploc_map() -> dict[str, str]:
    """Load TIPLOC→CRS mapping from static JSON file."""
    path = Path(__file__).parent.parent / "data" / "tiploc_crs.json"
    if not path.exists():
        log.warning("TIPLOC→CRS map not found at %s", path)
        return {}
    with open(path) as f:
        data = json.load(f)
    # Flatten to simple tiploc→crs
    return {k: v["crs"] for k, v in data.items()}


def _load_station_names() -> dict[str, str]:
    """Build CRS→display name mapping from tiploc_crs.json."""
    path = Path(__file__).parent.parent / "data" / "tiploc_crs.json"
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    # Deduplicate: pick the first name seen for each CRS
    names: dict[str, str] = {}
    for entry in data.values():
        crs = entry.get("crs", "")
        name = entry.get("name", "")
        if crs and name and crs not in names:
            names[crs] = name
    return names


_STATION_NAMES_CACHE = _load_station_names()


# ---------------------------------------------------------------------------
# DarwinState — thread-safe in-memory state
# ---------------------------------------------------------------------------

class DarwinState:
    """Thread-safe in-memory store for Darwin train service data."""

    def __init__(self):
        self._lock = threading.RLock()
        self.services: dict[str, dict] = {}       # RID → service dict
        self.station_index: dict[str, set] = {}    # CRS → set of RIDs
        self.station_messages: dict[str, list] = {}  # CRS → disruption messages
        self.tiploc_to_crs = _load_tiploc_map()
        self.stats = {
            "msg_count": 0,
            "schedule_count": 0,
            "status_count": 0,
            "last_update": None,
            "connected": False,
            "start_time": None,
        }

    def resolve_tiploc(self, tiploc: str) -> str | None:
        """Resolve a TIPLOC code to a CRS code."""
        return self.tiploc_to_crs.get(tiploc)

    def update_schedule(self, rid: str, uid: str, ssd: str, toc: str,
                        train_id: str, calling_points: list[dict]) -> None:
        """Store or replace a train schedule.

        calling_points: list of {"tiploc": ..., "crs": ..., "pta": ..., "ptd": ...,
                                  "wta": ..., "wtd": ..., "activity": ...}
        """
        with self._lock:
            service = {
                "rid": rid,
                "uid": uid,
                "ssd": ssd,           # Scheduled start date (YYYY-MM-DD)
                "toc": toc,           # Train operating company
                "train_id": train_id,  # Headcode e.g. "1A23"
                "calling_points": calling_points,
                "live": {},           # tiploc → live forecast/actual times
                "cancelled": False,
                "cancel_reason": "",
                "late_reason": "",
                "updated": time.time(),
            }

            # Remove old station_index entries if replacing
            if rid in self.services:
                old_svc = self.services[rid]
                for cp in old_svc.get("calling_points", []):
                    crs = cp.get("crs")
                    if crs and crs in self.station_index:
                        self.station_index[crs].discard(rid)

            self.services[rid] = service

            # Build station_index
            for cp in calling_points:
                crs = cp.get("crs")
                if crs:
                    if crs not in self.station_index:
                        self.station_index[crs] = set()
                    self.station_index[crs].add(rid)

            self.stats["schedule_count"] += 1
            self.stats["last_update"] = datetime.now(timezone.utc).isoformat()

    def update_status(self, rid: str, locations: list[dict],
                      cancelled: bool = False, cancel_reason: str = "",
                      late_reason: str = "") -> None:
        """Merge live forecast/actual times into an existing service.

        locations: list of {"tiploc": ..., "pta": ..., "ptd": ...,
                            "eta": ..., "etd": ..., "ata": ..., "atd": ...,
                            "plat": ..., "plat_suppressed": bool,
                            "plat_confirmed": bool}
        """
        with self._lock:
            if rid not in self.services:
                return  # No schedule yet — skip

            svc = self.services[rid]
            if cancelled:
                svc["cancelled"] = True
            if cancel_reason:
                svc["cancel_reason"] = cancel_reason
            if late_reason:
                svc["late_reason"] = late_reason

            for loc in locations:
                tiploc = loc.get("tiploc", "")
                if tiploc:
                    svc["live"][tiploc] = loc

            svc["updated"] = time.time()
            self.stats["status_count"] += 1
            self.stats["last_update"] = datetime.now(timezone.utc).isoformat()

    def deactivate(self, rid: str) -> None:
        """Mark a service as deactivated (remove from active state)."""
        with self._lock:
            if rid in self.services:
                svc = self.services.pop(rid)
                for cp in svc.get("calling_points", []):
                    crs = cp.get("crs")
                    if crs and crs in self.station_index:
                        self.station_index[crs].discard(rid)

    def get_departures(self, from_crs: str, to_crs: str | None = None,
                       count: int = 8) -> list[dict]:
        """Get upcoming departures from a station, optionally filtered by destination.

        Returns list of dicts compatible with render_trains():
        [{"scheduled": "14:30", "expected": "14:32", "platform": "3",
          "status": "On Time", "cancelled": False, "destination": "Edinburgh"}]
        """
        from_crs = from_crs.upper()
        if to_crs:
            to_crs = to_crs.upper()

        with self._lock:
            rids = self.station_index.get(from_crs, set()).copy()

        results = []
        now = datetime.now(timezone.utc)

        for rid in rids:
            with self._lock:
                svc = self.services.get(rid)
                if not svc:
                    continue
                # Copy relevant data under lock
                calling_points = list(svc.get("calling_points", []))
                live = dict(svc.get("live", {}))
                cancelled = svc.get("cancelled", False)
                cancel_reason = svc.get("cancel_reason", "")
                train_id = svc.get("train_id", "")

            # Find the from_crs calling point
            from_idx = None
            to_idx = None
            for i, cp in enumerate(calling_points):
                if cp.get("crs") == from_crs and from_idx is None:
                    from_idx = i
                if to_crs and cp.get("crs") == to_crs and from_idx is not None:
                    to_idx = i
                    break

            if from_idx is None:
                continue
            if to_crs and to_idx is None:
                continue  # Train doesn't call at destination after origin

            from_cp = calling_points[from_idx]
            tiploc = from_cp.get("tiploc", "")

            # Get scheduled departure time
            scheduled = from_cp.get("ptd") or from_cp.get("wtd") or ""
            if not scheduled:
                continue  # Origin has no departure time (terminal?)

            # Get live data if available
            live_data = live.get(tiploc, {})
            actual_dep = live_data.get("atd", "")
            expected_dep = live_data.get("etd", "")
            platform = live_data.get("plat", from_cp.get("plat", "-")) or "-"

            # Determine status
            if cancelled:
                status = "Cancelled"
                expected_str = "-"
            elif actual_dep:
                status = f"Dep {actual_dep}"
                expected_str = actual_dep
            elif expected_dep:
                if expected_dep == scheduled:
                    status = "On Time"
                    expected_str = "On Time"
                else:
                    status = expected_dep
                    expected_str = expected_dep
            else:
                status = "On Time"
                expected_str = "On Time"

            # Find final destination
            last_cp = calling_points[-1]
            dest_name = last_cp.get("name", last_cp.get("crs", "?"))

            # Parse scheduled time for sorting
            try:
                ssd = self.services.get(rid, {}).get("ssd", "")
                sched_parts = scheduled.split(":")
                sched_h, sched_m = int(sched_parts[0]), int(sched_parts[1])
                sort_key = sched_h * 60 + sched_m
            except (ValueError, IndexError):
                sort_key = 9999

            results.append({
                "scheduled": scheduled[:5],  # HH:MM
                "expected": expected_str,
                "platform": str(platform),
                "status": status,
                "cancelled": cancelled,
                "destination": dest_name,
                "train_id": train_id,
                "sort_key": sort_key,
            })

        # Sort by scheduled departure time, take top N
        results.sort(key=lambda x: x["sort_key"])

        # Filter out services that have already departed (actual departure set)
        # Keep cancelled ones visible though
        filtered = []
        for r in results:
            if r["status"].startswith("Dep ") and not r["cancelled"]:
                continue  # Already departed
            filtered.append(r)
            if len(filtered) >= count:
                break

        # If not enough after filtering departed, backfill
        if len(filtered) < count:
            for r in results:
                if r not in filtered:
                    filtered.append(r)
                    if len(filtered) >= count:
                        break

        return filtered[:count]

    def prune(self, max_age_hours: float = 4.0) -> int:
        """Remove services older than max_age_hours. Returns count removed."""
        cutoff = time.time() - (max_age_hours * 3600)
        to_remove = []

        with self._lock:
            for rid, svc in self.services.items():
                if svc.get("updated", 0) < cutoff:
                    to_remove.append(rid)

            for rid in to_remove:
                svc = self.services.pop(rid)
                for cp in svc.get("calling_points", []):
                    crs = cp.get("crs")
                    if crs and crs in self.station_index:
                        self.station_index[crs].discard(rid)

        return len(to_remove)

    def get_stats(self) -> dict:
        """Return consumer statistics."""
        with self._lock:
            return {
                **self.stats,
                "active_services": len(self.services),
                "indexed_stations": len(self.station_index),
            }

    def to_json(self) -> str:
        """Serialize state to JSON for snapshot file."""
        with self._lock:
            data = {
                "stats": self.get_stats(),
                "services": {},
                "station_index": {k: list(v) for k, v in self.station_index.items()},
                "station_messages": self.station_messages,
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
            }
            for rid, svc in self.services.items():
                data["services"][rid] = {
                    "rid": svc["rid"],
                    "uid": svc.get("uid", ""),
                    "ssd": svc.get("ssd", ""),
                    "toc": svc.get("toc", ""),
                    "train_id": svc.get("train_id", ""),
                    "calling_points": svc.get("calling_points", []),
                    "live": svc.get("live", {}),
                    "cancelled": svc.get("cancelled", False),
                    "cancel_reason": svc.get("cancel_reason", ""),
                    "late_reason": svc.get("late_reason", ""),
                    "updated": svc.get("updated", 0),
                }
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def from_json(cls, json_str: str) -> "DarwinState":
        """Restore state from a JSON snapshot."""
        state = cls()
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return state

        for rid, svc in data.get("services", {}).items():
            state.services[rid] = svc
            for cp in svc.get("calling_points", []):
                crs = cp.get("crs")
                if crs:
                    if crs not in state.station_index:
                        state.station_index[crs] = set()
                    state.station_index[crs].add(rid)

        state.station_messages = data.get("station_messages", {})
        stats = data.get("stats", {})
        state.stats["msg_count"] = stats.get("msg_count", 0)
        state.stats["schedule_count"] = stats.get("schedule_count", 0)
        state.stats["status_count"] = stats.get("status_count", 0)
        return state


# ---------------------------------------------------------------------------
# Darwin message parser
# ---------------------------------------------------------------------------

class DarwinParser:
    """Parse Darwin Push Port XML messages and update DarwinState."""

    def __init__(self, state: DarwinState):
        self.state = state

    def parse_message(self, raw: str | bytes) -> None:
        """Parse a Darwin message (JSON envelope or raw XML)."""
        self.state.stats["msg_count"] += 1

        # Try JSON envelope first (RDM wraps Darwin in JSON)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        try:
            envelope = json.loads(raw)
            # RDM JSON envelope — inner content is XML string
            xml_str = envelope.get("data", "") or envelope.get("message", "") or ""
            if not xml_str and isinstance(envelope, str):
                xml_str = envelope
            if xml_str:
                self._parse_xml(xml_str)
                return
        except (json.JSONDecodeError, AttributeError):
            pass

        # Try raw XML
        if raw.strip().startswith("<") or raw.strip().startswith("<?"):
            self._parse_xml(raw)
            return

        log.debug("Darwin: unrecognised message format (len=%d)", len(raw))

    def _parse_xml(self, xml_str: str) -> None:
        """Parse Darwin Push Port XML."""
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError as e:
            log.debug("Darwin XML parse error: %s", e)
            return

        # Strip namespace prefixes for easier matching
        tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        # Handle Pport envelope
        if tag == "Pport":
            for child in root:
                child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child_tag == SCHEDULE_TAG:
                    self._parse_schedule(child)
                elif child_tag == TRAIN_STATUS_TAG:
                    self._parse_train_status(child)
                elif child_tag == DEACTIVATED_TAG:
                    rid = child.get("rid", "")
                    if rid:
                        self.state.deactivate(rid)
                elif child_tag == STATION_MSG_TAG:
                    self._parse_station_message(child)
        elif tag == SCHEDULE_TAG:
            self._parse_schedule(root)
        elif tag == TRAIN_STATUS_TAG:
            self._parse_train_status(root)

    def _parse_schedule(self, elem: ET.Element) -> None:
        """Parse a schedule (uR) element."""
        rid = elem.get("rid", "")
        uid = elem.get("uid", "")
        ssd = elem.get("ssd", "")
        toc = elem.get("toc", "")
        train_id = elem.get("trainId", "")

        if not rid:
            return

        calling_points = []
        for child in elem:
            child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if child_tag in ("OR", "OPOR", "IP", "OPIP", "PP", "DT", "OPDT"):
                tiploc = child.get("tpl", "")
                crs = self.state.resolve_tiploc(tiploc)
                cp = {
                    "tiploc": tiploc,
                    "crs": crs,
                    "type": child_tag,
                    "pta": child.get("pta", ""),   # Public timetable arrival
                    "ptd": child.get("ptd", ""),   # Public timetable departure
                    "wta": child.get("wta", ""),   # Working timetable arrival
                    "wtd": child.get("wtd", ""),   # Working timetable departure
                    "activity": child.get("act", ""),
                }
                # Station name from cached tiploc_crs.json data
                if crs:
                    cp["name"] = _STATION_NAMES_CACHE.get(crs, crs)
                else:
                    cp["name"] = tiploc

                calling_points.append(cp)

        if calling_points:
            self.state.update_schedule(rid, uid, ssd, toc, train_id, calling_points)

    def _parse_train_status(self, elem: ET.Element) -> None:
        """Parse a train status (TS) element."""
        rid = elem.get("rid", "")
        if not rid:
            return

        locations = []
        cancelled = False
        cancel_reason = ""
        late_reason = ""

        for child in elem:
            child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

            if child_tag == "Location":
                tiploc = child.get("tpl", "")
                loc = {
                    "tiploc": tiploc,
                    "pta": child.get("pta", ""),
                    "ptd": child.get("ptd", ""),
                }
                # Extract forecast/actual times from nested elements
                for sub in child:
                    sub_tag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                    if sub_tag == "arr":
                        loc["eta"] = sub.get("et", "")
                        loc["ata"] = sub.get("at", "")
                        loc["arr_src"] = sub.get("src", "")
                    elif sub_tag == "dep":
                        loc["etd"] = sub.get("et", "")
                        loc["atd"] = sub.get("at", "")
                        loc["dep_src"] = sub.get("src", "")
                    elif sub_tag == "pass":
                        loc["etp"] = sub.get("et", "")
                        loc["atp"] = sub.get("at", "")
                    elif sub_tag == "plat":
                        loc["plat"] = sub.text or ""
                        loc["plat_suppressed"] = sub.get("platsup", "false") == "true"
                        loc["plat_confirmed"] = sub.get("conf", "false") == "true"
                    elif sub_tag == "length":
                        loc["length"] = sub.text or ""

                locations.append(loc)

            elif child_tag == "LateReason":
                late_reason = child.text or child.get("code", "")
            elif child_tag == "CancelReason":
                cancel_reason = child.text or child.get("code", "")
                cancelled = True

        self.state.update_status(rid, locations, cancelled, cancel_reason, late_reason)

    def _parse_station_message(self, elem: ET.Element) -> None:
        """Parse a station message (OW) element."""
        msg_id = elem.get("id", "")
        cat = elem.get("cat", "")
        sev = elem.get("sev", "")

        stations = []
        msg_text = ""

        for child in elem:
            child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if child_tag == "Station":
                crs = child.get("crs", "")
                if crs:
                    stations.append(crs)
            elif child_tag == "Msg":
                # Message may contain HTML-like markup
                msg_text = "".join(child.itertext())

        if stations and msg_text:
            msg = {
                "id": msg_id,
                "category": cat,
                "severity": sev,
                "text": msg_text.strip(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            with self.state._lock:
                for crs in stations:
                    if crs not in self.state.station_messages:
                        self.state.station_messages[crs] = []
                    # Replace message with same ID
                    self.state.station_messages[crs] = [
                        m for m in self.state.station_messages[crs] if m["id"] != msg_id
                    ]
                    self.state.station_messages[crs].append(msg)


# ---------------------------------------------------------------------------
# DarwinConsumer — Kafka consumer observer
# ---------------------------------------------------------------------------

class DarwinConsumer(Observer):
    """Darwin Push Port Kafka consumer — persistent observer."""

    name = "darwin_consumer"
    schedule = ""          # Not cron-driven
    persistent = True      # Runs in its own daemon thread

    SNAPSHOT_INTERVAL = 10  # seconds between snapshot writes
    PRUNE_INTERVAL = 300    # seconds between prune runs

    def __init__(self):
        self.bootstrap = os.environ.get(
            "DARWIN_KAFKA_BOOTSTRAP",
            "pkc-l6wr6.europe-west2.gcp.confluent.cloud:9092",
        )
        self.api_key = os.environ.get("DARWIN_KAFKA_KEY", "")
        self.api_secret = os.environ.get("DARWIN_KAFKA_SECRET", "")
        self.topic = os.environ.get(
            "DARWIN_KAFKA_TOPIC",
            "prod-1010-Darwin-Train-Information-Push-Port-IIII1_1-JSON",
        )
        self.snapshot_path = os.environ.get(
            "DARWIN_SNAPSHOT_PATH",
            str(Path.home() / ".hal" / "darwin_state.json"),
        )

    def run(self, ctx=None) -> ObserverResult:
        """Start the Kafka consumer. Blocks forever."""
        global _darwin_state

        if not self.api_key or not self.api_secret:
            log.warning(
                "[darwin_consumer] Kafka credentials not configured "
                "(DARWIN_KAFKA_KEY / DARWIN_KAFKA_SECRET). "
                "Darwin consumer inactive."
            )
            return ObserverResult(
                success=True,
                message="Darwin consumer skipped — no Kafka credentials",
            )

        # Try to import confluent_kafka
        try:
            from confluent_kafka import Consumer, KafkaError, KafkaException
        except ImportError:
            log.error("[darwin_consumer] confluent-kafka not installed")
            return ObserverResult(
                success=False,
                error="confluent-kafka package not installed",
            )

        # Initialize state (try to load from snapshot)
        state = DarwinState()
        snapshot_path = Path(self.snapshot_path)
        if snapshot_path.exists():
            try:
                state = DarwinState.from_json(snapshot_path.read_text())
                log.info(
                    "[darwin_consumer] Restored state from snapshot: "
                    "%d services, %d stations",
                    len(state.services), len(state.station_index),
                )
            except Exception as e:
                log.warning("[darwin_consumer] Failed to load snapshot: %s", e)
                state = DarwinState()

        state.stats["connected"] = False
        state.stats["start_time"] = datetime.now(timezone.utc).isoformat()
        _darwin_state = state

        parser = DarwinParser(state)

        # Kafka consumer config
        conf = {
            "bootstrap.servers": self.bootstrap,
            "security.protocol": "SASL_SSL",
            "sasl.mechanisms": "PLAIN",
            "sasl.username": self.api_key,
            "sasl.password": self.api_secret,
            "group.id": "nexus-darwin-consumer",
            "auto.offset.reset": "latest",
            "enable.auto.commit": True,
            "session.timeout.ms": 45000,
            "max.poll.interval.ms": 300000,
        }

        last_snapshot = 0
        last_prune = 0

        while True:
            consumer = None
            try:
                consumer = Consumer(conf)
                consumer.subscribe([self.topic])
                state.stats["connected"] = True
                log.info(
                    "[darwin_consumer] Connected to Kafka topic: %s",
                    self.topic,
                )

                while True:
                    msg = consumer.poll(1.0)

                    if msg is None:
                        pass
                    elif msg.error():
                        error = msg.error()
                        if error.code() == KafkaError._PARTITION_EOF:
                            pass  # Normal — end of partition
                        else:
                            log.warning(
                                "[darwin_consumer] Kafka error: %s", error
                            )
                    else:
                        # Process message
                        try:
                            parser.parse_message(msg.value())
                        except Exception as e:
                            log.debug(
                                "[darwin_consumer] Parse error: %s", e
                            )

                    # Periodic snapshot
                    now = time.time()
                    if now - last_snapshot >= self.SNAPSHOT_INTERVAL:
                        last_snapshot = now
                        self._write_snapshot(state, snapshot_path)

                    # Periodic prune
                    if now - last_prune >= self.PRUNE_INTERVAL:
                        last_prune = now
                        removed = state.prune()
                        if removed:
                            log.debug(
                                "[darwin_consumer] Pruned %d stale services",
                                removed,
                            )

            except KeyboardInterrupt:
                log.info("[darwin_consumer] Shutting down")
                break
            except Exception as e:
                state.stats["connected"] = False
                log.error(
                    "[darwin_consumer] Connection error: %s — reconnecting in 15s",
                    e,
                )
                time.sleep(15)
            finally:
                if consumer:
                    try:
                        consumer.close()
                    except Exception:
                        pass

        return ObserverResult(success=True, message="Darwin consumer stopped")

    def _write_snapshot(self, state: DarwinState, path: Path) -> None:
        """Write state snapshot to disk."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(state.to_json())
            tmp.rename(path)
        except Exception as e:
            log.debug("[darwin_consumer] Snapshot write failed: %s", e)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path as _Path

    project_root = _Path(__file__).parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv(project_root / ".env")
    except ImportError:
        pass

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    consumer = DarwinConsumer()
    print(f"Darwin consumer starting...")
    print(f"  Bootstrap: {consumer.bootstrap}")
    print(f"  Topic:     {consumer.topic}")
    print(f"  Key:       {'configured' if consumer.api_key else 'NOT SET'}")
    print(f"  Snapshot:  {consumer.snapshot_path}")

    if not consumer.api_key:
        print("\nWARNING: DARWIN_KAFKA_KEY not set. Set credentials in .env first.")
        sys.exit(1)

    try:
        consumer.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
