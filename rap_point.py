import os
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import numpy as np
import xarray as xr
from herbie import Herbie

HERBIE_DIR = Path(os.environ.get("HERBIE_DATA_DIR", "/tmp/herbie"))
HERBIE_DIR.mkdir(parents=True, exist_ok=True)

# Start with a small built-in airport list; expand later.
AIRPORTS = {
    "KMCI": (39.2975, -94.7309),
    "KMKC": (39.1279, -94.5892),
    "KSTL": (38.7525, -90.3734),
}

def _as_dataset(obj):
    """
    Herbie.xarray() may return:
      - xr.Dataset
      - list[xr.Dataset]
      - list[xr.DataArray]
    Normalize to a single xr.Dataset.
    """
    if isinstance(obj, xr.Dataset):
        return obj

    if isinstance(obj, list):
        if len(obj) == 0:
            raise ValueError("Herbie.xarray() returned an empty list (no fields matched).")

        if all(isinstance(x, xr.Dataset) for x in obj):
            return xr.merge(obj, compat="override", combine_attrs="override")

        if all(isinstance(x, xr.DataArray) for x in obj):
            dsets = []
            for i, da in enumerate(obj):
                name = da.name or f"var_{i}"
                dsets.append(da.to_dataset(name=name))
            return xr.merge(dsets, compat="override", combine_attrs="override")

        # Mixed types: try first element
        return _as_dataset(obj[0])

    raise TypeError(f"Unexpected return type from Herbie.xarray(): {type(obj)}")

_CACHE = {"ts": 0, "data": None, "key": None}

def get_rap_point_guidance_cached(stations: list[str], ttl_seconds: int = 600, fxx_max: int = 6) -> dict:
    key = (tuple([s.strip().upper() for s in stations if s.strip()]), int(fxx_max))
    now = time.time()

    if _CACHE["data"] is None or _CACHE["key"] != key or (now - _CACHE["ts"]) > ttl_seconds:
        _CACHE["data"] = fetch_rap_point_guidance(list(key[0]), fxx_max=key[1])
        _CACHE["ts"] = now
        _CACHE["key"] = key

    return _CACHE["data"]

def _pick_uv_at_level(point_ds: xr.Dataset, *, level_type: str, level: int):
    """
    Return (u, v) floats for the requested GRIB level.
    level_type examples:
      - "heightAboveGround" for 10m winds
      - "isobaricInhPa" for 925mb winds
    level is numeric: 10 or 925
    """
    u = v = None

    for _, da in point_ds.data_vars.items():
        tlev = da.attrs.get("GRIB_typeOfLevel")
        lev = da.attrs.get("GRIB_level")

        if tlev != level_type or lev != level:
            continue

        short = (da.attrs.get("GRIB_shortName") or "").lower()
        # GRIB shortName is often 'u'/'v', but sometimes appears like 'ugrd'/'vgrd'
        if short in ("u", "ugrd"):
            u = float(np.asarray(da.values).squeeze())
        elif short in ("v", "vgrd"):
            v = float(np.asarray(da.values).squeeze())

    return u, v

def _now_utc_hour_naive():
    # Herbie is happiest with naive datetimes representing UTC
    return datetime.utcnow().replace(minute=0, second=0, microsecond=0)

def _find_latest_cycle(max_lookback_hours: int = 8) -> datetime:
    """
    Try RAP cycles from now backward until we find one that has inventory
    for the SAME product we plan to use (awp130pgrb).
    """
    base = _now_utc_hour_naive()
    for h in range(0, max_lookback_hours + 1):
        dt = base - timedelta(hours=h)
        try:
            H = Herbie(
                dt,
                model="rap",
                product="awp130pgrb",
                fxx=0,
                save_dir=str(HERBIE_DIR),
                overwrite=True,
            )
            _ = H.inventory()
            return dt
        except Exception:
            continue
    return base

def fetch_rap_point_guidance(stations: list[str], fxx_max: int = 6) -> dict:
    """
    For each station, return f00..fxx_max point time series of:
      - 10m wind speed (kt)
      - 925mb wind speed (kt)
      - 10m->925mb vector shear magnitude (kt)

    Uses RAP product: awp130pgrb
    """
    cycle = _find_latest_cycle()
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    results: dict = {}
    errors: dict = {}

    stations_clean = [s.strip().upper() for s in stations if s.strip()]

    for stn in stations_clean:
        if stn not in AIRPORTS:
            errors[stn] = [f"Unknown station (not in AIRPORTS dict yet)."]
            results[stn] = {"lat": None, "lon": None, "series": []}
            continue

        lat, lon = AIRPORTS[stn]
        series = []

        for fxx in range(0, fxx_max + 1):
            try:
                H = Herbie(
                    cycle,
                    model="rap",
                    product="awp130pgrb",
                    fxx=fxx,
                    save_dir=str(HERBIE_DIR),
                    overwrite=True,
                )

                ds = _as_dataset(H.xarray(remove_grib=True))
                p = _ds_select_nearest(ds, lat, lon)

                u10, v10 = _pick_uv_at_level(p, level_type="heightAboveGround", level=10)
                u925, v925 = _pick_uv_at_level(p, level_type="isobaricInhPa", level=925)

                if None in (u10, v10, u925, v925):
                    raise ValueError(
                        "Missing U/V at 10m and/or 925mb. "
                        "Confirm GRIB attrs exist and levels are present in awp130pgrb."
                    )

                spd10 = _wind_speed(u10, v10)
                spd925 = _wind_speed(u925, v925)
                shear = _wind_speed(u925 - u10, v925 - v10)

                valid = cycle + timedelta(hours=fxx)
                valid_utc = valid.replace(tzinfo=timezone.utc).isoformat(timespec="minutes").replace("+00:00", "Z")

                series.append({
                    "fxx": fxx,
                    "valid_utc": valid_utc,
                    "wind10_kt": round(spd10 * 1.94384, 1),
                    "wind925_kt": round(spd925 * 1.94384, 1),
                    "shear_kt": round(shear * 1.94384, 1),
                })

            except Exception as e:
                errors.setdefault(stn, []).append(f"f{fxx:02d}: {e}")

        results[stn] = {"lat": lat, "lon": lon, "series": series}

    return {
        "model": "RAP",
        "product": "awp130pgrb",
        "cycle_utc": cycle_aware.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "fxx_max": fxx_max,
        "stations": stations_clean,
        "results": results,
        "errors": errors,
    }

