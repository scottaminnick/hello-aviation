import os
import traceback
from flask import Flask, jsonify, render_template_string, Response
from guidance import get_guidance_cached
from metar import get_metars_cached, summarize_metars
from rap_point import get_rap_point_guidance_cached
from winds import get_hrrr_gusts_cached   # ← new import

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Home page template (unchanged from original)
# ─────────────────────────────────────────────────────────────────────────────
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
      <p><a href="/map/winds">/map/winds</a> 🌬️ <b>HRRR Colorado Wind Gusts</b></p>
    </div>
  </body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# Wind Gust Map — HTML page template
# ─────────────────────────────────────────────────────────────────────────────
# Design notes:
#   • Dark "flight-deck" aesthetic — feels at home in an aviation context
#   • OpenTopoMap tiles show terrain hillshading beautifully under the gusts
#   • Canvas renderer (L.canvas) is critical for performance with ~7,500 rects
#   • Aviation colour scale: green / yellow / orange / red by knot threshold
#   • Data is fetched from /api/winds/colorado so the page is just a shell;
#     the heavy lifting lives in winds.py
# ─────────────────────────────────────────────────────────────────────────────
WINDS_MAP_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HRRR Wind Gusts — Colorado</title>

  <!-- Leaflet CSS -->
  <link rel="stylesheet"
        href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />

  <!-- Google Font: JetBrains Mono for that avionics readout feel -->
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@400;500;600&display=swap"
        rel="stylesheet" />

  <style>
    /* ── Reset & base ─────────────────────────────────────────────────── */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:        #0d1117;
      --panel:     #161b22;
      --border:    #30363d;
      --text:      #e6edf3;
      --muted:     #8b949e;
      --accent:    #58a6ff;

      /* Aviation gust colours — match the JS thresholds exactly */
      --c-green:   #2ecc71;
      --c-yellow:  #f1c40f;
      --c-orange:  #e67e22;
      --c-red:     #e74c3c;
    }

    html, body { height: 100%; background: var(--bg); color: var(--text);
                 font-family: 'Inter', sans-serif; }

    /* ── Top bar ──────────────────────────────────────────────────────── */
    header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 0.6rem 1.2rem;
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      gap: 1rem;
    }
    .header-left { display: flex; align-items: baseline; gap: 0.75rem; }
    .header-left h1 {
      font-family: 'JetBrains Mono', monospace;
      font-size: 1.05rem; font-weight: 700; letter-spacing: 0.04em;
      color: var(--accent);
    }
    .header-left .subtitle { font-size: 0.8rem; color: var(--muted); }

    /* Metadata strip — cycle time, valid time, point count */
    #meta-strip {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.72rem; color: var(--muted);
      display: flex; gap: 1.2rem; flex-wrap: wrap;
    }
    #meta-strip span b { color: var(--text); }

    .back-link {
      font-size: 0.8rem; color: var(--muted); text-decoration: none;
      border: 1px solid var(--border); border-radius: 4px;
      padding: 0.3rem 0.65rem;
      transition: color 0.15s, border-color 0.15s;
    }
    .back-link:hover { color: var(--accent); border-color: var(--accent); }

    /* ── Map container ────────────────────────────────────────────────── */
    #map {
      /* Fill the remaining viewport height below the header */
      width: 100%;
      height: calc(100vh - 52px);
    }

    /* ── Legend (floating card, bottom-left) ──────────────────────────── */
    #legend {
      position: absolute;
      bottom: 2rem; left: 1rem;
      z-index: 1000;
      background: rgba(13, 17, 23, 0.92);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.75rem 1rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.75rem;
      backdrop-filter: blur(6px);
      min-width: 170px;
    }
    #legend .leg-title {
      font-size: 0.65rem; letter-spacing: 0.08em; text-transform: uppercase;
      color: var(--muted); margin-bottom: 0.55rem;
    }
    .leg-row { display: flex; align-items: center; gap: 0.55rem; margin: 0.3rem 0; }
    .leg-swatch {
      width: 22px; height: 13px; border-radius: 3px; flex-shrink: 0;
      opacity: 0.85;
    }

    /* ── Loading overlay ──────────────────────────────────────────────── */
    #loading-overlay {
      position: absolute; inset: 0; z-index: 2000;
      background: rgba(13, 17, 23, 0.88);
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      gap: 1rem;
      font-family: 'JetBrains Mono', monospace;
      transition: opacity 0.4s;
    }
    #loading-overlay.hidden { opacity: 0; pointer-events: none; }

    .spinner {
      width: 42px; height: 42px;
      border: 3px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.9s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    #load-status { font-size: 0.8rem; color: var(--muted); }

    /* ── Error banner ─────────────────────────────────────────────────── */
    #error-banner {
      display: none;
      position: absolute; top: 1rem; left: 50%; transform: translateX(-50%);
      z-index: 3000;
      background: #3d1c1c; border: 1px solid var(--c-red);
      color: #ffb3b3; border-radius: 6px;
      padding: 0.6rem 1rem; font-size: 0.82rem; max-width: 90%;
    }

    /* ── Leaflet popup tweaks ─────────────────────────────────────────── */
    .leaflet-popup-content-wrapper {
      background: var(--panel) !important;
      border: 1px solid var(--border) !important;
      color: var(--text) !important;
      border-radius: 6px !important;
      box-shadow: 0 4px 16px rgba(0,0,0,0.5) !important;
      font-family: 'JetBrains Mono', monospace !important;
      font-size: 0.78rem !important;
    }
    .leaflet-popup-tip { background: var(--panel) !important; }
    .leaflet-popup-close-button { color: var(--muted) !important; }
  </style>
