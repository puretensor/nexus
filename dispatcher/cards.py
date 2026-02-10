"""Pillow-based image card renderer for dispatcher responses.

Generates dark-themed PNG cards (600px wide) for each dispatch category.
Cards are sent as photos to Telegram with HTML captions.
"""

import io
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Design constants
# ---------------------------------------------------------------------------

CARD_WIDTH = 600
PADDING = 24
ROW_HEIGHT = 32
HEADER_HEIGHT = 60
SEPARATOR_HEIGHT = 16

# Colors
BG = "#0a0e27"
PANEL = "#121836"
TEXT_PRIMARY = "#e8eaf6"
TEXT_SECONDARY = "#8892b0"
TEXT_MUTED = "#5a6380"

# Status colors
GREEN = "#4caf50"
AMBER = "#ff9800"
RED = "#f44336"

# Category accents
ACCENT_WEATHER = "#42a5f5"
ACCENT_CRYPTO = "#66bb6a"
ACCENT_TRAINS = "#ab47bc"
ACCENT_GOLD = "#ffd54f"
ACCENT_STATUS = "#26c6da"
ACCENT_MARKETS = "#ef5350"
ACCENT_FOREX = "#5c6bc0"

# Fonts
_FONT_PATH = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
_FONT_BOLD_PATH = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_PATH, size)


def _font_bold(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_BOLD_PATH, size)


# ---------------------------------------------------------------------------
# CardRenderer — reusable drawing primitives with auto-advancing cursor
# ---------------------------------------------------------------------------

class CardRenderer:
    """Builds a card image with auto-advancing vertical cursor."""

    def __init__(self, height: int, accent: str = ACCENT_WEATHER):
        self.width = CARD_WIDTH
        self.height = height
        self.accent = accent
        self.img = Image.new("RGB", (self.width, self.height), BG)
        self.draw = ImageDraw.Draw(self.img)
        self.y = 0

        # Draw panel background with rounded corners
        self._draw_panel()

    def _draw_panel(self):
        m = 8  # margin around panel
        self.draw.rounded_rectangle(
            [m, m, self.width - m, self.height - m],
            radius=12,
            fill=PANEL,
        )

    def draw_header(self, title: str, subtitle: str = ""):
        """Draw accent-colored header bar with title and optional subtitle."""
        self.y = 12
        # Accent bar
        self.draw.rectangle(
            [8, self.y, self.width - 8, self.y + 4],
            fill=self.accent,
        )
        self.y += 12
        # Title
        self.draw.text(
            (PADDING, self.y),
            title,
            fill=TEXT_PRIMARY,
            font=_font_bold(22),
        )
        if subtitle:
            self.draw.text(
                (PADDING, self.y + 28),
                subtitle,
                fill=TEXT_SECONDARY,
                font=_font(13),
            )
            self.y += 52
        else:
            self.y += 36
        return self

    def draw_separator(self):
        """Draw a thin horizontal separator line."""
        self.y += 4
        self.draw.line(
            [(PADDING, self.y), (self.width - PADDING, self.y)],
            fill=TEXT_MUTED,
            width=1,
        )
        self.y += SEPARATOR_HEIGHT - 4
        return self

    def draw_data_row(self, label: str, value: str, color: str = TEXT_PRIMARY):
        """Draw a label (left) + value (right) row."""
        font_label = _font(15)
        font_value = _font_bold(15)
        self.draw.text((PADDING, self.y), label, fill=TEXT_SECONDARY, font=font_label)
        # Right-align value
        bbox = self.draw.textbbox((0, 0), value, font=font_value)
        vw = bbox[2] - bbox[0]
        self.draw.text(
            (self.width - PADDING - vw, self.y),
            value,
            fill=color,
            font=font_value,
        )
        self.y += ROW_HEIGHT
        return self

    def draw_section_title(self, title: str, color: str = None):
        """Draw a small section title."""
        self.y += 4
        self.draw.text(
            (PADDING, self.y),
            title,
            fill=color or self.accent,
            font=_font_bold(14),
        )
        self.y += 24
        return self

    def draw_table_header(self, columns: list[tuple[str, int]]):
        """Draw table column headers.

        columns: list of (header_text, x_position)
        """
        font = _font_bold(13)
        for text, x in columns:
            self.draw.text((x, self.y), text, fill=TEXT_MUTED, font=font)
        self.y += 24
        return self

    def draw_table_row(self, cells: list[tuple[str, int, str]]):
        """Draw one table data row.

        cells: list of (text, x_position, color)
        """
        font = _font(14)
        for text, x, color in cells:
            self.draw.text((x, self.y), text, fill=color, font=font)
        self.y += ROW_HEIGHT
        return self

    def draw_status_dot(self, x: int, y: int, color: str):
        """Draw a small filled circle (status indicator)."""
        r = 5
        self.draw.ellipse([x - r, y - r, x + r, y + r], fill=color)

    def finalize(self) -> io.BytesIO:
        """Return the image as PNG bytes in a BytesIO buffer."""
        buf = io.BytesIO()
        self.img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf


