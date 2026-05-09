import os, json, secrets, sqlite3, hashlib
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DB = os.path.join(os.path.dirname(__file__), "direct.db")

# ── Utenti (aggiungi qui i tuoi) ──────────────────────────
USERS = {
    "luigi": {"password": "luigi123", "name": "Luigi", "color": "#7B61FF"},
    "amico": {"password": "amico123", "name": "Amico", "color": "#00D4FF"},
}

# ── Agent connections (sid -> agent_data) ─────────────────
agents = {}   # agent_token -> {user, status, tor_ip, last_seen}
events = {}   # user -> list of events from agent

# ─────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────
def init_db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT, label TEXT,
        vinted_user TEXT, vinted_email TEXT,
        tor_ip TEXT, status TEXT DEFAULT 'offline',
        created_at TEXT, last_active TEXT,
        offers_count INTEGER DEFAULT 0,
        monitoring INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER, user TEXT,
        offer_id TEXT UNIQUE,
        utente TEXT, prezzo TEXT, msg TEXT,
        stato TEXT DEFAULT 'In attesa',
        received_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS agent_tokens (
        token TEXT PRIMARY KEY,
        user TEXT, created_at TEXT
    )""")
    c.commit(); c.close()

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

# ─────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user" in session: return redirect("/dashboard")
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def login():
    d = request.json
    u = d.get("username","").lower().strip()
    p = d.get("password","").strip()
    if u in USERS and USERS[u]["password"] == p:
        session.permanent = True
        session["user"]  = u
        session["name"]  = USERS[u]["name"]
        session["color"] = USERS[u]["color"]
        return jsonify({"ok": True})
    return jsonify({"ok": False})

@app.route("/logout")
def logout():
    session.clear(); return redirect("/")

@app.route("/dashboard")
def dashboard():
    if "user" not in session: return redirect("/")
    return render_template("dashboard.html",
        user=session["user"], name=session["name"], color=session["color"])

# ─────────────────────────────────────────────────────────
#  API — AGENT TOKEN (per l'agent sul PC)
# ─────────────────────────────────────────────────────────
@app.route("/api/agent/token", methods=["POST"])
def get_agent_token():
    """L'agent chiama questa API per registrarsi"""
    d = request.json
    u = d.get("user","").lower()
    p = d.get("password","")
    if u not in USERS or USERS[u]["password"] != p:
        return jsonify({"ok": False}), 401
    token = secrets.token_hex(32)
    c = db()
    c.execute("INSERT OR REPLACE INTO agent_tokens VALUES (?,?,?)",
              (token, u, datetime.now().isoformat()))
    c.commit(); c.close()
    return jsonify({"ok": True, "token": token, "user": u})

