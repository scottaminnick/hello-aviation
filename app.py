import os
import traceback
from flask import Flask, jsonify, render_template_string, Response, request
from guidance import get_guidance_cached
from metar import get_metars_cached, summarize_metars
from rap_point import get_rap_point_guidance_cached
from winds import get_hrrr_gusts_cached, get_cycle_status_cached
from froude import get_froude_cached
from virga import get_virga_cached
from prefetch import start_prefetch_thread, get_all_status

app = Flask(__name__)

# Start background pre-fetcher (downloads F01-F12 for all products into cache)
start_prefetch_thread()

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
      <p><a href="/map/froude">/map/froude</a> (HRRR Colorado Froude Number)</p>
      <p><a href="/map/virga">/map/virga</a> (HRRR Colorado Virga Potential)</p>
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


FROUDE_MAP_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HRRR Froude Number - Colorado</title>
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
      min-height: 56px;
    }
    header h1 { font-size: 1rem; font-weight: 700; color: var(--accent); white-space: nowrap; }
    .subtitle { font-size: 0.8rem; color: var(--muted); margin-left: 0.5rem; }
    #cycle-wrap { display: flex; align-items: center; gap: 0.5rem; }
    #cycle-wrap label { font-size: 0.72rem; color: var(--muted); }
    #cycle-select {
      background: var(--panel); color: var(--text);
      border: 1px solid var(--border); border-radius: 4px;
      padding: 0.25rem 0.5rem; font-size: 0.75rem; cursor: pointer;
    }
    #progress-wrap {
      display: flex; align-items: center; gap: 0.5rem;
      background: rgba(88,166,255,0.06); border: 1px solid var(--border);
      border-radius: 6px; padding: 0.3rem 0.75rem; min-width: 160px;
    }
    #progress-label { font-size: 0.72rem; color: var(--muted); white-space: nowrap; }
    #progress-bar-track { flex: 1; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }
    #progress-bar-fill {
      height: 100%; width: 0%; border-radius: 3px;
      background: linear-gradient(90deg, var(--accent), #2ecc71);
      transition: width 0.4s ease;
    }
    #progress-pct { font-size: 0.8rem; font-weight: 700; color: var(--accent); min-width: 2.5rem; }
    #hours-wrap { display: flex; align-items: center; gap: 0.3rem; flex-wrap: wrap; }
    #hours-wrap label { font-size: 0.72rem; color: var(--muted); white-space: nowrap; margin-right: 0.2rem; }
    .hr-btn {
      font-size: 0.7rem; font-weight: 600; padding: 0.2rem 0.4rem;
      border-radius: 4px; border: 1px solid var(--border);
      background: var(--panel); color: var(--muted);
      cursor: not-allowed; opacity: 0.4; transition: all 0.15s; min-width: 2rem; text-align: center;
    }
    .hr-btn.avail { color: var(--text); opacity: 1; cursor: pointer; border-color: #444; }
    .hr-btn.avail:hover { background: #2a3a4a; border-color: var(--accent); }
    .hr-btn.active { background: var(--accent); color: #0d1117; border-color: var(--accent); cursor: default; }
    #meta-strip { font-size: 0.72rem; color: var(--muted); display: flex; gap: 1.2rem; flex-wrap: wrap; }
    #meta-strip b { color: var(--text); }
    .back-link {
      font-size: 0.8rem; color: var(--muted); text-decoration: none;
      border: 1px solid var(--border); border-radius: 4px; padding: 0.3rem 0.65rem; white-space: nowrap;
    }
    #map { width: 100%; height: calc(100vh - 56px); }
    #legend {
      position: absolute; bottom: 2rem; left: 1rem; z-index: 1000;
      background: rgba(13,17,23,0.92); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.75rem 1rem; font-size: 0.75rem; min-width: 210px; color: var(--text);
    }
    .leg-title { font-size: 0.65rem; text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; }
    .leg-row { display: flex; align-items: center; gap: 0.55rem; margin: 0.3rem 0; }
    .leg-swatch { width: 22px; height: 13px; border-radius: 3px; opacity: 0.85; }
    .leg-sub { font-size: 0.65rem; color: var(--muted); margin-left: auto; }
    #loading-overlay {
      position: absolute; inset: 0; z-index: 2000;
      background: rgba(13,17,23,0.88);
      display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1rem;
      transition: opacity 0.3s;
    }
    #loading-overlay.hidden { opacity: 0; pointer-events: none; }
    .spinner {
      width: 42px; height: 42px; border: 3px solid var(--border);
      border-top-color: var(--accent); border-radius: 50%; animation: spin 0.9s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    #load-status { font-size: 0.8rem; color: var(--muted); text-align: center; max-width: 300px; }
    #error-banner {
      display: none; position: absolute; top: 1rem; left: 50%; transform: translateX(-50%); z-index: 3000;
      background: #3d1c1c; border: 1px solid #e74c3c; color: #ffb3b3;
      border-radius: 6px; padding: 0.6rem 1rem; font-size: 0.82rem; max-width: 90%;
    }
  </style>
</head>
<body>
<header>
  <div style="display:flex; align-items:baseline; flex-shrink:0;">
    <h1>FROUDE NUMBER</h1>
    <span class="subtitle">Colorado &middot; 700 mb wind &middot; 850&ndash;500 mb stability</span>
  </div>
  <div id="cycle-wrap">
    <label for="cycle-select">CYCLE</label>
    <select id="cycle-select"><option>Loading...</option></select>
  </div>
  <div id="progress-wrap">
    <span id="progress-label">AVAIL</span>
    <div id="progress-bar-track"><div id="progress-bar-fill"></div></div>
    <span id="progress-pct">--%</span>
  </div>
  <div id="hours-wrap"><label>FCST HOUR</label></div>
  <div id="meta-strip">
    <span>VALID <b id="m-valid">--</b></span>
    <span>PTS <b id="m-pts">--</b></span>
  </div>
  <a class="back-link" href="/">&#8592; Home</a>
</header>

<div id="map"></div>

<div id="legend">
  <div class="leg-title">Froude Number  Fr = U / (N &times; h)</div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#2ecc71"></div>
    Fr &lt; 0.5 &mdash; Flow splitting
    <span class="leg-sub">low</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#f1c40f"></div>
    0.5 &le; Fr &lt; 0.8 &mdash; Transitional
    <span class="leg-sub">mod</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#e74c3c"></div>
    0.8 &le; Fr &le; 1.5 &mdash; Resonant
    <span class="leg-sub">HIGH</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#e67e22"></div>
    Fr &gt; 1.5 &mdash; Flow over
    <span class="leg-sub">mod</span>
  </div>
</div>

<div id="loading-overlay">
  <div class="spinner"></div>
  <div id="load-status">Checking HRRR availability...<br><small>First Froude load downloads two GRIB files (~300 MB) and may take 2&ndash;3 min</small></div>
</div>
<div id="error-banner"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// ── colour scale ──────────────────────────────────────────────────────────────
// cat: 1=splitting(green) 2=transitional(yellow) 3=resonant(red) 4=flow-over(orange)
function froudeColor(cat) {
  if (cat === 3) return '#e74c3c';   // resonant  – HIGH
  if (cat === 2) return '#f1c40f';   // transitional
  if (cat === 4) return '#e67e22';   // flow-over
  return '#2ecc71';                  // splitting – low
}

// ── map ───────────────────────────────────────────────────────────────────────
const map = L.map('map', {
  center: [39.0, -105.5], zoom: 7,
  renderer: L.canvas(), preferCanvas: true,
});
L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: 'Map: OpenTopoMap', maxZoom: 11,
}).addTo(map);