# ---------------------------------------------------------------------------
# Per-category render functions
# ---------------------------------------------------------------------------

def render_weather(data: dict) -> tuple[io.BytesIO, str]:
    """Render weather card.

    data keys: location, temp_c, feels_like_c, humidity, wind_kph, wind_dir,
               pressure_mb, uv, condition, forecast (list of dicts with
               date, high_c, low_c, condition)

    Returns (png_bytes, html_caption).
    """
    forecast = data.get("forecast", [])
    n_forecast = min(len(forecast), 3)
    # Height: header(52) + 6 rows(192) + separator(16) + section_title(28)
    #   + forecast rows(n*32) + bottom padding(20)
    height = 52 + 6 * ROW_HEIGHT + SEPARATOR_HEIGHT + 28 + n_forecast * ROW_HEIGHT + 28

    card = CardRenderer(height, ACCENT_WEATHER)
    card.draw_header(f"{data.get('location', 'Weather')}", data.get("condition", ""))

    card.draw_data_row("Temperature", f"{data.get('temp_c', '?')}\u00b0C")
    card.draw_data_row("Feels Like", f"{data.get('feels_like_c', '?')}\u00b0C")
    card.draw_data_row("Humidity", f"{data.get('humidity', '?')}%")
    card.draw_data_row("Wind", f"{data.get('wind_kph', '?')} km/h {data.get('wind_dir', '')}")
    card.draw_data_row("Pressure", f"{data.get('pressure_mb', '?')} mb")
    card.draw_data_row("UV Index", str(data.get("uv", "?")))

    if forecast:
        card.draw_separator()
        card.draw_section_title("3-Day Forecast")
        for day in forecast[:3]:
            label = day.get("date", "")
            temps = f"{day.get('low_c', '?')}\u00b0 / {day.get('high_c', '?')}\u00b0C"
            cond = day.get("condition", "")
            card.draw_data_row(f"{label}  {cond}", temps)

    caption = (
        f"\U0001f324  <b>{data.get('location', 'Weather')}</b>\n"
        f"{data.get('temp_c', '?')}\u00b0C \u2014 {data.get('condition', '')}"
    )
    return card.finalize(), caption


