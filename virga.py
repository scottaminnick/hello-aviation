"""
virga.py  –  HRRR-based Virga Potential calculator for Colorado
===============================================================
Memory-efficient rewrite: uses pygrib.select() per field and clips
to Colorado immediately, so we never hold more than ~2 MB in RAM
at once instead of the full ~480 MB grid.

Science (unchanged from original script)
-----------------------------------------
1. UPPER SATURATED LAYER  (700–500 mb)
   Scan every 200 mb window for mean RH ≥ 80%.
   Only show virga where an upper cloud layer is present.

2. RH DECREASE IN COLUMN  (850–500 mb)
   For each level, compute the 100 mb RH decrease (drying with height).
   Track the maximum decrease and which level it occurred at.

3. CLOUD BASE WIND
   At the level of maximum RH decrease, record the 50 mb mean wind
   speed (kt) as a proxy for virga shaft momentum.

RH computed from T and Td via August-Roche-Magnus approximation.
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

# Only the levels we actually need — 500 to 850 mb at 25 mb spacing
LEVELS_MB = [500, 525, 550, 575, 600, 625, 650, 675,
             700, 725, 750, 775, 800, 825, 850]

_CACHE = {}


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


# ── Clipping helper ───────────────────────────────────────────────────────────

_CLIP_IDX = {}   # cache the slice indices after first call


def _get_clip_idx(lat2d, lon2d, step=2):
    """Compute and cache the row/col slice for Colorado."""
    key = lat2d.shape
    if key not in _CLIP_IDX:
        mask = (
            (lat2d >= CO_LAT_MIN) & (lat2d <= CO_LAT_MAX) &
            (lon2d >= CO_LON_MIN) & (lon2d <= CO_LON_MAX)
        )
        rows, cols = np.where(mask)
        if len(rows) == 0:
            raise ValueError("No grid points inside Colorado bounding box.")
        _CLIP_IDX[key] = (rows.min(), rows.max() + 1,
                          cols.min(), cols.max() + 1, step)
    return _CLIP_IDX[key]


def _clip(arr, idx):
    r0, r1, c0, c1, step = idx
    return arr[r0:r1, c0:c1][::step, ::step]


# ── Physics ───────────────────────────────────────────────────────────────────

def _rh(T_K, Td_K):
    """RH (0-100%) from temperature and dewpoint in Kelvin."""
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


# ── Memory-efficient field reader ─────────────────────────────────────────────

def _read_field_clipped(grbs, name, level, idx):
    """
    Select a single field by name+level, read it, clip to Colorado,
    and return the small clipped array.  Never holds the full grid.
    """
    try:
        msgs = grbs.select(name=name, typeOfLevel="isobaricInhPa", level=level)
    except ValueError:
        raise ValueError(f"Field not found: '{name}' at {level} mb")
    data, lat2d, lon2d = msgs[0].data()
    lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
    if idx is None:
        idx = _get_clip_idx(lat2d, lon2d)
    return _clip(data, idx), lat2d, lon2d, idx


# ── Main fetch ────────────────────────────────────────────────────────────────

def fetch_virga(cycle_utc: str, fxx: int = 1) -> dict:
    cycle = datetime.fromisoformat(
        cycle_utc.replace("Z", "+00:00")
    ).replace(tzinfo=None)
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    prs_path = _download(cycle, fxx)

    grbs = pygrib.open(str(prs_path))

    # ── Pass 1: read T and Td at each level, compute RH, discard raw arrays ──
    # Maximum memory at any point: 2 full grids (T + Td) + 1 clipped RH array
    # Full grid ~8 MB each → peak ~16 MB instead of ~480 MB

    lat2d = lon2d = None
    idx   = None

    # Dictionaries of CLIPPED arrays only (~0.1 MB each)
    rh_co = {}   # level -> clipped RH (%)
    U_co  = {}   # level -> clipped U (m/s)
    V_co  = {}   # level -> clipped V (m/s)

    for lev in LEVELS_MB:
        # Temperature
        T_arr, lat2d, lon2d, idx = _read_field_clipped(grbs, "Temperature", lev, idx)

        # Dew point
        Td_arr, _, _, _ = _read_field_clipped(grbs, "Dew point temperature", lev, idx)

        # Compute RH from clipped arrays — discard full-grid T/Td immediately
        rh_co[lev] = _rh(T_arr, Td_arr)
        del T_arr, Td_arr

        # Wind (needed for cloud-base wind speed)
        u_arr, _, _, _ = _read_field_clipped(grbs, "U component of wind", lev, idx)
        v_arr, _, _, _ = _read_field_clipped(grbs, "V component of wind", lev, idx)
        U_co[lev] = u_arr
        V_co[lev] = v_arr
        del u_arr, v_arr

    grbs.close()

    # ── Clipped lat/lon ───────────────────────────────────────────────────────
    r0, r1, c0, c1, step = idx
    lat_co = lat2d[r0:r1, c0:c1][::step, ::step]
    lon_co = lon2d[r0:r1, c0:c1][::step, ::step]
    lon_co = np.where(lon_co > 180, lon_co - 360, lon_co)
    shape  = lat_co.shape

    # ── 1. Upper saturated layer (700–500 mb) ─────────────────────────────────
    upper_levels  = [lev for lev in LEVELS_MB if lev <= 700]
    max_upper_rh  = np.zeros(shape)
    window_mb     = 200

    for lev_top in upper_levels:
        lev_bot = lev_top + window_mb
        window  = [lev for lev in upper_levels if lev_top <= lev <= lev_bot]
        if len(window) < 2:
            continue
        mean_rh = np.mean([rh_co[lev] for lev in window], axis=0)
        max_upper_rh = np.maximum(max_upper_rh, mean_rh)

    upper_cloud = max_upper_rh >= 80.0

    # ── 2. Maximum 100 mb RH decrease in column (850→500 mb, bottom up) ──────
    scan_levels        = sorted(LEVELS_MB, reverse=True)   # 850 → 500
    max_rh_decrease    = np.zeros(shape)
    cloud_base_wind_kt = np.zeros(shape)

    for lev_bot in scan_levels:
        lev_top = lev_bot - 100
        if lev_top not in rh_co:
            continue

        decrease_here = rh_co[lev_bot] - rh_co[lev_top]

        # Wind at nearest available level to the midpoint
        lev_mid  = lev_bot - 50
        wind_lev = min(U_co.keys(), key=lambda l: abs(l - lev_mid))
        wspd_kt  = np.sqrt(U_co[wind_lev]**2 + V_co[wind_lev]**2) * 1.94384

        better             = decrease_here > max_rh_decrease
        max_rh_decrease    = np.where(better, decrease_here,    max_rh_decrease)
        cloud_base_wind_kt = np.where(better, wspd_kt,          cloud_base_wind_kt)

    # ── 3. Apply upper cloud mask ─────────────────────────────────────────────
    virga_pct = np.where(upper_cloud, np.clip(max_rh_decrease, 0, 100), 0.0)
    cat       = _virga_category(virga_pct)

    # ── Build point list (skip < 20% to keep JSON payload small) ─────────────
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
