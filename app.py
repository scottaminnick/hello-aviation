import os
import traceback
from flask import Flask, jsonify, render_template_string, Response, request
from guidance import get_guidance_cached
from metar import get_metars_cached, summarize_metars
from rap_point import get_rap_point_guidance_cached
from winds import get_hrrr_gusts_cached, get_cycle_status_cached
from froude import get_froude_cached
from icing         import get_icing_cached
from winds_surface import get_surface_wind_cached
from virga import get_virga_cached
from prefetch import start_prefetch_thread, get_all_status
from llti import get_llti_cached, get_llti_points_cached

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
      <p><a href="/map/hrrr">/map/winds</a> (HRRR Colorado Wind Gusts)</p>
      <p><a href="/map/froude">/map/froude</a> (HRRR Colorado Froude Number)</p>
      <p><a href="/map/virga">/map/virga</a> (HRRR Colorado Virga Potential)</p>
      <p><a href="/map/llti">/map/llti</a> (HRRR Colorado LLTI)</p>
      <p><a href="/debug/routes">/debug/routes</a> (registered routes)</p>
    </div>
  </body>
</html>
"""

HRRR_MAP_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>HRRR Colorado Guidance</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  :root {
    --bg:     #0d1117;
    --panel:  #161b22;
    --border: #30363d;
    --text:   #e6edf3;
    --muted:  #8b949e;
    --ac:     #58a6ff;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text);
         font-family: system-ui, sans-serif; height: 100dvh;
         display: flex; flex-direction: column; overflow: hidden; }

  /* ── header ── */
  #header {
    background: var(--panel); border-bottom: 1px solid var(--border);
    padding: 0.45rem 0.75rem; display: flex; align-items: center;
    gap: 0.75rem; flex-wrap: wrap; flex-shrink: 0;
  }
  #header .title { font-weight: 700; font-size: 0.95rem; white-space: nowrap; }

  select, input[type=range] { background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 5px; font-size: 0.78rem; }
  select { padding: 0.28rem 0.5rem; cursor: pointer; }

  .ctrl-group { display: flex; align-items: center; gap: 0.4rem; }
  .ctrl-label { font-size: 0.68rem; color: var(--muted); white-space: nowrap; }

  /* product dropdown highlight */
  #product-sel { font-weight: 600; color: var(--ac);
                 border-color: var(--ac); padding: 0.3rem 0.6rem; }

  /* ── hour buttons ── */
  #hour-bar {
    display: flex; align-items: center; gap: 0.3rem;
    padding: 0.3rem 0.75rem; background: var(--panel);
    border-bottom: 1px solid var(--border); flex-shrink: 0;
    flex-wrap: wrap;
  }
  .hbtn {
    font-size: 0.72rem; font-weight: 600; padding: 0.22rem 0.5rem;
    border-radius: 4px; border: 1px solid var(--border);
    background: var(--bg); color: var(--muted); cursor: pointer;
    transition: background 0.15s, color 0.15s; position: relative;
  }
  .hbtn.available { color: var(--text); border-color: #444; }
  .hbtn.active    { background: var(--ac); color: #000; border-color: var(--ac); }
  .hbtn.unavail   { opacity: 0.35; cursor: not-allowed; }
  .dot-badge {
    position: absolute; top: -3px; right: -3px;
    width: 6px; height: 6px; border-radius: 50%;
  }
  .dot-green  { background: #2ecc71; }
  .dot-yellow { background: #f1c40f; }
  .dot-grey   { background: #555; }

  #progress-bar {
    height: 3px; background: var(--border); flex: 1; border-radius: 2px;
    min-width: 60px;
  }
  #progress-fill { height: 100%; background: var(--ac); border-radius: 2px;
                   transition: width 0.4s; width: 0%; }

  /* ── main area ── */
  #main { flex: 1; display: flex; min-height: 0; }
  #map  { flex: 1; }

  /* ── sidebar ── */
  #sidebar {
    width: 210px; background: var(--panel); border-left: 1px solid var(--border);
    display: flex; flex-direction: column; flex-shrink: 0; overflow-y: auto;
  }

  #legend { padding: 0.75rem; }
  .leg-title { font-size: 0.72rem; font-weight: 700; color: var(--muted);
               margin-bottom: 0.5rem; }
  .leg-row { display: flex; align-items: center; gap: 0.55rem; margin: 0.3rem 0; }
  .leg-swatch { width: 22px; height: 13px; border-radius: 3px; opacity: 0.85;
                flex-shrink: 0; }
  .leg-sub { font-size: 0.65rem; color: var(--muted); margin-left: auto; }

  /* opacity slider */
  #opacity-wrap {
    padding: 0.6rem 0.75rem; border-top: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 0.3rem;
  }

  /* meta strip */
  #meta {
    padding: 0.5rem 0.75rem; font-size: 0.68rem; color: var(--muted);
    border-top: 1px solid var(--border);
  }
  #meta b { color: var(--text); }

  /* loading overlay */
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
    border-top-color: var(--ac); border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  #load-msg { font-size: 0.8rem; color: var(--muted); text-align: center;
              max-width: 240px; }

    .apt-label {
      background: none !important; border: none !important; box-shadow: none !important;
      font-size: 0.65rem; font-weight: 700;
      color: #58a6ff; text-shadow: 0 0 3px #0d1117, 0 0 3px #0d1117;
      padding: 0 !important;
    }
    .city-label {
      background: none !important; border: none !important; box-shadow: none !important;
      font-size: 0.62rem; color: #8b949e;
      text-shadow: 0 0 3px #0d1117, 0 0 3px #0d1117;
      padding: 0 !important;
    }
    .city-label-major {
      background: none !important; border: none !important; box-shadow: none !important;
      font-size: 0.72rem; font-weight: 600; color: #e6edf3;
      text-shadow: 0 0 4px #0d1117, 0 0 4px #0d1117;
      padding: 0 !important;
    }
    .leaflet-control-layers {
      background: var(--panel) !important;
      border: 1px solid var(--border) !important;
      color: var(--text) !important;
      font-size: 0.78rem;
    }
    .leaflet-control-layers label { color: var(--text) !important; }
    .leaflet-control-layers-overlays { padding: 0.2rem 0.4rem; }

  #error-bar {
    display: none; background: #5a1a1a; color: #f9a8a8;
    padding: 0.4rem 0.75rem; font-size: 0.78rem;
    border-bottom: 1px solid #8b2020;
  }
</style>
</head>
<body>

<div id="header">
  <span class="title">🏔 HRRR Colorado</span>

  <div class="ctrl-group">
    <span class="ctrl-label">PRODUCT</span>
    <select id="product-sel" onchange="onProductChange()">
      <option value="winds">Wind Gusts</option>
      <option value="froude">Froude Number</option>
      <option value="virga">Virga Potential</option>
      <option value="icing">Icing Threat</option>
      <option value="surface_wind">Surface Flow</option>
      <option value="llti">LLTI</option>
    </select>
  </div>

  <div class="ctrl-group">
    <span class="ctrl-label">CYCLE</span>
    <select id="cycle-sel" onchange="onCycleChange()">
      <option value="">—</option>
    </select>
  </div>

  <div class="ctrl-group" style="margin-left:auto;">
    <span class="ctrl-label">OPACITY</span>
    <input type="range" id="opacity-slider" min="10" max="100" step="5" value="65"
      style="width:80px;" oninput="updateOpacity(this.value)"/>
    <span id="opacity-val" style="font-size:0.72rem;color:var(--muted);width:28px;">65%</span>
  </div>

  <a href="/" style="font-size:0.75rem;color:var(--muted);text-decoration:none;
     padding:0.25rem 0.5rem;border:1px solid var(--border);border-radius:4px;">
    ← Home
  </a>
</div>

<div id="error-bar"></div>

<div id="hour-bar">
  <span class="ctrl-label">HOUR →</span>
  <!-- buttons injected by JS -->
  <div id="progress-bar"><div id="progress-fill"></div></div>
  <span id="cycle-pct" style="font-size:0.68rem;color:var(--muted);white-space:nowrap;"></span>
</div>

<div id="main">
  <div id="map" style="position:relative;">
    <div id="loading-overlay">
      <div class="spinner"></div>
      <div id="load-msg">Loading…</div>
    </div>
  </div>

  <div id="sidebar">
    <div id="legend"><!-- swapped by JS --></div>

    <div id="opacity-wrap" style="display:none;"><!-- hidden duplicate, sidebar space --></div>

    <div id="meta">
      <div>Valid: <b id="meta-valid">—</b></div>
      <div>Points: <b id="meta-pts">—</b></div>
      <div style="margin-top:0.4rem;font-size:0.63rem;">
        Click any grid cell for details
      </div>
    </div>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// ── product config ────────────────────────────────────────────────────────────
const PRODUCTS = {
  winds: {
    label:    'Wind Gusts',
    endpoint: '/api/winds/colorado',
    loadMsg:  'Fetching HRRR sfc…<br><small style="color:var(--muted)">~15 s first load</small>',
    color:    function(p) {
      var kt = p.gust_kt;
      if (kt >= 50) return '#e74c3c';
      if (kt >= 35) return '#e67e22';
      if (kt >= 20) return '#f1c40f';
      return '#2ecc71';
    },
    popup: function(p) {
      return '<b>' + p.gust_kt.toFixed(0) + ' kt gust</b><br>' +
             p.lat.toFixed(3) + '\u00b0N, ' + Math.abs(p.lon).toFixed(3) + '\u00b0W';
    },
    legend: `<div class="leg-title">Wind Gust (kt)</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#2ecc71"></div>&lt; 20 kt</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#f1c40f"></div>20 – 35 kt</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#e67e22"></div>35 – 50 kt</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#e74c3c"></div>&ge; 50 kt</div>`
  },

  froude: {
    label:    'Froude Number',
    endpoint: '/api/froude/colorado',
    loadMsg:  'Fetching HRRR prs…<br><small style="color:var(--muted)">~60 s first load</small>',
    color:    function(p) {
      var cat = p.cat;
      if (cat === 3) return '#e91e8c';
      if (cat === 2) return '#00bcd4';
      if (cat === 4) return '#7b1fa2';
      return '#2ecc71';
    },
    popup: function(p) {
      return '<b>Fr = ' + p.fr.toFixed(2) + '</b><br>' +
             'Wind 700 mb: ' + p.wind_kt.toFixed(0) + ' kt<br>' +
             'N: ' + (p.N * 1000).toFixed(2) + ' &times; 10&#8315;&#179; s&#8315;&#185;<br>' +
             'Terrain h: ' + p.h_m.toFixed(0) + ' m<br>' +
             'Orog: ' + p.orog_m.toFixed(0) + ' m MSL<br>' +
             p.lat.toFixed(3) + '\u00b0N, ' + Math.abs(p.lon).toFixed(3) + '\u00b0W';
    },
    legend: `<div class="leg-title">Froude Number  Fr = U / (N &times; h)</div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#2ecc71"></div>
    Fr &lt; 0.5 &mdash; Splitting <span class="leg-sub">low</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#00bcd4"></div>
    0.5 &le; Fr &lt; 0.8 &mdash; Transitional <span class="leg-sub">mod</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#e91e8c"></div>
    0.8 &le; Fr &le; 1.5 &mdash; Resonant <span class="leg-sub">HIGH</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#7b1fa2"></div>
    Fr &gt; 1.5 &mdash; Flow over <span class="leg-sub">mod</span>
  </div>`
  },

  virga: {
    label:    'Virga Potential',
    endpoint: '/api/virga/colorado',
    loadMsg:  'Fetching HRRR prs…<br><small style="color:var(--muted)">~90 s first load</small>',
    color:    function(p) {
      var cat = p.cat;
      if (cat >= 4) return '#8e44ad';
      if (cat >= 3) return '#e74c3c';
      if (cat >= 2) return '#e67e22';
      if (cat >= 1) return '#f1c40f';
      return '#2c3e50';
    },
    popup: function(p) {
      return '<b>Virga: ' + p.virga_pct.toFixed(0) + '%</b><br>' +
             'CB Wind: ' + p.cb_wind_kt.toFixed(0) + ' kt<br>' +
             'Upper RH: ' + p.upper_rh.toFixed(0) + '%<br>' +
             p.lat.toFixed(3) + '\u00b0N, ' + Math.abs(p.lon).toFixed(3) + '\u00b0W';
    },
    legend: `<div class="leg-title">Virga Potential (100 mb RH decrease)</div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#2c3e50"></div>
    Negligible (&lt;20%) <span class="leg-sub"></span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#f1c40f"></div>
    20–40% &mdash; Low <span class="leg-sub">light evap</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#e67e22"></div>
    40–60% &mdash; Moderate <span class="leg-sub"></span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#e74c3c"></div>
    60–80% &mdash; High <span class="leg-sub"></span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#8e44ad"></div>
    &ge;80% &mdash; Extreme <span class="leg-sub">full evap likely</span>
  </div>`
  }
,
  icing: {
    label:    'Icing Threat',
    endpoint: '/api/icing/colorado',
    loadMsg:  'Fetching HRRR prs…<br><small style="color:var(--muted)">RH + omega + convergence</small>',
    color:    function(p) {
      if (p.cat >= 3) return '#e74c3c';   // red    – high
      if (p.cat >= 2) return '#e67e22';   // orange – moderate
      if (p.cat >= 1) return '#f1c40f';   // yellow – low
      return '#2c3e50';                   // grey   – negligible
    },
    popup: function(p) {
      var upslope = '';
      if (p.wdir850 >= 45 && p.wdir850 <= 135 && p.spd850 >= 10)
        upslope = '<br><b style="color:#58a6ff">\u25b2 Front Range upslope</b>';
      if (p.wdir850 >= 225 && p.wdir850 <= 315 && p.spd850 >= 10)
        upslope = '<br><b style="color:#58a6ff">\u25b2 West slope upslope</b>';
      return '<b>Icing score: ' + p.score.toFixed(2) + '</b>' +
             ' (cat ' + p.cat + ')<br>' +
             'RH 850/700: ' + p.rh850.toFixed(0) + '% / ' + p.rh700.toFixed(0) + '%<br>' +
             'Sat: '    + p.sat.toFixed(2)    +
             '  Asc: '  + p.ascent.toFixed(2) +
             '  Conv: ' + p.conv.toFixed(2)   + '<br>' +
             '850mb wind: ' + p.spd850.toFixed(0) + ' kt @ ' + p.wdir850.toFixed(0) + '\u00b0' +
             upslope + '<br>' +
             p.lat.toFixed(3) + '\u00b0N, ' + Math.abs(p.lon).toFixed(3) + '\u00b0W';
    },
    legend: `<div class="leg-title">Winter Icing Threat Index</div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#2c3e50"></div>
    Negligible <span class="leg-sub">score &lt;0.35</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#f1c40f"></div>
    Low <span class="leg-sub">0.35–0.55</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#e67e22"></div>
    Moderate <span class="leg-sub">0.55–0.75</span>
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#e74c3c"></div>
    High <span class="leg-sub">&ge;0.75</span>
  </div>
  <div style="margin-top:0.6rem;font-size:0.63rem;color:var(--muted);">
    Sat(0.45) · Ascent(0.35) · Conv(0.20)<br>
    +0.15 Front Range upslope · +0.10 West slope
  </div>`
  }
,

  surface_wind: {
    label:      'Surface Flow',
    endpoint:   '/api/winds/surface',
    loadMsg:    'Fetching HRRR 10m wind…<br><small style="color:var(--muted)">~15 s</small>',
    renderMode: 'streamline',
    color: function(p) {
      if (p.cat >= 4) return '#e74c3c';   // red    ≥40 kt
      if (p.cat >= 3) return '#e67e22';   // orange 25-40 kt
      if (p.cat >= 2) return '#f1c40f';   // yellow 15-25 kt
      if (p.cat >= 1) return '#3d8f6e';   // teal   8-15 kt
      return '#1a3a5c';                   // dark blue <8 kt (nearly transparent feel)
    },
    popup: function(p) {
      return '<b>' + p.spd.toFixed(0) + ' kt</b> from ' + p.wdir.toFixed(0) + '\u00b0<br>' +
             p.lat.toFixed(3) + '\u00b0N, ' + Math.abs(p.lon).toFixed(3) + '\u00b0W';
    },
    legend: `<div class="leg-title">10m Wind Speed</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#1a3a5c"></div>&lt; 8 kt</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#3d8f6e"></div>8–15 kt</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#f1c40f"></div>15–25 kt</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#e67e22"></div>25–40 kt</div>
  <div class="leg-row"><div class="leg-swatch" style="background:#e74c3c"></div>&ge; 40 kt</div>
  <div style="margin-top:0.6rem;font-size:0.63rem;color:var(--muted);">
    White streamlines show flow direction &amp; speed.<br>Click any cell for wind details.
  </div>`

},

  llti: {
    label:    'LLTI',
    endpoint: '/api/llti/colorado',
    loadMsg:  'Fetching HRRR LLTI…<br><small style="color:var(--muted)">~90 s first load</small>',
    color:    function(p) {
      if (p.cat >= 3) return '#e74c3c';   // red    – high  (≥75)
      if (p.cat >= 2) return '#FF8C00';   // orange – moderate (50-75)
      if (p.cat >= 1) return '#FFD700';   // gold   – low (25-50)
      return '#006400';                   // dark green – negligible (<25)
    },
    popup: function(p) {
      return '<b>LLTI: ' + p.llti.toFixed(0) + '</b>' +
             ' (cat ' + p.cat + ')<br>' +
             'Mix Hgt: ' + p.mix_ft.toFixed(0) + ' ft<br>' +
             'Transport Wind: ' + p.trspd_kt.toFixed(1) + ' kt<br>' +
             'Sky: ' + p.sky_pct.toFixed(0) + '%<br>' +
             'Dewpoint Dep: ' + p.dd_f.toFixed(1) + '°F<br>' +
             p.lat.toFixed(3) + '°N, ' + Math.abs(p.lon).toFixed(3) + '°W';
    },
    legend: `<div class="leg-title">Low-Level Turbulence Index</div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#006400"></div>
    &lt; 25 &mdash; Negligible
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#FFD700"></div>
    25–50 &mdash; Low
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#FF8C00"></div>
    50–75 &mdash; Moderate
  </div>
  <div class="leg-row">
    <div class="leg-swatch" style="background:#e74c3c"></div>
    &ge; 75 &mdash; High
  </div>
  <div style="margin-top:0.6rem;font-size:0.63rem;color:var(--muted);">
    MixHgt(0.25) · TransWind(0.45)<br>
    Sky(0.15) · DewDep(0.15)<br>
    Transport wind: HPBL-coupled 10m+950–700mb
  </div>`
  }

};

// ── state ─────────────────────────────────────────────────────────────────────
var currentProduct  = 'winds';
var currentCycle    = null;
var currentFxx      = 1;
var currentOpacity  = 0.65;
var cycleStatus     = {};
var dataLayer       = null;
var statusTimer     = null;

// ── map init ──────────────────────────────────────────────────────────────────
var map = L.map('map', {
  center: [39.0, -105.5], zoom: 7, zoomControl: true
});

// ESRI World Shaded Relief — cool grey, no competing colours
L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}',
  { attribution: 'Tiles &copy; Esri', maxZoom: 13 }
).addTo(map);

// ── Reference layers ─────────────────────────────────────────────────────────

// ESRI Roads/Labels reference overlay (sits on top of shaded relief)
var roadsLayer = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}',
  { attribution: '', maxZoom: 13, opacity: 0.55 }
);

// ── Colorado public-use airports ─────────────────────────────────────────────
// [ICAO, name, lat, lon, type]  type: "com" = commercial/scheduled, "ga" = general aviation
var CO_AIRPORTS = [
  ["KDEN", "Denver Intl",               39.8561, -104.6737, "com"],
  ["KCOS", "Colorado Springs",          38.8059, -104.7008, "com"],
  ["KGJT", "Grand Junction Regional",   39.1224, -108.5268, "com"],
  ["KDRO", "Durango La Plata Co",       37.1515, -107.7538, "com"],
  ["KPUB", "Pueblo Memorial",           38.2891, -104.4966, "com"],
  ["KASE", "Aspen/Pitkin County",       39.2232, -106.8687, "com"],
  ["KEGE", "Eagle County Regional",     39.6426, -106.9177, "com"],
  ["KHDN", "Yampa Valley (Steamboat)",  40.4812, -107.2218, "com"],
  ["KGUC", "Gunnison-Crested Butte",    38.5339, -106.9330, "com"],
  ["KMTJ", "Montrose Regional",         38.5098, -107.8938, "com"],
  ["KALS", "San Luis Valley Regional",  37.4349, -105.8666, "com"],
  ["KTEX", "Telluride Regional",        37.9538, -107.9088, "com"],
  ["KFNL", "Northern CO Regional",      40.4518, -105.0110, "com"],
  ["KAPA", "Centennial",                39.5701, -104.8490, "ga"],
  ["KBJC", "Rocky Mtn Metro",           39.9088, -105.1172, "ga"],
  ["KBDU", "Boulder Municipal",         40.0394, -105.2257, "ga"],
  ["KGXY", "Greeley-Weld County",       40.4375, -104.6336, "ga"],
  ["KLMO", "Vance Brand (Longmont)",    40.1712, -105.1628, "ga"],
  ["KFCS", "Meadow Lake (Fountain)",    38.6784, -104.5698, "ga"],
  ["KAFF", "USAF Academy",              38.9697, -104.8130, "ga"],
  ["KBKF", "Buckley SFB (Aurora)",      39.7017, -104.7517, "ga"],
  ["KANK", "Harriet Alexander (Salida)",38.5398, -105.9952, "ga"],
  ["KAEJ", "Central CO Regional",       38.8440, -106.1188, "ga"],
  ["KLXV", "Lake County (Leadville)",   39.2238, -106.3177, "ga"],
  ["KRIL", "Garfield Co (Rifle)",       39.5263, -107.7266, "ga"],
  ["KCAG", "Craig-Moffat County",       40.4952, -107.5225, "ga"],
  ["KCNM", "Cortez Municipal",          37.3030, -108.6278, "ga"],
  ["KCFO", "Canon City",                38.4458, -105.1122, "ga"],
  ["KTAD", "Perry Stokes (Trinidad)",   37.2594, -104.3412, "ga"],
  ["KLIC", "Limon Municipal",           39.2748, -103.6659, "ga"],
  ["KLAA", "Lamar Municipal",           38.0697, -102.6886, "ga"],
  ["KLHX", "La Junta Municipal",        38.0497, -103.5094, "ga"],
  ["KSPD", "Springdale/SE CO",          37.3388, -102.6124, "ga"],
  ["KSBS", "Steamboat Springs",         40.5163, -106.8660, "ga"],
];

function buildAirportLayer() {
  var markers = [];
  CO_AIRPORTS.forEach(function(a) {
    var icao = a[0], name = a[1], lat = a[2], lon = a[3], type = a[4];
    var isCom = (type === "com");
    var m = L.circleMarker([lat, lon], {
      radius:      isCom ? 7 : 5,
      color:       isCom ? "#58a6ff" : "#8b949e",
      fillColor:   isCom ? "#1f6feb" : "#30363d",
      fillOpacity: 0.85,
      weight:      1.5
    });
    m.bindPopup(
      '<b>' + icao + '</b><br>' + name + '<br>' +
      '<span style="color:#8b949e;font-size:0.8em">' +
      lat.toFixed(3) + '\u00b0N\u00a0\u00a0' + Math.abs(lon).toFixed(3) + '\u00b0W</span>',
      { maxWidth: 180 }
    );
    markers.push(m);
  });

  var layer = L.layerGroup(markers);

  // Show/hide ICAO text labels based on zoom
  map.on("zoomend", function() {
    if (!map.hasLayer(layer)) return;
    var z = map.getZoom();
    markers.forEach(function(m) {
      if (z >= 8) {
        if (!m.getTooltip()) {
          var icao = m.getPopup().getContent().replace(/<b>(.*?)<\/b>.*/,'$1');
          m.bindTooltip(icao, {
            permanent: true, direction: "right",
            className: "apt-label", offset: [6, 0]
          }).openTooltip();
        }
      } else {
        if (m.getTooltip()) m.unbindTooltip();
      }
    });
  });

  return layer;
}

// ── Major Colorado cities ─────────────────────────────────────────────────────
var CO_CITIES = [
  ["Denver",          39.7392, -104.9903, true ],
  ["Colorado Springs",38.8339, -104.8214, true ],
  ["Grand Junction",  39.0639, -108.5506, true ],
  ["Pueblo",          38.2544, -104.6091, false],
  ["Fort Collins",    40.5853, -105.0844, false],
  ["Boulder",         40.0150, -105.2705, false],
  ["Greeley",         40.4233, -104.7091, false],
  ["Longmont",        40.1672, -105.1019, false],
  ["Loveland",        40.3978, -105.0747, false],
  ["Aurora",          39.7294, -104.8319, false],
  ["Lakewood",        39.7047, -105.0814, false],
  ["Arvada",          39.8028, -105.0875, false],
  ["Steamboat Springs",40.4850,-106.8317, false],
  ["Glenwood Springs",39.5505, -107.3248, false],
  ["Aspen",           39.1911, -106.8175, false],
  ["Telluride",       37.9375, -107.8123, false],
  ["Montrose",        38.4783, -107.8762, false],
  ["Alamosa",         37.4695, -105.8700, false],
  ["Trinidad",        37.1694, -104.5003, false],
  ["Lamar",           38.0872, -102.6207, false],
  ["Craig",           40.5153, -107.5464, false],
  ["Salida",          38.5347, -106.0000, false],
  ["Leadville",       39.2503, -106.2925, false],
  ["Durango",         37.2753, -107.8801, false],
];

function buildCityLayer() {
  var markers = [];
  CO_CITIES.forEach(function(c) {
    var name = c[0], lat = c[1], lon = c[2], major = c[3];
    var m = L.circleMarker([lat, lon], {
      radius:      major ? 5 : 3,
      color:       "#e6edf3",
      fillColor:   "#e6edf3",
      fillOpacity: major ? 0.9 : 0.6,
      weight:      1
    });
    m.bindTooltip(name, {
      permanent: true, direction: "right",
      className: major ? "city-label-major" : "city-label",
      offset:    [5, 0]
    });
    markers.push(m);
  });
  return L.layerGroup(markers);
}

// Build layers (not added to map yet — user toggles via layer control)
var airportLayer = buildAirportLayer();
var cityLayer    = buildCityLayer();

// ── Leaflet layer control ─────────────────────────────────────────────────────
var overlayMaps = {
  "\u2708 Airports": airportLayer,
  "\u25cf Cities":   cityLayer,
  "\u2261 Roads":    roadsLayer
};
L.control.layers(null, overlayMaps, { collapsed: false, position: "topright" })
  .addTo(map);


// ── product switching ─────────────────────────────────────────────────────────
function onProductChange() {
  currentProduct = document.getElementById('product-sel').value;
  updateLegend();
  if (currentCycle) loadData();
}

function updateLegend() {
  document.getElementById('legend').innerHTML =
    PRODUCTS[currentProduct].legend;
}

// ── opacity ───────────────────────────────────────────────────────────────────
function updateOpacity(val) {
  currentOpacity = val / 100;
  document.getElementById('opacity-val').textContent = val + '%';
  if (dataLayer) {
    dataLayer.eachLayer(function(l) {
      l.setStyle({ fillOpacity: currentOpacity });
    });
  }
}

// ── cycle / status ────────────────────────────────────────────────────────────
async function fetchStatus() {
  try {
    var resp = await fetch('/api/winds/status');
    if (!resp.ok) return;
    var s = await resp.json();

    // API returns {cycles: [{cycle_utc, available_hours, pct_complete}, ...]}
    // Convert to dict keyed by cycle_utc for easy lookup
    cycleStatus = {};
    (s.cycles || []).forEach(function(c) {
      cycleStatus[c.cycle_utc] = c;
    });

    // populate cycle dropdown
    var sel = document.getElementById('cycle-sel');
    var prev = sel.value;
    sel.innerHTML = '';
    Object.keys(cycleStatus).sort().reverse().forEach(function(c) {
      var opt = document.createElement('option');
      opt.value = c;
      var d = new Date(c);
      opt.textContent = d.toUTCString().slice(5,22) + 'Z';
      sel.appendChild(opt);
    });
    if (prev && cycleStatus[prev]) sel.value = prev;
    else if (!currentCycle && sel.options.length) {
      sel.value = sel.options[0].value;
      currentCycle = sel.value;
    }

    buildHourButtons();

    // progress bar for active cycle
    var cs = cycleStatus[currentCycle];
    if (cs) {
      document.getElementById('progress-fill').style.width = cs.pct_complete + '%';
      document.getElementById('cycle-pct').textContent = cs.pct_complete + '% ready';
    }
  } catch(e) { console.warn('status fetch failed', e); }
}

function onCycleChange() {
  currentCycle = document.getElementById('cycle-sel').value;
  buildHourButtons();
  loadData();
}

function buildHourButtons() {
  var bar = document.getElementById('hour-bar');
  // remove old buttons
  bar.querySelectorAll('.hbtn').forEach(function(b) { b.remove(); });

  var cs = cycleStatus[currentCycle];
  var avail = cs ? cs.available_hours : [];
  var cache = cs ? (cs.cached_hours || {}) : {};
  var label = bar.querySelector('.ctrl-label');

  for (var fxx = 1; fxx <= 12; fxx++) {
    (function(f) {
      var btn = document.createElement('button');
      btn.className = 'hbtn';
      btn.textContent = 'F' + String(f).padStart(2,'0');
      btn.dataset.fxx = f;

      var dot = document.createElement('span');
      dot.className = 'dot-badge';

      var cached = cache[currentProduct] && cache[currentProduct].includes(f);
      var loading = false;  // loading_hours not in status API

      if (cached)       { dot.classList.add('dot-green'); }
      else if (loading) { dot.classList.add('dot-yellow'); }
      else              { dot.classList.add('dot-grey'); }
      btn.appendChild(dot);

      if (avail.includes(f)) {
        btn.classList.add('available');
        btn.onclick = function() { selectHour(f); };
      } else {
        btn.classList.add('unavail');
        btn.disabled = true;
      }
      if (f === currentFxx) btn.classList.add('active');
      bar.insertBefore(btn, document.getElementById('progress-bar'));
    })(fxx);
  }
}

function selectHour(fxx) {
  currentFxx = fxx;
  document.querySelectorAll('.hbtn').forEach(function(b) {
    b.classList.toggle('active', parseInt(b.dataset.fxx) === fxx);
  });
  loadData();
}

// ── data fetch + render ───────────────────────────────────────────────────────
async function loadData() {
  if (!currentCycle) return;
  var prod = PRODUCTS[currentProduct];

  // show loading overlay
  document.getElementById('load-msg').innerHTML = prod.loadMsg;
  document.getElementById('loading-overlay').classList.remove('hidden');
  document.getElementById('error-bar').style.display = 'none';

  // clear previous layer
  if (dataLayer) {
    if (dataLayer._isStreamline) {
      _slStop();                   // remove canvas + cancel animation
      map.removeLayer(dataLayer);  // also remove the background tile layer
    } else {
      map.removeLayer(dataLayer);
    }
    dataLayer = null;
  }

  try {
    var url = prod.endpoint +
              '?fxx=' + currentFxx +
              '&cycle_utc=' + encodeURIComponent(currentCycle);
    var resp = await fetch(url);

    if (!resp.ok) {
      var txt = await resp.text();
      throw new Error(txt.slice(0, 200));
    }

    var data = await resp.json();
    renderLayer(data, prod);

    document.getElementById('meta-valid').textContent = data.valid_utc || '—';
    document.getElementById('meta-pts').textContent =
      (data.point_count || data.points.length).toLocaleString();

  } catch(e) {
    var eb = document.getElementById('error-bar');
    eb.textContent = e.message;
    eb.style.display = 'block';
    console.error(e);
  } finally {
    document.getElementById('loading-overlay').classList.add('hidden');
  }
}

function renderLayer(data, prod) {
  // Streamline mode: colour-fill background tiles first, then canvas animation
  if (prod.renderMode === 'streamline') {
    _slStop();

    // Render speed colour tiles as normal Leaflet rects (reuse rect renderer)
    var half    = (data.cell_size_deg || 0.05) / 2;
    var halfLon = (data.cell_size_deg || 0.05) * 1.25;
    var renderer = L.canvas();
    var rects = [];
    (data.points || []).forEach(function(p) {
      var color = prod.color(p);
      var rect = L.rectangle(
        [[p.lat - half, p.lon - halfLon], [p.lat + half, p.lon + halfLon]],
        { renderer: renderer, color: color, fillColor: color,
          fillOpacity: currentOpacity, weight: 0 }
      );
      rect.bindPopup(prod.popup(p), { maxWidth: 180 });
      rects.push(rect);
    });
    dataLayer = L.layerGroup(rects).addTo(map);
    dataLayer._isStreamline = true;   // so clear logic also calls _slStop

    // Start particle animation on top
    _slStartAnimation(data);
    return;
  }
  // Tiles overlap by ~4% to close sub-pixel gaps without visible bleed
  var cell    = data.cell_size_deg || 0.045;
  var half    = cell * 0.52;
  var halfLon = cell * 1.30;
  var renderer = L.canvas();
  var rects    = [];

  data.points.forEach(function(p) {
    var color = prod.color(p);
    var rect  = L.rectangle(
      [[p.lat - half, p.lon - halfLon], [p.lat + half, p.lon + halfLon]],
      { renderer: renderer, color: color, fillColor: color,
        fillOpacity: currentOpacity, weight: 0 }
    );
    rect.bindPopup(prod.popup(p), { maxWidth: 200 });
    rects.push(rect);
  });

  dataLayer = L.layerGroup(rects).addTo(map);
}

// ── init ──────────────────────────────────────────────────────────────────────
// Set product from URL param if present (?product=froude etc.)
(function() {
  var params = new URLSearchParams(window.location.search);
  var p = params.get('product');
  if (p && PRODUCTS[p]) {
    currentProduct = p;
    document.getElementById('product-sel').value = p;
  }
})();

updateLegend();
fetchStatus().then(function() {
  if (currentCycle) loadData();
});

// refresh status every 5 min
statusTimer = setInterval(fetchStatus, 300000);
// ═══════════════════════════════════════════════════════════════════════════════
// Streamline (particle animation) engine
// ═══════════════════════════════════════════════════════════════════════════════

var _sl = {
  canvas:   null,
  ctx:      null,
  animId:   null,
  data:     null,
  N:        1800,          // particle count (medium density)
  age_max:  120,           // frames before forced respawn
  speed_scale: 0.25,       // pixels per frame per m/s at zoom 7 baseline
  particles: [],
};

// Speed → colour  (blue → cyan → yellow → orange → red)
function _slColor(spd_ms) {
  var kt = spd_ms * 1.94384;
  if (kt >= 40) return 'rgba(231, 76, 60,  0.85)';   // red
  if (kt >= 25) return 'rgba(230,126, 34,  0.85)';   // orange
  if (kt >= 15) return 'rgba(241,196, 15,  0.85)';   // yellow
  if (kt >=  8) return 'rgba( 88,214,141,  0.85)';   // green
  return             'rgba( 52,152,219,  0.85)';      // blue
}

// Bilinear interpolation of U or V at fractional grid index (gx, gy)
function _slInterp(flat, cols, gx, gy) {
  var x0 = Math.floor(gx), y0 = Math.floor(gy);
  var x1 = x0 + 1,         y1 = y0 + 1;
  var rows = flat.length / cols;
  if (x0 < 0 || y0 < 0 || x1 >= cols || y1 >= rows) return 0;
  var fx = gx - x0, fy = gy - y0;
  var q00 = flat[y0 * cols + x0];
  var q10 = flat[y0 * cols + x1];
  var q01 = flat[y1 * cols + x0];
  var q11 = flat[y1 * cols + x1];
  return (q00*(1-fx)*(1-fy) + q10*fx*(1-fy) +
          q01*(1-fx)*fy     + q11*fx*fy);
}

// Convert lat/lon → fractional grid index
function _slLatLonToGrid(lat, lon, d) {
  var gx = (lon - d.lon_min) / (d.lon_max - d.lon_min) * (d.cols - 1);
  var gy = (lat - d.lat_min) / (d.lat_max - d.lat_min) * (d.rows - 1);
  return [gx, gy];
}

// Random particle within Colorado bounds
function _slRandomParticle(d) {
  return {
    lat: d.lat_min + Math.random() * (d.lat_max - d.lat_min),
    lon: d.lon_min + Math.random() * (d.lon_max - d.lon_min),
    age: Math.floor(Math.random() * 80),
  };
}

function _slInitParticles(d) {
  _sl.particles = [];
  for (var i = 0; i < _sl.N; i++) {
    _sl.particles.push(_slRandomParticle(d));
  }
}

function _slStartAnimation(data) {
  _sl.data = data;
  _slInitParticles(data);

  // Create canvas sized to map container
  var container = document.getElementById('map');
  var cvs = document.createElement('canvas');
  cvs.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:500;';
  cvs.width  = container.offsetWidth;
  cvs.height = container.offsetHeight;
  container.appendChild(cvs);
  _sl.canvas = cvs;
  _sl.ctx    = cvs.getContext('2d');

  // Reposition canvas when map is panned (Leaflet fires 'move')
  map.on('move zoom', _slResetOnMove);

  _slAnimate();
}

function _slResetOnMove() {
  // On pan/zoom: reset particles so they respawn in visible area
  if (_sl.data) _slInitParticles(_sl.data);
}

function _slAnimate() {
  var ctx = _sl.ctx;
  var d   = _sl.data;
  if (!ctx || !d) return;

  // Clear to fully transparent each frame — lets Leaflet colour tiles show through
  ctx.clearRect(0, 0, _sl.canvas.width, _sl.canvas.height);

  var zoomFactor = Math.pow(2, map.getZoom() - 7) * _sl.speed_scale;

  ctx.globalCompositeOperation = 'source-over';
  ctx.lineWidth = 2.8;   // thicker = visible against coloured background

  var ps = _sl.particles;
  for (var i = 0; i < ps.length; i++) {
    var p = ps[i];
    p.age++;

    var g  = _slLatLonToGrid(p.lat, p.lon, d);
    var u  = _slInterp(d.u_flat, d.cols, g[0], g[1]);
    var v  = _slInterp(d.v_flat, d.cols, g[0], g[1]);
    var spd = Math.sqrt(u*u + v*v);

    var dlat = (v / 111000) * zoomFactor * 40;
    var dlon = (u / (111000 * Math.cos(p.lat * Math.PI/180))) * zoomFactor * 40;

    // Store position history for trail segments (max 6 steps)
    if (!p.trail) p.trail = [];
    p.trail.push([p.lat, p.lon]);
    if (p.trail.length > 10) p.trail.shift();  // longer tail

    // Draw trail: older segments are more transparent
    if (p.trail.length > 1) {
      // Opacity scales with speed so calm air stays subtle
      var baseAlpha = Math.min(0.40 + (spd / 18) * 0.55, 0.95) * currentOpacity;
      for (var t = 1; t < p.trail.length; t++) {
        var segAlpha = baseAlpha * (t / p.trail.length);
        var ptA = map.latLngToContainerPoint(p.trail[t-1]);
        var ptB = map.latLngToContainerPoint(p.trail[t]);
        ctx.beginPath();
        ctx.strokeStyle = 'rgba(255,255,255,' + segAlpha.toFixed(2) + ')';
        ctx.moveTo(ptA.x, ptA.y);
        ctx.lineTo(ptB.x, ptB.y);
        ctx.stroke();
      }
    }

    // Advance
    p.lat += dlat;
    p.lon += dlon;

    // Respawn if out of bounds or too old
    if (p.age > _sl.age_max ||
        p.lat < d.lat_min || p.lat > d.lat_max ||
        p.lon < d.lon_min || p.lon > d.lon_max) {
      ps[i] = _slRandomParticle(d);
    }
  }

  _sl.animId = requestAnimationFrame(_slAnimate);
}

function _slStop() {
  if (_sl.animId) { cancelAnimationFrame(_sl.animId); _sl.animId = null; }
  map.off('move zoom', _slResetOnMove);
  if (_sl.canvas && _sl.canvas.parentNode) {
    _sl.canvas.parentNode.removeChild(_sl.canvas);
  }
  _sl.canvas = _sl.ctx = _sl.data = null;
  _sl.particles = [];
}

</script>
</body>
</html>"""