def render_crypto(data: dict) -> tuple[io.BytesIO, str]:
    """Render crypto card with multiple coins.

    data keys: coins (list of dicts with ticker, name, usd, gbp, change_24h)

    Returns (png_bytes, html_caption).
    """
    coins = data.get("coins", [])
    # header(52) + table_header(24) + rows(n*30) + bottom(20)
    height = 52 + 24 + len(coins) * 30 + 24

    card = CardRenderer(height, ACCENT_CRYPTO)
    card.draw_header("Cryptocurrency", "Live prices via CoinGecko")

    # Table layout
    cols = [("COIN", PADDING), ("USD", 180), ("GBP", 310), ("24H", 440)]
    card.draw_table_header(cols)

    caption_parts = []
    for coin in coins:
        usd = coin.get("usd", 0)
        gbp = coin.get("gbp", 0)
        chg = coin.get("change_24h", 0)
        chg_color = GREEN if chg >= 0 else RED

        # Format price based on magnitude
        if usd >= 100:
            usd_str = f"${usd:,.0f}"
            gbp_str = f"\u00a3{gbp:,.0f}"
        elif usd >= 1:
            usd_str = f"${usd:,.2f}"
            gbp_str = f"\u00a3{gbp:,.2f}"
        else:
            usd_str = f"${usd:,.4f}"
            gbp_str = f"\u00a3{gbp:,.4f}"

        ticker = coin.get("ticker", "?")
        cells = [
            (f"{coin.get('name', '?')} ({ticker})", PADDING, TEXT_PRIMARY),
            (usd_str, 180, TEXT_PRIMARY),
            (gbp_str, 310, TEXT_SECONDARY),
            (f"{chg:+.1f}%", 440, chg_color),
        ]
        font = _font(13)
        for text, x, color in cells:
            card.draw.text((x, card.y), text, fill=color, font=font)
        card.y += 30

        if len(caption_parts) < 3:  # Top 3 in caption
            caption_parts.append(f"{ticker} ${usd:,.0f}" if usd >= 100 else f"{ticker} ${usd:,.2f}")

    caption = (
        f"\U0001f4b0 <b>Crypto Prices</b>\n"
        + " \u00b7 ".join(caption_parts)
    )
    return card.finalize(), caption


def render_trains(data: dict) -> tuple[io.BytesIO, str]:
    """Render train departures card.

    data keys: origin, destination, departures (list of dicts with
               scheduled, expected, platform, status, cancelled)

    Returns (png_bytes, html_caption).
    """
    departures = data.get("departures", [])
    n_rows = min(len(departures), 6)
    # header(52) + table_header(24) + rows(n*32) + bottom(24)
    height = 52 + 24 + n_rows * ROW_HEIGHT + 32

    card = CardRenderer(height, ACCENT_TRAINS)
    origin = data.get("origin", "?")
    dest = data.get("destination", "?")
    card.draw_header(f"{origin} \u2192 {dest}", "National Rail departures")

    # Table columns
    cols = [("TIME", PADDING), ("EXPECTED", 140), ("PLAT", 320), ("STATUS", 400)]
    card.draw_table_header(cols)

    for dep in departures[:6]:
        cancelled = dep.get("cancelled", False)
        status = dep.get("status", "")
        if cancelled:
            color = RED
            status = "Cancelled"
        elif status.lower() in ("on time", ""):
            color = GREEN
            status = status or "On Time"
        else:
            color = AMBER

        cells = [
            (dep.get("scheduled", "?"), PADDING, TEXT_PRIMARY),
            (dep.get("expected", "?"), 140, color),
            (dep.get("platform", "-"), 320, TEXT_SECONDARY),
            (status, 400, color),
        ]
        card.draw_table_row(cells)

    if not departures:
        card.y += 8
        card.draw.text(
            (PADDING, card.y),
            "No departures found",
            fill=TEXT_SECONDARY,
            font=_font(15),
        )

    caption = (
        f"\U0001f682 <b>{origin} \u2192 {dest}</b>\n"
        f"{n_rows} departure{'s' if n_rows != 1 else ''} shown"
    )
    return card.finalize(), caption