</head>
<body>

<!-- ── Header ────────────────────────────────────────────────────────────── -->
<header>
  <div class="header-left">
    <h1>🌬 HRRR WIND GUSTS</h1>
    <span class="subtitle">Colorado · 10 m AGL · Aviation scale</span>
  </div>

  <div id="meta-strip">
    <span>CYCLE <b id="m-cycle">—</b></span>
    <span>VALID <b id="m-valid">—</b></span>
    <span>PTS <b id="m-pts">—</b></span>
  </div>

  <a class="back-link" href="/">← Home</a>
</header>

<!-- ── Map ───────────────────────────────────────────────────────────────── -->
<div id="map"></div>

<!-- ── Legend ────────────────────────────────────────────────────────────── -->
<div id="legend">
  <div class="leg-title">Wind Gust (kt)</div>
  <div class="leg-row"><div class="leg-swatch" style="background:var(--c-green)"></div>&lt; 20</div>
  <div class="leg-row"><div class="leg-swatch" style="background:var(--c-yellow)"></div>20 – 35</div>
  <div class="leg-row"><div class="leg-swatch" style="background:var(--c-orange)"></div>35 – 50</div>
  <div class="leg-row"><div class="leg-swatch" style="background:var(--c-red)"></div>≥ 50</div>
</div>

<!-- ── Loading overlay ───────────────────────────────────────────────────── -->
<div id="loading-overlay">
  <div class="spinner"></div>
  <div id="load-status">Fetching HRRR data…</div>
</div>

<!-- ── Error banner ──────────────────────────────────────────────────────── -->
<div id="error-banner"></div>

<!-- ── Leaflet JS ─────────────────────────────────────────────────────────── -->
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<script>
// ── Colour thresholds — aviation scale ────────────────────────────────────
// Match the legend exactly.
function gustColor(kt) {
  if (kt >= 50) return '#e74c3c';   // red   — severe
  if (kt >= 35) return '#e67e22';   // orange — significant
  if (kt >= 20) return '#f1c40f';   // yellow — moderate
  return '#2ecc71';                  // green  — light
}

// ── Initialise Leaflet map centred on Colorado ────────────────────────────
const map = L.map('map', {
  center: [39.0, -105.5],
  zoom: 7,
  // Canvas renderer is ~10× faster than SVG for thousands of rectangles
  renderer: L.canvas(),
  preferCanvas: true,
});

// OpenTopoMap shows beautiful hillshading — the terrain "pops" under the
// semi-transparent gust colour rectangles.
L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://openstreetmap.org/copyright">OpenStreetMap</a> contributors, '
             + '<a href="http://viewfinderpanoramas.org">SRTM</a> | '
             + 'Map style: © <a href="https://opentopomap.org">OpenTopoMap</a>',
  maxZoom: 11,
  opacity: 1.0,
}).addTo(map);

