import time
import requests

_CACHE = {"ts": 0, "data": None}

def fetch_metars(stations: list[str]) -> dict:
    station_str = ",".join([s.strip().upper() for s in stations if s.strip()])

    url = "https://aviationweather.gov/api/data/metar"
    params = {
        "ids": station_str,
        "format": "json",
    }

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()

    # AWC returns JSON for format=json; this should now be safe.
    data = r.json()

    return {
        "stations": stations,
        "count": len(data) if isinstance(data, list) else None,
        "data": data,
        "source": "aviationweather.gov/api/data/metar",
    }

def get_metars_cached(stations: list[str], ttl_seconds: int = 120) -> dict:
    now = time.time()
    if _CACHE["data"] is None or (now - _CACHE["ts"]) > ttl_seconds:
        _CACHE["data"] = fetch_metars(stations)
        _CACHE["ts"] = now
    return _CACHE["data"]
