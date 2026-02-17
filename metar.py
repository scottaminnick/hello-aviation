import os
import time
import requests

_CACHE = {"ts": 0, "data": None}

def fetch_metars(stations: list[str]) -> dict:
    """
    Fetch recent METARs as JSON using an HTTP endpoint.
    Uses aviationweather.gov JSON endpoint if available.
    """
    # AviationWeather has a JSON API endpoint for METARs.
    # If this ever changes, we can swap providers without changing the rest of your app.
    station_str = ",".join(stations)

    url = "https://aviationweather.gov/cgi-bin/data/metar.php"
    params = {"ids": station_str, "format": "json"}

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return {"stations": stations, "raw": r.json()}

def get_metars_cached(stations: list[str], ttl_seconds: int = 120) -> dict:
    now = time.time()
    if _CACHE["data"] is None or (now - _CACHE["ts"]) > ttl_seconds:
        _CACHE["data"] = fetch_metars(stations)
        _CACHE["ts"] = now
    return _CACHE["data"]
