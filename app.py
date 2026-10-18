import os
import random
import sqlite3
import threading
import time
import urllib.request

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "practicals.db")
PORT = int(os.environ.get("PORT", 5000))
# Set SELF_URL to your deployed URL, e.g. https://myapp.onrender.com
SELF_URL = (os.environ.get("SELF_URL") or os.environ.get("RENDER_EXTERNAL_URL")
            or f"http://127.0.0.1:{PORT}").rstrip("/")

app = Flask(__name__)


# ---------- DB ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS subjects(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL COLLATE NOCASE
        );
        CREATE TABLE IF NOT EXISTS practicals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            number TEXT NOT NULL,
            aim TEXT NOT NULL,
            code TEXT NOT NULL,
            language TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def clean(data, key, limit):
    return str(data.get(key, "")).strip()[:limit]


SELECT_P = """SELECT p.*, s.name AS subject FROM practicals p
              JOIN subjects s ON s.id = p.subject_id """


# ---------- Routes ----------
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/ping")
def ping():
    return "pong"


@app.route("/api/subjects", methods=["GET", "POST"])
def subjects():
    with db() as con:
        if request.method == "POST":
            name = clean(request.get_json(force=True), "name", 60)
            if not name:
                return jsonify(error="Subject name required"), 400
            try:
                cur = con.execute("INSERT INTO subjects(name) VALUES(?)", (name,))
            except sqlite3.IntegrityError:
                return jsonify(error="Subject already exists"), 409
            return jsonify(id=cur.lastrowid, name=name), 201
        return jsonify(rows(con.execute(
            """SELECT s.id, s.name, COUNT(p.id) AS count FROM subjects s
               LEFT JOIN practicals p ON p.subject_id = s.id
               GROUP BY s.id ORDER BY s.name""")))


@app.route("/api/subjects/<int:sid>", methods=["DELETE"])
def delete_subject(sid):
    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM practicals WHERE subject_id=?", (sid,)).fetchone()[0]
        if n:
            return jsonify(error="Delete its practicals first"), 400
        con.execute("DELETE FROM subjects WHERE id=?", (sid,))
    return jsonify(ok=True)


@app.route("/api/practicals", methods=["GET", "POST"])
def practicals():
    with db() as con:
        if request.method == "POST":
            d = request.get_json(force=True)
            sid, number = d.get("subject_id"), clean(d, "number", 20)
            aim, code, lang = clean(d, "aim", 1000), clean(d, "code", 100000), clean(d, "language", 30)
            if not (sid and number and aim and code):
                return jsonify(error="Subject, number, aim and code are required"), 400
            cur = con.execute(
                "INSERT INTO practicals(subject_id, number, aim, code, language) VALUES(?,?,?,?,?)",
                (sid, number, aim, code, lang))
            return jsonify(id=cur.lastrowid), 201
        sid = request.args.get("subject_id", type=int)
        q = SELECT_P + ("WHERE p.subject_id=? " if sid else "")
        q += "ORDER BY CAST(p.number AS INTEGER), p.number"
        return jsonify(rows(con.execute(q, (sid,) if sid else ())))


@app.route("/api/practicals/<int:pid>", methods=["PUT", "DELETE"])
def practical(pid):
    with db() as con:
        if request.method == "DELETE":
            con.execute("DELETE FROM practicals WHERE id=?", (pid,))
            return jsonify(ok=True)
        d = request.get_json(force=True)
        number, aim = clean(d, "number", 20), clean(d, "aim", 1000)
        code, lang = clean(d, "code", 100000), clean(d, "language", 30)
        if not (number and aim and code):
            return jsonify(error="Number, aim and code are required"), 400
        con.execute("UPDATE practicals SET number=?, aim=?, code=?, language=? WHERE id=?",
                    (number, aim, code, lang, pid))
    return jsonify(ok=True)


@app.route("/api/search")
def search():
    words = request.args.get("q", "").strip().lower().split()[:8]
    if not words:
        return jsonify([])
    where, args = [], []
    for w in words:  # every word must appear in aim, code, number or subject
        like = f"%{w.replace('%', '').replace('_', '')}%"
        where.append("(LOWER(p.aim) LIKE ? OR LOWER(p.code) LIKE ? "
                     "OR LOWER(p.number) LIKE ? OR LOWER(s.name) LIKE ?)")
        args += [like] * 4
    with db() as con:
        return jsonify(rows(con.execute(
            SELECT_P + "WHERE " + " AND ".join(where) +
            " ORDER BY s.name, CAST(p.number AS INTEGER) LIMIT 100", args)))


# ---------- Self ping (random 1-8 minutes) ----------
def self_ping():
    while True:
        time.sleep(random.randint(60, 480))
        try:
            urllib.request.urlopen(SELF_URL + "/ping", timeout=20).read()
            print("[self-ping] ok", time.strftime("%H:%M:%S"))
        except Exception as e:
            print("[self-ping] failed:", e)


def start_pinger():
    t = threading.Thread(target=self_ping, daemon=True)
    t.start()


init_db()
# avoid double thread under the Flask debug reloader
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("FLASK_DEBUG"):
    start_pinger()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