def render_gold(data: dict) -> tuple[io.BytesIO, str]:
    """Render gold/silver card.

    data keys: gold_usd, gold_gbp, gold_24h,
               silver_usd, silver_gbp, silver_24h

    Returns (png_bytes, html_caption).
    """
    height = 52 + 3 * ROW_HEIGHT + SEPARATOR_HEIGHT + 28 + 3 * ROW_HEIGHT + 28

    card = CardRenderer(height, ACCENT_GOLD)
    card.draw_header("Precious Metals", "Live spot prices")

    # Gold
    card.draw_section_title("Gold (XAU)")
    gold_chg = data.get("gold_24h", 0)
    gold_color = GREEN if gold_chg >= 0 else RED
    card.draw_data_row("USD/oz", f"${data.get('gold_usd', 0):,.2f}")
    card.draw_data_row("GBP/oz", f"\u00a3{data.get('gold_gbp', 0):,.2f}")
    card.draw_data_row("24h Change", f"{gold_chg:+.1f}%", gold_color)

    card.draw_separator()

    # Silver
    card.draw_section_title("Silver (XAG)")
    silver_chg = data.get("silver_24h", 0)
    silver_color = GREEN if silver_chg >= 0 else RED
    card.draw_data_row("USD/oz", f"${data.get('silver_usd', 0):,.2f}")
    card.draw_data_row("GBP/oz", f"\u00a3{data.get('silver_gbp', 0):,.2f}")
    card.draw_data_row("24h Change", f"{silver_chg:+.1f}%", silver_color)

    caption = (
        f"\U0001fA99 <b>Precious Metals</b>\n"
        f"Gold ${data.get('gold_usd', 0):,.0f}/oz \u00b7 Silver ${data.get('silver_usd', 0):,.2f}/oz"
    )
    return card.finalize(), caption


def render_status(data: dict) -> tuple[io.BytesIO, str]:
    """Render infrastructure status card.

    data keys: targets (list of dicts with name, status ('up'/'down'), job)

    Returns (png_bytes, html_caption).
    """
    targets = data.get("targets", [])
    n_rows = len(targets)
    # header(52) + table_header(24) + rows(n*32) + bottom(24)
    height = 52 + 24 + max(n_rows, 1) * ROW_HEIGHT + 32

    card = CardRenderer(height, ACCENT_STATUS)
    up_count = sum(1 for t in targets if t.get("status") == "up")
    down_count = n_rows - up_count
    subtitle = f"{up_count} up"
    if down_count:
        subtitle += f", {down_count} down"
    card.draw_header("Infrastructure Status", subtitle)

    # Table columns
    cols = [("NODE", PADDING), ("STATUS", 400)]
    card.draw_table_header(cols)

    for target in targets:
        name = target.get("name", "?")
        status = target.get("status", "unknown")
        is_up = status == "up"
        status_color = GREEN if is_up else RED
        status_text = "Online" if is_up else "Offline"

        cells = [
            (name, PADDING, TEXT_PRIMARY),
            (status_text, 400, status_color),
        ]
        card.draw_table_row(cells)
        # Draw status dot
        card.draw_status_dot(
            380, card.y - ROW_HEIGHT + 10,
            status_color,
        )

    if not targets:
        card.y += 8
        card.draw.text(
            (PADDING, card.y),
            "No targets found",
            fill=TEXT_SECONDARY,
            font=_font(15),
        )

    caption = (
        f"\U0001f5a5 <b>Infrastructure</b>\n"
        f"{up_count}/{n_rows} nodes online"
    )
    return card.finalize(), caption