let froudeLayer = null;
let cycleStatus = null;
let activeCycle = null;
let activeFxx   = 1;

// ── status ────────────────────────────────────────────────────────────────────
async function fetchStatus() {
  const r = await fetch('/api/winds/status');
  if (!r.ok) throw new Error('Status ' + r.status);
  return r.json();
}

function applyStatus(status) {
  cycleStatus = status;
  const sel = document.getElementById('cycle-select');
  sel.innerHTML = '';
  status.cycles.forEach(function(c, i) {
    const opt = document.createElement('option');
    opt.value = c.cycle_utc;
    opt.textContent = c.cycle_utc + '  (' + c.pct_complete + '%)';
    if (i === 0) opt.selected = true;
    sel.appendChild(opt);
  });
  updateUI(status.cycles[0]);
  if (!activeCycle) activeCycle = status.cycles[0].cycle_utc;
}

function updateUI(cycleData) {
  document.getElementById('progress-bar-fill').style.width = cycleData.pct_complete + '%';
  document.getElementById('progress-pct').textContent      = cycleData.pct_complete + '%';
  const wrap = document.getElementById('hours-wrap');
  wrap.querySelectorAll('.hr-btn').forEach(function(b) { b.remove(); });
  for (var fxx = 1; fxx <= 12; fxx++) {
    const btn = document.createElement('button');
    btn.className   = 'hr-btn';
    btn.textContent = 'F' + String(fxx).padStart(2,'0');
    btn.dataset.fxx = fxx;
    if (cycleData.available_hours.includes(fxx)) {
      btn.classList.add('avail');
      btn.addEventListener('click', onHourClick);
    }
    if (fxx === activeFxx) btn.classList.add('active');
    wrap.appendChild(btn);
  }
}

