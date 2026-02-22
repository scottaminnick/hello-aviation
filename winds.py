"""
winds.py - HRRR Wind Gust fetcher for Colorado

Strategy: use H.download(searchString=...) to byte-range download ONLY
the GUST field into a stable file path, then open with cfgrib and
immediately .load() into RAM before anything can delete the file.
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


def fetch_hrrr_gusts(fxx=1):
    cycle = _find_latest_hrrr_cycle()
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    H = Herbie(cycle, model="hrrr", product="sfc", fxx=fxx,
               save_dir=str(HERBIE_DIR), overwrite=False)

    # Download ONLY the gust bytes into a file we control.
    # searchString does a byte-range request - tiny download vs full 50MB file.
    grib_path = H.download(searchString=":GUST:10 m above ground:")

    # grib_path may be a Path or a list of Paths - normalise
    if isinstance(grib_path, list):
        grib_path = grib_path[0]
    grib_path = Path(grib_path)

    if not grib_path.exists():
        raise FileNotFoundError(f"Herbie download returned path that doesn't exist: {grib_path}")

    # Open with cfgrib directly - no filter needed since file only has gust
    datasets = cfgrib.open_datasets(
        str(grib_path),
        backend_kwargs={"indexpath": ""},
    )

    if not datasets:
        raise ValueError("cfgrib found no datasets in the downloaded gust GRIB file.")

    ds = next((d for d in datasets if len(d.data_vars) > 0), None)
    if ds is None:
        raise ValueError("All datasets from gust GRIB were empty.")

    vname   = list(ds.data_vars)[0]

    # .load() pulls ALL data into RAM right now, while the file still exists
    gust_da = ds[vname].load()

    # Sanity check: surface gusts should be 0-100 m/s
    raw_max = float(np.nanmax(gust_da.values))
    raw_min = float(np.nanmin(gust_da.values))
    if raw_max > 150 or raw_min < 0:
        raise ValueError(
            f"Gust values out of physical range "
            f"(min={raw_min:.1f}, max={raw_max:.1f} m/s). Wrong GRIB field."
        )

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
