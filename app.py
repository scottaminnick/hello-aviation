import os
import traceback
from flask import Flask, jsonify, render_template_string, Response, request
from guidance import get_guidance_cached
from metar import get_metars_cached, summarize_metars
from rap_point import get_rap_point_guidance_cached
from winds import get_hrrr_gusts_cached, get_cycle_status_cached

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
      --green: #2ecc71; --yellow: #f1c40f; --orange: #e67e22; --red: #e74c3c;
    }
    html, body { height: 100%; background: var(--bg); color: var(--text); font-family: sans-serif; }
    header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 0.5rem 1.2rem; background: var(--panel);
      border-bottom: 1px solid var(--border); gap: 1rem; flex-wrap: wrap;
      min-height: 56px;
    }
    header h1 { font-size: 1rem; font-weight: 700; color: var(--accent); white-space: nowrap; }
    .subtitle { font-size: 0.8rem; color: var(--muted); margin-left: 0.5rem; }

    /* ── Cycle selector ── */
    #cycle-wrap {
      display: flex; align-items: center; gap: 0.5rem;
    }
    #cycle-wrap label { font-size: 0.72rem; color: var(--muted); white-space: nowrap; }
    #cycle-select {
      background: var(--panel); color: var(--text);
      border: 1px solid var(--border); border-radius: 4px;
      padding: 0.25rem 0.5rem; font-size: 0.75rem; cursor: pointer;
    }

    /* ── Progress bar ── */
    #progress-wrap {
      display: flex; align-items: center; gap: 0.5rem;
      background: rgba(88,166,255,0.06); border: 1px solid var(--border);
      border-radius: 6px; padding: 0.3rem 0.75rem; min-width: 160px;
    }
    #progress-label { font-size: 0.72rem; color: var(--muted); white-space: nowrap; }
    #progress-bar-track {
      flex: 1; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden;
    }
    #progress-bar-fill {
      height: 100%; width: 0%; border-radius: 3px;
      background: linear-gradient(90deg, var(--accent), var(--green));
      transition: width 0.4s ease;
    }
    #progress-pct { font-size: 0.8rem; font-weight: 700; color: var(--accent); min-width: 2.5rem; }

    /* ── Hour buttons ── */
    #hours-wrap {
      display: flex; align-items: center; gap: 0.3rem; flex-wrap: wrap;
    }
    #hours-wrap label { font-size: 0.72rem; color: var(--muted); white-space: nowrap; margin-right: 0.2rem; }
    .hr-btn {
      font-size: 0.7rem; font-weight: 600; padding: 0.2rem 0.4rem;
      border-radius: 4px; border: 1px solid var(--border);
      background: var(--panel); color: var(--muted);
      cursor: not-allowed; opacity: 0.4; transition: all 0.15s;
      min-width: 2rem; text-align: center;
    }
    .hr-btn.avail {
      color: var(--text); opacity: 1; cursor: pointer;
      border-color: #444;
    }
    .hr-btn.avail:hover { background: #2a3a4a; border-color: var(--accent); }
    .hr-btn.active {
      background: var(--accent); color: #0d1117;
      border-color: var(--accent); cursor: default;
    }

    #meta-strip { font-size: 0.72rem; color: var(--muted); display: flex; gap: 1.2rem; flex-wrap: wrap; }
    #meta-strip b { color: var(--text); }

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
    <span class="subtitle">Colorado &middot; 10 m AGL &middot; Aviation scale</span>
  </div>

  <!-- Cycle selector -->
  <div id="cycle-wrap">
    <label for="cycle-select">CYCLE</label>
    <select id="cycle-select"><option>Loading...</option></select>
  </div>

  <!-- Progress bar -->
  <div id="progress-wrap">
    <span id="progress-label">AVAIL</span>
    <div id="progress-bar-track"><div id="progress-bar-fill"></div></div>
    <span id="progress-pct">--%</span>
  </div>

  <!-- Forecast hour buttons -->
  <div id="hours-wrap">
    <label>FCST HOUR</label>
    <!-- Buttons injected by JS -->
  </div>

  <div id="meta-strip">
    <span>VALID <b id="m-valid">--</b></span>
    <span>PTS <b id="m-pts">--</b></span>
  </div>
  <a class="back-link" href="/">&#8592; Home</a>
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
  <div id="load-status">Checking HRRR availability...</div>
</div>

<div id="error-banner"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// ── State ────────────────────────────────────────────────────────────────────
let gustLayer    = null;
let cycleStatus  = null;   // full status response from /api/winds/status
let activeCycle  = null;   // currently selected cycle_utc string
let activeFxx    = 1;

// ── Map setup ────────────────────────────────────────────────────────────────
const map = L.map('map', {
  center: [39.0, -105.5], zoom: 7,
  renderer: L.canvas(), preferCanvas: true,
});
L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: 'Map: OpenTopoMap', maxZoom: 11,
}).addTo(map);

