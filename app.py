import os
from flask import Flask, jsonify, render_template_string, Response
from guidance import get_guidance_cached
from metar import get_metars_cached, summarize_metars
from rap_point import get_rap_point_guidance_cached
from llti import get_llti_cached          # ← new LLTI module

app = Flask(__name__)

HOME_TEMPLATE = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{{ title }}</title>
    <style>
      body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 960px; }
      code, pre { background: #f4f4f4; padding: 0.2rem 0.4rem; border-radius: 6px; }
      .card { border: 1px solid #ddd; border-radius: 12px; padding: 1rem 1.2rem; margin: 1rem 0; }
      .muted { color: #666; }
      ul { margin: 0.4rem 0 0 1.2rem; }
      .hi { font-weight: 700; }
      .bad { font-weight: 700; text-transform: uppercase; }
      .llti-img { width: 100%; border-radius: 8px; margin-top: 0.5rem; }
      .llti-meta { display: flex; gap: 1.5rem; font-size: 0.9rem; margin: 0.5rem 0; }
      .llti-meta span { background: #f0f0f0; border-radius: 6px; padding: 0.2rem 0.6rem; }
    </style>
  </head>
  <body>
    <h1>{{ title }}</h1>
    <p class="muted">GitHub → Railway deployment pipeline is working.</p>

    <div class="card">
      <h2>Latest Guidance</h2>
      <p><b>Generated (UTC):</b> {{ g.generated_utc }}</p>
      <p><b>Product:</b> {{ g.product }}</p>
      <p><b>Message:</b> {{ g.message }}</p>
      {% if g.notes %}
      <p><b>Notes:</b></p>
      <ul>{% for n in g.notes %}<li>{{ n }}</li>{% endfor %}</ul>
      {% endif %}
    </div>

    <div class="card">
      <h2>HRRR Low-Level Turbulence Index (LLTI) – Colorado</h2>
      {% if llti_meta %}
      <div class="llti-meta">
        <span>🕐 Cycle: {{ llti_meta.cycle_utc }}</span>
        <span>▼ Min: {{ llti_meta.llti_min | round(0) | int }}</span>
        <span>~ Mean: {{ llti_meta.llti_mean | round(0) | int }}</span>
        <span>▲ Max: {{ llti_meta.llti_max | round(0) | int }}</span>
      </div>
      <img class="llti-img" src="/api/llti/image" alt="LLTI map" />
      {% else %}
      <p class="muted">LLTI image unavailable. Check logs for HRRR fetch errors.</p>
      {% endif %}
      <p class="muted" style="margin-top:0.5rem;">
        JSON metadata: <a href="/api/llti/meta">/api/llti/meta</a>
      </p>
    </div>

    <div class="card">
      <h2>Latest METARs</h2>
      <table style="width:100%; border-collapse: collapse;">
        <thead>
          <tr>
            <th align="left">Station</th>
            <th align="left">Time (UTC)</th>
            <th align="left">Cat</th>
            <th align="left">Wind</th>
            <th align="left">Vis</th>
            <th align="left">Ceiling</th>
            <th align="left">Cover</th>
          </tr>
        </thead>
        <tbody>
          {% for m in metars %}
          <tr>
            <td><b>{{ m.icao }}</b></td>
            <td>{{ m.time_utc }}</td>
            <td class="{% if m.fltCat in ['IFR','LIFR'] %}bad{% endif %}">{{ m.fltCat }}</td>
            <td class="{% if m.wgst and m.wgst|int >= 25 %}hi{% endif %}">{{ m.wind }}</td>
            <td>{{ m.vis }}</td>
            <td>{{ m.ceiling }}</td>
            <td>{{ m.cover }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      <p class="muted" style="margin-top:0.8rem;">Raw JSON: <a href="/api/metars">/api/metars</a></p>
    </div>

    <div class="card">
      <h3>Useful links</h3>
      <p><a href="/health">/health</a> (ops check)</p>
      <p><a href="/api/guidance">/api/guidance</a> (JSON for scripts/coworkers)</p>
      <p><a href="/api/metars">/api/metars</a> (latest METAR JSON)</p>
      <p><a href="/api/rap/points">/api/rap/points</a> (RAP point guidance)</p>
      <p><a href="/api/llti/image">/api/llti/image</a> (LLTI PNG)</p>
      <p><a href="/api/llti/meta">/api/llti/meta</a> (LLTI metadata JSON)</p>
    </div>
  </body>
</html>
"""

@app.get("/")
def home():
    title = os.environ.get("APP_TITLE", "Aviation Guidance")
    g = get_guidance_cached(ttl_seconds=int(os.environ.get("GUIDANCE_TTL", "300")))

    stations_default = [
        s.strip().upper()
        for s in os.environ.get("METAR_STATIONS", "KMCI,KSTL,KMKC").split(",")
        if s.strip()
    ]
    metars_raw = get_metars_cached(
        stations=stations_default,
        ttl_seconds=int(os.environ.get("METAR_TTL", "120")),
    )
    metars = summarize_metars(metars_raw)

    # LLTI: try to get cached metadata for the summary row;
    # if HRRR is unavailable we show a graceful message instead of a 500.
    try:
        _, llti_meta = get_llti_cached(
            ttl_seconds=int(os.environ.get("LLTI_TTL", "600"))
        )
    except Exception:
        llti_meta = None

    return render_template_string(
        HOME_TEMPLATE, title=title, g=g, metars=metars, llti_meta=llti_meta
    )


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/api/guidance")
def api_guidance():
    g = get_guidance_cached(ttl_seconds=int(os.environ.get("GUIDANCE_TTL", "300")))
    return jsonify(g)


@app.get("/api/metars")
def api_metars():
    stations_default = [
        s.strip().upper()
        for s in os.environ.get("METAR_STATIONS", "KMCI,KSTL,KMKC").split(",")
        if s.strip()
    ]
    metars = get_metars_cached(
        stations=stations_default,
        ttl_seconds=int(os.environ.get("METAR_TTL", "120")),
    )
    return jsonify(metars)


@app.get("/api/rap/points")
def api_rap_points():
    stations_default = os.environ.get("RAP_STATIONS", "KMCI,KSTL,KMKC").split(",")
    fxx_max = int(os.environ.get("RAP_FXX_MAX", "6"))
    ttl = int(os.environ.get("RAP_TTL", "600"))
    data = get_rap_point_guidance_cached(
        stations=stations_default, ttl_seconds=ttl, fxx_max=fxx_max
    )
    return jsonify(data)


# ── LLTI endpoints ────────────────────────────────────────────────────────────

@app.get("/api/llti/image")
def api_llti_image():
    """
    Returns the LLTI map as a PNG image (cached up to LLTI_TTL seconds).
    Suitable for embedding in <img src="/api/llti/image">.
    """
    ttl = int(os.environ.get("LLTI_TTL", "600"))
    try:
        png_bytes, _ = get_llti_cached(ttl_seconds=ttl)
        return Response(png_bytes, mimetype="image/png")
    except Exception as e:
        import traceback
        return Response(
            f"LLTI image error:\n{traceback.format_exc()}",
            mimetype="text/plain",
            status=500,
        )


@app.get("/api/llti/meta")
def api_llti_meta():
    """
    Returns LLTI metadata as JSON: cycle time, grid stats, configuration.
    """
    ttl = int(os.environ.get("LLTI_TTL", "600"))
    try:
        _, meta = get_llti_cached(ttl_seconds=ttl)
        return jsonify(meta)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


# ── Error handler ─────────────────────────────────────────────────────────────

import traceback

@app.errorhandler(Exception)
def handle_exception(e):
    tb = traceback.format_exc()
    return Response(tb, mimetype="text/plain", status=500)
