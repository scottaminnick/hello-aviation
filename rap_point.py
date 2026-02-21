import numpy as np
import xarray as xr
from datetime import timedelta, timezone
from herbie import Herbie


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


def fetch_rap_point_guidance(stations: list[str], fxx_max: int = 6) -> dict:
    """
    For each station, return f00..fxx_max point time series of:
      - 10m wind speed (kt)
      - 925mb wind speed (kt)
      - 10m->925mb vector shear magnitude (kt)

    Uses RAP:
      - wrfmsl product for 10m winds
      - wrfprs product for pressure-level winds (925mb)
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
                H10 = Herbie(cycle, model="rap", product="wrfmsl", fxx=fxx)
                ds10 = _as_dataset(H10.xarray(":(UGRD|VGRD|u|v):10 m above ground:", remove_grib=True))
                p10 = _ds_select_nearest(ds10, lat, lon)
                u10, v10 = _pick_uv_at_level(p10, level_type="heightAboveGround", level=10)

                # --- 925mb winds from wrfprs ---
                H925 = Herbie(cycle, model="rap", product="wrfprs", fxx=fxx)
                ds925 = _as_dataset(H925.xarray(":(UGRD|VGRD|u|v):925 mb:", remove_grib=True))
                p925 = _ds_select_nearest(ds925, lat, lon)
                u925, v925 = _pick_uv_at_level(p925, level_type="isobaricInhPa", level=925)

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
