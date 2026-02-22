"""
virga.py  –  HRRR-based Virga Potential calculator for Colorado
===============================================================
Single-pass pygrib read: iterate the file ONCE, clip each field
to Colorado immediately, discard the full grid.  O(708) reads
instead of O(708 × 60) from repeated select() calls.

Science
-------
1. Upper saturated layer (700-500 mb): mean RH >= 80% over 200 mb depth
2. Max 100 mb RH decrease in column (850-500 mb): evaporation zone
3. Cloud base wind at level of max decrease (kt): virga shaft momentum
RH computed from T + Td via August-Roche-Magnus approximation.
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

CO_LAT_MIN, CO_LAT_MAX = 36.8, 41.2
CO_LON_MIN, CO_LON_MAX = -109.2, -101.9

# Levels we care about: 500-850 mb at 25 mb spacing
LEVELS_SET = frozenset([500,525,550,575,600,625,650,675,
                        700,725,750,775,800,825,850])
LEVELS_MB  = sorted(LEVELS_SET)   # ascending  (500→850)

WANTED_NAMES = frozenset([
    "Temperature",
    "Dew point temperature",
    "U component of wind",
    "V component of wind",
])

_CACHE    = {}
_CLIP_IDX = {}   # cache slice indices by grid shape


# ── Herbie helpers ────────────────────────────────────────────────────────────

def _now_utc_hour_naive():
    return datetime.utcnow().replace(minute=0, second=0, microsecond=0)


def _find_latest_hrrr_cycle(max_lookback_hours=6):
    base = _now_utc_hour_naive()
    for h in range(max_lookback_hours + 1):
        candidate = base - timedelta(hours=h)
        try:
            H = Herbie(candidate, model="hrrr", product="prs", fxx=0,
                       save_dir=str(HERBIE_DIR), overwrite=False)
            H.inventory()
            return candidate
        except Exception:
            continue
    return base - timedelta(hours=2)


def _download(cycle, fxx):
    H = Herbie(cycle, model="hrrr", product="prs", fxx=fxx,
               save_dir=str(HERBIE_DIR), overwrite=False)
    p = Path(H.download())
    if not p.exists():
        raise FileNotFoundError(f"Download failed: {p}")
    return p


# ── Clip helpers ──────────────────────────────────────────────────────────────

def _get_clip_idx(lat2d, lon2d, step=2):
    key = lat2d.shape
    if key not in _CLIP_IDX:
        mask = (
            (lat2d >= CO_LAT_MIN) & (lat2d <= CO_LAT_MAX) &
            (lon2d >= CO_LON_MIN) & (lon2d <= CO_LON_MAX)
        )
        rows, cols = np.where(mask)
        if len(rows) == 0:
            raise ValueError("No HRRR grid points inside Colorado bounding box.")
        _CLIP_IDX[key] = (rows.min(), rows.max() + 1,
                          cols.min(), cols.max() + 1, step)
    return _CLIP_IDX[key]


def _clip(arr, idx):
    r0, r1, c0, c1, step = idx
    return arr[r0:r1, c0:c1][::step, ::step]


# ── Physics ───────────────────────────────────────────────────────────────────

def _rh(T_K, Td_K):
    T_C  = T_K  - 273.15
    Td_C = Td_K - 273.15
    e_T  = 6.112 * np.exp(17.67 * T_C  / (T_C  + 243.5))
    e_Td = 6.112 * np.exp(17.67 * Td_C / (Td_C + 243.5))
    return np.clip(100.0 * e_Td / e_T, 0.0, 100.0)


def _virga_category(pct):
    cat = np.zeros_like(pct, dtype=int)
    cat[pct >= 20] = 1
    cat[pct >= 40] = 2
    cat[pct >= 60] = 3
    cat[pct >= 80] = 4
    return cat


# ── Single-pass GRIB reader ───────────────────────────────────────────────────

def _read_all_fields_clipped(prs_path):
    """
    ONE pass through the GRIB2 file.
    For each message: check level + name, read data, clip to Colorado,
    store the small (~0.1 MB) clipped array.  Discard full grid immediately.

    Returns:
        lat_co, lon_co  : clipped coordinate arrays
        T_co, Td_co     : dicts  level_mb -> clipped array (K)
        U_co, V_co      : dicts  level_mb -> clipped array (m/s)
    """
    T_co  = {}
    Td_co = {}
    U_co  = {}
    V_co  = {}
    lat_co = lon_co = None
    idx    = None

    grbs = pygrib.open(str(prs_path))

    for grb in grbs:
        # Fast pre-filter — skip irrelevant levels and variable names
        if grb.typeOfLevel != "isobaricInhPa":
            continue
        lev = grb.level
        if lev not in LEVELS_SET:
            continue
        name = grb.name
        if name not in WANTED_NAMES:
            continue

        # Read full grid, compute clip indices on first encounter
        data, lat2d, lon2d = grb.data()
        lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)

        if idx is None:
            idx    = _get_clip_idx(lat2d, lon2d)
            r0, r1, c0, c1, step = idx
            lat_co = lat2d[r0:r1, c0:c1][::step, ::step]
            lon_co = lon2d[r0:r1, c0:c1][::step, ::step]

        small = _clip(data, idx)
        del data, lat2d, lon2d   # free full grid immediately

        if name == "Temperature":
            T_co[lev]  = small
        elif name == "Dew point temperature":
            Td_co[lev] = small
        elif name == "U component of wind":
            U_co[lev]  = small
        elif name == "V component of wind":
            V_co[lev]  = small

    grbs.close()

    # Validate we got everything
    missing = [lev for lev in LEVELS_MB if lev not in T_co or lev not in Td_co]
    if missing:
        raise ValueError(f"Missing T/Td at levels: {missing}")

    return lat_co, lon_co, T_co, Td_co, U_co, V_co


# ── Main fetch ────────────────────────────────────────────────────────────────

def fetch_virga(cycle_utc: str, fxx: int = 1) -> dict:
    cycle = datetime.fromisoformat(
        cycle_utc.replace("Z", "+00:00")
    ).replace(tzinfo=None)
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    prs_path = _download(cycle, fxx)

    lat_co, lon_co, T_co, Td_co, U_co, V_co = _read_all_fields_clipped(prs_path)
    shape = lat_co.shape

    # Pre-compute clipped RH at each level
    rh_co = {lev: _rh(T_co[lev], Td_co[lev]) for lev in LEVELS_MB}
    del T_co, Td_co   # no longer needed

    # ── 1. Upper saturated layer (700-500 mb) ─────────────────────────────────
    upper_levels = [lev for lev in LEVELS_MB if lev <= 700]
    max_upper_rh = np.zeros(shape)
    window_mb    = 200

    for lev_top in upper_levels:
        lev_bot = lev_top + window_mb
        window  = [l for l in upper_levels if lev_top <= l <= lev_bot]
        if len(window) < 2:
            continue
        mean_rh = np.mean([rh_co[l] for l in window], axis=0)
        max_upper_rh = np.maximum(max_upper_rh, mean_rh)

    upper_cloud = max_upper_rh >= 80.0

    # ── 2. Max 100 mb RH decrease in column (850→500 mb, bottom up) ──────────
    scan_levels        = sorted(LEVELS_MB, reverse=True)   # 850→500
    max_rh_decrease    = np.zeros(shape)
    cloud_base_wind_kt = np.zeros(shape)

    for lev_bot in scan_levels:
        lev_top = lev_bot - 100
        if lev_top not in rh_co:
            continue
        decrease_here = rh_co[lev_bot] - rh_co[lev_top]

        lev_mid  = lev_bot - 50
        wind_lev = min(U_co.keys(), key=lambda l: abs(l - lev_mid))
        wspd_kt  = np.sqrt(U_co[wind_lev]**2 + V_co[wind_lev]**2) * 1.94384

        better             = decrease_here > max_rh_decrease
        max_rh_decrease    = np.where(better, decrease_here,    max_rh_decrease)
        cloud_base_wind_kt = np.where(better, wspd_kt,          cloud_base_wind_kt)

    # ── 3. Apply upper cloud mask ─────────────────────────────────────────────
    virga_pct = np.where(upper_cloud, np.clip(max_rh_decrease, 0, 100), 0.0)
    cat       = _virga_category(virga_pct)

    # ── Build point list ──────────────────────────────────────────────────────
    points = []
    ny, nx = shape
    for i in range(ny):
        for j in range(nx):
            vpct = float(virga_pct[i, j])
            if vpct < 20.0:
                continue
            points.append({
                "lat":        round(float(lat_co[i, j]), 4),
                "lon":        round(float(lon_co[i, j]), 4),
                "virga_pct":  round(vpct, 1),
                "cat":        int(cat[i, j]),
                "cb_wind_kt": round(float(cloud_base_wind_kt[i, j]), 1),
                "upper_rh":   round(float(max_upper_rh[i, j]), 1),
            })

    valid_dt = (cycle + timedelta(hours=fxx)).replace(tzinfo=timezone.utc)
    return {
        "model":         "HRRR",
        "product":       "prs",
        "cycle_utc":     cycle_aware.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "valid_utc":     valid_dt.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "fxx":           fxx,
        "cell_size_deg": 0.055,
        "point_count":   len(points),
        "points":        points,
    }


# ── Cache wrapper ─────────────────────────────────────────────────────────────

def get_virga_cached(cycle_utc: str, fxx: int = 1, ttl_seconds: int = 600) -> dict:
    key    = (cycle_utc, fxx)
    now    = time.time()
    cached = _CACHE.get(key)
    if cached is None or (now - cached["ts"]) > ttl_seconds:
        _CACHE[key] = {"ts": now, "data": fetch_virga(cycle_utc=cycle_utc, fxx=fxx)}
    return _CACHE[key]["data"]
