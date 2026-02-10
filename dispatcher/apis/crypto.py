"""Crypto price API client using CoinGecko (no auth required)."""

from dispatcher.apis import get_session, ttl_cache, DispatchError

COINS = "bitcoin,ethereum,solana,ripple,dogecoin"

COINGECKO_URL = (
    f"https://api.coingecko.com/api/v3/simple/price"
    f"?ids={COINS}"
    f"&vs_currencies=usd,gbp"
    f"&include_24hr_change=true"
    f"&include_market_cap=true"
)

# Display config: (coingecko_id, ticker, display_name)
COIN_LIST = [
    ("bitcoin", "BTC", "Bitcoin"),
    ("ethereum", "ETH", "Ethereum"),
    ("solana", "SOL", "Solana"),
    ("ripple", "XRP", "XRP"),
    ("dogecoin", "DOGE", "Dogecoin"),
]


@ttl_cache(seconds=60)
async def fetch_crypto() -> dict:
    """Fetch crypto prices from CoinGecko.

    Returns dict suitable for cards.render_crypto().
    """
    session = await get_session()
    try:
        async with session.get(COINGECKO_URL) as resp:
            if resp.status == 429:
                raise DispatchError("CoinGecko rate limit hit — try again in a minute")
            if resp.status != 200:
                raise DispatchError(f"CoinGecko API returned {resp.status}")
            data = await resp.json()
    except DispatchError:
        raise
    except Exception as e:
        raise DispatchError(f"Crypto fetch failed: {e}")

    coins = []
    for cg_id, ticker, name in COIN_LIST:
        coin_data = data.get(cg_id, {})
        coins.append({
            "ticker": ticker,
            "name": name,
            "usd": coin_data.get("usd", 0),
            "gbp": coin_data.get("gbp", 0),
            "change_24h": coin_data.get("usd_24h_change", 0) or 0,
            "mcap": coin_data.get("usd_market_cap", 0),
        })

    return {"coins": coins}