function gustColor(kt) {
  if (kt >= 50) return '#e74c3c';
  if (kt >= 35) return '#e67e22';
  if (kt >= 20) return '#f1c40f';
  return '#2ecc71';
}

// ── Status / availability ────────────────────────────────────────────────────
async function fetchStatus() {
  const resp = await fetch('/api/winds/status');
  if (!resp.ok) throw new Error('Status fetch failed: ' + resp.status);
  return resp.json();
}

function applyStatus(status) {
  cycleStatus = status;

  // Populate cycle dropdown
  const sel = document.getElementById('cycle-select');
  sel.innerHTML = '';
  status.cycles.forEach(function(c, idx) {
    const opt    = document.createElement('option');
    opt.value    = c.cycle_utc;
    opt.textContent = c.cycle_utc + '  (' + c.pct_complete + '% complete)';
    if (idx === 0) opt.selected = true;
    sel.appendChild(opt);
  });

  // Show the current cycle's availability
  updateProgressAndButtons(status.cycles[0]);

  if (!activeCycle) activeCycle = status.cycles[0].cycle_utc;
}

function updateProgressAndButtons(cycleData) {
  // Progress bar
  document.getElementById('progress-bar-fill').style.width = cycleData.pct_complete + '%';
  document.getElementById('progress-pct').textContent      = cycleData.pct_complete + '%';

  // Hour buttons (F01-F12)
  const wrap = document.getElementById('hours-wrap');
  // Remove old buttons but keep the label
  wrap.querySelectorAll('.hr-btn').forEach(function(b) { b.remove(); });

  for (var fxx = 1; fxx <= 12; fxx++) {
    const btn     = document.createElement('button');
    btn.className = 'hr-btn';
    btn.textContent = 'F' + String(fxx).padStart(2, '0');
    btn.dataset.fxx = fxx;

    const avail = cycleData.available_hours.includes(fxx);
    if (avail) {
      btn.classList.add('avail');
      btn.addEventListener('click', onHourClick);
    }
    if (fxx === activeFxx) btn.classList.add('active');
    wrap.appendChild(btn);
  }
}

function setActiveButton(fxx) {
  document.querySelectorAll('.hr-btn').forEach(function(b) {
    b.classList.toggle('active', parseInt(b.dataset.fxx) === fxx);
  });
}

function onHourClick(e) {
  const fxx = parseInt(e.target.dataset.fxx);
  if (fxx === activeFxx) return;
  activeFxx = fxx;
  setActiveButton(fxx);
  loadGusts(activeCycle, fxx);
}

// Cycle dropdown change
document.getElementById('cycle-select').addEventListener('change', function() {
  activeCycle = this.value;
  // Update progress bar for the newly selected cycle
  const cycleData = cycleStatus.cycles.find(function(c) { return c.cycle_utc === activeCycle; });
  if (cycleData) updateProgressAndButtons(cycleData);
  // Load the first available hour for this cycle
  if (cycleData && cycleData.available_hours.length > 0) {
    activeFxx = cycleData.available_hours[0];
    setActiveButton(activeFxx);
    loadGusts(activeCycle, activeFxx);
  }
});