// ── Fetch gust grid from our Flask API ───────────────────────────────────
async function loadGusts() {
  const overlay   = document.getElementById('loading-overlay');
  const statusEl  = document.getElementById('load-status');
  const errorEl   = document.getElementById('error-banner');

  statusEl.textContent = 'Fetching HRRR data… (first load may take ~30 s)';

  try {
    const resp = await fetch('/api/winds/colorado');
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Server error ${resp.status}: ${text.slice(0, 200)}`);
    }
    const data = await resp.json();

    // ── Update metadata strip ────────────────────────────────────────────
    document.getElementById('m-cycle').textContent = data.cycle_utc;
    document.getElementById('m-valid').textContent = data.valid_utc;
    document.getElementById('m-pts').textContent   = data.point_count.toLocaleString();

    // ── Render grid cells ────────────────────────────────────────────────
    // Each point is the centre of a ~6 km cell.
    // We offset by half the cell_size to draw the bounding rectangle.
    const halfLat = data.cell_size_deg / 2;
    // Longitude degrees per cell varies with latitude; 0.069° ≈ 6 km at 39°N
    const halfLon = data.cell_size_deg * 1.25;

    statusEl.textContent = `Rendering ${data.point_count.toLocaleString()} cells…`;

    // Batch the rendering into the next animation frame so the browser
    // doesn't lock up on us
    requestAnimationFrame(() => {
      const renderer = L.canvas();

      data.points.forEach(p => {
        const bounds = [
          [p.lat - halfLat, p.lon - halfLon],
          [p.lat + halfLat, p.lon + halfLon],
        ];
        const color = gustColor(p.gust_kt);
        const rect = L.rectangle(bounds, {
          renderer,
          color:       color,
          fillColor:   color,
          fillOpacity: 0.60,   // terrain hillshading shows through
          weight:      0,      // no border — cleaner at this density
        });

        // Click any cell for a detailed popup
        rect.bindPopup(
          `<b style="font-size:1rem">${p.gust_kt.toFixed(0)} kt</b><br>` +
          `<span style="color:#8b949e">${p.lat.toFixed(3)}°N, ${Math.abs(p.lon).toFixed(3)}°W</span>`,
          { maxWidth: 150 }
        );

        rect.addTo(map);
      });

      // ── Hide loading overlay ─────────────────────────────────────────
      overlay.classList.add('hidden');
    });

  } catch (err) {
    overlay.classList.add('hidden');
    errorEl.style.display = 'block';
    errorEl.textContent = '⚠ ' + err.message;
    console.error(err);
  }
}

loadGusts();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Routes — existing (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

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
```

Commit it, wait for Railway to redeploy, then visit:
```
https://your-railway-url.up.railway.app/debug/routes

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


# ─────────────────────────────────────────────────────────────────────────────
# Routes — NEW: HRRR Colorado wind gust map
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/map/winds")
def map_winds():
    """Serve the interactive wind gust map page."""
    return render_template_string(WINDS_MAP_TEMPLATE)


@app.get("/api/winds/colorado")
def api_winds_colorado():
    """
    Return HRRR wind gust grid over Colorado as JSON.
    The Leaflet page fetches this endpoint asynchronously.

    Query params (optional):
      fxx    — forecast hour, 0–18 (default 0 = analysis)
      ttl    — cache TTL in seconds (default 600)
    """
    from flask import request
    fxx = int(request.args.get("fxx", 0))
    ttl = int(request.args.get("ttl", os.environ.get("WINDS_TTL", "600")))

    data = get_hrrr_gusts_cached(fxx=fxx, ttl_seconds=ttl)
    return jsonify(data)


# ─────────────────────────────────────────────────────────────────────────────
# Global error handler — returns a readable traceback in plain text
# so you can debug directly in the browser or Railway logs
# ─────────────────────────────────────────────────────────────────────────────

@app.errorhandler(Exception)
def handle_exception(e):
    tb = traceback.format_exc()
    return Response(tb, mimetype="text/plain", status=500)

