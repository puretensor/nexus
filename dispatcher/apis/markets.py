"""Stock market indices via Yahoo Finance (no auth required).

Provides US, UK, European, and Asian market indices with open/closed status.
"""

import time
from dispatcher.apis import get_session, ttl_cache, DispatchError

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Index definitions: (yahoo_symbol, display_name, region)
US_INDICES = [
    ("^GSPC", "S&P 500", "US"),
    ("^DJI", "Dow Jones", "US"),
    ("^IXIC", "Nasdaq", "US"),
]

UK_INDICES = [
    ("^FTSE", "FTSE 100", "UK"),
]

EU_INDICES = [
    ("^GDAXI", "DAX", "EU"),
    ("^STOXX50E", "Euro Stoxx 50", "EU"),
]

ASIA_INDICES = [
    ("^N225", "Nikkei 225", "Asia"),
    ("^HSI", "Hang Seng", "Asia"),
]

ALL_INDICES = US_INDICES + UK_INDICES + EU_INDICES + ASIA_INDICES


async def _fetch_index(symbol: str) -> dict:
    """Fetch a single index from Yahoo Finance."""
    session = await get_session()
    url = f"{YAHOO_BASE}/{symbol}?range=1d&interval=1d"
    try:
        async with session.get(url, headers=YAHOO_HEADERS) as resp:
            if resp.status != 200:
                raise DispatchError(f"Yahoo Finance returned {resp.status} for {symbol}")
            data = await resp.json()
    except DispatchError:
        raise
    except Exception as e:
        raise DispatchError(f"Market data fetch failed for {symbol}: {e}")

    try:
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice", 0)
        prev_close = meta.get("chartPreviousClose", 0)
        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0

        # Market open/closed from trading periods
        tp = meta.get("currentTradingPeriod", {}).get("regular", {})
        now = int(time.time())
        market_open = tp.get("start", 0) <= now <= tp.get("end", 0)

        return {
            "symbol": symbol,
            "name": meta.get("shortName", symbol).strip(),
            "price": price,
            "change_pct": change_pct,
            "prev_close": prev_close,
            "day_high": meta.get("regularMarketDayHigh", 0),
            "day_low": meta.get("regularMarketDayLow", 0),
            "market_open": market_open,
            "currency": meta.get("currency", ""),
        }
    except (KeyError, IndexError, TypeError) as e:
        raise DispatchError(f"Unexpected Yahoo response for {symbol}: {e}")


@ttl_cache(seconds=60)
async def fetch_us_markets() -> dict:
    """Fetch US market indices."""
    indices = []
    for symbol, name, region in US_INDICES:
        try:
            data = await _fetch_index(symbol)
            data["display_name"] = name
            data["region"] = region
            indices.append(data)
        except DispatchError:
            indices.append({"display_name": name, "region": region,
                            "price": 0, "change_pct": 0, "market_open": False, "error": True})
    return {"title": "US Markets", "indices": indices}


@ttl_cache(seconds=60)
async def fetch_uk_markets() -> dict:
    """Fetch UK market indices."""
    indices = []
    for symbol, name, region in UK_INDICES:
        try:
            data = await _fetch_index(symbol)
            data["display_name"] = name
            data["region"] = region
            indices.append(data)
        except DispatchError:
            indices.append({"display_name": name, "region": region,
                            "price": 0, "change_pct": 0, "market_open": False, "error": True})
    return {"title": "UK Markets", "indices": indices}


@ttl_cache(seconds=60)
async def fetch_all_markets() -> dict:
    """Fetch all global market indices."""
    indices = []
    for symbol, name, region in ALL_INDICES:
        try:
            data = await _fetch_index(symbol)
            data["display_name"] = name
            data["region"] = region
            indices.append(data)
        except DispatchError:
            indices.append({"display_name": name, "region": region,
                            "price": 0, "change_pct": 0, "market_open": False, "error": True})
    return {"title": "World Markets", "indices": indices}
