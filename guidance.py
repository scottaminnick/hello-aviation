import os
import time
from datetime import datetime, timezone

# Simple in-memory cache
_CACHE = {"ts": 0, "data": None}

def build_guidance():
    """
    Replace the internals of this function later with real model logic:
    - fetch RAP/HRRR
    - compute Froude
    - generate tiles/plots
    - etc.
    """
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "product": os.environ.get("PRODUCT_NAME", "demo-guidance"),
        "message": "Guidance template is running. Next step: plug in real data.",
        "notes": [
            "This endpoint is meant to be stable for coworkers/scripts.",
            "Add your science in guidance.build_guidance()."
        ],
    }

def get_guidance_cached(ttl_seconds: int = 300):
    """
    Cache guidance for ttl_seconds so we don't recompute/fetch every request.
    """
    now = time.time()
    if _CACHE["data"] is None or (now - _CACHE["ts"]) > ttl_seconds:
        _CACHE["data"] = build_guidance()
        _CACHE["ts"] = now
    return _CACHE["data"]
