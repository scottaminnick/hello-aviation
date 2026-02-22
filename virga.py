"""
virga.py  –  HRRR-based Virga Potential calculator for Colorado
===============================================================
Adapted from virga_potential_forecast_siphon.py (RAP/Siphon/matplotlib)
Rewritten for HRRR prs product via pygrib, returning JSON for Leaflet.

Science (unchanged from original script)
-----------------------------------------
1. UPPER SATURATED LAYER
   Scan 700–500 mb for the maximum mean RH over any 200 mb depth.
   Only proceed where that max mean RH ≥ 80 % (cloud layer present).

2. RH DECREASE IN COLUMN
   Scan 850–500 mb from bottom upward. At each level, compute the
   100 mb RH decrease (RH at level minus RH 100 mb above it).
   Track the maximum decrease found anywhere in the column.
   This is the "evaporation zone" where virga occurs.

3. CLOUD BASE WIND
   At the level with the greatest 100 mb RH decrease, record the
   50 mb mean wind speed.  This approximates the momentum that
   virga shafts would carry downward — a proxy for wind shear /
   gust potential at the surface beneath a virga shaft.

Output categories (virga potential %)
--------------------------------------
  < 20 %   negligible
  20–40 %  low
  40–60 %  moderate
  60–80 %  high
  ≥ 80 %   extreme

Data source
-----------
  HRRR prs product (wrfprsf##.grib2) – same file used by froude.py.
  RH computed from Temperature (T) and Dew-point temperature (Td)
  using the August-Roche-Magnus approximation:
      e(T) = 6.112 * exp(17.67 * T_C / (T_C + 243.5))
      RH    = 100 * e(Td) / e(T)
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

# Colorado bounding box (same as winds.py / froude.py)
CO_LAT_MIN, CO_LAT_MAX = 36.8, 41.2
CO_LON_MIN, CO_LON_MAX = -109.2, -101.9

# Pressure levels available in HRRR prs at 25 mb spacing
# We only need the tropospheric layers relevant to virga
LEVELS_MB = [500, 525, 550, 575, 600, 625, 650, 675,
             700, 725, 750, 775, 800, 825, 850]

# In-memory cache keyed by (cycle_utc, fxx)
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
    """Download HRRR prs GRIB2 and return local Path."""
    H = Herbie(cycle, model="hrrr", product="prs", fxx=fxx,
               save_dir=str(HERBIE_DIR), overwrite=False)
    p = Path(H.download())
    if not p.exists():
        raise FileNotFoundError(f"Download failed: {p}")
    return p


# ── Physics ───────────────────────────────────────────────────────────────────

def _rh_from_t_td(T_K, Td_K):
    """
    Relative humidity (0–100 %) from temperature and dewpoint (both Kelvin).
    Uses the August-Roche-Magnus approximation.
    """
    T_C  = T_K  - 273.15
    Td_C = Td_K - 273.15
    e_T  = 6.112 * np.exp(17.67 * T_C  / (T_C  + 243.5))
    e_Td = 6.112 * np.exp(17.67 * Td_C / (Td_C + 243.5))
    rh   = 100.0 * e_Td / e_T
    return np.clip(rh, 0.0, 100.0)


def _virga_category(pct):
    """Integer category for colour coding."""
    cat = np.zeros_like(pct, dtype=int)
    cat[pct >= 20] = 1   # low
    cat[pct >= 40] = 2   # moderate
    cat[pct >= 60] = 3   # high
    cat[pct >= 80] = 4   # extreme
    return cat


# ── Main fetch ────────────────────────────────────────────────────────────────

def fetch_virga(cycle_utc: str, fxx: int = 1) -> dict:
    """
    Compute virga potential grid over Colorado.

    cycle_utc : ISO string e.g. '2026-02-22T02:00Z'
    fxx       : forecast hour 1-12
    """
    cycle = datetime.fromisoformat(
        cycle_utc.replace("Z", "+00:00")
    ).replace(tzinfo=None)
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    prs_path = _download(cycle, fxx)

    # ── Read all needed fields into memory ────────────────────────────────────
    # We open the file once and pull everything we need level by level.
    grbs = pygrib.open(str(prs_path))

    T_dict  = {}   # level_mb -> 2D array (K)
    Td_dict = {}
    U_dict  = {}
    V_dict  = {}
    lat2d   = None
    lon2d   = None

    for grb in grbs:
        lev = grb.level
        if lev not in LEVELS_MB:
            continue
        if grb.typeOfLevel != "isobaricInhPa":
            continue

        name = grb.name
        if lat2d is None:
            data, lat2d, lon2d = grb.data()
            lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
            arr = data
        else:
            arr = grb.values

        if name == "Temperature":
            T_dict[lev] = arr
        elif name == "Dew point temperature":
            Td_dict[lev] = arr
        elif name == "U component of wind":
            U_dict[lev] = arr
        elif name == "V component of wind":
            V_dict[lev] = arr

    grbs.close()

    # Confirm we got what we need
    for lev in LEVELS_MB:
        for d, label in [(T_dict, "T"), (Td_dict, "Td")]:
            if lev not in d:
                raise ValueError(f"Missing {label} at {lev} mb in {prs_path.name}")

    # ── Clip everything to Colorado ───────────────────────────────────────────
    step = 2
    mask = (
        (lat2d >= CO_LAT_MIN) & (lat2d <= CO_LAT_MAX) &
        (lon2d >= CO_LON_MIN) & (lon2d <= CO_LON_MAX)
    )
    rows, cols = np.where(mask)
    if len(rows) == 0:
        raise ValueError("No grid points inside Colorado bounding box.")
    r0, r1 = rows.min(), rows.max() + 1
    c0, c1 = cols.min(), cols.max() + 1

    def clip(arr):
        return arr[r0:r1, c0:c1][::step, ::step]

    lat_co = clip(lat2d)
    lon_co = clip(lon2d)

    # Build clipped RH dict and wind dict
    rh = {}   # level_mb -> 2D RH array (%)
    U  = {}
    V  = {}
    for lev in LEVELS_MB:
        rh[lev] = clip(_rh_from_t_td(T_dict[lev], Td_dict[lev]))
        if lev in U_dict:
            U[lev] = clip(U_dict[lev])
            V[lev] = clip(V_dict[lev])

    shape = lat_co.shape

    # ── 1. Find upper saturated layer (700–500 mb) ────────────────────────────
    # Scan every 200 mb window: [700-500], [675-475], [650-450], [625-425], [600-400]
    # We defined LEVELS_MB top-down (500→850), so index carefully.
    upper_levels = [lev for lev in LEVELS_MB if lev <= 700]   # 500–700 mb

    max_upper_rh = np.zeros(shape)
    window_mb    = 200   # thickness of saturated layer

    for i, lev_top in enumerate(upper_levels):
        lev_bot = lev_top + window_mb
        window  = [lev for lev in upper_levels if lev_top <= lev <= lev_bot]
        if len(window) < 2:
            continue
        mean_rh = np.mean([rh[lev] for lev in window], axis=0)
        max_upper_rh = np.maximum(max_upper_rh, mean_rh)

    upper_cloud_present = max_upper_rh >= 80.0   # boolean mask

    # ── 2. Maximum 100 mb RH decrease in column ───────────────────────────────
    # Scan from 850 mb upward (bottom to top).
    # At each level, RH decrease = RH(level) - RH(level - 100 mb)
    # i.e., how much drier the layer 100 mb above is compared to below.
    scan_levels = sorted(LEVELS_MB, reverse=True)   # 850 → 500

    max_rh_decrease    = np.zeros(shape)
    cloud_base_wind_kt = np.zeros(shape)   # wind at level of max decrease

    for lev_bot in scan_levels:
        lev_top = lev_bot - 100
        if lev_top not in rh:
            continue

        decrease_here = rh[lev_bot] - rh[lev_top]   # positive = dries upward

        # Wind speed at the 50 mb mid-point of the drying layer
        lev_mid = lev_bot - 50
        # Use nearest available level for wind
        wind_lev = min(U.keys(), key=lambda l: abs(l - lev_mid))
        wspd_here = np.sqrt(U[wind_lev]**2 + V[wind_lev]**2) * 1.94384  # kt

        # Where this is the largest decrease so far, record it
        better = decrease_here > max_rh_decrease
        max_rh_decrease    = np.where(better, decrease_here, max_rh_decrease)
        cloud_base_wind_kt = np.where(better, wspd_here,     cloud_base_wind_kt)

    # ── 3. Apply upper cloud mask ─────────────────────────────────────────────
    # Only show virga potential where an upper saturated layer exists
    virga_pct = np.where(upper_cloud_present, max_rh_decrease, 0.0)
    virga_pct = np.clip(virga_pct, 0.0, 100.0)

    cat = _virga_category(virga_pct)

    # ── Build point list ──────────────────────────────────────────────────────
    points = []
    ny, nx = lat_co.shape
    for i in range(ny):
        for j in range(nx):
            vpct = float(virga_pct[i, j])
            if vpct < 20.0:        # skip negligible cells to keep payload small
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
        "model":       "HRRR",
        "product":     "prs",
        "cycle_utc":   cycle_aware.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "valid_utc":   valid_dt.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "fxx":         fxx,
        "cell_size_deg": 0.055,
        "point_count": len(points),
        "points":      points,
    }


# ── Cache wrapper ─────────────────────────────────────────────────────────────

def get_virga_cached(cycle_utc: str, fxx: int = 1, ttl_seconds: int = 600) -> dict:
    key    = (cycle_utc, fxx)
    now    = time.time()
    cached = _CACHE.get(key)
    if cached is None or (now - cached["ts"]) > ttl_seconds:
        _CACHE[key] = {"ts": now, "data": fetch_virga(cycle_utc=cycle_utc, fxx=fxx)}
    return _CACHE[key]["data"]