function setActiveBtn(fxx) {
  document.querySelectorAll('.hr-btn').forEach(function(b) {
    b.classList.toggle('active', parseInt(b.dataset.fxx) === fxx);
  });
}

function onHourClick(e) {
  const fxx = parseInt(e.target.dataset.fxx);
  if (fxx === activeFxx) return;
  activeFxx = fxx;
  setActiveBtn(fxx);
  loadFroude(activeCycle, fxx);
}

document.getElementById('cycle-select').addEventListener('change', function() {
  activeCycle = this.value;
  const cd = cycleStatus.cycles.find(function(c) { return c.cycle_utc === activeCycle; });
  if (cd) updateUI(cd);
  if (cd && cd.available_hours.length > 0) {
    activeFxx = cd.available_hours[0];
    setActiveBtn(activeFxx);
    loadFroude(activeCycle, activeFxx);
  }
});

// ── data loader ───────────────────────────────────────────────────────────────
async function loadFroude(cycle_utc, fxx) {
  const overlay  = document.getElementById('loading-overlay');
  const statusEl = document.getElementById('load-status');
  const errorEl  = document.getElementById('error-banner');

  overlay.classList.remove('hidden');
  statusEl.innerHTML = 'Computing Froude number &mdash; ' + cycle_utc +
    ' F' + String(fxx).padStart(2,'0') +
    '<br><small>Downloads prs + sfc GRIB files; first load ~2 min</small>';
  errorEl.style.display = 'none';

  try {
    const url  = '/api/froude/colorado?fxx=' + fxx + '&cycle_utc=' + encodeURIComponent(cycle_utc);
    const resp = await fetch(url);

    if (!resp.ok) {
      const body = await resp.json().catch(function() { return null; });
      if (resp.status === 404 && body && body.error === 'not_available') {
        overlay.classList.add('hidden');
        errorEl.style.display = 'block';
        errorEl.textContent = '\u26a0\ufe0f F' + String(fxx).padStart(2,'0') +
          ' not yet on AWS \u2014 try a lower hour.';
        return;
      }
      throw new Error('Server ' + resp.status);
    }

    const data = await resp.json();
    document.getElementById('m-valid').textContent = data.valid_utc;
    document.getElementById('m-pts').textContent   = data.point_count.toLocaleString();
    statusEl.textContent = 'Rendering ' + data.point_count.toLocaleString() + ' cells...';

    if (froudeLayer) { map.removeLayer(froudeLayer); froudeLayer = null; }

    const half     = data.cell_size_deg / 2;
    const halfLon  = data.cell_size_deg * 1.25;
    const renderer = L.canvas();
    const rects    = [];

    data.points.forEach(function(p) {
      const color = froudeColor(p.cat);
      const rect  = L.rectangle(
        [[p.lat - half, p.lon - halfLon], [p.lat + half, p.lon + halfLon]],
        { renderer: renderer, color: color, fillColor: color, fillOpacity: 0.65, weight: 0 }
      );
      rect.bindPopup(
        '<b>Fr = ' + p.fr.toFixed(2) + '</b><br>' +
        'Wind 700 mb: ' + p.wind_kt.toFixed(0) + ' kt<br>' +
        'N: ' + (p.N * 1000).toFixed(2) + ' &times; 10&#8315;&#179; s&#8315;&#185;<br>' +
        'Terrain h: ' + p.h_m.toFixed(0) + ' m<br>' +
        'Orog: ' + p.orog_m.toFixed(0) + ' m MSL<br>' +
        p.lat.toFixed(3) + '\u00b0N, ' + Math.abs(p.lon).toFixed(3) + '\u00b0W',
        { maxWidth: 180 }
      );
      rects.push(rect);
    });

    froudeLayer = L.layerGroup(rects).addTo(map);
    overlay.classList.add('hidden');

  } catch (err) {
    overlay.classList.add('hidden');
    errorEl.style.display = 'block';
    errorEl.textContent   = 'Error: ' + err.message;
    console.error(err);
  }
}

