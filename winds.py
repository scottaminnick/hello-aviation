"""
winds.py - HRRR Wind Gust fetcher for Colorado

Key lessons learned:
  - subset_* files (from searchString downloads) are deleted by Herbie before
    cfgrib finishes reading them. Never use H.xarray() or H.download(searchString).
  - H.download() with NO searchString saves a persistent full GRIB2 file.
  - cfgrib filter_by_keys must be tight enough to match exactly one field,
    or cfgrib iterates every message (slow + warning spam).
  - .load() must be called immediately after open to pull data into RAM.
"""

import os
import time
import cfgrib
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


def _open_gust_from_grib(grib_path: Path) -> np.ndarray:
    """
    Try progressively looser cfgrib filters until we find the gust field.
    Returns a loaded DataArray (already in RAM).
    Never falls back to no filter at all - that opens every message.
    """
    filters_to_try = [
        {"shortName": "gust", "typeOfLevel": "heightAboveGround", "level": 10},
        {"shortName": "gust", "typeOfLevel": "heightAboveGround"},
        {"shortName": "gust", "stepType": "instant"},
        {"shortName": "gust"},
    ]

    for filt in filters_to_try:
        try:
            datasets = cfgrib.open_datasets(
                str(grib_path),
                backend_kwargs={"indexpath": ""},
                filter_by_keys=filt,
            )
        except Exception:
            continue

        ds = next((d for d in datasets if len(d.data_vars) > 0), None)
        if ds is None:
            continue

        vname   = list(ds.data_vars)[0]
        gust_da = ds[vname].load()   # into RAM NOW

        raw_max = float(np.nanmax(gust_da.values))
        raw_min = float(np.nanmin(gust_da.values))

        # Valid surface gusts: 0-100 m/s
        if 0 <= raw_min and raw_max <= 150:
            return gust_da   # good field found

        # Wrong field (huge values) - try next filter
        continue

    raise ValueError(
        f"Could not find a physically plausible gust field in {grib_path.name}. "
        "All filters either matched nothing or returned out-of-range values."
    )


def fetch_hrrr_gusts(fxx=1):
    cycle = _find_latest_hrrr_cycle()
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    H = Herbie(cycle, model="hrrr", product="sfc", fxx=fxx,
               save_dir=str(HERBIE_DIR), overwrite=False)

    # Download the FULL sfc GRIB2 - no searchString.
    # This writes to a stable, persistent path (no subset_ prefix).
    grib_path = Path(H.download())

    if not grib_path.exists():
        raise FileNotFoundError(f"Expected GRIB2 file not found: {grib_path}")

    gust_da = _open_gust_from_grib(grib_path)

    lat2d    = gust_da.coords["latitude"].values
    lon2d    = gust_da.coords["longitude"].values
    lon2d    = np.where(lon2d > 180, lon2d - 360, lon2d)
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
