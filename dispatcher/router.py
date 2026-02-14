"""Slash-command card dispatcher.

Provides handler functions for Telegram slash commands that return fast API +
Pillow card responses, bypassing Claude for pure data lookups.

Commands (registered in bot.py):
    /weather [location]   — Weather card
    /crypto               — Crypto prices card
    /trains [from] [to]   — Train departures card
    /gold                 — Gold & silver prices card
    /markets [us|uk]      — Stock market indices card
    /forex                — Forex rates card
    /nodes                — Infrastructure status card

Refresh buttons on each card trigger refresh_dispatch() via callback queries.
"""

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from dispatcher.apis import DispatchError
from dispatcher.apis.weather import fetch_weather
from dispatcher.apis.crypto import fetch_crypto
from dispatcher.apis.trains import fetch_trains, resolve_station, DEFAULT_FROM, DEFAULT_TO
from dispatcher.apis.gold import fetch_gold
from dispatcher.apis.status import fetch_status
from dispatcher.apis.markets import fetch_us_markets, fetch_uk_markets, fetch_all_markets
from dispatcher.apis.forex import fetch_forex
from dispatcher.cards import (
    render_weather,
    render_crypto,
    render_trains,
    render_gold,
    render_status,
    render_markets_unified,
)

log = logging.getLogger("dispatcher")

# ---------------------------------------------------------------------------
# Location / parameter extractors
# ---------------------------------------------------------------------------

# Extracts location from natural language weather queries
_WEATHER_LOCATION_RE = re.compile(
    r"(?:weather|forecast|temperature|temp)"
    r"(?:\s+(?:like\s+)?)?(?:in|for|at|of)\s+"
    r"(.+?)(?:\s*[?.!,]?\s*$)",
    re.I
)

# Also try: "in <location> weather" or trailing location after comma
_WEATHER_LOCATION_ALT = re.compile(
    r"(?:in|for|at)\s+(.+?)(?:\s+(?:weather|forecast|temperature|temp))",
    re.I
)

# Trailing noise phrases to strip from extracted locations
_LOCATION_NOISE_RE = re.compile(
    r"\s+(?:at the moment|right now|today|tomorrow|currently|please|now|"
    r"this (?:morning|afternoon|evening|week)|don'?t .+).*$",
    re.I
)

# Station pair extractor — "<from> to <to>"
_STATION_PAIR_RE = re.compile(
    r"\b(?:from\s+)?(\w[\w\s]*?)\s+to\s+(\w[\w\s]*?)(?:\s+train|\s+departure|[?.!,]|$)",
    re.I
)


def extract_weather_location(text: str) -> str | None:
    """Try to extract a location from a weather query."""
    # Try "weather in X" pattern
    m = _WEATHER_LOCATION_RE.search(text)
    if m:
        loc = m.group(1).strip().rstrip("?.!,")
        loc = _LOCATION_NOISE_RE.sub("", loc).strip().rstrip(",")
        if loc:
            return loc
    # Try "in X weather" pattern
    m = _WEATHER_LOCATION_ALT.search(text)
    if m:
        loc = m.group(1).strip().rstrip("?.!,")
        loc = _LOCATION_NOISE_RE.sub("", loc).strip().rstrip(",")
        if loc:
            return loc
    # Try bare "weather <location>" (no preposition)
    m = re.match(
        r"^(?:weather|forecast|temperature|temp)\s+([A-Za-z][\w\s,.-]+)$",
        text.strip().rstrip("?.!"), re.I
    )
    if m:
        loc = m.group(1).strip().rstrip(",")
        loc = _LOCATION_NOISE_RE.sub("", loc).strip().rstrip(",")
        if loc:
            return loc
    return None


def extract_stations(text: str) -> tuple[str, str]:
    """Extract from/to station CRS codes from text. Returns defaults if not found."""
    m = _STATION_PAIR_RE.search(text)
    if m:
        from_text = m.group(1).strip()
        to_text = m.group(2).strip()
        from_crs = resolve_station(from_text)
        to_crs = resolve_station(to_text)
        if from_crs and to_crs:
            return from_crs, to_crs
        if from_crs:
            return from_crs, DEFAULT_TO
    return DEFAULT_FROM, DEFAULT_TO


# ---------------------------------------------------------------------------
# Handler functions — called by command handlers in bot.py
# ---------------------------------------------------------------------------

async def handle_weather(location: str | None, chat, bot) -> None:
    """Fetch weather data and send card."""
    data = await fetch_weather(location)
    png_buf, caption = render_weather(data)

    loc_param = location or ""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Refresh", callback_data=f"refresh:weather:{loc_param}")
    ]])
    await bot.send_photo(
        chat_id=chat.id,
        photo=png_buf,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def handle_markets_unified(chat, bot) -> None:
    """Fetch all market data and send unified card."""
    market_data = await fetch_all_markets()
    forex_data = await fetch_forex()
    crypto_data = await fetch_crypto()
    gold_data = await fetch_gold()

    png_buf, caption = render_markets_unified(market_data, forex_data, crypto_data, gold_data)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Refresh", callback_data="refresh:markets:")
    ]])
    await bot.send_photo(
        chat_id=chat.id,
        photo=png_buf,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def handle_trains(from_crs: str, to_crs: str, chat, bot) -> None:
    """Fetch train departures and send card."""
    data = await fetch_trains(from_crs, to_crs)
    png_buf, caption = render_trains(data)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Refresh", callback_data=f"refresh:trains:{from_crs}:{to_crs}")
    ]])
    await bot.send_photo(
        chat_id=chat.id,
        photo=png_buf,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def handle_status(chat, bot) -> None:
    """Fetch infrastructure status and send card."""
    data = await fetch_status()
    png_buf, caption = render_status(data)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Refresh", callback_data="refresh:status:")
    ]])
    await bot.send_photo(
        chat_id=chat.id,
        photo=png_buf,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Cache management + refresh dispatch
# ---------------------------------------------------------------------------

def _clear_caches(*funcs):
    """Clear TTL caches for functions that have cache_clear."""
    for f in funcs:
        if hasattr(f, "cache_clear"):
            f.cache_clear()


async def refresh_dispatch(category: str, params: str, chat, bot) -> None:
    """Re-dispatch a category from a callback query refresh button.

    category: 'weather', 'crypto', 'trains', 'gold', 'status', 'markets', 'forex', 'world'
    params: category-specific parameters from callback_data
    """
    if category == "weather":
        _clear_caches(fetch_weather)
        await handle_weather(params or None, chat, bot)
    elif category in ("markets", "world", "crypto", "gold", "forex"):
        _clear_caches(fetch_all_markets, fetch_us_markets, fetch_uk_markets,
                       fetch_forex, fetch_crypto, fetch_gold)
        await handle_markets_unified(chat, bot)
    elif category == "trains":
        _clear_caches(fetch_trains)
        parts = params.split(":") if params else []
        if len(parts) == 2:
            await handle_trains(parts[0], parts[1], chat, bot)
        else:
            await handle_trains(DEFAULT_FROM, DEFAULT_TO, chat, bot)
    elif category == "status":
        _clear_caches(fetch_status)
        await handle_status(chat, bot)
    else:
        log.warning("Unknown refresh category: %s", category)
