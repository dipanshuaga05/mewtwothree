import hmac
import os
import random
import secrets
import sqlite3
import threading
import time
import urllib.request

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "todo.db")
PORT = int(os.environ.get("PORT", 5000))
# Set SELF_URL to your deployed URL, e.g. https://myapp.onrender.com
SELF_URL = (os.environ.get("SELF_URL") or os.environ.get("RENDER_EXTERNAL_URL")
            or f"http://127.0.0.1:{PORT}").rstrip("/")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "4789")
if APP_PASSWORD == "changeme":
    print("[warning] APP_PASSWORD not set, using default 'changeme'")

TOKEN_TTL = 12 * 3600
TOKENS = {}  # in-memory only: token -> expiry. No cookies, nothing saved in the browser.
FAILS = {}   # ip -> (count, first_fail_time)

app = Flask(__name__)

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1a1d23;--line:#d1d5db;--accent:#4f46e5}
@media(prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#171b22;--text:#e6e8ec;--line:#2d3440;--accent:#818cf8}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);font-family:system-ui,sans-serif}
form{width:min(320px,88vw)}
input{width:100%;padding:14px 16px;font-size:16px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--text)}
input:focus{outline:2px solid var(--accent);outline-offset:-1px}
input.err{border-color:#dc2626;animation:s .3s}
@keyframes s{25%{transform:translateX(-6px)}75%{transform:translateX(6px)}}
</style></head><body>
<form id="f"><input id="p" type="password" autofocus autocomplete="off" aria-label="Password"></form>
<script>
const f=document.getElementById('f'),p=document.getElementById('p');
f.onsubmit=async e=>{
  e.preventDefault();p.classList.remove('err');
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p.value})});
  if(r.ok){const h=await r.text();document.open();document.write(h);document.close();}
  else{p.value='';void p.offsetWidth;p.classList.add('err');p.focus();}
};
</script></body></html>"""


def valid_token(t):
    exp = TOKENS.get(t)
    if exp and exp > time.time():
        return True
    TOKENS.pop(t, None)
    return False


@app.before_request
def guard():
    if request.path.startswith("/api/") and not valid_token(request.headers.get("X-Token", "")):
        return jsonify(error="Unauthorized"), 401


# ---------- DB ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS lists(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL COLLATE NOCASE
        );
        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
            no TEXT NOT NULL,
            title TEXT NOT NULL,
            details TEXT NOT NULL,
            tag TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def clean(data, key, limit):
    return str(data.get(key, "")).strip()[:limit]


SELECT_T = """SELECT t.*, l.name AS list FROM tasks t JOIN lists l ON l.id = t.list_id """


# ---------- Auth ----------
@app.route("/")
def index():
    return LOGIN_HTML, 401


@app.route("/login", methods=["POST"])
def login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    count, first = FAILS.get(ip, (0, time.time()))
    if time.time() - first > 600:
        count, first = 0, time.time()
    if count >= 5:
        return "", 429
    pw = str((request.get_json(silent=True) or {}).get("password", ""))
    if not hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
        FAILS[ip] = (count + 1, first)
        time.sleep(1)
        return "", 401
    FAILS.pop(ip, None)
    now = time.time()
    for t in [t for t, e in TOKENS.items() if e < now]:
        TOKENS.pop(t, None)
    token = secrets.token_urlsafe(32)
    TOKENS[token] = now + TOKEN_TTL
    with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__TOKEN__", token)
    resp = app.response_class(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/logout", methods=["POST"])
def logout():
    TOKENS.pop(request.headers.get("X-Token", ""), None)
    return jsonify(ok=True)


@app.route("/ping")
def ping():
    return "pong"


# ---------- API ----------
@app.route("/api/lists", methods=["GET", "POST"])
def lists():
    with db() as con:
        if request.method == "POST":
            name = clean(request.get_json(force=True), "name", 60)
            if not name:
                return jsonify(error="Name required"), 400
            try:
                cur = con.execute("INSERT INTO lists(name) VALUES(?)", (name,))
            except sqlite3.IntegrityError:
                return jsonify(error="Already exists"), 409
            return jsonify(id=cur.lastrowid, name=name), 201
        return jsonify(rows(con.execute(
            """SELECT l.id, l.name, COUNT(t.id) AS count FROM lists l
               LEFT JOIN tasks t ON t.list_id = l.id GROUP BY l.id ORDER BY l.name""")))


@app.route("/api/lists/<int:lid>", methods=["DELETE"])
def delete_list(lid):
    with db() as con:
        if con.execute("SELECT COUNT(*) FROM tasks WHERE list_id=?", (lid,)).fetchone()[0]:
            return jsonify(error="Delete its items first"), 400
        con.execute("DELETE FROM lists WHERE id=?", (lid,))
    return jsonify(ok=True)


@app.route("/api/tasks", methods=["GET", "POST"])
def tasks():
    with db() as con:
        if request.method == "POST":
            d = request.get_json(force=True)
            lid, no = d.get("list_id"), clean(d, "no", 20)
            title, details, tag = clean(d, "title", 1000), clean(d, "details", 100000), clean(d, "tag", 30)
            if not (lid and no and title and details):
                return jsonify(error="All fields except tag are required"), 400
            cur = con.execute("INSERT INTO tasks(list_id, no, title, details, tag) VALUES(?,?,?,?,?)",
                              (lid, no, title, details, tag))
            return jsonify(id=cur.lastrowid), 201
        lid = request.args.get("list_id", type=int)
        q = SELECT_T + ("WHERE t.list_id=? " if lid else "") + "ORDER BY CAST(t.no AS INTEGER), t.no"
        return jsonify(rows(con.execute(q, (lid,) if lid else ())))


@app.route("/api/tasks/<int:tid>", methods=["PUT", "DELETE"])
def task(tid):
    with db() as con:
        if request.method == "DELETE":
            con.execute("DELETE FROM tasks WHERE id=?", (tid,))
            return jsonify(ok=True)
        d = request.get_json(force=True)
        no, title = clean(d, "no", 20), clean(d, "title", 1000)
        details, tag = clean(d, "details", 100000), clean(d, "tag", 30)
        if not (no and title and details):
            return jsonify(error="All fields except tag are required"), 400
        con.execute("UPDATE tasks SET no=?, title=?, details=?, tag=? WHERE id=?", (no, title, details, tag, tid))
    return jsonify(ok=True)


@app.route("/api/search")
def search():
    words = request.args.get("q", "").strip().lower().split()[:8]
    if not words:
        return jsonify([])
    where, args = [], []
    for w in words:  # every word must appear somewhere in the item
        like = f"%{w.replace('%', '').replace('_', '')}%"
        where.append("(LOWER(t.title) LIKE ? OR LOWER(t.details) LIKE ? OR LOWER(t.no) LIKE ? OR LOWER(l.name) LIKE ?)")
        args += [like] * 4
    with db() as con:
        return jsonify(rows(con.execute(
            SELECT_T + "WHERE " + " AND ".join(where) + " ORDER BY l.name, CAST(t.no AS INTEGER) LIMIT 100", args)))


# ---------- Self ping (random 1-8 minutes) ----------
def self_ping():
    while True:
        time.sleep(random.randint(60, 480))
        try:
            urllib.request.urlopen(SELF_URL + "/ping", timeout=20).read()
            print("[self-ping] ok", time.strftime("%H:%M:%S"))
        except Exception as e:
            print("[self-ping] failed:", e)


init_db()
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("FLASK_DEBUG"):
    threading.Thread(target=self_ping, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