@app.get("/map/hrrr")
def map_hrrr():
    return render_template_string(HRRR_MAP_TEMPLATE)

@app.get("/map/winds")
def map_winds():
    return render_template_string(HRRR_MAP_TEMPLATE)

@app.get("/map/froude")
def map_froude():
    from flask import redirect
    return redirect("/map/hrrr?product=froude")

@app.get("/map/virga")
def map_virga():
    from flask import redirect
    return redirect("/map/hrrr?product=virga")

@app.get("/map/llti")
def map_llti():
    from flask import redirect
    return redirect("/map/hrrr?product=llti")

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
            "did not find", "not found", "no such file", "404", "unavailable",
            "nomads", "full file", "byte-range", "grib_lock timeout"
        ])
        if not_ready:
            return jsonify({
                "error": "not_available",
                "message": f"F{fxx:02d} for cycle {cycle_utc} is not yet available.",
                "fxx": fxx, "cycle_utc": cycle_utc,
            }), 404
        raise


@app.get("/api/icing/colorado")
def api_icing_colorado():
    fxx       = int(request.args.get("fxx", 1))
    cycle_utc = request.args.get("cycle_utc")
    ttl       = int(request.args.get("ttl", os.environ.get("ICING_TTL", "600")))

    if not cycle_utc:
        status    = get_cycle_status_cached(ttl_seconds=300)
        cycle_utc = status["cycles"][0]["cycle_utc"]

    try:
        data = get_icing_cached(cycle_utc=cycle_utc, fxx=fxx, ttl_seconds=ttl)
        return jsonify(data)
    except Exception as e:
        msg = str(e)
        not_ready = any(k in msg.lower() for k in [
            "did not find", "not found", "no such file", "404", "unavailable",
            "nomads", "full file", "byte-range", "grib_lock timeout"
        ])
        if not_ready:
            return jsonify({
                "error": "not_available",
                "message": f"F{fxx:02d} for cycle {cycle_utc} is not yet available.",
                "fxx": fxx, "cycle_utc": cycle_utc,
            }), 404
        raise


