"""Gold and silver price API client.

Primary: GoldAPI.io (if GOLD_API_KEY set, 250 free req/month)
Fallback: Swissquote live quotes (no auth, no rate limit documented)
FX conversion: Frankfurter API (ECB rates, no auth)
"""

import os
from dispatcher.apis import get_session, ttl_cache, DispatchError

GOLD_API_KEY = os.environ.get("GOLD_API_KEY", "")

SWISSQUOTE_BASE = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument"


@ttl_cache(seconds=300)
async def _fetch_fx_usd_gbp() -> float:
    """Get USD->GBP exchange rate from Frankfurter."""
    session = await get_session()
    try:
        async with session.get("https://api.frankfurter.dev/v1/latest?base=USD&symbols=GBP") as resp:
            if resp.status != 200:
                return 0.80  # sensible fallback
            data = await resp.json()
            return data.get("rates", {}).get("GBP", 0.80)
    except Exception:
        return 0.80


@ttl_cache(seconds=300)
async def _fetch_goldapi() -> dict | None:
    """Fetch gold and silver from GoldAPI.io. Returns None if no key."""
    if not GOLD_API_KEY:
        return None

    session = await get_session()
    headers = {"x-access-token": GOLD_API_KEY, "Content-Type": "application/json"}
    result = {}

    for metal, symbol in [("gold", "XAU"), ("silver", "XAG")]:
        try:
            url = f"https://www.goldapi.io/api/{symbol}/USD"
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None  # fall through to Swissquote
                data = await resp.json()
                result[f"{metal}_usd"] = data.get("price", 0)
                result[f"{metal}_24h"] = data.get("ch_pct", 0) or 0
        except Exception:
            return None

    # Convert to GBP
    fx = await _fetch_fx_usd_gbp()
    result["gold_gbp"] = result.get("gold_usd", 0) * fx
    result["silver_gbp"] = result.get("silver_usd", 0) * fx
    return result


async def _swissquote_price(symbol: str) -> float:
    """Fetch a single instrument mid-price from Swissquote."""
    session = await get_session()
    url = f"{SWISSQUOTE_BASE}/{symbol}/USD"
    async with session.get(url) as resp:
        if resp.status != 200:
            raise DispatchError(f"Swissquote returned {resp.status} for {symbol}")
        data = await resp.json()
    profiles = data[0].get("spreadProfilePrices", [])
    if not profiles:
        raise DispatchError(f"No price data for {symbol}")
    bid = profiles[0].get("bid", 0)
    ask = profiles[0].get("ask", 0)
    return (bid + ask) / 2


@ttl_cache(seconds=300)
async def _fetch_swissquote_metals() -> dict:
    """Fetch gold and silver from Swissquote live forex feed (no auth)."""
    try:
        gold_usd = await _swissquote_price("XAU")
        silver_usd = await _swissquote_price("XAG")
    except DispatchError:
        raise
    except Exception as e:
        raise DispatchError(f"Metals price fetch failed: {e}")

    fx = await _fetch_fx_usd_gbp()
    return {
        "gold_usd": gold_usd,
        "gold_gbp": gold_usd * fx,
        "gold_24h": 0,  # Swissquote doesn't provide 24h change
        "silver_usd": silver_usd,
        "silver_gbp": silver_usd * fx,
        "silver_24h": 0,
    }


async def fetch_gold() -> dict:
    """Fetch gold and silver prices.

    Tries GoldAPI first (if key configured), falls back to Swissquote.
    Returns dict suitable for cards.render_gold().
    """
    # Try GoldAPI first (has 24h change data)
    result = await _fetch_goldapi()
    if result:
        return result

    # Fallback to Swissquote (no 24h change, but accurate spot prices)
    return await _fetch_swissquote_metals()
