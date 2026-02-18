import os
import time
from datetime import datetime, timezone, timedelta

import numpy as np

# Herbie pulls model data from multiple sources and loads into xarray
from herbie import Herbie

_CACHE = {"ts": 0, "data": None}

# Start with a small built-in airport list; expand later.
# (You can also move this to a JSON file later.)
AIRPORTS = {
    "KMCI": (39.2975, -94.7309),
    "KMKC": (39.1279, -94.5892),
    "KSTL": (38.7525, -90.3734),
}

def _now_utc_hour_naive():
    # Herbie is happiest with naive datetimes representing UTC
    return datetime.utcnow().replace(minute=0, second=0, microsecond=0)

def _find_latest_cycle(max_lookback_hours: int = 8) -> datetime:
    base = _now_utc_hour_naive()
    for h in range(0, max_lookback_hours + 1):
        dt = base - timedelta(hours=h)
        try:
            H = Herbie(dt, model="rap", product="awp", fxx=0)
            _ = H.inventory()
            return dt
        except Exception:
            continue
    return base

def _ds_select_nearest(ds, lat: float, lon: float):
    """
    Robustly select nearest grid point regardless of coord naming.
    """
    # Common coord names
    for yname, xname in [("latitude", "longitude"), ("lat", "lon")]:
        if yname in ds.coords and xname in ds.coords:
            return ds.sel({yname: lat, xname: lon}, method="nearest")
    # Some GRIB loads use 2D lat/lon; fallback: find nearest by brute force
    if "latitude" in ds and "longitude" in ds:
        lat2 = ds["latitude"].values
        lon2 = ds["longitude"].values
        d2 = (lat2 - lat) ** 2 + (lon2 - lon) ** 2
        iy, ix = np.unravel_index(np.nanargmin(d2), d2.shape)
        return ds.isel(y=iy, x=ix)
    raise ValueError("Could not find lat/lon coordinates in dataset.")

def _wind_speed(u, v):
    return float(np.sqrt(u*u + v*v))

def fetch_rap_point_guidance(stations: list[str], fxx_max: int = 6) -> dict:
    cycle = _find_latest_cycle()
    results = {}
    errors = {}

    for stn in stations:
        stn = stn.strip().upper()
        if not stn:
            continue
        if stn not in AIRPORTS:
            errors[stn] = "Unknown station (not in AIRPORTS dict yet)."
            continue

        lat, lon = AIRPORTS[stn]
        series = []

        for fxx in range(0, fxx_max + 1):
            try:
                H = Herbie(cycle, model="rap", product="awp", fxx=fxx)

                # Pull only what we need: 10m wind and 925mb wind (good LL proxy)
                # Regex selects u/v at those levels.
                ds = H.xarray(":(UGRD|VGRD):(10 m above ground|925 mb):", remove_grib=True)

                p = _ds_select_nearest(ds, lat, lon)

                # Names depend on cfgrib; search variables by pattern
                # Typical: u10 / v10 not guaranteed; so we locate by attrs.
                u10 = v10 = u925 = v925 = None
                for name, da in p.data_vars.items():
                    s = str(da.attrs)
                    if "10 m above ground" in s and "UGRD" in s:
                        u10 = float(da.values)
                    if "10 m above ground" in s and "VGRD" in s:
                        v10 = float(da.values)
                    if "925 mb" in s and "UGRD" in s:
                        u925 = float(da.values)
                    if "925 mb" in s and "VGRD" in s:
                        v925 = float(da.values)

                if None in (u10, v10, u925, v925):
                    raise ValueError("Missing one or more required wind components in parsed data.")

                spd10 = _wind_speed(u10, v10)
                spd925 = _wind_speed(u925, v925)

                # Simple low-level shear proxy (vector magnitude difference)
                shear = _wind_speed(u925 - u10, v925 - v10)

                valid_utc = valid.replace(tzinfo=timezone.utc).isoformat(timespec="minutes").replace("+00:00", "Z")
                series.append({
                    "fxx": fxx,
                    "valid_utc": valid.isoformat(timespec="minutes").replace("+00:00", "Z"),
                    "wind10_kt": round(spd10 * 1.94384, 1),
                    "wind925_kt": round(spd925 * 1.94384, 1),
                    "shear_kt": round(shear * 1.94384, 1),
                })
            except Exception as e:
                errors.setdefault(stn, []).append(f"f{fxx:02d}: {e}")

        results[stn] = {
            "lat": lat,
            "lon": lon,
            "series": series
        }

    return {
        "model": "RAP",
        "product": "awp",
        cycle_aware = cycle.replace(tzinfo=timezone.utc)
        "cycle_utc": cycle_aware.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "fxx_max": fxx_max,
        "stations": stations,
        "results": results,
        "errors": errors,
    }

def get_rap_point_guidance_cached(stations: list[str], ttl_seconds: int = 600, fxx_max: int = 6) -> dict:
    now = time.time()
    if _CACHE["data"] is None or (now - _CACHE["ts"]) > ttl_seconds:
        _CACHE["data"] = fetch_rap_point_guidance(stations, fxx_max=fxx_max)
        _CACHE["ts"] = now
    return _CACHE["data"]
