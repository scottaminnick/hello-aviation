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

from datetime import datetime, timezone

def summarize_metars(metar_data: dict) -> list[dict]:
    """
    Convert AWC METAR JSON into a small, consistent summary list for the UI.
    """
    out = []
    for m in metar_data.get("data", []):
        # Time: prefer reportTime; fallback to obsTime (epoch seconds)
        rt = m.get("reportTime")
        if rt:
            time_utc = rt.replace(".000Z", "Z")
        else:
            obs = m.get("obsTime")
            time_utc = datetime.fromtimestamp(obs, tz=timezone.utc).isoformat(timespec="minutes").replace("+00:00", "Z") if obs else "—"

        # Wind formatting
        wdir = m.get("wdir")
        wspd = m.get("wspd")
        wgst = m.get("wgst")
        if wdir is None or wspd is None:
            wind = "—"
        else:
            wind = f"{int(wdir):03d}/{int(wspd)}"
            if wgst is not None:
                wind += f"G{int(wgst)}"

        # Ceiling/cover quick read
        cover = m.get("cover") or "—"
        clouds = m.get("clouds") or []
        # Grab the lowest layer base if available
        bases = [c.get("base") for c in clouds if c.get("base") is not None]
        ceil = f"{min(bases)} ft" if bases else "—"

        out.append({
            "icao": m.get("icaoId", "—"),
            "name": m.get("name", ""),
            "time_utc": time_utc,
            "fltCat": m.get("fltCat", "—"),
            "wind": wind,
            "vis": m.get("visib", "—"),
            "cover": cover,
            "ceiling": ceil,
            "temp_c": m.get("temp"),
            "dewp_c": m.get("dewp"),
            "altim_hpa": m.get("altim"),
            "raw": m.get("rawOb", ""),
        })
    return out
