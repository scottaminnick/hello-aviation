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

# In-memory cache - keyed by fxx so different hours don't collide
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
    Download HRRR surface wind gusts over Colorado and return
    a JSON-serialisable dict for the Leaflet map.

    Note: fxx=0 (analysis hour) often has uninitialized or accumulated
    gust values in HRRR. fxx=1 is the first reliable forecast hour.
    We filter for stepType='instant' to get the instantaneous gust field,
    not the max-over-period accumulated field.
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

    local_path = H.download()

    # Filter for instantaneous gust (not max-over-period)
    datasets = cfgrib.open_datasets(
        str(local_path),
        backend_kwargs={"indexpath": ""},
        filter_by_keys={"shortName": "gust", "stepType": "instant"},
    )

    # Fallback without stepType if instant filter finds nothing
    if not datasets or all(len(d.data_vars) == 0 for d in datasets):
        datasets = cfgrib.open_datasets(
            str(local_path),
            backend_kwargs={"indexpath": ""},
            filter_by_keys={"shortName": "gust"},
        )

    if not datasets:
        raise ValueError("No gust variable found in HRRR sfc GRIB2.")

    ds = None
    for candidate_ds in datasets:
        if len(candidate_ds.data_vars) > 0:
            ds = candidate_ds
            break

    if ds is None:
        raise ValueError("All gust datasets were empty.")

    vname = list(ds.data_vars)[0]
    gust_da = ds[vname].load()  # load into RAM immediately

    lat2d = gust_da.coords["latitude"].values
    lon2d = gust_da.coords["longitude"].values
    lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
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
    gust_kt = gust_arr[r0:r1, c0:c1] * 1.94384  # m/s -> knots

    # Downsample 2x (~6 km, ~7500 points for Leaflet performance)
    step = 2
    lat_ds  = lat_co[::step, ::step]
    lon_ds  = lon_co[::step, ::step]
    gust_ds = gust_kt[::step, ::step]

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


def get_hrrr_gusts_cached(fxx=1, ttl_seconds=600):
    """
    Cache wrapper keyed by fxx so different forecast hours don't overwrite
    each other. Re-fetches at most every ttl_seconds.
    """
    now = time.time()
    cached = _CACHE.get(fxx)

    if cached is None or (now - cached["ts"]) > ttl_seconds:
        _CACHE[fxx] = {
            "ts":   now,
            "data": fetch_hrrr_gusts(fxx=fxx),
        }

    return _CACHE[fxx]["data"]
