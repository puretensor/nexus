"""Foreign exchange rates via Frankfurter API (ECB rates, no auth).

Provides key currency pairs with daily change calculation.
"""

from datetime import datetime, timedelta
from dispatcher.apis import get_session, ttl_cache, DispatchError

FRANKFURTER_BASE = "https://api.frankfurter.dev/v1"

# Key pairs to display — (base, quote, display_name)
FX_PAIRS = [
    ("GBP", "USD", "GBP/USD"),
    ("EUR", "USD", "EUR/USD"),
    ("USD", "JPY", "USD/JPY"),
    ("EUR", "GBP", "EUR/GBP"),
    ("USD", "CHF", "USD/CHF"),
    ("GBP", "EUR", "GBP/EUR"),
]


@ttl_cache(seconds=300)
async def _fetch_rates(base: str, symbols: tuple[str, ...]) -> dict:
    """Fetch current rates from Frankfurter."""
    session = await get_session()
    syms = ",".join(symbols)
    url = f"{FRANKFURTER_BASE}/latest?base={base}&symbols={syms}"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise DispatchError(f"Frankfurter returned {resp.status}")
            data = await resp.json()
            return data.get("rates", {})
    except DispatchError:
        raise
    except Exception as e:
        raise DispatchError(f"FX rate fetch failed: {e}")


@ttl_cache(seconds=3600)
async def _fetch_previous_rates(base: str, symbols: tuple[str, ...]) -> dict:
    """Fetch previous business day rates for change calculation."""
    session = await get_session()
    # Go back up to 4 days to find a business day
    for days_back in range(1, 5):
        date = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        syms = ",".join(symbols)
        url = f"{FRANKFURTER_BASE}/{date}?base={base}&symbols={syms}"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rates = data.get("rates", {})
                    if rates:
                        return rates
        except Exception:
            continue
    return {}


async def fetch_forex() -> dict:
    """Fetch all key FX pairs with daily change.

    Returns dict suitable for cards.render_forex().
    """
    # Group by base currency to minimize API calls
    bases = {}
    for base, quote, _ in FX_PAIRS:
        bases.setdefault(base, set()).add(quote)

    current_rates = {}
    prev_rates = {}
    for base, quotes in bases.items():
        quote_tuple = tuple(sorted(quotes))
        try:
            current = await _fetch_rates(base, quote_tuple)
            previous = await _fetch_previous_rates(base, quote_tuple)
            for q in quote_tuple:
                key = f"{base}/{q}"
                current_rates[key] = current.get(q, 0)
                prev_rates[key] = previous.get(q, 0)
        except DispatchError:
            for q in quote_tuple:
                current_rates[f"{base}/{q}"] = 0
                prev_rates[f"{base}/{q}"] = 0

    pairs = []
    for base, quote, display in FX_PAIRS:
        key = f"{base}/{quote}"
        rate = current_rates.get(key, 0)
        prev = prev_rates.get(key, 0)
        change_pct = ((rate - prev) / prev * 100) if prev else 0
        pairs.append({
            "pair": display,
            "rate": rate,
            "change_pct": change_pct,
        })

    return {"pairs": pairs}
