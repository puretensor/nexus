"""Weather API client using wttr.in (no auth required)."""

import os
from dispatcher.apis import get_session, ttl_cache, DispatchError

DEFAULT_LOCATION = os.environ.get("WEATHER_DEFAULT_LOCATION", "London,UK")


@ttl_cache(seconds=300)
async def fetch_weather(location: str | None = None) -> dict:
    """Fetch current weather and 3-day forecast from wttr.in.

    Returns dict suitable for cards.render_weather().
    """
    loc = location or DEFAULT_LOCATION
    url = f"https://wttr.in/{loc}?format=j1"

    session = await get_session()
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise DispatchError(f"Weather API returned {resp.status} for '{loc}'")
            data = await resp.json(content_type=None)
    except DispatchError:
        raise
    except Exception as e:
        raise DispatchError(f"Weather fetch failed: {e}")

    try:
        current = data["current_condition"][0]
        weather_data = data.get("weather", [])

        result = {
            "location": loc.replace("+", " "),
            "temp_c": current.get("temp_C", "?"),
            "feels_like_c": current.get("FeelsLikeC", "?"),
            "humidity": current.get("humidity", "?"),
            "wind_kph": current.get("windspeedKmph", "?"),
            "wind_dir": current.get("winddir16Point", ""),
            "pressure_mb": current.get("pressure", "?"),
            "uv": current.get("uvIndex", "?"),
            "condition": current.get("weatherDesc", [{}])[0].get("value", ""),
            "forecast": [],
        }

        for day in weather_data[:3]:
            from datetime import datetime
            date_str = day.get("date", "")
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                label = dt.strftime("%a %d %b")
            except ValueError:
                label = date_str
            result["forecast"].append({
                "date": label,
                "high_c": day.get("maxtempC", "?"),
                "low_c": day.get("mintempC", "?"),
                "condition": day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "")
                if len(day.get("hourly", [])) > 4 else "",
            })

        return result
    except (KeyError, IndexError) as e:
        raise DispatchError(f"Unexpected weather data format: {e}")
