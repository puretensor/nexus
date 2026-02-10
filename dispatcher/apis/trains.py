"""UK train departures client using Huxley2 (National Rail Darwin proxy).

Requires a free Darwin API token from https://realtime.nationalrail.co.uk/OpenLDBWSRegistration/
Set DARWIN_API_TOKEN in .env.
"""

import os
from dispatcher.apis import get_session, ttl_cache, DispatchError

DARWIN_TOKEN = os.environ.get("DARWIN_API_TOKEN", "")
HUXLEY_BASE = "https://huxley2.azurewebsites.net"

# Default stations (overridable via env)
DEFAULT_FROM = os.environ.get("TRAINS_DEFAULT_FROM", "KGX")
DEFAULT_TO = os.environ.get("TRAINS_DEFAULT_TO", "PAD")

# Station CRS code aliases — common UK stations
STATION_ALIASES = {
    # London terminals
    "paddington": "PAD",
    "london paddington": "PAD",
    "kings cross": "KGX",
    "king's cross": "KGX",
    "london kings cross": "KGX",
    "euston": "EUS",
    "london euston": "EUS",
    "waterloo": "WAT",
    "london waterloo": "WAT",
    "victoria": "VIC",
    "london victoria": "VIC",
    "liverpool street": "LST",
    "london bridge": "LBG",
    "st pancras": "STP",
    "marylebone": "MYB",
    "charing cross": "CHX",
    "london": "KGX",
    # Major cities
    "edinburgh": "EDB",
    "edinburgh waverley": "EDB",
    "glasgow": "GLC",
    "glasgow central": "GLC",
    "birmingham": "BHM",
    "birmingham new street": "BHM",
    "manchester": "MAN",
    "manchester piccadilly": "MAN",
    "leeds": "LDS",
    "york": "YRK",
    "bristol": "BRI",
    "bristol temple meads": "BRI",
    "liverpool": "LIV",
    "liverpool lime street": "LIV",
    "newcastle": "NCL",
    "sheffield": "SHF",
    "nottingham": "NOT",
    "cardiff": "CDF",
    "cardiff central": "CDF",
    "oxford": "OXF",
    "cambridge": "CBG",
    "bath": "BTH",
    "bath spa": "BTH",
    "brighton": "BTN",
    "reading": "RDG",
    "southampton": "SOU",
    "southampton central": "SOU",
    "exeter": "EXD",
    "exeter st davids": "EXD",
    "plymouth": "PLY",
    "peterborough": "PBO",
    "coventry": "COV",
    "crewe": "CRE",
    "preston": "PRE",
    "darlington": "DAR",
    "doncaster": "DON",
    "slough": "SLO",
    "maidenhead": "MAI",
    "windsor": "WNR",
    "windsor riverside": "WNR",
    "windsor central": "WNC",
    "ealing broadway": "EAL",
    "staines": "SNS",
    "guildford": "GLD",
    "swindon": "SWI",
}

# Full station names for CRS codes (for display)
STATION_NAMES = {
    "PAD": "London Paddington",
    "KGX": "London Kings Cross",
    "EUS": "London Euston",
    "WAT": "London Waterloo",
    "VIC": "London Victoria",
    "LST": "London Liverpool Street",
    "LBG": "London Bridge",
    "STP": "London St Pancras",
    "MYB": "London Marylebone",
    "CHX": "London Charing Cross",
    "EDB": "Edinburgh Waverley",
    "GLC": "Glasgow Central",
    "BHM": "Birmingham New Street",
    "MAN": "Manchester Piccadilly",
    "LDS": "Leeds",
    "YRK": "York",
    "BRI": "Bristol Temple Meads",
    "LIV": "Liverpool Lime Street",
    "NCL": "Newcastle",
    "SHF": "Sheffield",
    "NOT": "Nottingham",
    "CDF": "Cardiff Central",
    "OXF": "Oxford",
    "CBG": "Cambridge",
    "BTH": "Bath Spa",
    "BTN": "Brighton",
    "RDG": "Reading",
    "SOU": "Southampton Central",
    "EXD": "Exeter St Davids",
    "PLY": "Plymouth",
    "PBO": "Peterborough",
    "COV": "Coventry",
    "CRE": "Crewe",
    "PRE": "Preston",
    "DAR": "Darlington",
    "DON": "Doncaster",
    "SLO": "Slough",
    "MAI": "Maidenhead",
    "WNR": "Windsor & Eton Riverside",
    "WNC": "Windsor & Eton Central",
    "EAL": "Ealing Broadway",
    "SNS": "Staines",
    "GLD": "Guildford",
    "SWI": "Swindon",
}


def resolve_station(text: str) -> str | None:
    """Resolve a station name or CRS code to a 3-letter CRS code."""
    text = text.strip().lower()
    # Direct CRS code (3 uppercase letters)
    if len(text) == 3 and text.isalpha():
        return text.upper()
    return STATION_ALIASES.get(text)


@ttl_cache(seconds=30)
async def fetch_trains(from_crs: str = DEFAULT_FROM, to_crs: str = DEFAULT_TO, count: int = 6) -> dict:
    """Fetch next departures from Huxley2.

    Returns dict suitable for cards.render_trains().
    """
    if not DARWIN_TOKEN:
        raise DispatchError(
            "Train data unavailable — DARWIN_API_TOKEN not configured.\n"
            "Register free at realtime.nationalrail.co.uk"
        )

    url = f"{HUXLEY_BASE}/departures/{from_crs}/to/{to_crs}/{count}"
    params = {"accessToken": DARWIN_TOKEN}

    session = await get_session()
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise DispatchError(f"Darwin API returned {resp.status}: {body[:200]}")
            data = await resp.json()
    except DispatchError:
        raise
    except Exception as e:
        raise DispatchError(f"Train data fetch failed: {e}")

    try:
        services = data.get("trainServices") or []
        origin_name = STATION_NAMES.get(from_crs, from_crs)
        dest_name = STATION_NAMES.get(to_crs, to_crs)

        departures = []
        for svc in services[:count]:
            scheduled = svc.get("std", "?")
            expected = svc.get("etd", "On Time")
            platform = svc.get("platform", "-") or "-"
            cancelled = svc.get("isCancelled", False) or svc.get("cancelReason") is not None

            if cancelled:
                status = "Cancelled"
            elif expected.lower() == "on time":
                status = "On Time"
            elif expected.lower() == "delayed":
                status = "Delayed"
            else:
                status = expected  # Usually the expected time like "14:22"

            departures.append({
                "scheduled": scheduled,
                "expected": expected if not cancelled else "-",
                "platform": platform,
                "status": status,
                "cancelled": cancelled,
            })

        return {
            "origin": origin_name,
            "destination": dest_name,
            "departures": departures,
        }
    except (KeyError, TypeError) as e:
        raise DispatchError(f"Unexpected Darwin response format: {e}")
