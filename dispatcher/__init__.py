"""Dispatcher subsystem — slash-command card responses bypassing Claude."""
from dispatcher.router import (
    refresh_dispatch,
    extract_weather_location,
    extract_stations,
    handle_weather,
    handle_crypto,
    handle_trains,
    handle_gold,
    handle_status,
    handle_markets,
    handle_forex,
    handle_world,
)
from dispatcher.apis import DispatchError