@app.get("/api/winds/surface")
def api_winds_surface():
    fxx       = int(request.args.get("fxx", 1))
    cycle_utc = request.args.get("cycle_utc")
    ttl       = int(request.args.get("ttl", os.environ.get("WIND_SURF_TTL", "600")))

    if not cycle_utc:
        status    = get_cycle_status_cached(ttl_seconds=300)
        cycle_utc = status["cycles"][0]["cycle_utc"]

    try:
        data = get_surface_wind_cached(cycle_utc=cycle_utc, fxx=fxx, ttl_seconds=ttl)
        return jsonify(data)
    except Exception as e:
        msg = str(e)
        not_ready = any(k in msg.lower() for k in [
            "did not find", "not found", "no such file", "404", "unavailable",
            "nomads", "full file", "byte-range", "grib_lock"
        ])
        if not_ready:
            return jsonify({
                "error":     "not_available",
                "message":   f"F{fxx:02d} not yet available.",
                "fxx":       fxx,
                "cycle_utc": cycle_utc,
            }), 404
        raise


@app.get("/debug/sfc_fields")
def debug_sfc_fields():
    """Show actual GRIB field names in the sfc subset to fix search string."""
    import traceback
    try:
        from herbie import Herbie
        from winds_surface import HERBIE_DIR, _now_utc_hour_naive
        from datetime import timedelta
        import pygrib

        base = _now_utc_hour_naive()
        cycle = None
        for h in range(8):
            dt = base - timedelta(hours=h)
            try:
                H = Herbie(dt, model="hrrr", product="sfc", fxx=1,
                           save_dir=str(HERBIE_DIR), overwrite=False)
                H.inventory()
                cycle = dt
                break
            except Exception:
                continue

        if cycle is None:
            return "Could not find a valid HRRR sfc cycle", 500

        # Download full sfc file (small enough for debug)
        H = Herbie(cycle, model="hrrr", product="sfc", fxx=1,
                   save_dir=str(HERBIE_DIR), overwrite=False)
        path = H.download()

        rows = []
        grbs = pygrib.open(str(path))
        for grb in grbs:
            if "wind" in grb.name.lower() or "UGRD" in str(grb) or "VGRD" in str(grb):
                rows.append(f"{grb.name!r:45s}  typeOfLevel={grb.typeOfLevel!r:25s}  level={grb.level}")
        grbs.close()

        return "\n".join(rows) or "No wind fields found", 200, {"Content-Type": "text/plain"}
    except Exception:
        return traceback.format_exc(), 500, {"Content-Type": "text/plain"}


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