// ── init ──────────────────────────────────────────────────────────────────────
async function init() {
  try {
    const status = await fetchStatus();
    applyStatus(status);
    const latest = status.cycles[0];
    activeCycle  = latest.cycle_utc;
    if (latest.available_hours.length > 0) {
      activeFxx = latest.available_hours[0];
      setActiveBtn(activeFxx);
      loadFroude(activeCycle, activeFxx);
    }
  } catch (err) {
    document.getElementById('load-status').textContent = 'Status check failed: ' + err.message;
  }
}

setInterval(async function() {
  try { applyStatus(await fetchStatus()); } catch(e) {}
}, 5 * 60 * 1000);

init();
</script>
</body>
</html>
"""


@app.get("/map/froude")
def map_froude():
    return render_template_string(FROUDE_MAP_TEMPLATE)


@app.get("/api/froude/colorado")
def api_froude_colorado():
    fxx       = int(request.args.get("fxx", 1))
    cycle_utc = request.args.get("cycle_utc")
    ttl       = int(request.args.get("ttl", os.environ.get("FROUDE_TTL", "600")))

    if not cycle_utc:
        status    = get_cycle_status_cached(ttl_seconds=300)
        cycle_utc = status["cycles"][0]["cycle_utc"]

    try:
        data = get_froude_cached(cycle_utc=cycle_utc, fxx=fxx, ttl_seconds=ttl)
        return jsonify(data)
    except Exception as e:
        msg = str(e)
        not_ready = any(k in msg.lower() for k in [
            "did not find", "not found", "no such file", "404", "unavailable"
        ])
        if not_ready:
            return jsonify({
                "error": "not_available",
                "message": f"F{fxx:02d} for cycle {cycle_utc} is not yet on AWS.",
                "fxx": fxx, "cycle_utc": cycle_utc,
            }), 404
        raise


VIRGA_MAP_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HRRR Virga Potential - Colorado</title>
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
      min-height: 56px;
    }
    header h1 { font-size: 1rem; font-weight: 700; color: var(--accent); white-space: nowrap; }
    .subtitle { font-size: 0.8rem; color: var(--muted); margin-left: 0.5rem; }
    #cycle-wrap { display: flex; align-items: center; gap: 0.5rem; }
    #cycle-wrap label { font-size: 0.72rem; color: var(--muted); }
    #cycle-select {
      background: var(--panel); color: var(--text);
      border: 1px solid var(--border); border-radius: 4px;
      padding: 0.25rem 0.5rem; font-size: 0.75rem; cursor: pointer;
    }
    #progress-wrap {
      display: flex; align-items: center; gap: 0.5rem;
      background: rgba(88,166,255,0.06); border: 1px solid var(--border);
      border-radius: 6px; padding: 0.3rem 0.75rem; min-width: 160px;
    }
    #progress-label { font-size: 0.72rem; color: var(--muted); white-space: nowrap; }
    #progress-bar-track { flex: 1; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }
    #progress-bar-fill {
      height: 100%; width: 0%; border-radius: 3px;
      background: linear-gradient(90deg, var(--accent), #2ecc71);
      transition: width 0.4s ease;
    }
    #progress-pct { font-size: 0.8rem; font-weight: 700; color: var(--accent); min-width: 2.5rem; }
    #hours-wrap { display: flex; align-items: center; gap: 0.3rem; flex-wrap: wrap; }
    #hours-wrap label { font-size: 0.72rem; color: var(--muted); white-space: nowrap; margin-right: 0.2rem; }
    .hr-btn {
      font-size: 0.7rem; font-weight: 600; padding: 0.2rem 0.4rem;
      border-radius: 4px; border: 1px solid var(--border);
      background: var(--panel); color: var(--muted);
      cursor: not-allowed; opacity: 0.4; transition: all 0.15s; min-width: 2rem; text-align: center;
      position: relative;
    }
    .hr-btn.avail { color: var(--text); opacity: 1; cursor: pointer; border-color: #444; }
    .hr-btn.avail:hover { background: #2a3a4a; border-color: var(--accent); }
    .hr-btn.active { background: var(--accent); color: #0d1117; border-color: var(--accent); cursor: default; }
    /* Cache-ready dot indicator */
    .hr-btn.cache-ready::after {
      content: ''; position: absolute; top: -3px; right: -3px;
      width: 6px; height: 6px; border-radius: 50%; background: #2ecc71;
    }
    .hr-btn.cache-loading::after {
      content: ''; position: absolute; top: -3px; right: -3px;
      width: 6px; height: 6px; border-radius: 50%; background: #f1c40f;
    }
    #meta-strip { font-size: 0.72rem; color: var(--muted); display: flex; gap: 1.2rem; flex-wrap: wrap; }
    #meta-strip b { color: var(--text); }
    .back-link {
      font-size: 0.8rem; color: var(--muted); text-decoration: none;
      border: 1px solid var(--border); border-radius: 4px; padding: 0.3rem 0.65rem; white-space: nowrap;
    }
    #map { width: 100%; height: calc(100vh - 56px); }
    #legend {
      position: absolute; bottom: 2rem; left: 1rem; z-index: 1000;
      background: rgba(13,17,23,0.92); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.75rem 1rem; font-size: 0.75rem; min-width: 220px;
    }
    .leg-title { font-size: 0.65rem; text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; }
    .leg-row { display: flex; align-items: center; gap: 0.55rem; margin: 0.3rem 0; }
    .leg-swatch { width: 22px; height: 13px; border-radius: 3px; opacity: 0.85; }
    .leg-sub { font-size: 0.65rem; color: var(--muted); margin-left: auto; }
    #cache-strip {
      font-size: 0.68rem; color: var(--muted); padding: 0 0.75rem;
      display: flex; align-items: center; gap: 0.5rem;
    }
    .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
    .dot-green  { background: #2ecc71; }
    .dot-yellow { background: #f1c40f; }
    .dot-grey   { background: #555; }
    #loading-overlay {
      position: absolute; inset: 0; z-index: 2000; background: rgba(13,17,23,0.88);
      display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1rem;
      transition: opacity 0.3s;
    }
    #loading-overlay.hidden { opacity: 0; pointer-events: none; }
    .spinner {
      width: 42px; height: 42px; border: 3px solid var(--border);
      border-top-color: var(--accent); border-radius: 50%; animation: spin 0.9s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    #load-status { font-size: 0.8rem; color: var(--muted); text-align: center; max-width: 320px; }
    #error-banner {
      display: none; position: absolute; top: 1rem; left: 50%; transform: translateX(-50%); z-index: 3000;
      background: #3d1c1c; border: 1px solid #e74c3c; color: #ffb3b3;
      border-radius: 6px; padding: 0.6rem 1rem; font-size: 0.82rem; max-width: 90%;
    }
  </style>
</head>
<body>
<header>
  <div style="display:flex; align-items:baseline; flex-shrink:0;">
    <h1>VIRGA POTENTIAL</h1>
    <span class="subtitle">Colorado &middot; 700&ndash;500 mb sat layer &middot; 100 mb RH decrease</span>
  </div>
  <div id="cycle-wrap">
    <label for="cycle-select">CYCLE</label>
    <select id="cycle-select"><option>Loading...</option></select>
  </div>
  <div id="progress-wrap">
    <span id="progress-label">AVAIL</span>
    <div id="progress-bar-track"><div id="progress-bar-fill"></div></div>
    <span id="progress-pct">--%</span>
  </div>
  <div id="hours-wrap"><label>FCST HOUR</label></div>
  <div id="cache-strip">
    <span class="dot dot-green"></span>ready&nbsp;
    <span class="dot dot-yellow"></span>loading&nbsp;
    <span class="dot dot-grey"></span>pending
  </div>
  <div id="meta-strip">
    <span>VALID <b id="m-valid">--</b></span>
    <span>PTS <b id="m-pts">--</b></span>
  </div>
  <a class="back-link" href="/">&#8592; Home</a>
</header>

<div id="map"></div>

<div id="legend">
  <div class="leg-title">Virga Potential (100 mb RH decrease where upper cloud present)</div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#f1c40f"></div>
    20&ndash;40% &mdash; Low
    <span class="leg-sub">light evap</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#e67e22"></div>
    40&ndash;60% &mdash; Moderate
    <span class="leg-sub"></span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#e74c3c"></div>
    60&ndash;80% &mdash; High
    <span class="leg-sub"></span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#8e44ad"></div>
    &ge;80% &mdash; Extreme
    <span class="leg-sub">full evap likely</span>
  </div>
  <div style="margin-top:0.6rem; font-size:0.65rem; color:var(--muted);">
    Contour lines = cloud base wind (kt)
  </div>
</div>

<div id="loading-overlay">
  <div class="spinner"></div>
  <div id="load-status">Checking HRRR availability...<br>
    <small>Green dot on hour button = pre-cached, loads instantly</small>
  </div>
</div>
<div id="error-banner"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
function virgaColor(cat) {
  if (cat === 4) return '#8e44ad';   // extreme - purple
  if (cat === 3) return '#e74c3c';   // high - red
  if (cat === 2) return '#e67e22';   // moderate - orange
  return '#f1c40f';                  // low - yellow
}

const map = L.map('map', {
  center: [39.0, -105.5], zoom: 7,
  renderer: L.canvas(), preferCanvas: true,
});
L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: 'Map: OpenTopoMap', maxZoom: 11,
}).addTo(map);

let virgaLayer  = null;
let cycleStatus = null;
let cacheStatus = null;
let activeCycle = null;
let activeFxx   = 1;

// ── Status fetchers ───────────────────────────────────────────────────────────
async function fetchStatus() {
  const r = await fetch('/api/winds/status');
  if (!r.ok) throw new Error('Status ' + r.status);
  return r.json();
}

async function fetchCacheStatus() {
  const r = await fetch('/api/cache/status');
  if (!r.ok) return null;
  return r.json();
}

function applyStatus(status) {
  cycleStatus = status;
  const sel = document.getElementById('cycle-select');
  sel.innerHTML = '';
  status.cycles.forEach(function(c, i) {
    const opt = document.createElement('option');
    opt.value = c.cycle_utc;
    opt.textContent = c.cycle_utc + '  (' + c.pct_complete + '%)';
    if (i === 0) opt.selected = true;
    sel.appendChild(opt);
  });
  updateUI(status.cycles[0]);
  if (!activeCycle) activeCycle = status.cycles[0].cycle_utc;
}

function updateUI(cycleData) {
  document.getElementById('progress-bar-fill').style.width = cycleData.pct_complete + '%';
  document.getElementById('progress-pct').textContent      = cycleData.pct_complete + '%';
  const wrap = document.getElementById('hours-wrap');
  wrap.querySelectorAll('.hr-btn').forEach(function(b) { b.remove(); });

  const virga_cache = cacheStatus && cacheStatus.products
    ? cacheStatus.products.virga || {}
    : {};

  for (var fxx = 1; fxx <= 12; fxx++) {
    const btn = document.createElement('button');
    btn.className   = 'hr-btn';
    btn.textContent = 'F' + String(fxx).padStart(2,'0');
    btn.dataset.fxx = fxx;

    if (cycleData.available_hours.includes(fxx)) {
      btn.classList.add('avail');
      btn.addEventListener('click', onHourClick);
    }

    // Cache dot indicator
    const cs = virga_cache[fxx];
    if (cs === 'ready')   btn.classList.add('cache-ready');
    if (cs === 'loading') btn.classList.add('cache-loading');

    if (fxx === activeFxx) btn.classList.add('active');
    wrap.appendChild(btn);
  }
}

function setActiveBtn(fxx) {
  document.querySelectorAll('.hr-btn').forEach(function(b) {
    b.classList.toggle('active', parseInt(b.dataset.fxx) === fxx);
  });
}

function onHourClick(e) {
  const fxx = parseInt(e.target.dataset.fxx);
  if (fxx === activeFxx) return;
  activeFxx = fxx;
  setActiveBtn(fxx);
  loadVirga(activeCycle, fxx);
}

document.getElementById('cycle-select').addEventListener('change', function() {
  activeCycle = this.value;
  const cd = cycleStatus.cycles.find(function(c) { return c.cycle_utc === activeCycle; });
  if (cd) updateUI(cd);
  if (cd && cd.available_hours.length > 0) {
    activeFxx = cd.available_hours[0];
    setActiveBtn(activeFxx);
    loadVirga(activeCycle, activeFxx);
  }
});

// ── Data loader ───────────────────────────────────────────────────────────────
async function loadVirga(cycle_utc, fxx) {
  const overlay  = document.getElementById('loading-overlay');
  const statusEl = document.getElementById('load-status');
  const errorEl  = document.getElementById('error-banner');

  // Check if this hour is pre-cached (instant) or needs downloading
  const cs = cacheStatus && cacheStatus.products && cacheStatus.products.virga
    ? cacheStatus.products.virga[fxx] : null;
  const isReady = (cs === 'ready');

  overlay.classList.remove('hidden');
  statusEl.innerHTML = isReady
    ? 'Loading from cache &mdash; F' + String(fxx).padStart(2,'0') + ' (instant)'
    : 'Downloading &amp; computing virga &mdash; F' + String(fxx).padStart(2,'0') +
      '<br><small>First load downloads ~200 MB prs file, ~90 s</small>';
  errorEl.style.display = 'none';

  try {
    const url  = '/api/virga/colorado?fxx=' + fxx + '&cycle_utc=' + encodeURIComponent(cycle_utc);
    const resp = await fetch(url);

    if (!resp.ok) {
      const body = await resp.json().catch(function() { return null; });
      if (resp.status === 404 && body && body.error === 'not_available') {
        overlay.classList.add('hidden');
        errorEl.style.display = 'block';
        errorEl.textContent = '\u26a0\ufe0f F' + String(fxx).padStart(2,'0') +
          ' not yet on AWS \u2014 try a lower hour.';
        return;
      }
      throw new Error('Server ' + resp.status);
    }

    const data = await resp.json();
    document.getElementById('m-valid').textContent = data.valid_utc;
    document.getElementById('m-pts').textContent   = data.point_count.toLocaleString();

    if (virgaLayer) { map.removeLayer(virgaLayer); virgaLayer = null; }

    if (data.point_count === 0) {
      overlay.classList.add('hidden');
      errorEl.style.display = 'block';
      errorEl.textContent = 'No virga potential areas this hour (all RH decrease < 20%).';
      return;
    }

    const half     = data.cell_size_deg / 2;
    const halfLon  = data.cell_size_deg * 1.25;
    const renderer = L.canvas();
    const rects    = [];

    data.points.forEach(function(p) {
      const color = virgaColor(p.cat);
      const rect  = L.rectangle(
        [[p.lat - half, p.lon - halfLon], [p.lat + half, p.lon + halfLon]],
        { renderer: renderer, color: color, fillColor: color, fillOpacity: 0.65, weight: 0 }
      );
      rect.bindPopup(
        '<b>Virga: ' + p.virga_pct.toFixed(0) + '%</b><br>' +
        'CB Wind: ' + p.cb_wind_kt.toFixed(0) + ' kt<br>' +
        'Upper RH: ' + p.upper_rh.toFixed(0) + '%<br>' +
        p.lat.toFixed(3) + '\u00b0N, ' + Math.abs(p.lon).toFixed(3) + '\u00b0W',
        { maxWidth: 170 }
      );
      rects.push(rect);
    });

    virgaLayer = L.layerGroup(rects).addTo(map);
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
    // Fetch both status calls in parallel
    const [status, cache] = await Promise.all([fetchStatus(), fetchCacheStatus()]);
    cacheStatus = cache;
    applyStatus(status);

    const latest = status.cycles[0];
    activeCycle  = latest.cycle_utc;
    if (latest.available_hours.length > 0) {
      activeFxx = latest.available_hours[0];
      setActiveBtn(activeFxx);
      loadVirga(activeCycle, activeFxx);
    }
  } catch (err) {
    document.getElementById('load-status').textContent = 'Init failed: ' + err.message;
  }
}

// Refresh availability every 5 min; refresh cache status every 30 s
setInterval(async function() {
  try { applyStatus(await fetchStatus()); } catch(e) {}
}, 5 * 60 * 1000);

setInterval(async function() {
  try {
    cacheStatus = await fetchCacheStatus();
    // Re-draw buttons for active cycle to update cache dots
    if (cycleStatus) {
      const cd = cycleStatus.cycles.find(function(c) { return c.cycle_utc === activeCycle; });
      if (cd) updateUI(cd);
    }
  } catch(e) {}
}, 30 * 1000);

init();
</script>
</body>
</html>
"""


