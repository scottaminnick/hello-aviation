"""
winds.py - HRRR Wind Gust fetcher for Colorado

Uses Herbie's searchstring (byte-range) download to fetch ONLY the
GUST:10 m above ground field (~KB instead of the full ~50 MB GRIB2).
Immediately calls .load() to pull data into RAM before the temp file
is released, avoiding lazy-read file-not-found errors.
"""

import os
import time
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

# In-memory cache keyed by fxx
_CACHE = {}


def _now_utc_hour_naive():
    return datetime.utcnow().replace(minute=0, second=0, microsecond=0)


def _find_latest_hrrr_cycle(max_lookback_hours=6):
    base = _now_utc_hour_naive()
    for h in range(max_lookback_hours + 1):
        candidate = base - timedelta(hours=h)
        try:
            H = Herbie(
                candidate,
                model="hrrr",
                product="sfc",
                fxx=0,
                save_dir=str(HERBIE_DIR),
                overwrite=False,
            )
            H.inventory()
            return candidate
        except Exception:
            continue
    return base - timedelta(hours=2)


def fetch_hrrr_gusts(fxx=1):
    """
    Fetch HRRR 10 m wind gust over Colorado using Herbie's byte-range
    searchstring download -- grabs only the GUST field, not the full file.
    """
    cycle = _find_latest_hrrr_cycle()
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    H = Herbie(
        cycle,
        model="hrrr",
        product="sfc",
        fxx=fxx,
        save_dir=str(HERBIE_DIR),
        overwrite=False,
    )

    # H.xarray() with a searchstring does a byte-range download of just
    # the matching GRIB message -- much faster than the full file.
    # remove_grib=False keeps the temp file alive while we call .load().
    ds = H.xarray(":GUST:10 m above ground:", remove_grib=False)

    # Herbie may return a Dataset or a list -- normalise to Dataset
    if isinstance(ds, list):
        if len(ds) == 0:
            raise ValueError("Herbie returned empty list for GUST searchstring.")
        import xarray as xr
        ds = xr.merge(ds, compat="override")

    # Find the gust variable (usually named 'gust' or 'si10')
    vname = None
    for name in ds.data_vars:
        vname = name
        break
    if vname is None:
        raise ValueError(f"No data variables in dataset. vars={list(ds.data_vars)}")

    # Load into RAM immediately -- critical before temp file is released
    gust_da = ds[vname].load()

    # Sanity check: surface gusts should be 0-100 m/s
    raw_max = float(np.nanmax(gust_da.values))
    raw_min = float(np.nanmin(gust_da.values))
    if raw_max > 150 or raw_min < 0:
        raise ValueError(
            f"Gust values out of physical range "
            f"(min={raw_min:.1f}, max={raw_max:.1f} m/s). Wrong field grabbed."
        )

    # Lat/lon - HRRR uses 2D Lambert Conformal grids
    lat2d = gust_da.coords["latitude"].values
    lon2d = gust_da.coords["longitude"].values
    lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
    gust_arr = gust_da.values

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

    # Downsample 2x (~6 km, ~7500 points for Leaflet performance)
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
    """
    Cache wrapper keyed by fxx so different forecast hours don't collide.
    Re-fetches at most every ttl_seconds (HRRR updates hourly, 600s is fine).
    """
    now    = time.time()
    cached = _CACHE.get(fxx)
    if cached is None or (now - cached["ts"]) > ttl_seconds:
        _CACHE[fxx] = {"ts": now, "data": fetch_hrrr_gusts(fxx=fxx)}
    return _CACHE[fxx]["data"]