def _ds_select_nearest(ds: xr.Dataset, lat: float, lon: float) -> xr.Dataset:
    """
    Select nearest grid point.
    Handles:
      - 1D coords named (latitude, longitude) or (lat, lon)
      - 2D lat/lon grids
    """
    # 1D coordinate case
    for yname, xname in [("latitude", "longitude"), ("lat", "lon")]:
        if yname in ds.coords and xname in ds.coords:
            try:
                return ds.sel({yname: lat, xname: lon}, method="nearest")
            except Exception:
                # Some datasets have coords but no index; fall through to brute force
                pass

    # 2D coordinate case
    if "latitude" in ds.variables and "longitude" in ds.variables:
        lat2 = ds["latitude"].values
        lon2 = ds["longitude"].values
        d2 = (lat2 - lat) ** 2 + (lon2 - lon) ** 2
        iy, ix = np.unravel_index(np.nanargmin(d2), d2.shape)

        # Common RAP grids use y/x dims; if not, infer from lat2 shape
        if "y" in ds.dims and "x" in ds.dims:
            return ds.isel(y=iy, x=ix)

        # Fallback: pick the last two dims
        ydim, xdim = list(ds.dims)[-2], list(ds.dims)[-1]
        return ds.isel({ydim: iy, xdim: ix})

    raise ValueError("Could not find usable latitude/longitude coordinates in dataset.")


def _wind_speed(u: float, v: float) -> float:
    return float(np.sqrt(u * u + v * v))

def fetch_rap_point_guidance(stations: list[str], fxx_max: int = 6) -> dict:
    """
    For each station, return f00..fxx_max point time series of:
      - 10m wind speed (kt)
      - 925mb wind speed (kt)
      - 10m->925mb vector shear magnitude (kt)

    Uses RAP:
      - awp130pgrb product for 10m winds
      - awp130pgrb product for pressure-level winds (925mb)
    """
    cycle = _find_latest_cycle()
    cycle_aware = cycle.replace(tzinfo=timezone.utc)

    results: dict = {}
    errors: dict = {}

    stations_clean = [s.strip().upper() for s in stations if s.strip()]

    for stn in stations_clean:
        if stn not in AIRPORTS:
            errors[stn] = [f"Unknown station (not in AIRPORTS dict yet)."]
            results[stn] = {"lat": None, "lon": None, "series": []}
            continue

        lat, lon = AIRPORTS[stn]
        series = []

        for fxx in range(0, fxx_max + 1):
            try:
                # --- 10m winds from wrfmsl ---
                H = Herbie(cycle, model="rap", product="awp130pgrb", fxx=fxx, save_dir=str(HERBIE_DIR), overwrite=True)
                ds = _as_dataset(H.xarray(remove_grib=True))
                p = _ds_select_nearest(ds, lat, lon)

                u10, v10 = _pick_uv_at_level(p, level_type="heightAboveGround", level=10)
                u925, v925 = _pick_uv_at_level(p, level_type="isobaricInhPa", level=925)


                if None in (u10, v10, u925, v925):
                    raise ValueError("Missing U/V at 10m and/or 925mb (check products/levels).")

                spd10 = _wind_speed(u10, v10)
                spd925 = _wind_speed(u925, v925)
                shear = _wind_speed(u925 - u10, v925 - v10)

                valid = cycle + timedelta(hours=fxx)
                valid_utc = valid.replace(tzinfo=timezone.utc).isoformat(timespec="minutes").replace("+00:00", "Z")

                series.append({
                    "fxx": fxx,
                    "valid_utc": valid_utc,
                    "wind10_kt": round(spd10 * 1.94384, 1),
                    "wind925_kt": round(spd925 * 1.94384, 1),
                    "shear_kt": round(shear * 1.94384, 1),
                })

            except Exception as e:
                errors.setdefault(stn, []).append(f"f{fxx:02d}: {e}")

        results[stn] = {"lat": lat, "lon": lon, "series": series}

    return {
        "model": "RAP",
        "product": {"wind10": "wrfmsl", "wind925": "wrfprs"},
        "cycle_utc": cycle_aware.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "fxx_max": fxx_max,
        "stations": stations_clean,
        "results": results,
        "errors": errors,
    }