def render_markets(data: dict) -> tuple[io.BytesIO, str]:
    """Render stock market indices card.

    data keys: title, indices (list of dicts with display_name, price,
               change_pct, market_open, region, error?)

    Returns (png_bytes, html_caption).
    """
    indices = data.get("indices", [])
    title = data.get("title", "Markets")
    # header(52) + table_header(24) + rows(n*30) + bottom(20)
    height = 52 + 24 + max(len(indices), 1) * 30 + 24

    card = CardRenderer(height, ACCENT_MARKETS)

    # Count open/closed
    open_count = sum(1 for i in indices if i.get("market_open"))
    if open_count == len(indices):
        subtitle = "Markets open"
    elif open_count == 0:
        subtitle = "Markets closed"
    else:
        subtitle = f"{open_count}/{len(indices)} open"
    card.draw_header(title, subtitle)

    # Table layout
    cols = [("INDEX", PADDING), ("PRICE", 260), ("CHANGE", 400), ("", 520)]
    card.draw_table_header(cols)

    caption_parts = []
    for idx in indices:
        if idx.get("error"):
            cells = [
                (idx.get("display_name", "?"), PADDING, TEXT_SECONDARY),
                ("unavailable", 260, TEXT_MUTED),
                ("", 400, TEXT_MUTED),
                ("", 520, TEXT_MUTED),
            ]
        else:
            price = idx.get("price", 0)
            chg = idx.get("change_pct", 0)
            is_open = idx.get("market_open", False)
            chg_color = GREEN if chg >= 0 else RED
            name = idx.get("display_name", "?")
            state_text = "" if is_open else "Closed"
            state_color = TEXT_MUTED

            cells = [
                (name, PADDING, TEXT_PRIMARY),
                (f"{price:,.0f}" if price >= 100 else f"{price:,.2f}", 260, TEXT_PRIMARY),
                (f"{chg:+.2f}%", 400, chg_color),
                (state_text, 520, state_color),
            ]
            caption_parts.append(f"{name} {chg:+.1f}%")

        font = _font(13)
        for text, x, color in cells:
            card.draw.text((x, card.y), text, fill=color, font=font)
        card.y += 30

    caption = (
        f"\U0001f4c8 <b>{title}</b>\n"
        + " \u00b7 ".join(caption_parts[:4])
    )
    return card.finalize(), caption


def render_forex(data: dict) -> tuple[io.BytesIO, str]:
    """Render forex / currency pairs card.

    data keys: pairs (list of dicts with pair, rate, change_pct)

    Returns (png_bytes, html_caption).
    """
    pairs = data.get("pairs", [])
    # header(52) + table_header(24) + rows(n*30) + bottom(20)
    height = 52 + 24 + max(len(pairs), 1) * 30 + 24

    card = CardRenderer(height, ACCENT_FOREX)
    card.draw_header("Foreign Exchange", "ECB reference rates")

    cols = [("PAIR", PADDING), ("RATE", 240), ("CHANGE", 420)]
    card.draw_table_header(cols)

    caption_parts = []
    for pair in pairs:
        rate = pair.get("rate", 0)
        chg = pair.get("change_pct", 0)
        chg_color = GREEN if chg >= 0 else RED
        name = pair.get("pair", "?")

        # Format rate — more decimals for small rates, fewer for big
        if rate >= 100:
            rate_str = f"{rate:,.2f}"
        else:
            rate_str = f"{rate:.4f}"

        cells = [
            (name, PADDING, TEXT_PRIMARY),
            (rate_str, 240, TEXT_PRIMARY),
            (f"{chg:+.2f}%", 420, chg_color),
        ]
        font = _font(14)
        for text, x, color in cells:
            card.draw.text((x, card.y), text, fill=color, font=font)
        card.y += 30

        caption_parts.append(f"{name} {rate_str}")

    caption = (
        f"\U0001f4b1 <b>Forex Rates</b>\n"
        + " \u00b7 ".join(caption_parts[:3])
    )
    return card.finalize(), caption


