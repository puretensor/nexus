"""Dispatcher subsystem — slash-command card responses bypassing Claude."""
from dispatcher.router import (
    refresh_dispatch,
    extract_weather_location,
    extract_stations,
    handle_weather,
    handle_markets_unified,
    handle_trains,
    handle_status,
)
from dispatcher.apis import DispatchError
