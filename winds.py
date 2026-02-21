"""
winds.py — HRRR Wind Gust fetcher for Colorado
------------------------------------------------
Uses Herbie to pull the surface wind gust field from the latest HRRR run,
clips to a Colorado bounding box, converts m/s → knots, and returns a
list of grid-cell dicts that the Leaflet map can render.

HRRR runs every hour at 3 km resolution. Over Colorado that's roughly
200 × 150 = 30,000 grid points — too many for smooth Leaflet SVG rendering.
We downsample by a factor of 2 (every other point in both directions) to
get ~7,500 points at ~6 km effective spacing. The map still looks great at
that density and the browser stays happy.
"""

import os
import time
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta, timezone

import xarray as xr
from herbie import Herbie

# ── File cache directory (same as rap_point.py for consistency) ─────────────
HERBIE_DIR = Path(os.environ.get("HERBIE_DATA_DIR", "/tmp/herbie"))
HERBIE_DIR.mkdir(parents=True, exist_ok=True)

# ── Colorado bounding box (a little padding beyond the state lines) ──────────
CO_LAT_MIN = 36.8
CO_LAT_MAX = 41.2
CO_LON_MIN = -109.2
CO_LON_MAX = -101.9

# ── Simple in-memory cache ────────────────────────────────────────────────────
# Stores the last successful result plus a timestamp.
# Cache is invalidated after ttl_seconds (default 10 min) so we
# don't hammer HRRR/S3 on every page load.
_CACHE: dict = {"ts": 0.0, "data": None}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_utc_hour_naive() -> datetime:
    """Return current UTC time rounded down to the hour, timezone-naïve.
    Herbie works best with naïve datetimes that represent UTC."""
    return datetime.utcnow().replace(minute=0, second=0, microsecond=0)


def _find_latest_hrrr_cycle(max_lookback_hours: int = 6) -> datetime:
    """
    Walk backwards from the current hour until we find an HRRR cycle
    that actually has data on the server. HRRR is usually available
    within ~45 min of the top of the hour, so looking back 2-3 hours
    is almost always enough. We try up to 6 to be safe.
    """
    base = _now_utc_hour_naive()
    for h in range(max_lookback_hours + 1):
        candidate = base - timedelta(hours=h)
        try:
            H = Herbie(
                candidate,
                model="hrrr",
                product="sfc",   # surface fields — contains wind gusts
                fxx=0,
                save_dir=str(HERBIE_DIR),
                overwrite=False,
            )
            _ = H.inventory()   # raises if the file doesn't exist yet
            return candidate
        except Exception:
            continue
    # Absolute fallback: return 2 hours ago and let the caller deal with it
    return base - timedelta(hours=2)


def _extract_gust_variable(ds: xr.Dataset | xr.DataArray) -> xr.DataArray:
    """
    Herbie.xarray() can return a Dataset, a DataArray, or a list of either.
    This helper normalises to a single DataArray containing wind gusts.
    """
    # Unwrap list
    if isinstance(ds, list):
        ds = ds[0]

    # DataArray — return directly
    if isinstance(ds, xr.DataArray):
        return ds

    # Dataset — find the gust variable by name or GRIB attribute
    for vname, da in ds.data_vars.items():
        short = str(da.attrs.get("GRIB_shortName", "")).lower()
        if "gust" in vname.lower() or short in ("gust", "fg"):
            return da

    # Last resort: grab the first data variable
    first = list(ds.data_vars)[0]
    return ds[first]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_hrrr_gusts(fxx: int = 0) -> dict:
    """
    Download HRRR surface wind gusts, clip to Colorado, and return a
    JSON-serialisable dict ready for the Leaflet map.

    Parameters
    ----------
    fxx : forecast hour (0 = analysis/most current, up to 18)

    Returns
    -------
    dict with keys:
        model, cycle_utc, valid_utc, fxx, cell_size_deg,
        point_count, points (list of {lat, lon, gust_kt})
    """
    cycle = _find_latest_hrrr_cycle()
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    # searchString tells Herbie to extract ONLY the gust field from the
    # GRIB2 file — much faster than loading every variable.
    H = Herbie(
        cycle,
        model="hrrr",
        product="sfc",
        fxx=fxx,
        save_dir=str(HERBIE_DIR),
        overwrite=False,
    )

    raw = H.xarray(":GUST:10 m above ground:", remove_grib=False)
    gust_da = _extract_gust_variable(raw)
    gust_da = gust_da.load()  # force eager read into memory before subset file is cleaned up

    # HRRR uses 2-D latitude/longitude arrays on a Lambert Conformal grid.
    # They're stored as coordinate variables named 'latitude' and 'longitude'.
    lat2d = gust_da.coords["latitude"].values   # shape (y, x)
    lon2d = gust_da.coords["longitude"].values  # shape (y, x), range 0-360

    # Convert 0-360 → -180/+180 so we match normal geographic conventions
    lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)

    gust_arr = gust_da.values  # shape (y, x), m/s

    # ── Clip to Colorado ─────────────────────────────────────────────────────
    mask = (
        (lat2d >= CO_LAT_MIN) & (lat2d <= CO_LAT_MAX) &
        (lon2d >= CO_LON_MIN) & (lon2d <= CO_LON_MAX)
    )
    rows, cols = np.where(mask)
    if len(rows) == 0:
        raise ValueError(
            "No HRRR grid points found inside the Colorado bounding box. "
            "Check CO_LAT/LON constants in winds.py."
        )

    r0, r1 = rows.min(), rows.max() + 1
    c0, c1 = cols.min(), cols.max() + 1

    lat_co  = lat2d[r0:r1, c0:c1]
    lon_co  = lon2d[r0:r1, c0:c1]
    gust_co = gust_arr[r0:r1, c0:c1]   # still m/s

    # ── Convert m/s → knots ───────────────────────────────────────────────────
    gust_kt = gust_co * 1.94384

    # ── Downsample 2× in each direction ──────────────────────────────────────
    # HRRR 3 km → effective 6 km, ~7,500 points instead of ~30,000.
    # This keeps Leaflet fast while still resolving the major terrain features
    # (Front Range, Sawatch, San Juans, etc.).
    step = 2
    lat_ds  = lat_co [::step, ::step]
    lon_ds  = lon_co [::step, ::step]
    gust_ds = gust_kt[::step, ::step]

    # ── Build output list ─────────────────────────────────────────────────────
    # cell_size_deg is the approximate width of each rendered rectangle in degrees.
    # At 39°N, 6 km ≈ 0.054° latitude and ≈ 0.069° longitude.
    # The Leaflet JS will use half this value on each side of the centre point.
    cell_size_deg = 0.055

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
        "model":        "HRRR",
        "cycle_utc":    cycle_aware.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "valid_utc":    valid_dt.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "fxx":          fxx,
        "cell_size_deg": cell_size_deg,
        "point_count":  len(points),
        "points":       points,
    }


def get_hrrr_gusts_cached(fxx: int = 0, ttl_seconds: int = 600) -> dict:
    """
    Thin cache wrapper around fetch_hrrr_gusts().
    Downloading + processing a HRRR GRIB2 file takes 20-60 seconds;
    caching means repeated map loads feel instant.

    ttl_seconds=600 means we'll re-fetch at most every 10 minutes,
    which is fine since HRRR only updates once per hour.
    """
    now = time.time()
    if _CACHE["data"] is None or (now - _CACHE["ts"]) > ttl_seconds:
        _CACHE["data"] = fetch_hrrr_gusts(fxx=fxx)
        _CACHE["ts"]   = now
    return _CACHE["data"]
