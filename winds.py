"""
winds.py - HRRR Wind Gust fetcher for Colorado

Uses pygrib instead of cfgrib. pygrib reads GRIB2 synchronously —
no lazy loading, no race conditions with temp files, much simpler API.
"""

import os
import time
import pygrib
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta, timezone
from herbie import Herbie

HERBIE_DIR = Path(os.environ.get("HERBIE_DATA_DIR", "/tmp/herbie"))
HERBIE_DIR.mkdir(parents=True, exist_ok=True)

CO_LAT_MIN = 36.8
CO_LAT_MAX = 41.2
CO_LON_MIN = -109.2
CO_LON_MAX = -101.9

_CACHE = {}


def _now_utc_hour_naive():
    return datetime.utcnow().replace(minute=0, second=0, microsecond=0)


def _find_latest_hrrr_cycle(max_lookback_hours=6):
    base = _now_utc_hour_naive()
    for h in range(max_lookback_hours + 1):
        candidate = base - timedelta(hours=h)
        try:
            H = Herbie(candidate, model="hrrr", product="sfc", fxx=0,
                       save_dir=str(HERBIE_DIR), overwrite=False)
            H.inventory()
            return candidate
        except Exception:
            continue
    return base - timedelta(hours=2)


def fetch_hrrr_gusts(fxx=1):
    cycle = _find_latest_hrrr_cycle()
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    # Download the full sfc GRIB2 to a stable persistent path
    H = Herbie(cycle, model="hrrr", product="sfc", fxx=fxx,
               save_dir=str(HERBIE_DIR), overwrite=False)
    grib_path = Path(H.download())

    if not grib_path.exists():
        raise FileNotFoundError(f"GRIB2 file not found after download: {grib_path}")

    # pygrib reads synchronously - no lazy loading, no race conditions
    grbs = pygrib.open(str(grib_path))

    # Select the 10 m wind gust field
    # pygrib.select() raises ValueError if nothing matches
    try:
        msgs = grbs.select(name="Wind speed (gust)", typeOfLevel="heightAboveGround", level=10)
    except ValueError:
        # Try a broader search if the exact name doesn't match
        try:
            msgs = grbs.select(shortName="gust", typeOfLevel="heightAboveGround", level=10)
        except ValueError:
            grbs.close()
            raise ValueError(
                "Could not find 'Wind speed (gust)' at 10 m above ground in HRRR sfc file. "
                "Check pygrib field names with grbs.read() to debug."
            )

    msg = msgs[0]

    # .values reads the full array into RAM immediately - synchronous, safe
    gust_arr, lat2d, lon2d = msg.data()
    grbs.close()

    # HRRR longitudes are 0-360, convert to -180/+180
    lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)

    # Sanity check: surface gusts should be 0-100 m/s
    raw_max = float(np.nanmax(gust_arr))
    raw_min = float(np.nanmin(gust_arr))
    if raw_max > 150 or raw_min < 0:
        raise ValueError(
            f"Gust values out of physical range "
            f"(min={raw_min:.1f}, max={raw_max:.1f} m/s). Wrong GRIB field."
        )

    # Clip to Colorado bounding box
    mask = (
        (lat2d >= CO_LAT_MIN) & (lat2d <= CO_LAT_MAX) &
        (lon2d >= CO_LON_MIN) & (lon2d <= CO_LON_MAX)
    )
    rows, cols = np.where(mask)
    if len(rows) == 0:
        raise ValueError("No HRRR grid points found inside Colorado bounding box.")

    r0, r1 = rows.min(), rows.max() + 1
    c0, c1 = cols.min(), cols.max() + 1

    # Downsample 2x (~6 km effective resolution, ~7500 points for Leaflet)
    step    = 2
    lat_ds  = lat2d[r0:r1, c0:c1][::step, ::step]
    lon_ds  = lon2d[r0:r1, c0:c1][::step, ::step]
    gust_ds = gust_arr[r0:r1, c0:c1][::step, ::step] * 1.94384  # m/s -> knots

    points = []
    for i in range(lat_ds.shape[0]):
        for j in range(lat_ds.shape[1]):
            g = float(gust_ds[i, j])
            if np.isnan(g):
                continue
            points.append({
                "lat":     round(float(lat_ds[i, j]), 4),
                "lon":     round(float(lon_ds[i, j]), 4),
                "gust_kt": round(g, 1),
            })

    valid_dt = (cycle + timedelta(hours=fxx)).replace(tzinfo=timezone.utc)
    return {
        "model":         "HRRR",
        "cycle_utc":     cycle_aware.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "valid_utc":     valid_dt.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "fxx":           fxx,
        "cell_size_deg": 0.055,
        "point_count":   len(points),
        "points":        points,
    }


def get_hrrr_gusts_cached(fxx=1, ttl_seconds=600):
    now    = time.time()
    cached = _CACHE.get(fxx)
    if cached is None or (now - cached["ts"]) > ttl_seconds:
        _CACHE[fxx] = {"ts": now, "data": fetch_hrrr_gusts(fxx=fxx)}
    return _CACHE[fxx]["data"]