# Two new routes anywhere in the file:
@app.get("/api/llti/image")
def api_llti_image():
    ttl = int(os.environ.get("LLTI_TTL", "600"))
    try:
        png_bytes, _ = get_llti_cached(ttl_seconds=ttl)
        return Response(png_bytes, mimetype="image/png")
    except Exception:
        import traceback
        return Response(traceback.format_exc(), mimetype="text/plain", status=500)

@app.get("/api/llti/meta")
def api_llti_meta():
    ttl = int(os.environ.get("LLTI_TTL", "600"))
    try:
        _, meta = get_llti_cached(ttl_seconds=ttl)
        return jsonify(meta)
    except Exception:
        import traceback
        return jsonify({"error": traceback.format_exc()}), 500

@app.get("/api/llti/colorado")
def api_llti_colorado():
    fxx       = int(request.args.get("fxx", 1))
    cycle_utc = request.args.get("cycle_utc")
    ttl       = int(request.args.get("ttl", os.environ.get("LLTI_TTL", "600")))

    if not cycle_utc:
        status    = get_cycle_status_cached(ttl_seconds=300)
        cycle_utc = status["cycles"][0]["cycle_utc"]

    try:
        data = get_llti_points_cached(cycle_utc=cycle_utc, fxx=fxx, ttl_seconds=ttl)
        return jsonify(data)
    except Exception as e:
        msg = str(e)
        not_ready = any(k in msg.lower() for k in [
            "did not find", "not found", "no such file", "404", "unavailable",
            "nomads", "full file", "byte-range", "grib_lock timeout"
        ])
        if not_ready:
            return jsonify({
                "error":     "not_available",
                "message":   f"F{fxx:02d} for cycle {cycle_utc} is not yet available.",
                "fxx":       fxx,
                "cycle_utc": cycle_utc,
            }), 404
        raise

@app.errorhandler(Exception)
def handle_exception(e):
    tb = traceback.format_exc()
    return Response(tb, mimetype="text/plain", status=500)




