import os
from flask import Flask, jsonify, render_template_string
from guidance import get_guidance_cached
from metar import get_metars_cached

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
      <h3>Useful links</h3>
      <p><a href="/health">/health</a> (ops check)</p>
      <p><a href="/api/guidance">/api/guidance</a> (JSON for scripts/coworkers)</p>
    </div>
  </body>
</html>
"""

@app.get("/")
def home():
    title = os.environ.get("APP_TITLE", "Aviation Guidance")
    g = get_guidance_cached(ttl_seconds=int(os.environ.get("GUIDANCE_TTL", "300")))
    return render_template_string(HOME_TEMPLATE, title=title, g=g)

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
    stations_default = os.environ.get("METAR_STATIONS", "KMCI,KSTL,KMKC").split(",")
    metars = get_metars_cached(
        stations=stations_default,
        ttl_seconds=int(os.environ.get("METAR_TTL", "120"))
    )
    return jsonify(metars)