def render_world(market_data: dict, forex_data: dict, crypto_data: dict,
                 gold_data: dict) -> tuple[io.BytesIO, str]:
    """Render combined world markets snapshot card.

    Combines market indices, top forex pairs, top crypto, and gold into one card.
    """
    indices = market_data.get("indices", [])
    pairs = forex_data.get("pairs", [])[:3]  # Top 3 FX pairs
    coins = crypto_data.get("coins", [])[:2]  # BTC + ETH only
    # header(52) + indices section(28 + n*28) + sep(16) + fx section(28 + n*28)
    # + sep(16) + crypto section(28 + 2*28) + sep(16) + gold(28 + 2*28) + bottom(20)
    n_idx = len(indices)
    n_fx = len(pairs)
    n_crypto = len(coins)
    height = (52 + 28 + n_idx * 28 + 16 + 28 + n_fx * 28 + 16
              + 28 + n_crypto * 28 + 16 + 28 + 2 * 28 + 24)

    card = CardRenderer(height, ACCENT_MARKETS)

    # Count open markets
    open_count = sum(1 for i in indices if i.get("market_open"))
    card.draw_header("World Markets", f"{open_count}/{n_idx} markets open")

    # --- Indices ---
    card.draw_section_title("Stock Indices", ACCENT_MARKETS)
    for idx in indices:
        if idx.get("error"):
            continue
        name = idx.get("display_name", "?")
        price = idx.get("price", 0)
        chg = idx.get("change_pct", 0)
        is_open = idx.get("market_open", False)
        chg_color = GREEN if chg >= 0 else RED
        state = "" if is_open else " (Closed)"

        label = f"{name}{state}"
        value = f"{price:,.0f}  {chg:+.2f}%"

        font_l = _font(13)
        font_v = _font_bold(13)
        card.draw.text((PADDING, card.y), label, fill=TEXT_PRIMARY, font=font_l)
        bbox = card.draw.textbbox((0, 0), value, font=font_v)
        vw = bbox[2] - bbox[0]
        card.draw.text((card.width - PADDING - vw, card.y), value, fill=chg_color, font=font_v)
        card.y += 28

    # --- FX ---
    card.draw_separator()
    card.draw_section_title("Currencies", ACCENT_FOREX)
    for pair in pairs:
        name = pair.get("pair", "?")
        rate = pair.get("rate", 0)
        chg = pair.get("change_pct", 0)
        chg_color = GREEN if chg >= 0 else RED
        rate_str = f"{rate:.4f}" if rate < 100 else f"{rate:,.2f}"

        font_l = _font(13)
        font_v = _font_bold(13)
        card.draw.text((PADDING, card.y), name, fill=TEXT_PRIMARY, font=font_l)
        value = f"{rate_str}  {chg:+.2f}%"
        bbox = card.draw.textbbox((0, 0), value, font=font_v)
        vw = bbox[2] - bbox[0]
        card.draw.text((card.width - PADDING - vw, card.y), value, fill=chg_color, font=font_v)
        card.y += 28

    # --- Crypto ---
    card.draw_separator()
    card.draw_section_title("Crypto", ACCENT_CRYPTO)
    for coin in coins:
        ticker = coin.get("ticker", "?")
        usd = coin.get("usd", 0)
        chg = coin.get("change_24h", 0)
        chg_color = GREEN if chg >= 0 else RED

        font_l = _font(13)
        font_v = _font_bold(13)
        card.draw.text((PADDING, card.y), f"{coin.get('name', '?')} ({ticker})", fill=TEXT_PRIMARY, font=font_l)
        value = f"${usd:,.0f}  {chg:+.1f}%"
        bbox = card.draw.textbbox((0, 0), value, font=font_v)
        vw = bbox[2] - bbox[0]
        card.draw.text((card.width - PADDING - vw, card.y), value, fill=chg_color, font=font_v)
        card.y += 28

    # --- Gold ---
    card.draw_separator()
    card.draw_section_title("Metals", ACCENT_GOLD)
    for metal, key in [("Gold", "gold_usd"), ("Silver", "silver_usd")]:
        price = gold_data.get(key, 0)
        chg = gold_data.get(key.replace("_usd", "_24h"), 0)
        chg_color = GREEN if chg >= 0 else RED

        font_l = _font(13)
        font_v = _font_bold(13)
        card.draw.text((PADDING, card.y), metal, fill=TEXT_PRIMARY, font=font_l)
        chg_str = f"  {chg:+.1f}%" if chg else ""
        value = f"${price:,.2f}/oz{chg_str}"
        bbox = card.draw.textbbox((0, 0), value, font=font_v)
        vw = bbox[2] - bbox[0]
        card.draw.text((card.width - PADDING - vw, card.y), value, fill=chg_color if chg else TEXT_PRIMARY, font=font_v)
        card.y += 28

    caption = (
        f"\U0001f30d <b>World Markets Snapshot</b>\n"
        f"{open_count}/{n_idx} markets open"
    )
    return card.finalize(), caption