// ── Gust data loader ─────────────────────────────────────────────────────────
async function loadGusts(cycle_utc, fxx) {
  const overlay  = document.getElementById('loading-overlay');
  const statusEl = document.getElementById('load-status');
  const errorEl  = document.getElementById('error-banner');

  overlay.classList.remove('hidden');
  statusEl.textContent = 'Fetching HRRR ' + cycle_utc + ' F' + String(fxx).padStart(2,'0') + '...';
  errorEl.style.display = 'none';

  try {
    const url  = '/api/winds/colorado?fxx=' + fxx + '&cycle_utc=' + encodeURIComponent(cycle_utc);
    const resp = await fetch(url);

    if (!resp.ok) {
      const body = await resp.json().catch(function() { return null; });
      if (resp.status === 404 && body && body.error === 'not_available') {
        overlay.classList.add('hidden');
        errorEl.style.display = 'block';
        errorEl.textContent = '\u26a0\ufe0f F' + String(fxx).padStart(2,'0') +
          ' not yet available \u2014 try a lower forecast hour.';
        return;
      }
      throw new Error('Server ' + resp.status);
    }

    const data = await resp.json();
    document.getElementById('m-valid').textContent = data.valid_utc;
    document.getElementById('m-pts').textContent   = data.point_count.toLocaleString();

    statusEl.textContent = 'Rendering ' + data.point_count.toLocaleString() + ' cells...';

    if (gustLayer) { map.removeLayer(gustLayer); gustLayer = null; }

    const halfLat  = data.cell_size_deg / 2;
    const halfLon  = data.cell_size_deg * 1.25;
    const renderer = L.canvas();
    const rects    = [];

    data.points.forEach(function(p) {
      var color = gustColor(p.gust_kt);
      var rect  = L.rectangle(
        [[p.lat - halfLat, p.lon - halfLon], [p.lat + halfLat, p.lon + halfLon]],
        { renderer: renderer, color: color, fillColor: color, fillOpacity: 0.60, weight: 0 }
      );
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
    errorEl.textContent   = 'Error: ' + err.message;
    console.error(err);
  }
}

// ── Init + auto-refresh ───────────────────────────────────────────────────────
async function init() {
  try {
    const status = await fetchStatus();
    applyStatus(status);

    // Load first available hour from latest cycle
    const latest = status.cycles[0];
    activeCycle  = latest.cycle_utc;
    if (latest.available_hours.length > 0) {
      activeFxx = latest.available_hours[0];
      setActiveButton(activeFxx);
      loadGusts(activeCycle, activeFxx);
    } else {
      // Latest cycle has nothing yet; try previous
      const prev = status.cycles[1];
      if (prev && prev.available_hours.length > 0) {
        activeCycle = prev.cycle_utc;
        document.getElementById('cycle-select').value = activeCycle;
        updateProgressAndButtons(prev);
        activeFxx = prev.available_hours[0];
        setActiveButton(activeFxx);
        loadGusts(activeCycle, activeFxx);
      }
    }
  } catch (err) {
    document.getElementById('load-status').textContent = 'Status check failed: ' + err.message;
    console.error(err);
  }
}

// Refresh status every 5 minutes (new hours come online as HRRR publishes)
setInterval(async function() {
  try {
    const status = await fetchStatus();
    applyStatus(status);
  } catch (e) { /* silent - don't disrupt active use */ }
}, 5 * 60 * 1000);

init();
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


@app.get("/debug/grib_fields")
def debug_grib_fields():
    """Dump gust-related field names from latest HRRR sfc F01 GRIB2."""
    import pygrib
    from winds import _find_latest_hrrr_cycle, HERBIE_DIR
    from herbie import Herbie
    from pathlib import Path

    cycle = _find_latest_hrrr_cycle()
    H = Herbie(cycle, model="hrrr", product="sfc", fxx=1,
               save_dir=str(HERBIE_DIR), overwrite=False)
    grib_path = Path(H.download())

    grbs = pygrib.open(str(grib_path))
    all_fields = []
    for grb in grbs:
        all_fields.append({
            "name":        grb.name,
            "shortName":   grb.shortName,
            "typeOfLevel": grb.typeOfLevel,
            "level":       grb.level,
            "stepType":    grb.stepType,
        })
    grbs.close()

    gust_fields = [f for f in all_fields
                   if "gust" in f["name"].lower() or f["shortName"] == "gust"]

    return jsonify({
        "cycle":        cycle.isoformat(),
        "grib_file":    grib_path.name,
        "total_fields": len(all_fields),
        "gust_fields":  gust_fields,
    })


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


@app.get("/api/winds/status")
def api_winds_status():
    """Return availability of F01-F12 for the latest two HRRR cycles."""
    ttl  = int(os.environ.get("STATUS_TTL", "300"))
    data = get_cycle_status_cached(ttl_seconds=ttl)
    return jsonify(data)


@app.get("/api/winds/colorado")
def api_winds_colorado():
    fxx       = int(request.args.get("fxx", 1))
    cycle_utc = request.args.get("cycle_utc")   # e.g. "2026-02-22T01:00Z"
    ttl       = int(request.args.get("ttl", os.environ.get("WINDS_TTL", "600")))

    # If no cycle specified, use the latest available
    if not cycle_utc:
        status    = get_cycle_status_cached(ttl_seconds=300)
        cycle_utc = status["cycles"][0]["cycle_utc"]

    try:
        data = get_hrrr_gusts_cached(cycle_utc=cycle_utc, fxx=fxx, ttl_seconds=ttl)
        return jsonify(data)
    except Exception as e:
        msg = str(e)
        not_ready = any(k in msg.lower() for k in [
            "did not find", "not found", "no such file", "404", "unavailable"
        ])
        if not_ready:
            return jsonify({
                "error": "not_available",
                "message": f"F{fxx:02d} for cycle {cycle_utc} is not yet available on AWS.",
                "fxx": fxx,
                "cycle_utc": cycle_utc,
            }), 404
        raise


@app.errorhandler(Exception)
def handle_exception(e):
    tb = traceback.format_exc()
    return Response(tb, mimetype="text/plain", status=500)
