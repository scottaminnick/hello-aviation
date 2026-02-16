import os
from flask import Flask, jsonify

app = Flask(__name__)

@app.get("/")
def home():
    return """
    <h1>Hello Aviation</h1>
    <p>If you can read this, GitHub → Railway is working.</p>
    <p>Try <a href="/health">/health</a></p>
    """

@app.get("/health")
def health():
    return jsonify(status="ok")

if __name__ == "__main__":
    # Local dev only; Railway will use gunicorn
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