@app.get("/map/virga")
def map_virga():
    return render_template_string(VIRGA_MAP_TEMPLATE)


@app.get("/api/virga/colorado")
def api_virga_colorado():
    fxx       = int(request.args.get("fxx", 1))
    cycle_utc = request.args.get("cycle_utc")
    ttl       = int(request.args.get("ttl", os.environ.get("VIRGA_TTL", "600")))

    if not cycle_utc:
        status    = get_cycle_status_cached(ttl_seconds=300)
        cycle_utc = status["cycles"][0]["cycle_utc"]

    try:
        data = get_virga_cached(cycle_utc=cycle_utc, fxx=fxx, ttl_seconds=ttl)
        return jsonify(data)
    except Exception as e:
        msg = str(e)
        not_ready = any(k in msg.lower() for k in [
            "did not find", "not found", "no such file", "404", "unavailable"
        ])
        if not_ready:
            return jsonify({
                "error": "not_available",
                "message": f"F{fxx:02d} for cycle {cycle_utc} is not yet on AWS.",
                "fxx": fxx, "cycle_utc": cycle_utc,
            }), 404
        raise


@app.get("/api/cache/status")
def api_cache_status():
    """Return pre-fetch cache status for all products and forecast hours."""
    return jsonify(get_all_status())



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


