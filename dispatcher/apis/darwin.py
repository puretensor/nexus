"""Darwin real-time departure data — async bridge to DarwinState.

Reads from the in-memory DarwinState maintained by the darwin_consumer
observer. Falls back to the Huxley2 REST API if Darwin state is empty.
"""

import logging

from dispatcher.apis import DispatchError
from dispatcher.apis.trains import STATION_NAMES

log = logging.getLogger("dispatcher")


async def fetch_darwin_departures(from_crs: str, to_crs: str | None = None,
                                  count: int = 6) -> dict:
    """Fetch live departures from Darwin state.

    Returns dict compatible with cards.render_trains():
    {"origin": "...", "destination": "...", "departures": [...]}

    Raises DispatchError if no data available.
    """
    from observers.darwin_consumer import get_darwin_state

    state = get_darwin_state()
    if state is None:
        raise DispatchError("Darwin consumer not running")

    stats = state.get_stats()
    if stats.get("active_services", 0) == 0:
        raise DispatchError(
            "Darwin feed has no active services — "
            "feed may still be loading or credentials not configured"
        )

    departures = state.get_departures(from_crs, to_crs, count=count)

    origin_name = STATION_NAMES.get(from_crs.upper(), from_crs.upper())
    dest_name = STATION_NAMES.get(to_crs.upper(), to_crs.upper()) if to_crs else "All destinations"

    if not departures:
        return {
            "origin": origin_name,
            "destination": dest_name,
            "departures": [],
        }

    return {
        "origin": origin_name,
        "destination": dest_name,
        "departures": departures,
    }
