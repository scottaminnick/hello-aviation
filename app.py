import os
import traceback
from flask import Flask, jsonify, render_template_string, Response, request
from guidance import get_guidance_cached
from metar import get_metars_cached, summarize_metars
from rap_point import get_rap_point_guidance_cached
from winds import get_hrrr_gusts_cached

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
    <p class="muted">GitHub to Railway deployment pipeline is working.</p>

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
      <h2>Latest METARs</h2>
      <table style="width:100%; border-collapse: collapse;">
        <thead>
          <tr>
            <th align="left">Station</th><th align="left">Time (UTC)</th>
            <th align="left">Cat</th><th align="left">Wind</th>
            <th align="left">Vis</th><th align="left">Ceiling</th>
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
            <td>{{ m.vis }}</td><td>{{ m.ceiling }}</td><td>{{ m.cover }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      <p class="muted" style="margin-top:0.8rem;">Raw JSON: <a href="/api/metars">/api/metars</a></p>
    </div>

    <div class="card">
      <h3>Useful links</h3>
      <p><a href="/health">/health</a> (ops check)</p>
      <p><a href="/api/guidance">/api/guidance</a> (JSON)</p>
      <p><a href="/api/metars">/api/metars</a> (latest METARs)</p>
      <p><a href="/api/rap/points">/api/rap/points</a> (RAP point guidance)</p>
      <p><a href="/map/winds">/map/winds</a> (HRRR Colorado Wind Gusts)</p>
      <p><a href="/debug/routes">/debug/routes</a> (registered routes)</p>
    </div>
  </body>
</html>
"""

WINDS_MAP_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HRRR Wind Gusts - Colorado</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0d1117; --panel: #161b22; --border: #30363d;
      --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
    }
    html, body { height: 100%; background: var(--bg); color: var(--text); font-family: sans-serif; }
    header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 0.5rem 1.2rem; background: var(--panel);
      border-bottom: 1px solid var(--border); gap: 1rem; flex-wrap: wrap;
    }
    header h1 { font-size: 1rem; font-weight: 700; color: var(--accent); white-space: nowrap; }
    .subtitle { font-size: 0.8rem; color: var(--muted); margin-left: 0.5rem; }
    #meta-strip { font-size: 0.72rem; color: var(--muted); display: flex; gap: 1.2rem; flex-wrap: wrap; }
    #meta-strip b { color: var(--text); }

    /* ── Forecast hour slider ── */
    #slider-wrap {
      display: flex; align-items: center; gap: 0.6rem;
      background: rgba(88,166,255,0.08); border: 1px solid var(--border);
      border-radius: 6px; padding: 0.3rem 0.75rem;
    }
    #slider-wrap label { font-size: 0.75rem; color: var(--muted); white-space: nowrap; }
    #fxx-slider {
      -webkit-appearance: none; appearance: none;
      width: 160px; height: 4px; border-radius: 2px;
      background: var(--border); outline: none; cursor: pointer;
    }
    #fxx-slider::-webkit-slider-thumb {
      -webkit-appearance: none; appearance: none;
      width: 14px; height: 14px; border-radius: 50%;
      background: var(--accent); cursor: pointer;
    }
    #fxx-label {
      font-size: 0.8rem; font-weight: 700; color: var(--accent);
      min-width: 2.8rem; text-align: right;
    }

    .back-link {
      font-size: 0.8rem; color: var(--muted); text-decoration: none;
      border: 1px solid var(--border); border-radius: 4px; padding: 0.3rem 0.65rem;
      white-space: nowrap;
    }
    #map { width: 100%; height: calc(100vh - 56px); }
    #legend {
      position: absolute; bottom: 2rem; left: 1rem; z-index: 1000;
      background: rgba(13,17,23,0.92); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.75rem 1rem; font-size: 0.75rem;
      min-width: 170px; color: var(--text);
    }
    .leg-title { font-size: 0.65rem; text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; }
    .leg-row { display: flex; align-items: center; gap: 0.55rem; margin: 0.3rem 0; }
    .leg-swatch { width: 22px; height: 13px; border-radius: 3px; opacity: 0.85; }
    #loading-overlay {
      position: absolute; inset: 0; z-index: 2000;
      background: rgba(13,17,23,0.88);
      display: flex; flex-direction: column;
      align-items: center; justify-content: center; gap: 1rem;
      transition: opacity 0.3s;
    }
    #loading-overlay.hidden { opacity: 0; pointer-events: none; }
    .spinner {
      width: 42px; height: 42px; border: 3px solid var(--border);
      border-top-color: var(--accent); border-radius: 50%;
      animation: spin 0.9s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    #load-status { font-size: 0.8rem; color: var(--muted); }
    #error-banner {
      display: none; position: absolute; top: 1rem; left: 50%;
      transform: translateX(-50%); z-index: 3000;
      background: #3d1c1c; border: 1px solid #e74c3c;
      color: #ffb3b3; border-radius: 6px;
      padding: 0.6rem 1rem; font-size: 0.82rem; max-width: 90%;
    }
  </style>
</head>
<body>
<header>
  <div style="display:flex; align-items:baseline; flex-shrink:0;">
    <h1>HRRR WIND GUSTS</h1>
    <span class="subtitle">Colorado · 10 m AGL · Aviation scale</span>
  </div>

  <!-- Forecast hour slider -->
  <div id="slider-wrap">
    <label for="fxx-slider">FCST HOUR</label>
    <input type="range" id="fxx-slider" min="1" max="12" step="1" value="1" />
    <span id="fxx-label">F01</span>
  </div>

  <div id="meta-strip">
    <span>CYCLE <b id="m-cycle">--</b></span>
    <span>VALID <b id="m-valid">--</b></span>
    <span>PTS <b id="m-pts">--</b></span>
  </div>
  <a class="back-link" href="/">Back to Home</a>
</header>

<div id="map"></div>

<div id="legend">
  <div class="leg-title">Wind Gust (kt)</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#2ecc71"></div>Less than 20</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#f1c40f"></div>20 to 35</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#e67e22"></div>35 to 50</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#e74c3c"></div>50 and above</div>
</div>

<div id="loading-overlay">
  <div class="spinner"></div>
  <div id="load-status">Fetching HRRR F01 data... first load may take 30–60 s</div>
</div>

<div id="error-banner"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
function gustColor(kt) {
  if (kt >= 50) return '#e74c3c';
  if (kt >= 35) return '#e67e22';
  if (kt >= 20) return '#f1c40f';
  return '#2ecc71';
}

const map = L.map('map', {
  center: [39.0, -105.5],
  zoom: 7,
  renderer: L.canvas(),
  preferCanvas: true,
});

L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: 'Map: OpenTopoMap',
  maxZoom: 11,
}).addTo(map);

// Track the current gust layer group so we can clear it on slider change
let gustLayer = null;

async function loadGusts(fxx) {
  const overlay  = document.getElementById('loading-overlay');
  const statusEl = document.getElementById('load-status');
  const errorEl  = document.getElementById('error-banner');

  // Show overlay for every load (not just the first)
  overlay.classList.remove('hidden');
  statusEl.textContent = 'Fetching HRRR F0' + fxx + ' data...';
  errorEl.style.display = 'none';

  // Disable slider while loading to prevent queuing multiple requests
  const slider = document.getElementById('fxx-slider');
  slider.disabled = true;

  try {
    const resp = await fetch('/api/winds/colorado?fxx=' + fxx);
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error('Server ' + resp.status + ': ' + text.slice(0, 300));
    }
    const data = await resp.json();

    document.getElementById('m-cycle').textContent = data.cycle_utc;
    document.getElementById('m-valid').textContent = data.valid_utc;
    document.getElementById('m-pts').textContent   = data.point_count.toLocaleString();

    statusEl.textContent = 'Rendering ' + data.point_count.toLocaleString() + ' cells...';

    // Clear previous layer
    if (gustLayer) {
      map.removeLayer(gustLayer);
      gustLayer = null;
    }

    const halfLat  = data.cell_size_deg / 2;
    const halfLon  = data.cell_size_deg * 1.25;
    const renderer = L.canvas();
    const rects    = [];

    data.points.forEach(function(p) {
      var bounds = [
        [p.lat - halfLat, p.lon - halfLon],
        [p.lat + halfLat, p.lon + halfLon],
      ];
      var color = gustColor(p.gust_kt);
      var rect  = L.rectangle(bounds, {
        renderer:    renderer,
        color:       color,
        fillColor:   color,
        fillOpacity: 0.60,
        weight:      0,
      });
      rect.bindPopup(
        '<b>' + p.gust_kt.toFixed(0) + ' kt</b><br>' +
        p.lat.toFixed(3) + '\u00b0N, ' + Math.abs(p.lon).toFixed(3) + '\u00b0W',
        { maxWidth: 150 }
      );
      rects.push(rect);
    });

    gustLayer = L.layerGroup(rects).addTo(map);
    overlay.classList.add('hidden');

  } catch (err) {
    overlay.classList.add('hidden');
    errorEl.style.display = 'block';
    errorEl.textContent = 'Error: ' + err.message;
    console.error(err);
  } finally {
    slider.disabled = false;
  }
}

// Slider wiring
const slider   = document.getElementById('fxx-slider');
const fxxLabel = document.getElementById('fxx-label');

slider.addEventListener('input', function() {
  var val = parseInt(this.value);
  fxxLabel.textContent = 'F' + String(val).padStart(2, '0');
});

// Load on slider release (mouseup / touchend) to avoid firing mid-drag
slider.addEventListener('change', function() {
  loadGusts(parseInt(this.value));
});

// Initial load at F01 (F00 analysis hour has unreliable gust values in HRRR)
loadGusts(1);
</script>
</body>
</html>
"""


@app.get("/")
def home():
    title = os.environ.get("APP_TITLE", "Aviation Guidance")
    g = get_guidance_cached(ttl_seconds=int(os.environ.get("GUIDANCE_TTL", "300")))
    stations_default = [s.strip().upper() for s in
                        os.environ.get("METAR_STATIONS", "KMCI,KSTL,KMKC").split(",")
                        if s.strip()]
    metars_raw = get_metars_cached(stations=stations_default,
                                   ttl_seconds=int(os.environ.get("METAR_TTL", "120")))
    metars = summarize_metars(metars_raw)
    return render_template_string(HOME_TEMPLATE, title=title, g=g, metars=metars)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/debug/routes")
def debug_routes():
    routes = [str(rule) for rule in app.url_map.iter_rules()]
    return jsonify(sorted(routes))


@app.get("/api/guidance")
def api_guidance():
    g = get_guidance_cached(ttl_seconds=int(os.environ.get("GUIDANCE_TTL", "300")))
    return jsonify(g)


@app.get("/api/metars")
def api_metars():
    stations_default = [s.strip().upper() for s in
                        os.environ.get("METAR_STATIONS", "KMCI,KSTL,KMKC").split(",")
                        if s.strip()]
    metars = get_metars_cached(stations=stations_default,
                               ttl_seconds=int(os.environ.get("METAR_TTL", "120")))
    return jsonify(metars)


@app.get("/api/rap/points")
def api_rap_points():
    stations_default = os.environ.get("RAP_STATIONS", "KMCI,KSTL,KMKC").split(",")
    fxx_max = int(os.environ.get("RAP_FXX_MAX", "6"))
    ttl     = int(os.environ.get("RAP_TTL", "600"))
    data = get_rap_point_guidance_cached(stations=stations_default,
                                         ttl_seconds=ttl, fxx_max=fxx_max)
    return jsonify(data)


@app.get("/map/winds")
def map_winds():
    return render_template_string(WINDS_MAP_TEMPLATE)


@app.get("/api/winds/colorado")
def api_winds_colorado():
    fxx = int(request.args.get("fxx", 0))
    ttl = int(request.args.get("ttl", os.environ.get("WINDS_TTL", "600")))
    data = get_hrrr_gusts_cached(fxx=fxx, ttl_seconds=ttl)
    return jsonify(data)


@app.errorhandler(Exception)
def handle_exception(e):
    tb = traceback.format_exc()
    return Response(tb, mimetype="text/plain", status=500)
