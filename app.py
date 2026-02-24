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
      <p><a href="/map/hrrr">/map/winds</a> (HRRR Colorado Wind Gusts)</p>
      <p><a href="/map/froude">/map/froude</a> (HRRR Colorado Froude Number)</p>
      <p><a href="/map/virga">/map/virga</a> (HRRR Colorado Virga Potential)</p>
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
    cycleStatus = s;

    // populate cycle dropdown
    var sel = document.getElementById('cycle-sel');
    var prev = sel.value;
    sel.innerHTML = '';
    Object.keys(s).sort().reverse().forEach(function(c) {
      var opt = document.createElement('option');
      opt.value = c;
      var d = new Date(c);
      opt.textContent = d.toUTCString().slice(5,22) + 'Z';
      sel.appendChild(opt);
    });
    if (prev && s[prev]) sel.value = prev;
    else if (!currentCycle && sel.options.length) {
      sel.value = sel.options[0].value;
      currentCycle = sel.value;
    }

    buildHourButtons();

    // progress bar for active cycle
    var cs = s[currentCycle];
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
      var loading = cs && cs.loading_hours && cs.loading_hours.includes(f);

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
  if (dataLayer) { map.removeLayer(dataLayer); dataLayer = null; }

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
  var half    = (data.cell_size_deg || 0.045) / 2;
  var halfLon = (data.cell_size_deg || 0.045) * 1.25;
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
</script>
</body>
</html>"""

@app.get("/map/hrrr")
def map_hrrr():
    return render_template_string(HRRR_MAP_TEMPLATE)

@app.get("/map/hrrr")
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