@app.get("/debug/prs_fields")
def debug_prs_fields():
    """
    Dump ALL field names from the HRRR pressure-level (prs) product.
    Used to confirm which fields are available for Froude number calculation:
      - U/V wind components at pressure levels (for wind perpendicular to terrain)
      - Temperature at multiple levels (for Brunt-Vaisala frequency / stability)
      - Geopotential height (to convert pressure levels to meters)
    """
    import pygrib
    from winds import _find_latest_hrrr_cycle, HERBIE_DIR
    from herbie import Herbie
    from pathlib import Path

    cycle     = _find_latest_hrrr_cycle()
    H         = Herbie(cycle, model="hrrr", product="prs", fxx=1,
                       save_dir=str(HERBIE_DIR), overwrite=False)
    grib_path = Path(H.download())

    grbs      = pygrib.open(str(grib_path))
    all_fields = []
    for grb in grbs:
        all_fields.append({
            "name":        grb.name,
            "shortName":   grb.shortName,
            "typeOfLevel": grb.typeOfLevel,
            "level":       grb.level,
        })
    grbs.close()

    # Filter to just the fields relevant to Froude number
    froude_keywords = ["wind", "temperature", "geopotential", "height",
                       "u-component", "v-component", "u component", "v component"]
    froude_fields = [
        f for f in all_fields
        if any(kw in f["name"].lower() for kw in froude_keywords)
        and f["typeOfLevel"] == "isobaricInhPa"
    ]

    # Get unique pressure levels available
    levels = sorted(set(f["level"] for f in froude_fields))

    return jsonify({
        "cycle":          cycle.isoformat(),
        "grib_file":      grib_path.name,
        "total_fields":   len(all_fields),
        "pressure_levels_mb": levels,
        "froude_relevant_fields": froude_fields,
    })


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
