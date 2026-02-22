"""
winds.py - HRRR Wind Gust fetcher for Colorado
Downloads the full HRRR sfc GRIB2, opens it directly with cfgrib
(no searchstring subsetting), clips to Colorado, and returns a
list of grid-cell dicts for the Leaflet map.
"""

import os
import time
import cfgrib
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta, timezone
from herbie import Herbie

# File cache directory
HERBIE_DIR = Path(os.environ.get("HERBIE_DATA_DIR", "/tmp/herbie"))
HERBIE_DIR.mkdir(parents=True, exist_ok=True)

# Colorado bounding box (with a little padding)
CO_LAT_MIN = 36.8
CO_LAT_MAX = 41.2
CO_LON_MIN = -109.2
CO_LON_MAX = -101.9

# Simple in-memory cache
_CACHE = {"ts": 0.0, "data": None}


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


def fetch_hrrr_gusts(fxx=0):
    """
    Download HRRR surface wind gusts over Colorado and return
    a JSON-serialisable dict for the Leaflet map.
    """
    cycle = _find_latest_hrrr_cycle()
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    # Build Herbie object and download the full GRIB2 file
    H = Herbie(
        cycle,
        model="hrrr",
        product="sfc",
        fxx=fxx,
        save_dir=str(HERBIE_DIR),
        overwrite=False,
    )

    # H.download() returns the local Path to the full GRIB2 file
    local_path = H.download()

    # Open directly with cfgrib - filter for gust shortName only
    # indexpath='' keeps the index in memory rather than writing to disk
    datasets = cfgrib.open_datasets(
        str(local_path),
        backend_kwargs={"indexpath": ""},
        filter_by_keys={"shortName": "gust"},
    )

    if not datasets:
        raise ValueError(
            "No gust variable found in HRRR sfc GRIB2. "
            "shortName='gust' not present in this file."
        )

    # Grab the first dataset that has data variables
    ds = None
    for candidate_ds in datasets:
        if len(candidate_ds.data_vars) > 0:
            ds = candidate_ds
            break

    if ds is None:
        raise ValueError("All gust datasets were empty.")

    # Get the first (should be only) data variable
    vname = list(ds.data_vars)[0]
    gust_da = ds[vname]

    # Load ALL data into RAM immediately - critical to avoid lazy read issues
    gust_da = gust_da.load()

    # Get lat/lon grids - HRRR uses 2D arrays on Lambert Conformal projection
    lat2d = gust_da.coords["latitude"].values
    lon2d = gust_da.coords["longitude"].values

    # HRRR longitudes are 0-360, convert to -180/+180
    lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)

    # Gust values in m/s
    gust_arr = gust_da.values

    # Clip to Colorado
    mask = (
        (lat2d >= CO_LAT_MIN) & (lat2d <= CO_LAT_MAX) &
        (lon2d >= CO_LON_MIN) & (lon2d <= CO_LON_MAX)
    )
    rows, cols = np.where(mask)

    if len(rows) == 0:
        raise ValueError("No HRRR grid points found inside Colorado bounding box.")

    r0, r1 = rows.min(), rows.max() + 1
    c0, c1 = cols.min(), cols.max() + 1

    lat_co  = lat2d[r0:r1, c0:c1]
    lon_co  = lon2d[r0:r1, c0:c1]
    gust_co = gust_arr[r0:r1, c0:c1]

    # Convert m/s to knots
    gust_kt = gust_co * 1.94384

    # Downsample 2x (~6 km effective resolution, ~7500 points)
    step = 2
    lat_ds  = lat_co[::step, ::step]
    lon_ds  = lon_co[::step, ::step]
    gust_ds = gust_kt[::step, ::step]

    # Build point list for Leaflet
    points = []
    ny, nx = lat_ds.shape
    for i in range(ny):
        for j in range(nx):
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


def get_hrrr_gusts_cached(fxx=0, ttl_seconds=600):
    """
    Cache wrapper - re-fetches at most every ttl_seconds.
    HRRR updates hourly so 600s (10 min) is plenty.
    """
    now = time.time()
    if _CACHE["data"] is None or (now - _CACHE["ts"]) > ttl_seconds:
        _CACHE["data"] = fetch_hrrr_gusts(fxx=fxx)
        _CACHE["ts"] = now
    return _CACHE["data"]
