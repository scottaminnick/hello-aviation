"""
froude.py  –  HRRR-based Froude Number calculator for Colorado
================================================================
Fr = U_perp / (N * h)

  U_perp  wind speed perpendicular to the terrain barrier (m/s)
          sourced from HRRR prs product at 700 mb
          (700 mb ≈ 3 km MSL ≈ typical Front Range / Rockies crest)

  N       Brunt-Väisälä frequency (s⁻¹) – atmospheric stability
          computed from potential temperature between 850 mb and 500 mb

  h       terrain height scale (m)
          sourced from HRRR sfc orography field (model terrain, collocated
          with the HRRR grid – no external API required)

Interpretation
--------------
  Fr < 0.5          flow splitting  – air goes around the barrier
  0.5 ≤ Fr < 1.0    transitional    – developing wave activity
  Fr ≈ 1.0          resonant        – CRITICAL, severe mountain waves
  Fr > 1.5          flow-over       – weaker wave activity

Data sources
------------
  HRRR prs  (wrfprsf##.grib2)  – U, V, T, gh at pressure levels
  HRRR sfc  (wrfsfcf##.grib2)  – orography (terrain height MSL)

Both files come from the same cycle/fxx so the grids are identical.
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

# Colorado bounding box
CO_LAT_MIN, CO_LAT_MAX = 36.8, 41.2
CO_LON_MIN, CO_LON_MAX = -109.2, -101.9

# Gravity (m s⁻²)
G = 9.81

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


def _download(cycle, product, fxx):
    """Download a HRRR GRIB2 file and return its local Path."""
    H = Herbie(cycle, model="hrrr", product=product, fxx=fxx,
               save_dir=str(HERBIE_DIR), overwrite=False)
    p = Path(H.download())
    if not p.exists():
        raise FileNotFoundError(f"Download failed: {p}")
    return p


def _read_field(grib_path, name, level, typeOfLevel="isobaricInhPa"):
    """
    Open a GRIB2 file with pygrib, select one message, read into RAM.
    Returns (data_2d, lat_2d, lon_2d).  Longitudes converted to ±180.
    """
    grbs = pygrib.open(str(grib_path))
    try:
        msgs = grbs.select(name=name, typeOfLevel=typeOfLevel, level=level)
    except ValueError:
        grbs.close()
        raise ValueError(
            f"Field not found: name='{name}' typeOfLevel='{typeOfLevel}' level={level} "
            f"in {grib_path.name}"
        )
    data, lat2d, lon2d = msgs[0].data()
    grbs.close()
    lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
    return data, lat2d, lon2d


def _clip_to_colorado(arr, lat2d, lon2d, step=2):
    """Clip a 2-D array to the Colorado bounding box and downsample."""
    mask = (
        (lat2d >= CO_LAT_MIN) & (lat2d <= CO_LAT_MAX) &
        (lon2d >= CO_LON_MIN) & (lon2d <= CO_LON_MAX)
    )
    rows, cols = np.where(mask)
    if len(rows) == 0:
        raise ValueError("No grid points inside Colorado bounding box.")
    r0, r1 = rows.min(), rows.max() + 1
    c0, c1 = cols.min(), cols.max() + 1
    return (arr[r0:r1, c0:c1][::step, ::step],
            lat2d[r0:r1, c0:c1][::step, ::step],
            lon2d[r0:r1, c0:c1][::step, ::step])


# ── Science ───────────────────────────────────────────────────────────────────

def _potential_temp(T_K, p_mb):
    """Potential temperature θ = T * (1000/p)^(R/Cp).  T in Kelvin."""
    return T_K * (1000.0 / p_mb) ** 0.286


def _brunt_vaisala(T_850, T_500, gh_850, gh_500):
    """
    Brunt-Väisälä frequency N (s⁻¹) from two pressure levels.

    N² = (g / θ_mean) * (Δθ / Δz)

    Args:
        T_850, T_500  :  temperature (K) at 850 mb and 500 mb
        gh_850, gh_500:  geopotential height (m) at those levels
    Returns:
        N  (s⁻¹, always ≥ 0.001 to avoid division-by-zero in stable layers)
    """
    theta_850  = _potential_temp(T_850,  850)
    theta_500  = _potential_temp(T_500,  500)
    theta_mean = 0.5 * (theta_850 + theta_500)

    delta_theta = theta_500 - theta_850          # positive = stable
    delta_z     = gh_500   - gh_850              # always positive

    # Clamp: neutral/unstable layers get a small positive N
    N2 = np.maximum((G / theta_mean) * (delta_theta / delta_z), 1e-6)
    return np.sqrt(N2)


def _terrain_scale(orog, plains_elev=1500.0):
    """
    Characteristic terrain height  h = max(orog - plains_elev, 100).

    plains_elev: representative elevation of the upwind plains (m MSL).
    Colorado's high plains sit at ~1,500 m; the Rockies add another
    1,500–3,000 m on top, giving h ≈ 1,500–3,000 m.
    """
    return np.maximum(orog - plains_elev, 100.0)


def _u_perp(U, V, barrier_angle_deg=180.0):
    """
    Wind component perpendicular to the terrain barrier.

    barrier_angle_deg: the compass direction the barrier faces
        (the direction FROM WHICH perpendicular flow would come).
        Colorado's Front Range faces east  →  barrier_angle = 90°
        The N-S-running Rockies are mostly perpendicular to westerlies,
        so we use 270° (flow from the west, i.e. the U component dominates).

    For the Front Range / Colorado Rockies the primary barrier is
    oriented N-S.  A westerly (U > 0) flow is perfectly perpendicular.
    We use the full horizontal wind projected onto the W-E axis.
    """
    angle_rad = np.radians(barrier_angle_deg)
    return U * np.cos(angle_rad) + V * np.sin(angle_rad)


def _classify(fr):
    """Integer risk category for map colouring."""
    cat = np.zeros_like(fr, dtype=int)
    cat[fr < 0.5]                         = 1   # splitting – low
    cat[(fr >= 0.5) & (fr < 0.8)]         = 2   # transitional
    cat[(fr >= 0.8) & (fr <= 1.5)]        = 3   # resonant – high
    cat[fr > 1.5]                         = 4   # flow-over – moderate
    return cat


# ── Main fetch function ───────────────────────────────────────────────────────

def fetch_froude(cycle_utc: str, fxx: int = 1) -> dict:
    """
    Compute Froude number grid over Colorado for a given HRRR cycle + hour.

    cycle_utc: ISO string e.g. '2026-02-22T02:00Z'
    fxx:       forecast hour (1-12)
    """
    cycle = datetime.fromisoformat(
        cycle_utc.replace("Z", "+00:00")
    ).replace(tzinfo=None)
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    # ── Download both products ────────────────────────────────────────────────
    prs_path = _download(cycle, "prs", fxx)
    sfc_path = _download(cycle, "sfc", fxx)

    # ── Read atmospheric fields from prs file ─────────────────────────────────
    # Wind at 700 mb (≈ mountain-crest level)
    U700, lat2d, lon2d = _read_field(prs_path, "U component of wind", 700)
    V700, _,    _      = _read_field(prs_path, "V component of wind", 700)

    # Temperature + geopotential height for stability calculation
    T850,  _, _ = _read_field(prs_path, "Temperature",        850)
    T500,  _, _ = _read_field(prs_path, "Temperature",        500)
    GH850, _, _ = _read_field(prs_path, "Geopotential height", 850)
    GH500, _, _ = _read_field(prs_path, "Geopotential height", 500)

    # ── Read terrain from sfc file ────────────────────────────────────────────
    # HRRR sfc carries an "Orography" field – model terrain MSL in metres.
    # This is collocated with the prs grid, so no interpolation needed.
    try:
        orog, _, _ = _read_field(sfc_path, "Orography", 0, typeOfLevel="surface")
    except ValueError:
        # Fallback name used in some HRRR builds
        try:
            orog, _, _ = _read_field(sfc_path, "Geometric height", 0,
                                     typeOfLevel="surface")
        except ValueError:
            # Last resort: use geopotential height at 850 mb as a rough proxy
            orog = GH850

    # ── Clip everything to Colorado ───────────────────────────────────────────
    step = 2
    U700_co, lat_co, lon_co = _clip_to_colorado(U700, lat2d, lon2d, step)
    V700_co, _,      _      = _clip_to_colorado(V700, lat2d, lon2d, step)
    T850_co, _,      _      = _clip_to_colorado(T850, lat2d, lon2d, step)
    T500_co, _,      _      = _clip_to_colorado(T500, lat2d, lon2d, step)
    GH850_co, _,     _      = _clip_to_colorado(GH850, lat2d, lon2d, step)
    GH500_co, _,     _      = _clip_to_colorado(GH500, lat2d, lon2d, step)
    orog_co, _,      _      = _clip_to_colorado(orog, lat2d, lon2d, step)

    # ── Compute Froude components ─────────────────────────────────────────────
    N = _brunt_vaisala(T850_co, T500_co, GH850_co, GH500_co)   # s⁻¹
    h = _terrain_scale(orog_co)                                  # m
    u = _u_perp(U700_co, V700_co, barrier_angle_deg=270.0)      # m/s (westerlies)

    # Fr = |U_perp| / (N * h)   — use absolute value (direction doesn't matter)
    fr = np.abs(u) / (N * h)
    fr = np.clip(fr, 0, 10)    # cap extreme values for colour scaling

    cat = _classify(fr)

    # ── Build point list for Leaflet ──────────────────────────────────────────
    wind_spd = np.sqrt(U700_co**2 + V700_co**2) * 1.94384   # m/s → kt
    points = []
    ny, nx = lat_co.shape
    for i in range(ny):
        for j in range(nx):
            if np.isnan(fr[i, j]):
                continue
            points.append({
                "lat":      round(float(lat_co[i, j]), 4),
                "lon":      round(float(lon_co[i, j]), 4),
                "fr":       round(float(fr[i, j]), 3),
                "cat":      int(cat[i, j]),
                "wind_kt":  round(float(wind_spd[i, j]), 1),
                "N":        round(float(N[i, j]), 5),
                "h_m":      round(float(h[i, j]), 0),
                "orog_m":   round(float(orog_co[i, j]), 0),
            })

    valid_dt = (cycle + timedelta(hours=fxx)).replace(tzinfo=timezone.utc)
    return {
        "model":         "HRRR",
        "product":       "prs+sfc",
        "wind_level_mb": 700,
        "stability_layers": "850-500 mb",
        "cycle_utc":     cycle_aware.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "valid_utc":     valid_dt.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "fxx":           fxx,
        "cell_size_deg": 0.055,
        "point_count":   len(points),
        "points":        points,
    }


# ── Cache wrapper ─────────────────────────────────────────────────────────────

def get_froude_cached(cycle_utc: str, fxx: int = 1, ttl_seconds: int = 600) -> dict:
    """Cache keyed by (cycle_utc, fxx). Re-fetches after ttl_seconds."""
    key    = (cycle_utc, fxx)
    now    = time.time()
    cached = _CACHE.get(key)
    if cached is None or (now - cached["ts"]) > ttl_seconds:
        _CACHE[key] = {"ts": now, "data": fetch_froude(cycle_utc=cycle_utc, fxx=fxx)}
    return _CACHE[key]["data"]
