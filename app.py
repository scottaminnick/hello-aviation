import os
from flask import Flask, jsonify, render_template_string
from guidance import get_guidance_cached
from metar import get_metars_cached, summarize_metars
from rap_point import get_rap_point_guidance_cached
# (make sure rap_point.py defines this exact function name)

app = Flask(__name__)

HOME_TEMPLATE = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{{ title }}</title>
    <style>
      body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 900px; }
      code, pre { background: #f4f4f4; padding: 0.2rem 0.4rem; border-radius: 6px; }
      .card { border: 1px solid #ddd; border-radius: 12px; padding: 1rem 1.2rem; margin: 1rem 0; }
      .muted { color: #666; }
      ul { margin: 0.4rem 0 0 1.2rem; }
      .hi { font-weight: 700; }
      .bad { font-weight: 700; text-transform: uppercase; }

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
      <ul>
        {% for n in g.notes %}
        <li>{{ n }}</li>
        {% endfor %}
      </ul>
      {% endif %}
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
    </div>
  </body>
</html>
"""

@app.get("/")
def home():
    title = os.environ.get("APP_TITLE", "Aviation Guidance")
    g = get_guidance_cached(ttl_seconds=int(os.environ.get("GUIDANCE_TTL", "300")))

    stations_default = [s.strip().upper() for s in os.environ.get("METAR_STATIONS", "KMCI,KSTL,KMKC").split(",") if s.strip()]
    metars_raw = get_metars_cached(
        stations=stations_default,
        ttl_seconds=int(os.environ.get("METAR_TTL", "120"))
    )
    metars = summarize_metars(metars_raw)

    return render_template_string(HOME_TEMPLATE, title=title, g=g, metars=metars)

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.get("/api/guidance")
def api_guidance():
    g = get_guidance_cached(ttl_seconds=int(os.environ.get("GUIDANCE_TTL", "300")))
    return jsonify(g)

@app.get("/api/metars")
def api_metars():
    # Default stations can be overridden by query string or env var later
    stations_default = [s.strip().upper() for s in os.environ.get("METAR_STATIONS", "KMCI,KSTL,KMKC").split(",") if s.strip()]
    metars = get_metars_cached(
        stations=stations_default,
        ttl_seconds=int(os.environ.get("METAR_TTL", "120"))
    )
    return jsonify(metars)

@app.get("/api/rap/points")
def api_rap_points():
    stations_default = os.environ.get("RAP_STATIONS", "KMCI,KSTL,KMKC").split(",")
    fxx_max = int(os.environ.get("RAP_FXX_MAX", "6"))
    ttl = int(os.environ.get("RAP_TTL", "600"))

    data = get_rap_point_guidance_cached(
      stations=stations_default,
      ttl_seconds=ttl,
      fxx_max=fxx_max
  )
  return jsonify(data)