@app.route("/api/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    """L'agent manda heartbeat ogni 10sec con lo stato"""
    token = request.headers.get("X-Agent-Token","")
    c = db()
    row = c.execute("SELECT user FROM agent_tokens WHERE token=?", (token,)).fetchone()
    c.close()
    if not row: return jsonify({"ok": False}), 401
    user = row["user"]
    d = request.json or {}
    agents[token] = {
        "user": user,
        "status": d.get("tor_status", "offline"),
        "tor_ip": d.get("tor_ip", ""),
        "last_seen": datetime.now().isoformat(),
        "active_sessions": d.get("active_sessions", [])
    }
    # Ritorna i comandi pending per l'agent
    cmds = events.get(user, [])
    events[user] = []
    return jsonify({"ok": True, "commands": cmds})

@app.route("/api/agent/status")
def agent_status():
    """Il frontend chiede lo stato dell'agent"""
    if "user" not in session: return jsonify({"connected": False})
    user = session["user"]
    for tok, ag in agents.items():
        if ag["user"] == user:
            last = datetime.fromisoformat(ag["last_seen"])
            secs = (datetime.now() - last).total_seconds()
            if secs < 30:
                return jsonify({
                    "connected": True,
                    "tor_status": ag["status"],
                    "tor_ip": ag["tor_ip"],
                    "active_sessions": ag["active_sessions"]
                })
    return jsonify({"connected": False, "tor_status": "offline"})

# ─────────────────────────────────────────────────────────
#  API — SESSIONS
# ─────────────────────────────────────────────────────────
@app.route("/api/sessions")
def get_sessions():
    if "user" not in session: return jsonify([])
    c = db()
    rows = c.execute(
        "SELECT * FROM sessions WHERE user=? ORDER BY id DESC", (session["user"],)
    ).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/sessions/<int:sid>")
def get_session(sid):
    if "user" not in session: return jsonify({})
    c = db()
    r = c.execute("SELECT * FROM sessions WHERE id=? AND user=?",
                  (sid, session["user"])).fetchone()
    if not r: return jsonify({}), 404
    offs = c.execute("SELECT * FROM offers WHERE session_id=? ORDER BY received_at DESC",
                     (sid,)).fetchall()
    c.close()
    data = dict(r)
    data["offers"] = [dict(o) for o in offs]
    return jsonify(data)

@app.route("/api/sessions/new", methods=["POST"])
def new_session():
    if "user" not in session: return jsonify({"ok": False})
    d = request.json
    label = d.get("label", "Sessione")
    c = db()
    cur = c.execute(
        "INSERT INTO sessions (user,label,status,created_at) VALUES (?,?,?,?)",
        (session["user"], label, "starting", datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    sid = cur.lastrowid; c.commit(); c.close()
    # Manda comando all'agent
    _send_cmd(session["user"], {"action": "new_session", "session_id": sid, "label": label})
    return jsonify({"ok": True, "id": sid})

@app.route("/api/sessions/<int:sid>/update", methods=["POST"])
def update_session(sid):
    """L'agent aggiorna i dati della sessione (email, vinted_user, tor_ip ecc)"""
    token = request.headers.get("X-Agent-Token","")
    c = db()
    row = c.execute("SELECT user FROM agent_tokens WHERE token=?", (token,)).fetchone()
    if not row: return jsonify({"ok": False}), 401
    d = request.json or {}
    fields = []
    vals = []
    for k in ["vinted_user","vinted_email","tor_ip","status","monitoring","offers_count"]:
        if k in d:
            fields.append(f"{k}=?")
            vals.append(d[k])
    if fields:
        vals += [sid, row["user"]]
        c.execute(f"UPDATE sessions SET {','.join(fields)},last_active=? WHERE id=? AND user=?",
                  vals[:-2] + [datetime.now().strftime("%Y-%m-%d %H:%M")] + vals[-2:])
        c.commit()
    c.close()
    return jsonify({"ok": True})

@app.route("/api/sessions/<int:sid>/delete", methods=["POST"])
def delete_session(sid):
    if "user" not in session: return jsonify({"ok": False})
    _send_cmd(session["user"], {"action": "delete_session", "session_id": sid})
    c = db()
    c.execute("DELETE FROM sessions WHERE id=? AND user=?", (sid, session["user"]))
    c.execute("DELETE FROM offers WHERE session_id=?", (sid,))
    c.commit(); c.close()
    return jsonify({"ok": True})

@app.route("/api/sessions/<int:sid>/monitor", methods=["POST"])
def toggle_monitor(sid):
    if "user" not in session: return jsonify({"ok": False})
    d = request.json
    action = d.get("action", "start")
    _send_cmd(session["user"], {"action": f"monitor_{action}", "session_id": sid})
    c = db()
    c.execute("UPDATE sessions SET monitoring=? WHERE id=? AND user=?",
              (1 if action=="start" else 0, sid, session["user"]))
    c.commit(); c.close()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────────────────
#  API — OFFERS
# ─────────────────────────────────────────────────────────
@app.route("/api/offers")
def get_offers():
    if "user" not in session: return jsonify([])
    c = db()
    rows = c.execute(
        """SELECT o.*, s.label as sess_label
           FROM offers o JOIN sessions s ON o.session_id=s.id
           WHERE o.user=? ORDER BY o.received_at DESC LIMIT 50""",
        (session["user"],)
    ).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/offers/new", methods=["POST"])
def new_offer():
    """L'agent invia nuove offerte"""
    token = request.headers.get("X-Agent-Token","")
    c = db()
    row = c.execute("SELECT user FROM agent_tokens WHERE token=?", (token,)).fetchone()
    if not row: return jsonify({"ok": False}), 401
    d = request.json
    try:
        c.execute(
            "INSERT OR IGNORE INTO offers (session_id,user,offer_id,utente,prezzo,msg,stato,received_at) VALUES (?,?,?,?,?,?,?,?)",
            (d["session_id"], row["user"], d["offer_id"], d["utente"],
             d["prezzo"], d["msg"], "In attesa", datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        c.execute("UPDATE sessions SET offers_count=offers_count+1 WHERE id=?", (d["session_id"],))
        c.commit()
    except: pass
    c.close()
    return jsonify({"ok": True})

@app.route("/api/offers/<int:oid>/stato", methods=["POST"])
def update_offer(oid):
    if "user" not in session: return jsonify({"ok": False})
    stato = request.json.get("stato","")
    c = db()
    c.execute("UPDATE offers SET stato=? WHERE id=? AND user=?", (stato, oid, session["user"]))
    c.commit(); c.close()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────────────────
#  API — STATS
# ─────────────────────────────────────────────────────────
@app.route("/api/stats")
def get_stats():
    if "user" not in session: return jsonify({})
    c = db()
    u = session["user"]
    sess  = c.execute("SELECT COUNT(*) FROM sessions WHERE user=?", (u,)).fetchone()[0]
    offs  = c.execute("SELECT COUNT(*) FROM offers WHERE user=?", (u,)).fetchone()[0]
    comp  = c.execute("SELECT COUNT(*) FROM offers WHERE user=? AND stato='Completata'", (u,)).fetchone()[0]
    mon   = c.execute("SELECT COUNT(*) FROM sessions WHERE user=? AND monitoring=1", (u,)).fetchone()[0]
    c.close()
    rate = f"{int(comp/max(offs,1)*100)}%" if offs else "—%"
    return jsonify({"sessions": sess, "offers": offs, "success_rate": rate, "monitoring": mon})

# ─────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────
def _send_cmd(user, cmd):
    if user not in events: events[user] = []
    events[user].append(cmd)

if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
