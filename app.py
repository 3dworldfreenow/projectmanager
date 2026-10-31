"""
IoT Project Manager — a small Flask app for a two-person team
(software owner + hardware/IoT expert).

Features
  * First-run setup creates the two accounts; change password any time
  * Chat edit with version history visible to both people
  * Server-side call recording (streamed live), Firmware versions, Milestones/Payments
  * Chat (persisted, searchable)
  * Peer-to-peer voice/video calls (WebRTC) — recorded and stored
  * Documents (upload / notes)
  * Agreement (versioned, both must accept)
  * Hardware list with version history
  * Hardware testing log
  * Research / task tracker (weather station + IoT projects)

Run:
  pip install -r requirements.txt
  python app.py
  open http://localhost:5000   -> first visit asks you to create both accounts
"""
import os, re, sqlite3, datetime, uuid
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, send_from_directory, flash, jsonify, abort)
import threading, collections
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "iotpm.db")
UPLOADS = os.path.join(BASE, "uploads")
RECORDINGS = os.path.join(UPLOADS, "recordings")
os.makedirs(RECORDINGS, exist_ok=True)

# Accounts are created on first launch at /setup (no hardcoded passwords).

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-" + uuid.uuid4().hex)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB (recordings)
# in-memory signaling for WebRTC (per user). Messages keep a sequence number and
# live for 2 minutes, so EVERY open tab of a user sees them (a second tab must not
# "steal" the answer / ICE candidates meant for the chat tab).
SIGNALS = collections.defaultdict(list)   # user -> [(seq, time, msg)]
SIG_LOCK = threading.Lock()
SIG_SEQ = [0]

# ---------------------------------------------------------------- database ---
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, pw_hash TEXT, role TEXT, is_admin INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS invites(
  token TEXT PRIMARY KEY, role TEXT, created_by TEXT, expires TEXT, used_by TEXT, ts TEXT, max_uses INTEGER DEFAULT 1, uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY, name TEXT, description TEXT, status TEXT DEFAULT 'active',
  created TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, project_id INTEGER, sender TEXT, body TEXT, ts TEXT,
  filename TEXT, orig_name TEXT, edited INTEGER DEFAULT 0,
  reply_to INTEGER, pinned INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, kind TEXT DEFAULT 'text');
CREATE TABLE IF NOT EXISTS seen(
  username TEXT, project_id INTEGER, last_msg_id INTEGER DEFAULT 0, last_login TEXT,
  PRIMARY KEY(username, project_id));
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY, project_id INTEGER, who TEXT, action TEXT, detail TEXT, link TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS message_history(
  id INTEGER PRIMARY KEY, message_id INTEGER, body TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS firmware(
  id INTEGER PRIMARY KEY, project_id INTEGER, version TEXT, target TEXT, notes TEXT,
  filename TEXT, orig_name TEXT, author TEXT, ts TEXT, status TEXT DEFAULT 'dev');
CREATE TABLE IF NOT EXISTS milestones(
  id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, details TEXT, amount REAL,
  due TEXT, status TEXT DEFAULT 'pending', agreement_version INTEGER,
  paid_on TEXT, paid_ref TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY, project_id INTEGER, caller TEXT, callee TEXT,
  started TEXT, ended TEXT, kind TEXT, recording TEXT, notes TEXT, recording2 TEXT,
  status TEXT DEFAULT 'ringing');
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, body TEXT,
  filename TEXT, uploaded_by TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS agreements(
  id INTEGER PRIMARY KEY, version INTEGER, body TEXT, author TEXT, ts TEXT,
  accepted_by TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS acceptances(
  agreement_id INTEGER, username TEXT, ts TEXT, PRIMARY KEY(agreement_id, username));
CREATE TABLE IF NOT EXISTS hardware(
  id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT, part_no TEXT,
  qty INTEGER, supplier TEXT, cost REAL, status TEXT, notes TEXT,
  version INTEGER DEFAULT 1, updated_by TEXT, ts TEXT, photo TEXT, datasheet TEXT, min_qty INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS hardware_history(
  id INTEGER PRIMARY KEY, hardware_id INTEGER, version INTEGER, snapshot TEXT,
  changed_by TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS tests(
  id INTEGER PRIMARY KEY, project_id INTEGER, hardware_id INTEGER, title TEXT,
  procedure TEXT, result TEXT, passed INTEGER, tested_by TEXT, ts TEXT, firmware_id INTEGER);
CREATE TABLE IF NOT EXISTS test_files(
  id INTEGER PRIMARY KEY, test_id INTEGER, filename TEXT, orig_name TEXT);
CREATE TABLE IF NOT EXISTS research(
  id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, details TEXT,
  owner TEXT, status TEXT DEFAULT 'todo', priority TEXT DEFAULT 'medium',
  due TEXT, ts TEXT, decision TEXT);
CREATE TABLE IF NOT EXISTS research_log(
  id INTEGER PRIMARY KEY, research_id INTEGER, author TEXT, body TEXT, filename TEXT, orig_name TEXT,
  kind TEXT DEFAULT 'note', ts TEXT);
CREATE TABLE IF NOT EXISTS research_files(
  id INTEGER PRIMARY KEY, research_id INTEGER, filename TEXT, orig_name TEXT,
  uploaded_by TEXT, ts TEXT);
"""

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

def log_activity(action, detail="", link=""):
    db().execute("INSERT INTO activity(project_id,who,action,detail,link,ts) VALUES(?,?,?,?,?,?)",
                 (session.get("pid", 1), session.get("user"), action, detail, link, now()))

def unread_count():
    if "user" not in session: return 0
    row = db().execute("SELECT last_msg_id FROM seen WHERE username=? AND project_id=?",
                       (session["user"], pid())).fetchone()
    last = row["last_msg_id"] if row else 0
    return db().execute("SELECT COUNT(*) FROM messages WHERE project_id=? AND id>? AND sender!=? AND deleted=0",
                        (pid(), last, session["user"])).fetchone()[0]

def init_db():
    _init_db_core()
    from pm import init_pm
    from iot import init_iot
    from hw import init_hw
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    init_pm(con); init_iot(con); init_hw(con); con.close()

def _init_db_core():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    # add columns to DBs created by earlier versions
    for tbl, col, typ in [("messages","reply_to","INTEGER"),("messages","pinned","INTEGER DEFAULT 0"),
                          ("messages","deleted","INTEGER DEFAULT 0"),("messages","kind","TEXT DEFAULT 'text'"),
                          ("calls","status","TEXT DEFAULT 'ringing'"),("hardware","photo","TEXT"),
                          ("hardware","datasheet","TEXT"),("hardware","min_qty","INTEGER DEFAULT 0"),
                          ("tests","firmware_id","INTEGER"),("research","decision","TEXT")]:
        try: con.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError: pass
    if not con.execute("SELECT 1 FROM projects").fetchone():
        con.execute("INSERT INTO projects(name,description,created) VALUES(?,?,?)",
                    ("Weather Station", "Outdoor weather station: temp, humidity, pressure, rain, wind.", now()))
        con.execute("INSERT INTO projects(name,description,created) VALUES(?,?,?)",
                    ("General IoT", "Shared research & experiments.", now()))
    con.commit(); con.close()

# ------------------------------------------------------------------ auth ----
def users_exist():
    return db().execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

ROLES = ["Project Manager", "Software Engineer", "IoT Engineer", "Hardware Engineer",
         "Hardware Tester", "Firmware Developer", "Designer", "Client / Observer", "Guest"]

def all_users():
    return [r["username"] for r in db().execute("SELECT username FROM users WHERE active=1 ORDER BY rowid")]

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("is_admin"):
            flash("Admin only"); return redirect(url_for("dashboard"))
        return f(*a, **k)
    return login_required(w)

def ident():
    """Signaling identity: logged-in username, or 'guest:<room>:<name>' for a call guest."""
    return session.get("user") or session.get("guest_id")

def participant_required(f):
    """Allows logged-in members and call guests (for signaling / recording only)."""
    @wraps(f)
    def w(*a, **k):
        if not ident():
            return jsonify(ok=False, error="not a participant"), 401
        return f(*a, **k)
    return w

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not users_exist():
            return redirect(url_for("setup"))
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w

@app.context_processor
def inject():
    projects = db().execute("SELECT * FROM projects ORDER BY id").fetchall() if "user" in session else []
    others = [u for u in all_users() if u != session.get("user")] if "user" in session else []
    return dict(user=session.get("user"), role=session.get("role"), is_admin=session.get("is_admin"),
                projects=projects, current_pid=session.get("pid"),
                other_user=others[0] if others else None, others=others, ROLES=ROLES,
                unread=unread_count() if "user" in session else 0,
                missed=db().execute("SELECT COUNT(*) FROM calls WHERE callee=? AND status='missed'",
                                    (session.get("user"),)).fetchone()[0] if "user" in session else 0)

@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First-time setup: create the two accounts. Only works while no users exist."""
    if users_exist():
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        try:
            for i in ("1", "2"):
                u, pw = f[f"user{i}"].strip(), f[f"pass{i}"]
                if not u or len(pw) < 6:
                    flash("Both usernames are required and passwords need 6+ characters"); break
                db().execute("INSERT INTO users(username,pw_hash,role,is_admin) VALUES(?,?,?,?)",
                             (u, generate_password_hash(pw), f[f"role{i}"], 1 if i == "1" else 0))
            else:
                db().commit()
                flash("Accounts created — please log in")
                return redirect(url_for("login"))
            db().rollback()
        except sqlite3.IntegrityError:
            db().rollback(); flash("The two usernames must be different")
    return render_template("setup.html")

@app.route("/password", methods=["GET", "POST"])
@login_required
def password():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE username=?", (session["user"],)).fetchone()
        if not check_password_hash(u["pw_hash"], request.form["old"]):
            flash("Current password is wrong")
        elif len(request.form["new"]) < 6:
            flash("New password needs 6+ characters")
        else:
            db().execute("UPDATE users SET pw_hash=? WHERE username=?",
                         (generate_password_hash(request.form["new"]), session["user"]))
            db().commit(); flash("Password changed"); return redirect(url_for("dashboard"))
    return render_template("password.html")

@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    d = db()
    if request.method == "POST":
        f = request.form; u = f["username"].strip()
        if not u or len(f["password"]) < 6:
            flash("Username required, password 6+ characters")
        else:
            try:
                d.execute("INSERT INTO users(username,pw_hash,role,is_admin) VALUES(?,?,?,?)",
                          (u, generate_password_hash(f["password"]), f.get("role_custom") or f["role"],
                           1 if f.get("is_admin") else 0))
                d.commit(); flash(f"User {u} added")
            except sqlite3.IntegrityError:
                flash("That username already exists")
        return redirect(url_for("users"))
    rows = d.execute("SELECT * FROM users ORDER BY rowid").fetchall()
    invites = d.execute("SELECT * FROM invites WHERE expires>=? AND uses<max_uses ORDER BY ts DESC", (now(),)).fetchall()
    base_url = os.environ.get("APP_URL") or request.url_root.rstrip("/")
    return render_template("users.html", users=rows, invites=invites, base_url=base_url)

@app.route("/invite", methods=["POST"])
@admin_required
def invite_create():
    f = request.form
    days = int(f.get("days") or 7); uses = int(f.get("uses") or 1)
    token = uuid.uuid4().hex[:20]
    exp = (datetime.datetime.now() + datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    db().execute("INSERT INTO invites(token,role,created_by,expires,ts,max_uses) VALUES(?,?,?,?,?,?)",
                 (token, f.get("role_custom") or f.get("role", "Guest"), session["user"], exp, now(), uses))
    db().commit(); flash("Invite link created — share it below")
    return redirect(url_for("users") + "#invites")

@app.route("/invite/<token>/revoke", methods=["POST"])
@admin_required
def invite_revoke(token):
    db().execute("DELETE FROM invites WHERE token=?", (token,)); db().commit()
    return redirect(url_for("users") + "#invites")

@app.route("/join/<token>", methods=["GET", "POST"])
def join(token):
    """Guest / member signs up through an invite link."""
    inv = db().execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
    if not inv or inv["expires"] < now() or inv["uses"] >= inv["max_uses"]:
        return render_template("join.html", invalid=True), 410
    if request.method == "POST":
        u, pw = request.form["username"].strip(), request.form["password"]
        if not u or len(pw) < 6:
            flash("Username required and password needs 6+ characters")
        else:
            try:
                db().execute("INSERT INTO users(username,pw_hash,role,is_admin) VALUES(?,?,?,0)", (u, generate_password_hash(pw), inv["role"]))
                db().execute("UPDATE invites SET uses=uses+1, used_by=COALESCE(used_by,'')||?||',' WHERE token=?", (u, token))
                db().commit()
                session["user"], session["role"], session["is_admin"] = u, inv["role"], False
                session["pid"] = 1
                log_activity("joined via invite", f"{u} as {inv['role']}", "/users")
                db().commit()
                flash(f"Welcome, {u}! You joined as {inv['role']}.")
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                flash("That username is taken — choose another")
    return render_template("join.html", inv=inv, invalid=False)

@app.route("/users/<name>/update", methods=["POST"])
@admin_required
def user_update(name):
    d = db(); f = request.form; action = f.get("action")
    if action == "role":
        d.execute("UPDATE users SET role=? WHERE username=?", (f.get("role_custom") or f["role"], name))
    elif action == "reset" and len(f.get("password", "")) >= 6:
        d.execute("UPDATE users SET pw_hash=? WHERE username=?", (generate_password_hash(f["password"]), name))
    elif action == "toggle" and name != session["user"]:
        d.execute("UPDATE users SET active=1-active WHERE username=?", (name,))
    elif action == "admin" and name != session["user"]:
        d.execute("UPDATE users SET is_admin=1-is_admin WHERE username=?", (name,))
    d.commit(); return redirect(url_for("users"))

@app.route("/sw.js")
def sw():
    r = send_from_directory(os.path.join(BASE, "static"), "sw.js")
    r.headers["Service-Worker-Allowed"] = "/"; r.headers["Cache-Control"] = "no-cache"; return r

@app.route("/offline")
def offline():
    return "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><body style='font-family:sans-serif;text-align:center;padding:60px 20px;color:#1c2430'><h2>You're offline</h2><p>Rainflow needs a connection. Try again when you're back online.</p><a href='/' style='color:#2f7de1'>Retry</a>"

@app.route("/login", methods=["GET", "POST"])
def login():
    if not users_exist():
        return redirect(url_for("setup"))
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE username=?", (request.form["username"],)).fetchone()
        if u and u["active"] and check_password_hash(u["pw_hash"], request.form["password"]):
            session["user"], session["role"], session["is_admin"] = u["username"], u["role"], bool(u["is_admin"])
            session.setdefault("pid", 1)
            prev = db().execute("SELECT last_login FROM seen WHERE username=? AND project_id=?", (u["username"], 1)).fetchone()
            session["prev_login"] = prev["last_login"] if prev and prev["last_login"] else None
            db().execute("INSERT INTO seen(username,project_id,last_login) VALUES(?,?,?) "
                         "ON CONFLICT(username,project_id) DO UPDATE SET last_login=excluded.last_login", (u["username"], 1, now()))
            db().commit()
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Wrong username or password")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.route("/project/<int:pid>")
@login_required
def switch_project(pid):
    session["pid"] = pid
    return redirect(request.referrer or url_for("dashboard"))

def pid():
    return session.get("pid", 1)

# ------------------------------------------------------------- dashboard ----
@app.route("/")
@login_required
def dashboard():
    d, p = db(), pid()
    stats = dict(
        messages=d.execute("SELECT COUNT(*) FROM messages WHERE project_id=?", (p,)).fetchone()[0],
        calls=d.execute("SELECT COUNT(*) FROM calls WHERE project_id=?", (p,)).fetchone()[0],
        docs=d.execute("SELECT COUNT(*) FROM documents WHERE project_id=?", (p,)).fetchone()[0],
        hardware=d.execute("SELECT COUNT(*) FROM hardware WHERE project_id=?", (p,)).fetchone()[0],
        tests_pass=d.execute("SELECT COUNT(*) FROM tests WHERE project_id=? AND passed=1", (p,)).fetchone()[0],
        tests_fail=d.execute("SELECT COUNT(*) FROM tests WHERE project_id=? AND passed=0", (p,)).fetchone()[0],
        research_open=d.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND status!='done' AND parent_id IS NULL", (p,)).fetchone()[0],
        research_done=d.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND status='done' AND parent_id IS NULL", (p,)).fetchone()[0],
    )
    recent_msgs = d.execute("SELECT * FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 5", (p,)).fetchall()
    recent_calls = d.execute("SELECT * FROM calls WHERE project_id=? ORDER BY id DESC LIMIT 5", (p,)).fetchall()
    due_reminders()
    tasks = d.execute("SELECT *, assignee AS owner FROM tasks WHERE project_id=? AND status!='done' AND parent_id IS NULL ORDER BY "
                      "CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END LIMIT 8", (p,)).fetchall()
    agreement = d.execute("SELECT * FROM agreements ORDER BY version DESC LIMIT 1").fetchone()
    ms = d.execute("SELECT status, amount FROM milestones WHERE project_id=?", (p,)).fetchall()
    ms_total = len(ms); ms_done = sum(1 for m in ms if m["status"] in ("delivered", "paid"))
    amt_total = sum(m["amount"] or 0 for m in ms); amt_paid = sum(m["amount"] or 0 for m in ms if m["status"] == "paid")
    tests_total = stats["tests_pass"] + stats["tests_fail"]
    pulse = dict(
        tests=round(100 * stats["tests_pass"] / tests_total) if tests_total else None,
        milestones=round(100 * ms_done / ms_total) if ms_total else None, ms_done=ms_done, ms_total=ms_total,
        paid=round(100 * amt_paid / amt_total) if amt_total else None, amt_paid=amt_paid, amt_total=amt_total,
        research=round(100 * stats["research_done"] / (stats["research_done"] + stats["research_open"])) if (stats["research_done"] + stats["research_open"]) else None,
    )
    low = d.execute("SELECT COUNT(*) FROM hardware WHERE project_id=? AND min_qty>0 AND qty<=min_qty", (p,)).fetchone()[0]
    fw = d.execute("SELECT version, status FROM firmware WHERE project_id=? ORDER BY id DESC LIMIT 1", (p,)).fetchone()
    members = all_users()
    activity = d.execute("SELECT * FROM activity WHERE project_id=? ORDER BY id DESC LIMIT 6", (p,)).fetchall()
    try:
        devs = d.execute("SELECT name,last_seen,last_data FROM devices WHERE project_id=? AND enabled=1", (p,)).fetchall()
        import json as _j
        devices = [dict(name=x["name"], online=bool(x["last_seen"]) and (datetime.datetime.now() - datetime.datetime.strptime(x["last_seen"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 600,
                        last=_j.loads(x["last_data"]) if x["last_data"] else {}) for x in devs]
    except Exception:
        devices = []
    return render_template("dashboard.html", stats=stats, recent_msgs=recent_msgs, recent_calls=recent_calls,
                           tasks=tasks, agreement=agreement, pulse=pulse, low=low, fw=fw, members=members, activity=activity, devices=devices)

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

def save_upload(f):
    """Save an uploaded file; return (stored_name, original_name)."""
    orig = f.filename
    fn = f"{uuid.uuid4().hex[:8]}_{secure_filename(orig) or 'file'}"
    f.save(os.path.join(UPLOADS, fn))
    return fn, orig

@app.template_filter("is_image")
def is_image(fn):
    return bool(fn) and os.path.splitext(fn)[1].lower() in IMG_EXT

# ------------------------------------------------------------------ chat ----
@app.route("/chat")
@login_required
def chat():
    q = request.args.get("q", "").strip()
    if q:
        msgs = db().execute("SELECT * FROM messages WHERE project_id=? AND body LIKE ? ORDER BY id",
                            (pid(), f"%{q}%")).fetchall()
    else:
        # lazy loading: only the latest 50 messages; older ones load on scroll-up via /chat/older
        msgs = db().execute("SELECT * FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 50", (pid(),)).fetchall()[::-1]
    pinned = db().execute("SELECT * FROM messages WHERE project_id=? AND pinned=1 AND deleted=0 ORDER BY id", (pid(),)).fetchall()
    byid = _quotes(msgs)
    last = db().execute("SELECT MAX(id) FROM messages WHERE project_id=?", (pid(),)).fetchone()[0] or 0
    _mark_seen(last)
    has_older = bool(msgs) and db().execute("SELECT 1 FROM messages WHERE project_id=? AND id<? LIMIT 1", (pid(), msgs[0]["id"])).fetchone() is not None
    return render_template("chat.html", msgs=msgs, q=q, pinned=pinned, byid=byid, members=all_users(), has_older=has_older, last_id=last)

def _quotes(msgs):
    ids = [m["reply_to"] for m in msgs if m["reply_to"]]
    if not ids: return {}
    rows = db().execute(f"SELECT * FROM messages WHERE id IN ({','.join('?'*len(ids))})", ids).fetchall()
    return {r["id"]: r for r in rows}

@app.route("/chat/older")
@login_required
def chat_older():
    before = int(request.args.get("before", 0))
    rows = db().execute("SELECT * FROM messages WHERE project_id=? AND id<? ORDER BY id DESC LIMIT 50", (pid(), before)).fetchall()[::-1]
    q = _quotes(rows)
    has_more = bool(rows) and db().execute("SELECT 1 FROM messages WHERE project_id=? AND id<? LIMIT 1", (pid(), rows[0]["id"])).fetchone() is not None
    return jsonify(msgs=[dict(r, is_image=is_image(r["filename"])) for r in rows], has_more=has_more,
                   quotes={k: dict(v) for k, v in q.items()})

def _mark_seen(last_id):
    db().execute("INSERT INTO seen(username,project_id,last_msg_id) VALUES(?,?,?) "
                 "ON CONFLICT(username,project_id) DO UPDATE SET last_msg_id=MAX(seen.last_msg_id, excluded.last_msg_id)",
                 (session["user"], pid(), last_id))
    db().commit()

@app.route("/chat/send", methods=["POST"])
@login_required
def chat_send():
    """Send a text message and/or a file / image / code file."""
    body = request.form.get("body", "").strip()
    fn = orig = None
    f = request.files.get("file")
    if f and f.filename:
        fn, orig = save_upload(f)
    if not body and not fn:
        return jsonify(ok=False), 400
    kind = request.form.get("kind", "voice" if (orig or "").startswith("voice_") else "text")
    reply_to = request.form.get("reply_to") or None
    cur = db().execute("INSERT INTO messages(project_id,sender,body,ts,filename,orig_name,reply_to,kind) VALUES(?,?,?,?,?,?,?,?)",
                       (pid(), session["user"], body, now(), fn, orig, reply_to, kind))
    db().commit(); _mark_seen(cur.lastrowid)
    for u in all_users():
        if f"@{u}" in body and u != session["user"]:
            log_activity("mentioned", f"@{u} in chat: {body[:60]}", "/chat")
    # push to members who are not actively online (browser closed / phone locked)
    from pm import push
    preview = body[:90] if body else ("📎 " + (orig or "file"))
    push([u for u in all_users() if u != session["user"]], session["user"], preview, "/chat")
    return jsonify(ok=True, id=cur.lastrowid)

@app.route("/chat/<int:mid>/<action>", methods=["POST"])
@login_required
def chat_action(mid, action):
    m = db().execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if not m: return jsonify(ok=False), 404
    if action == "delete" and m["sender"] == session["user"]:
        db().execute("UPDATE messages SET deleted=1, body='', filename=NULL WHERE id=?", (mid,))
    elif action == "pin":
        db().execute("UPDATE messages SET pinned=1-pinned WHERE id=?", (mid,))
        log_activity("pinned message" if not m["pinned"] else "unpinned message", (m["body"] or m["orig_name"] or "")[:60], "/chat")
    else:
        return jsonify(ok=False), 400
    db().commit(); return jsonify(ok=True)

@app.route("/chat/voice", methods=["POST"])
@login_required
def chat_voice():
    f = request.files["file"]
    fn = f"voice_{uuid.uuid4().hex[:8]}.webm"
    f.save(os.path.join(UPLOADS, fn))
    cur = db().execute("INSERT INTO messages(project_id,sender,body,ts,filename,orig_name,kind) VALUES(?,?,?,?,?,?,?)",
                       (pid(), session["user"], "", now(), fn, "voice note", "voice"))
    db().commit(); _mark_seen(cur.lastrowid)
    return jsonify(ok=True)

@app.route("/chat/<int:mid>/edit", methods=["POST"])
@login_required
def chat_edit(mid):
    """Edit your own message; the previous text is kept and visible to both."""
    m = db().execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    new = (request.form.get("body") or "").strip()
    if not m or m["sender"] != session["user"] or not new or new == m["body"]:
        return jsonify(ok=False), 400
    db().execute("INSERT INTO message_history(message_id,body,ts) VALUES(?,?,?)", (mid, m["body"], m["ts"]))
    db().execute("UPDATE messages SET body=?, edited=edited+1 WHERE id=?", (new, mid))
    db().commit()
    return jsonify(ok=True)

@app.route("/chat/<int:mid>/history")
@login_required
def chat_history(mid):
    rows = db().execute("SELECT body, ts FROM message_history WHERE message_id=? ORDER BY id DESC", (mid,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/chat/poll")
@login_required
def chat_poll():
    """Return messages newer than ?after=<id> (client polls every 1.5 s)."""
    from pm import touch_presence; touch_presence()
    after = int(request.args.get("after", 0))
    rows = db().execute("SELECT * FROM messages WHERE project_id=? AND id>? ORDER BY id",
                        (pid(), after)).fetchall()
    # also report recently edited messages so the other side sees edits live
    edited = db().execute("SELECT id, body, edited, deleted, pinned FROM messages WHERE project_id=? AND (edited>0 OR deleted=1 OR pinned=1) AND id<=?",
                          (pid(), after)).fetchall()
    if rows: _mark_seen(rows[-1]["id"])
    byid = {}
    for r in rows:
        if r["reply_to"]:
            q = db().execute("SELECT sender, body, orig_name FROM messages WHERE id=?", (r["reply_to"],)).fetchone()
            if q: byid[r["reply_to"]] = dict(q)
    return jsonify(new=[dict(r, is_image=is_image(r["filename"])) for r in rows],
                   edited=[dict(r) for r in edited], quotes=byid)

# ---- WebRTC signaling: server only relays offer/answer/ICE between the two peers
@app.route("/rtc-config")
@participant_required
def rtc_config():
    """ICE servers for WebRTC. Set TURN_URL / TURN_USER / TURN_PASS in the environment
    (systemd unit) to make calls work across strict NATs and mobile data."""
    servers = [{"urls": "stun:stun.l.google.com:19302"}]
    if os.environ.get("TURN_URL"):
        servers.append({"urls": os.environ["TURN_URL"].split(","),
                        "username": os.environ.get("TURN_USER", ""), "credential": os.environ.get("TURN_PASS", "")})
    return jsonify(iceServers=servers)

@app.route("/signal", methods=["POST"])
@participant_required
def signal_send():
    m = request.get_json(); m["from"] = ident()
    import time as _t
    with SIG_LOCK:
        SIG_SEQ[0] += 1; m["seq"] = SIG_SEQ[0]
        SIGNALS[m["to"]].append((SIG_SEQ[0], _t.time(), m))
    if m.get("type") == "offer":
        from pm import push
        if "user" in session and not m.get("room"):
            push([m["to"]], f"📞 Incoming {m.get('kind','video')} call", f"{session['user']} is calling you — tap to answer", "/chat", urgent=True, even_if_online=True)
    return jsonify(ok=True)

@app.route("/signal/poll")
@participant_required
def signal_poll():
    """Return this user's signals with seq > ?since. Nothing is deleted on read;
    entries expire after 120 s. A fresh tab (since=0) only gets the last 30 s."""
    import time as _t
    if "user" in session:
        from pm import touch_presence; touch_presence()
    me = ident()
    since = int(request.args.get("since", 0)); now_t = _t.time()
    with SIG_LOCK:
        q = [x for x in SIGNALS[me] if now_t - x[1] < 120]
        SIGNALS[me] = q
        out = [m for seq, t, m in q if (now_t - t < 30) if since == 0] if since == 0 else [m for seq, t, m in q if seq > since]
        top = q[-1][0] if q else since
    return jsonify(msgs=out, seq=max(top, since))

# ----------------------------------------------------------------- calls ----
@app.route("/calls")
@login_required
def calls():
    rows = db().execute("SELECT * FROM calls WHERE project_id=? ORDER BY id DESC", (pid(),)).fetchall()
    missed_rows = [c for c in rows if c["callee"] == session["user"] and c["status"] in ("missed", "missed_seen")]
    import glob as _g
    recs = {}
    for c in rows:
        files = sorted(_g.glob(os.path.join(RECORDINGS, f"call_{c['id']}_*.webm")))
        recs[c["id"]] = [(os.path.basename(f), os.path.basename(f)[len(f"call_{c['id']}_"):-5].replace("guest_", "guest ").replace("_", " ") + f" · {os.path.getsize(f)/1048576:.1f} MB") for f in files if os.path.getsize(f) > 0]
    return render_template("calls.html", calls=rows, missed_rows=missed_rows, recs=recs)

@app.route("/call/start", methods=["POST"])
@login_required
def call_start():
    d = request.get_json()
    cur = db().execute("INSERT INTO calls(project_id,caller,callee,started,kind) VALUES(?,?,?,?,?)",
                       (pid(), session["user"], d["callee"], now(), d.get("kind", "video")))
    db().commit(); log_activity("started a call", f"{d.get('kind','video')} call to {d['callee']}", "/calls")
    return jsonify(id=cur.lastrowid)

@app.route("/call/<int:cid>/status/<status>", methods=["POST"])
@login_required
def call_status(cid, status):
    if status in ("answered", "missed", "rejected"):
        db().execute("UPDATE calls SET status=? WHERE id=? AND status='ringing'", (status, cid))
        if status == "missed":
            db().execute("UPDATE calls SET ended=? WHERE id=? AND ended IS NULL", (now(), cid))
        db().commit()
    return jsonify(ok=True)

@app.route("/calls/clear_missed", methods=["POST"])
@login_required
def clear_missed():
    db().execute("UPDATE calls SET status='missed_seen' WHERE callee=? AND status='missed'", (session["user"],))
    db().commit(); return redirect(url_for("calls"))

def _rec_name(cid):
    return f"call_{cid}_{secure_filename(ident().replace(':', '_').replace(' ', '_'))}.webm"

@app.route("/call/<int:cid>/chunk", methods=["POST"])
@participant_required
def call_chunk(cid):
    """Server-side recording: the browser streams 2-second chunks here during the call,
    so the server keeps the recording even if a phone dies or the tab closes."""
    fn = _rec_name(cid)
    with open(os.path.join(RECORDINGS, fn), "ab") as out:
        out.write(request.get_data())
    c = db().execute("SELECT caller FROM calls WHERE id=?", (cid,)).fetchone()
    col = "recording" if c and c["caller"] == ident() else "recording2"
    db().execute(f"UPDATE calls SET {col}=? WHERE id=?", (fn, cid)); db().commit()
    return jsonify(ok=True)

@app.route("/call/<int:cid>/end", methods=["POST"])
@participant_required
def call_end(cid):
    db().execute("UPDATE calls SET ended=?, status=CASE WHEN status='ringing' THEN 'missed' ELSE status END WHERE id=? AND ended IS NULL", (now(), cid)); db().commit()
    return jsonify(ok=True)

@app.route("/call/<int:cid>/notes", methods=["POST"])
@login_required
def call_notes(cid):
    db().execute("UPDATE calls SET notes=? WHERE id=?", (request.form["notes"], cid)); db().commit()
    return redirect(url_for("calls"))

@app.route("/recordings/<path:fn>")
@login_required
def recording(fn):
    r = send_from_directory(RECORDINGS, fn, mimetype="video/webm", conditional=True)
    r.headers["Accept-Ranges"] = "bytes"; return r

# -------------------------------------------------------------- activity ----
@app.route("/activity")
@login_required
def activity():
    page = int(request.args.get("page", 1)); per = 40
    rows = db().execute("SELECT * FROM activity WHERE project_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (pid(), per + 1, (page - 1) * per)).fetchall()
    has_more = len(rows) > per; rows = rows[:per]
    # group by day
    days = []
    for r in rows:
        if not days or days[-1][0] != r["ts"][:10]: days.append((r["ts"][:10], []))
        days[-1][1].append(r)
    return render_template("activity.html", days=days, page=page, has_more=has_more, since=session.get("prev_login"))

# ---------------------------------------------------------- meeting rooms ----
# Group calls with a waiting room. Members join directly; guests (no account)
# join by link after the host admits them. Mesh WebRTC, every participant records.
ROOMS = {}          # token -> {"host": str, "call_id": int, "members": {ident: {...}}, "title": str}
ROOM_LOCK = threading.Lock()

def _room(token):
    r = ROOMS.get(token)
    if not r: abort(404)
    return r

def _is_host(r):
    return session.get("user") and (session["user"] == r["host"] or session.get("is_admin"))

@app.route("/meet/new", methods=["POST"])
@login_required
def meet_new():
    token = uuid.uuid4().hex[:12]
    title = request.form.get("title") or request.args.get("title") or f"{session['user']}'s meeting"
    cur = db().execute("INSERT INTO calls(project_id,caller,callee,started,kind,status) VALUES(?,?,?,?,?,?)",
                       (pid(), session["user"], "room", now(), "meeting", "answered"))
    db().commit()
    with ROOM_LOCK:
        ROOMS[token] = {"host": session["user"], "call_id": cur.lastrowid, "members": {}, "title": title, "created": now()}
    log_activity("started a meeting", title, f"/meet/{token}")
    # tell the person we were in a 1:1 call with (if any) to move into the room
    peer = request.form.get("peer") or request.args.get("peer")
    if peer:
        import time as _t
        with SIG_LOCK:
            SIG_SEQ[0] += 1
            SIGNALS[peer].append((SIG_SEQ[0], _t.time(), {"type": "moveroom", "from": session["user"], "token": token, "seq": SIG_SEQ[0], "to": peer}))
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(token=token, url=url_for("meet", token=token, _external=True))
    return redirect(url_for("meet", token=token))

@app.route("/meet/<token>")
def meet(token):
    r = ROOMS.get(token)
    if not r:
        return render_template("meet_guest.html", token=token, gone=True), 410
    if "user" in session:
        return render_template("meet.html", token=token, room=r, me=session["user"], is_host=bool(_is_host(r)), guest=False)
    gid = session.get("guest_id")
    if gid and gid.startswith(f"guest:{token}:"):
        return render_template("meet.html", token=token, room=r, me=gid, is_host=False, guest=True,
                               guest_name=gid.split(":", 2)[2])
    return render_template("meet_guest.html", token=token, room=r, gone=False)

@app.route("/meet/<token>/guest", methods=["POST"])
def meet_guest(token):
    r = _room(token)
    name = re.sub(r"[^\w .\-]", "", request.form.get("name", "").strip())[:30].strip() or "guest"
    session.clear(); session["guest_id"] = f"guest:{token}:{name}"
    with ROOM_LOCK:
        r["members"][session["guest_id"]] = {"name": name, "role": "guest", "status": "waiting", "seen": now(), "t": __import__("time").time()}
    return redirect(url_for("meet", token=token))

@app.route("/meet/<token>/join", methods=["POST"])
@participant_required
def meet_join(token):
    r = _room(token); me = ident()
    with ROOM_LOCK:
        m = r["members"].get(me)
        if "user" in session:
            r["members"][me] = {"name": session["user"], "role": "host" if _is_host(r) else "member", "status": "admitted",
                                "seen": now(), "t": __import__("time").time()}
        elif m:
            m["seen"] = now(); m["t"] = __import__("time").time()
        else:
            return jsonify(ok=False), 403
    return _roster(r, me)

def _roster(r, me):
    import time as _t
    with ROOM_LOCK:
        for k in [k for k, v in r["members"].items() if _t.time() - v["t"] > 15]:
            r["members"].pop(k, None)           # dropped off
        mine = r["members"].get(me, {})
        host = _is_host(r)
        people = [dict(id=k, name=v["name"], role=v["role"], status=v["status"]) for k, v in r["members"].items()
                  if v["status"] == "admitted" or host]
    return jsonify(me=me, status=mine.get("status", "waiting"), is_host=bool(host), call_id=r["call_id"],
                   title=r["title"], people=people)

@app.route("/meet/<token>/roster")
@participant_required
def meet_roster(token):
    r = _room(token); me = ident()
    with ROOM_LOCK:
        if me in r["members"]:
            r["members"][me]["t"] = __import__("time").time()
    return _roster(r, me)

@app.route("/meet/<token>/admit", methods=["POST"])
@login_required
def meet_admit(token):
    r = _room(token)
    if not _is_host(r): return jsonify(ok=False), 403
    j = request.get_json(); gid = j.get("id"); allow = bool(j.get("allow", True))
    import time as _t
    with ROOM_LOCK:
        m = r["members"].get(gid)
        if m:
            if allow: m["status"] = "admitted"
            else: r["members"].pop(gid, None)
        SIG_SEQ[0] += 1
        SIGNALS[gid].append((SIG_SEQ[0], _t.time(), {"type": "admitted" if allow else "denied", "from": session["user"], "to": gid, "seq": SIG_SEQ[0]}))
    if m and allow: log_activity("admitted guest to meeting", m["name"], f"/meet/{token}")
    return jsonify(ok=True)

@app.route("/meet/<token>/leave", methods=["POST"])
@participant_required
def meet_leave(token):
    r = ROOMS.get(token); me = ident()
    if r:
        with ROOM_LOCK:
            r["members"].pop(me, None)
            empty = not r["members"]
        if empty:
            db().execute("UPDATE calls SET ended=? WHERE id=? AND ended IS NULL", (now(), r["call_id"])); db().commit()
    if session.get("guest_id"):
        session.clear()
    return jsonify(ok=True)

# ------------------------------------------------------------- documents ----
@app.route("/docs", methods=["GET", "POST"])
@login_required
def docs():
    if request.method == "POST":
        fn = None
        f = request.files.get("file")
        if f and f.filename:
            fn = f"{uuid.uuid4().hex[:8]}_{secure_filename(f.filename)}"
            f.save(os.path.join(UPLOADS, fn))
        db().execute("INSERT INTO documents(project_id,title,body,filename,uploaded_by,ts) VALUES(?,?,?,?,?,?)",
                     (pid(), request.form["title"], request.form.get("body", ""), fn, session["user"], now()))
        log_activity("added document", request.form["title"], "/docs"); db().commit()
        return redirect(url_for("docs"))
    rows = db().execute("SELECT * FROM documents WHERE project_id=? ORDER BY id DESC", (pid(),)).fetchall()
    return render_template("docs.html", docs=rows)

@app.route("/docs/<int:did>/delete", methods=["POST"])
@login_required
def doc_delete(did):
    db().execute("DELETE FROM documents WHERE id=?", (did,)); db().commit()
    return redirect(url_for("docs"))

@app.route("/uploads/<path:fn>")
@login_required
def uploaded(fn):
    return send_from_directory(UPLOADS, fn)

# ------------------------------------------------------------- agreement ----
@app.route("/agreement", methods=["GET", "POST"])
@login_required
def agreement():
    d = db()
    if request.method == "POST":
        if "accept" in request.form:
            a = d.execute("SELECT * FROM agreements ORDER BY version DESC LIMIT 1").fetchone()
            acc = set(filter(None, a["accepted_by"].split(","))); acc.add(session["user"])
            d.execute("UPDATE agreements SET accepted_by=? WHERE id=?", (",".join(sorted(acc)), a["id"]))
            d.execute("INSERT OR REPLACE INTO acceptances(agreement_id,username,ts) VALUES(?,?,?)", (a["id"], session["user"], now()))
            log_activity("accepted agreement", f"v{a['version']}", "/agreement")
        else:
            v = (d.execute("SELECT MAX(version) FROM agreements").fetchone()[0] or 0) + 1
            d.execute("INSERT INTO agreements(version,body,author,ts) VALUES(?,?,?,?)",
                      (v, request.form["body"], session["user"], now()))
            log_activity("wrote agreement", f"v{v}", "/agreement")
        d.commit(); return redirect(url_for("agreement"))
    versions = d.execute("SELECT * FROM agreements ORDER BY version DESC").fetchall()
    current = versions[0] if versions else None
    # parties = active members (guests excluded); who signed which version, when
    parties = d.execute("SELECT username, role FROM users WHERE active=1 AND role!='Guest' ORDER BY rowid").fetchall()
    signed = {}
    if current:
        signed = {r["username"]: r["ts"] for r in d.execute("SELECT username, ts FROM acceptances WHERE agreement_id=?", (current["id"],))}
        for u in filter(None, current["accepted_by"].split(",")):   # older rows without timestamp
            signed.setdefault(u, current["ts"])
    hist = {v["id"]: {r["username"]: r["ts"] for r in d.execute("SELECT username, ts FROM acceptances WHERE agreement_id=?", (v["id"],))} for v in versions}
    return render_template("agreement.html", versions=versions, current=current, parties=parties, signed=signed, hist=hist,
                           clauses=parse_clauses(current["body"]) if current else [])

def parse_clauses(body):
    """Turn free text into sections/clauses: '## Title' starts a section; numbered/bulleted lines or
    blank-line paragraphs are clauses. Returns [{title, items:[text,...]}]."""
    import re as _re
    sections = []; cur = {"title": "", "items": []}; buf = []
    def flush():
        if buf:
            cur["items"].append(" ".join(x.strip() for x in buf)); buf.clear()
    for line in (body or "").splitlines():
        st = line.strip()
        if st.startswith("#"):
            flush()
            if cur["items"] or cur["title"]: sections.append(cur)
            cur = {"title": st.lstrip("#").strip(), "items": []}; continue
        if not st:
            flush(); continue
        m = _re.match(r"^(\d+[\.\)]|[-*•]|[a-z][\.\)])\s+(.*)$", st, _re.I)
        if m:
            flush(); buf.append(m.group(2))
        else:
            buf.append(st)
    flush()
    if cur["items"] or cur["title"]: sections.append(cur)
    return sections

# -------------------------------------------------------------- hardware ----
HW_FIELDS = ["name", "part_no", "qty", "supplier", "cost", "status", "notes", "datasheet", "min_qty", "category", "value", "package", "refs", "per_board"]

@app.route("/hardware", methods=["GET", "POST"])
@login_required
def hardware():
    d = db()
    if request.method == "POST":
        f = request.form
        if "boards" in f:                                   # number of boards to build
            d.execute("UPDATE projects SET boards=? WHERE id=?", (max(1, int(f["boards"] or 1)), pid())); d.commit()
            return redirect(url_for("hardware"))
        if f.get("quick"):                                  # quick-add lines
            from hw import parse_quick
            n = 0
            for line in f["quick"].splitlines():
                if not line.strip(): continue
                q = parse_quick(line)
                d.execute("INSERT INTO hardware(project_id,name,category,value,package,refs,per_board,qty,status,updated_by,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (pid(), q["name"], q["category"], q["value"], q["package"], q["refs"], q["per_board"], 0, "planned", session["user"], now())); n += 1
            log_activity("added parts", f"{n} via quick-add", "/hardware"); d.commit(); flash(f"{n} parts added")
            return redirect(url_for("hardware"))
        if request.files.get("csv") and request.files["csv"].filename:   # CSV import (KiCad/any): header row with names
            import csv, io
            rdr = csv.DictReader(io.StringIO(request.files["csv"].read().decode("utf-8-sig", errors="ignore")))
            n = 0
            for row in rdr:
                g = lambda *keys: next((row[k] for k in row if k and k.strip().lower().replace(" ", "_") in keys and row[k]), "")
                name = g("name", "part", "description", "comment", "mpn") or g("value")
                if not name: continue
                d.execute("INSERT INTO hardware(project_id,name,category,value,package,refs,per_board,qty,cost,supplier,part_no,status,updated_by,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (pid(), name, g("category") or "Other", g("value"), g("footprint", "package"), g("refs", "reference", "designator", "references"),
                           int(float(g("qty_per_board", "quantity", "qty") or 1)), 0, float(g("cost", "price", "unit_price") or 0), g("supplier", "vendor"), g("part_no", "mpn", "sku"),
                           "planned", session["user"], now())); n += 1
            log_activity("imported BOM", f"{n} rows", "/hardware"); d.commit(); flash(f"Imported {n} parts")
            return redirect(url_for("hardware"))
        vals = [f.get(k, "") or (0 if k in ("qty", "min_qty") else 1 if k == "per_board" else "") for k in HW_FIELDS]
        photo = None
        up = request.files.get("photo")
        if up and up.filename: photo, _ = save_upload(up)
        d.execute(f"INSERT INTO hardware(project_id,{','.join(HW_FIELDS)},photo,updated_by,ts) "
                  f"VALUES(?,{','.join('?'*len(HW_FIELDS))},?,?,?)", (pid(), *vals, photo, session["user"], now()))
        log_activity("added hardware", f["name"], "/hardware"); d.commit()
        return redirect(url_for("hardware"))
    rows = d.execute("SELECT * FROM hardware WHERE project_id=? ORDER BY category, name", (pid(),)).fetchall()
    boards = (d.execute("SELECT boards FROM projects WHERE id=?", (pid(),)).fetchone() or {"boards": 1})["boards"] or 1
    groups = {}
    for r in rows: groups.setdefault(r["category"] or "Other", []).append(r)
    per_board_cost = sum((r["cost"] or 0) * (r["per_board"] or 1) for r in rows)
    need = {r["id"]: (r["per_board"] or 1) * boards for r in rows}
    short = [r for r in rows if (r["qty"] or 0) < need[r["id"]]]
    low = [r for r in rows if (r["min_qty"] or 0) > 0 and (r["qty"] or 0) <= (r["min_qty"] or 0)]
    designs_using = {}
    try:
        for x in d.execute("SELECT DISTINCT p.hardware_id, ds.id, ds.title FROM design_pins p JOIN design_versions v ON v.id=p.version_id JOIN designs ds ON ds.id=v.design_id WHERE p.hardware_id IS NOT NULL"):
            designs_using.setdefault(x["hardware_id"], []).append((x["id"], x["title"]))
    except Exception: pass
    return render_template("hardware.html", items=rows, bom_groups=groups, boards=boards, per_board_cost=per_board_cost,
                           total=per_board_cost * boards, need=need, short=short, low=low, designs_using=designs_using)

@app.route("/hardware/export")
@login_required
def hardware_export():
    """BOM export to Excel (falls back to CSV if openpyxl is missing)."""
    rows = db().execute("SELECT * FROM hardware WHERE project_id=? ORDER BY id", (pid(),)).fetchall()
    cols = ["category", "refs", "name", "value", "package", "part_no", "per_board", "qty", "cost", "supplier", "status", "datasheet", "notes"]
    pname = db().execute("SELECT name FROM projects WHERE id=?", (pid(),)).fetchone()["name"]
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook(); ws = wb.active; ws.title = "BOM"
        ws.append([c.replace("_", " ").title() for c in cols] + ["Line total"])
        for c in ws[1]: c.font = Font(bold=True)
        for r in rows:
            ws.append([r[c] for c in cols] + [(r["cost"] or 0) * (r["per_board"] or 1)])
        ws.append([]); ws.append(["TOTAL"] + [""] * (len(cols) - 1) + [f"=SUM(N2:N{len(rows)+1})"])
        for col, w in zip("ABCDEFGHIJKLMN", (22, 12, 28, 16, 10, 16, 8, 6, 10, 16, 12, 28, 28, 12)): ws.column_dimensions[col].width = w
        path = os.path.join(UPLOADS, f"BOM_{secure_filename(pname)}.xlsx"); wb.save(path)
        return send_from_directory(UPLOADS, os.path.basename(path), as_attachment=True)
    except ImportError:
        import csv, io
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(cols)
        for r in rows: w.writerow([r[c] for c in cols])
        from flask import Response
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=BOM_{secure_filename(pname)}.csv"})

@app.route("/hardware/<int:hid>", methods=["GET", "POST"])
@login_required
def hardware_item(hid):
    d = db()
    item = d.execute("SELECT * FROM hardware WHERE id=?", (hid,)).fetchone() or abort(404)
    if request.method == "POST":
        # snapshot old version
        snap = " | ".join(f"{k}={item[k]}" for k in HW_FIELDS)
        d.execute("INSERT INTO hardware_history(hardware_id,version,snapshot,changed_by,ts) VALUES(?,?,?,?,?)",
                  (hid, item["version"], snap, item["updated_by"], item["ts"]))
        vals = [request.form.get(k, "") or (0 if k in ("qty","min_qty") else "") for k in HW_FIELDS]
        d.execute(f"UPDATE hardware SET {','.join(k+'=?' for k in HW_FIELDS)},version=version+1,updated_by=?,ts=? WHERE id=?",
                  (*vals, session["user"], now(), hid))
        f = request.files.get("photo")
        if f and f.filename:
            photo, _ = save_upload(f); d.execute("UPDATE hardware SET photo=? WHERE id=?", (photo, hid))
        log_activity("updated hardware", f"{item['name']} → v{item['version']+1}", f"/hardware/{hid}")
        d.commit(); return redirect(url_for("hardware_item", hid=hid))
    history = d.execute("SELECT * FROM hardware_history WHERE hardware_id=? ORDER BY version DESC", (hid,)).fetchall()
    tests = d.execute("SELECT * FROM tests WHERE hardware_id=? ORDER BY id DESC", (hid,)).fetchall()
    return render_template("hardware_item.html", item=item, history=history, tests=tests)

# ----------------------------------------------------------------- tests ----
@app.route("/tests", methods=["GET", "POST"])
@login_required
def tests():
    d = db()
    if request.method == "POST":
        cur = d.execute("INSERT INTO tests(project_id,hardware_id,title,procedure,result,passed,tested_by,ts,firmware_id) VALUES(?,?,?,?,?,?,?,?,?)",
                  (pid(), request.form.get("hardware_id") or None, request.form["title"],
                   request.form.get("procedure", ""), request.form.get("result", ""),
                   1 if request.form.get("passed") == "1" else 0, session["user"], now(),
                   request.form.get("firmware_id") or None))
        for f in request.files.getlist("files"):
            if f and f.filename:
                fn, orig = save_upload(f)
                d.execute("INSERT INTO test_files(test_id,filename,orig_name) VALUES(?,?,?)", (cur.lastrowid, fn, orig))
        log_activity("logged test", f"{request.form['title']} — {'PASS' if request.form.get('passed')=='1' else 'FAIL'}", "/tests")
        d.commit(); return redirect(url_for("tests"))
    rows = d.execute("SELECT t.*, h.name AS hw_name, f.version AS fw_version FROM tests t "
                     "LEFT JOIN hardware h ON h.id=t.hardware_id LEFT JOIN firmware f ON f.id=t.firmware_id "
                     "WHERE t.project_id=? ORDER BY t.id DESC", (pid(),)).fetchall()
    hw = d.execute("SELECT id,name FROM hardware WHERE project_id=?", (pid(),)).fetchall()
    fw = d.execute("SELECT id,version FROM firmware WHERE project_id=? ORDER BY id DESC", (pid(),)).fetchall()
    tfiles = {}
    for f in d.execute("SELECT * FROM test_files").fetchall():
        tfiles.setdefault(f["test_id"], []).append(f)
    return render_template("tests.html", tests=rows, hw=hw, fw=fw, tfiles=tfiles)

# -------------------------------------------------------------- research ----
@app.route("/_old/research", methods=["GET", "POST"])
@login_required
def research_old():
    d = db()
    if request.method == "POST":
        cur = d.execute("INSERT INTO research(project_id,title,details,owner,priority,due,ts) VALUES(?,?,?,?,?,?,?)",
                  (pid(), request.form["title"], request.form.get("details", ""), request.form.get("owner"),
                   request.form.get("priority", "medium"), request.form.get("due", ""), now()))
        log_activity("added research task", request.form["title"], "/research")
        d.commit(); return redirect(url_for("research_item", rid=cur.lastrowid))
    rows = d.execute("SELECT * FROM research WHERE project_id=? ORDER BY status='done', "
                     "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, due", (pid(),)).fetchall()
    counts = {r["research_id"]: r["n"] for r in d.execute("SELECT research_id, COUNT(*) n FROM research_log GROUP BY research_id")}
    last = {r["research_id"]: r["ts"] for r in d.execute("SELECT research_id, MAX(ts) ts FROM research_log GROUP BY research_id")}
    return render_template("research.html", items=rows, users=all_users(), counts=counts, last=last)

@app.route("/_old/research/<int:rid>")
@login_required
def research_item(rid):
    d = db()
    item = d.execute("SELECT * FROM research WHERE id=?", (rid,)).fetchone() or abort(404)
    log = d.execute("SELECT * FROM research_log WHERE research_id=? ORDER BY id DESC", (rid,)).fetchall()
    legacy = d.execute("SELECT * FROM research_files WHERE research_id=? ORDER BY id DESC", (rid,)).fetchall()
    return render_template("research_item.html", item=item, log=log, legacy=legacy, users=all_users())

@app.route("/research/<int:rid>/log", methods=["POST"])
@login_required
def research_log(rid):
    """Add a dated entry to the research log: text and/or a file, image, voice or video note."""
    d = db(); body = (request.form.get("body") or "").strip()
    f = request.files.get("file"); fn = orig = None; kind = request.form.get("kind", "note")
    if f and f.filename:
        fn, orig = save_upload(f)
        if kind == "note":
            kind = "image" if is_image(fn) else "file"
    if not body and not fn:
        return redirect(url_for("research_item", rid=rid))
    d.execute("INSERT INTO research_log(research_id,author,body,filename,orig_name,kind,ts) VALUES(?,?,?,?,?,?,?)",
              (rid, session["user"], body, fn, orig, kind, now()))
    t = d.execute("SELECT title FROM research WHERE id=?", (rid,)).fetchone()
    log_activity(f"added {kind} to research", t["title"], f"/research/{rid}")
    d.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(url_for("research_item", rid=rid))

@app.route("/research/<int:rid>/log/<int:lid>/delete", methods=["POST"])
@login_required
def research_log_delete(rid, lid):
    db().execute("DELETE FROM research_log WHERE id=? AND research_id=? AND author=?", (lid, rid, session["user"]))
    db().commit(); return redirect(url_for("research_item", rid=rid))

@app.route("/research/<int:rid>/file", methods=["POST"])
@login_required
def research_file(rid):
    return research_log(rid)

@app.route("/research/<int:rid>/update", methods=["POST"])
@login_required
def research_update(rid):
    f = request.form
    db().execute("UPDATE research SET title=?, details=?, decision=?, owner=?, priority=?, due=?, status=? WHERE id=?",
                 (f["title"], f.get("details", ""), f.get("decision", ""), f.get("owner"), f.get("priority", "medium"),
                  f.get("due", ""), f.get("status", "todo"), rid))
    log_activity("updated research", f["title"], f"/research/{rid}"); db().commit()
    return redirect(url_for("research_item", rid=rid))

@app.route("/research/<int:rid>/details", methods=["POST"])
@login_required
def research_details(rid):
    db().execute("UPDATE research SET details=? WHERE id=?", (request.form["details"], rid)); db().commit()
    return redirect(url_for("research_item", rid=rid))

@app.route("/research/<int:rid>/status/<status>", methods=["POST"])
@login_required
def research_status(rid, status):
    if status in ("todo", "doing", "done"):
        db().execute("UPDATE research SET status=? WHERE id=?", (status, rid)); db().commit()
    return redirect(request.referrer or url_for("research"))

# -------------------------------------------------------------- firmware ----
@app.route("/firmware", methods=["GET", "POST"])
@login_required
def firmware():
    d = db()
    if request.method == "POST":
        fn = orig = None
        f = request.files.get("file")
        if f and f.filename:
            fn, orig = save_upload(f)
        d.execute("INSERT INTO firmware(project_id,version,target,notes,filename,orig_name,author,ts,status) VALUES(?,?,?,?,?,?,?,?,?)",
                  (pid(), request.form["version"], request.form.get("target", ""), request.form.get("notes", ""),
                   fn, orig, session["user"], now(), request.form.get("status", "dev")))
        log_activity("released firmware", request.form["version"], "/firmware")
        d.commit(); return redirect(url_for("firmware"))
    rows = d.execute("SELECT * FROM firmware WHERE project_id=? ORDER BY id DESC", (pid(),)).fetchall()
    hw = d.execute("SELECT name FROM hardware WHERE project_id=?", (pid(),)).fetchall()
    return render_template("firmware.html", items=rows, hw=hw)

@app.route("/firmware/<int:fid>/status/<status>", methods=["POST"])
@login_required
def firmware_status(fid, status):
    if status in ("dev", "testing", "stable", "deprecated"):
        db().execute("UPDATE firmware SET status=? WHERE id=?", (status, fid)); db().commit()
    return redirect(url_for("firmware"))

# ------------------------------------------------------------ milestones ----
@app.route("/milestones", methods=["GET", "POST"])
@login_required
def milestones():
    d = db()
    cur = d.execute("SELECT version FROM agreements ORDER BY version DESC LIMIT 1").fetchone()
    if request.method == "POST":
        d.execute("INSERT INTO milestones(project_id,title,details,amount,due,agreement_version,ts) VALUES(?,?,?,?,?,?,?)",
                  (pid(), request.form["title"], request.form.get("details", ""),
                   float(request.form.get("amount") or 0), request.form.get("due", ""),
                   cur["version"] if cur else None, now()))
        log_activity("added milestone", request.form["title"], "/milestones")
        d.commit(); return redirect(url_for("milestones"))
    rows = d.execute("SELECT * FROM milestones WHERE project_id=? ORDER BY due, id", (pid(),)).fetchall()
    total = sum(r["amount"] or 0 for r in rows)
    paid = sum(r["amount"] or 0 for r in rows if r["status"] == "paid")
    return render_template("milestones.html", items=rows, total=total, paid=paid, agreement=cur)

@app.route("/milestones/<int:mid>/update", methods=["POST"])
@login_required
def milestone_update(mid):
    f = request.form
    status = f.get("status")
    if status in ("pending", "in_progress", "delivered", "paid"):
        db().execute("UPDATE milestones SET status=?, paid_on=?, paid_ref=? WHERE id=?",
                     (status, f.get("paid_on", "") if status == "paid" else None,
                      f.get("paid_ref", "") if status == "paid" else None, mid))
        m = db().execute("SELECT title FROM milestones WHERE id=?", (mid,)).fetchone()
        log_activity(f"milestone {status.replace('_',' ')}", m["title"], "/milestones")
        db().commit()
    return redirect(url_for("milestones"))

# ------------------------------------------------------------- pdf export ----
def _pdf(title, lines, meta):
    """Build a simple PDF with reportlab; returns a Flask response.
    If reportlab is missing, returns a print-ready HTML page (browser → Save as PDF)."""
    from flask import Response, make_response
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib import colors
        import io
        buf = io.BytesIO(); doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm)
        st = getSampleStyleSheet(); story = [Paragraph(title, st["Title"])]
        for k, v in meta: story.append(Paragraph(f"<b>{k}:</b> {v}", st["Normal"]))
        story.append(Spacer(1, 8*mm))
        for item in lines:
            if isinstance(item, list):   # table
                t = Table(item, hAlign="LEFT")
                t.setStyle(TableStyle([("GRID", (0,0), (-1,-1), 0.5, colors.grey), ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
                                       ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold")]))
                story.append(t); story.append(Spacer(1, 4*mm))
            else:
                story.append(Paragraph(item.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br/>"), st["Normal"]))
                story.append(Spacer(1, 2*mm))
        doc.build(story)
        r = make_response(buf.getvalue()); r.headers["Content-Type"] = "application/pdf"
        r.headers["Content-Disposition"] = f'inline; filename="{secure_filename(title)}.pdf"'; return r
    except ImportError:
        html = f"<html><body style='font-family:sans-serif;max-width:800px;margin:40px auto'><h1>{title}</h1>"
        html += "".join(f"<p><b>{k}:</b> {v}</p>" for k, v in meta)
        for item in lines:
            if isinstance(item, list):
                html += "<table border=1 cellpadding=6 style='border-collapse:collapse'>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in item) + "</table><br>"
            else: html += f"<p>{item}</p>"
        return Response(html + "<script>print()</script></body></html>")

@app.route("/agreement/<int:aid>/pdf")
@login_required
def agreement_pdf(aid):
    a = db().execute("SELECT * FROM agreements WHERE id=?", (aid,)).fetchone() or abort(404)
    acc = db().execute("SELECT username, ts FROM acceptances WHERE agreement_id=? ORDER BY ts", (aid,)).fetchall()
    rows = [["Accepted by", "Role", "Date & time"]]
    for r in acc:
        role = db().execute("SELECT role FROM users WHERE username=?", (r["username"],)).fetchone()
        rows.append([r["username"], role["role"] if role else "", r["ts"]])
    if len(rows) == 1: rows.append(["(no acceptances yet)", "", ""])
    return _pdf(f"Working Agreement v{a['version']}", [a["body"], "", "Acceptances:", rows],
                [("Author", a["author"]), ("Written", a["ts"]), ("Exported", now()), ("Exported by", session["user"])])

@app.route("/milestones/<int:mid>/invoice")
@login_required
def invoice(mid):
    m = db().execute("SELECT * FROM milestones WHERE id=?", (mid,)).fetchone() or abort(404)
    p = db().execute("SELECT name FROM projects WHERE id=?", (m["project_id"],)).fetchone()
    payer = db().execute("SELECT username FROM users WHERE is_admin=1 ORDER BY rowid LIMIT 1").fetchone()
    payee = db().execute("SELECT username, role FROM users WHERE is_admin=0 AND active=1 ORDER BY rowid LIMIT 1").fetchone()
    table = [["Description", "Amount (INR)"], [f"{m['title']}\n{m['details'] or ''}", f"{m['amount'] or 0:,.2f}"], ["TOTAL", f"{m['amount'] or 0:,.2f}"]]
    meta = [("Invoice no.", f"INV-{m['project_id']:02d}-{m['id']:04d}"), ("Date", now()[:10]), ("Project", p["name"]),
            ("Bill to", payer["username"] if payer else ""), ("From", f"{payee['username']} ({payee['role']})" if payee else ""),
            ("Milestone status", m["status"].replace("_", " ")), ("Due", m["due"] or "—"),
            ("Agreement", f"v{m['agreement_version']}" if m["agreement_version"] else "—")]
    if m["status"] == "paid": meta.append(("Paid on", f"{m['paid_on']} ref {m['paid_ref'] or ''}"))
    return _pdf("Invoice", [table, "Thank you."], meta)

# ---------------------------------------------------------------- backup ----
def make_backup_zip(include_rec=False, by="system"):
    """Create a backup zip (consistent DB snapshot + uploads). Returns temp path."""
    import zipfile, tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False); tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        snap = tempfile.NamedTemporaryFile(suffix=".db", delete=False); snap.close()
        src = sqlite3.connect(DB); dst = sqlite3.connect(snap.name); src.backup(dst); dst.close(); src.close()
        z.write(snap.name, "iotpm.db"); os.unlink(snap.name)
        for root, dirs, files in os.walk(UPLOADS):
            if not include_rec and os.path.abspath(root).startswith(os.path.abspath(RECORDINGS)):
                continue
            for f in files:
                if f == ".gitkeep": continue
                full = os.path.join(root, f); z.write(full, os.path.relpath(full, BASE))
        z.writestr("README-RESTORE.txt", "Restore: stop the service, copy iotpm.db and the uploads/ folder into the app folder, start the service.\n"
                   f"Created {now()} by {by}. Recordings included: {include_rec}.\n")
    return tmp.name

@app.route("/admin/backup")
@admin_required
def admin_backup():
    from flask import send_file
    include_rec = request.args.get("recordings") == "1"
    path = make_backup_zip(include_rec, session["user"])
    log_activity("downloaded a backup", "with recordings" if include_rec else "without recordings", "/users"); db().commit()
    name = f"rainflow-backup-{datetime.datetime.now():%Y%m%d-%H%M}{'-full' if include_rec else ''}.zip"
    r = send_file(path, as_attachment=True, download_name=name)
    r.call_on_close(lambda: os.path.exists(path) and os.unlink(path)); return r

@app.route("/admin/export")
@admin_required
def admin_export():
    """Portable data export: every table as CSV + one JSON (no binary files, no password hashes)."""
    import io, zipfile, csv, json as _json, tempfile
    from flask import send_file
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False); tmp.close()
    d = db(); allj = {}
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        for (t,) in d.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            rows = d.execute(f"SELECT * FROM {t}").fetchall()
            cols = [c[1] for c in d.execute(f"PRAGMA table_info({t})")]
            if t == "users": cols = [c for c in cols if c != "pw_hash"]
            if t == "meta": continue
            buf = io.StringIO(); w = csv.writer(buf); w.writerow(cols)
            for r in rows: w.writerow([r[c] for c in cols])
            z.writestr(f"csv/{t}.csv", buf.getvalue()); allj[t] = [{c: r[c] for c in cols} for r in rows]
        z.writestr("rainflow-export.json", _json.dumps(allj, indent=1, default=str))
    log_activity("exported data (CSV/JSON)", "", "/users"); db().commit()
    r = send_file(tmp.name, as_attachment=True, download_name=f"rainflow-export-{datetime.datetime.now():%Y%m%d}.zip")
    r.call_on_close(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name)); return r

@app.route("/admin/backup/status")
@admin_required
def backup_status():
    import glob as _g
    size = lambda p: sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs) if os.path.isdir(p) else 0
    auto = sorted(_g.glob(os.path.join(BASE, "backups", "rainflow-*")))
    g = gdrive_status()
    return jsonify(db_mb=round(os.path.getsize(DB) / 1048576, 1), uploads_mb=round((size(UPLOADS) - size(RECORDINGS)) / 1048576, 1),
                   recordings_mb=round(size(RECORDINGS) / 1048576, 1),
                   auto_backups=[(os.path.basename(a), round(os.path.getsize(a) / 1048576, 1)) for a in auto[-5:]], gdrive=g)

# ----------------------------------------------------------- google drive ----
# Connect once in the browser (OAuth); the server then uploads backups to a
# "Rainflow backups" folder in that Google Drive. Needs GOOGLE_CLIENT_ID /
# GOOGLE_CLIENT_SECRET in the environment (free, see README).
import urllib.request, urllib.parse, json as _json

def _meta_get(k, default=None):
    r = db().execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return _json.loads(r["value"]) if r else default

def _meta_set(k, v):
    db().execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, _json.dumps(v))); db().commit()

def gdrive_configured():
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))

def gdrive_status():
    t = _meta_get("gdrive")
    return {"configured": gdrive_configured(), "connected": bool(t and t.get("refresh_token")),
            "email": (t or {}).get("email"), "last": (t or {}).get("last"), "last_error": (t or {}).get("last_error")}

def _gdrive_token():
    t = _meta_get("gdrive") or {}
    data = urllib.parse.urlencode({"client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                                   "refresh_token": t["refresh_token"], "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data), timeout=20) as r:
        return _json.loads(r.read())["access_token"]

def _gapi(method, url, token, body=None, ctype="application/json"):
    req = urllib.request.Request(url, data=body, method=method, headers={"Authorization": "Bearer " + token, "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=120) as r:
        return _json.loads(r.read() or b"{}")

def gdrive_upload(path, name):
    """Upload a file into the 'Rainflow backups' folder (created on first use)."""
    tok = _gdrive_token(); t = _meta_get("gdrive") or {}
    folder = t.get("folder")
    if not folder:
        q = urllib.parse.quote("name='Rainflow backups' and mimeType='application/vnd.google-apps.folder' and trashed=false")
        found = _gapi("GET", f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id)", tok).get("files")
        folder = found[0]["id"] if found else _gapi("POST", "https://www.googleapis.com/drive/v3/files", tok,
                                                     _json.dumps({"name": "Rainflow backups", "mimeType": "application/vnd.google-apps.folder"}).encode())["id"]
        t["folder"] = folder; _meta_set("gdrive", t)
    # resumable upload (works for large files)
    meta = _json.dumps({"name": name, "parents": [folder]}).encode()
    req = urllib.request.Request("https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable", data=meta, method="POST",
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json; charset=UTF-8",
                                          "X-Upload-Content-Type": "application/zip", "X-Upload-Content-Length": str(os.path.getsize(path))})
    with urllib.request.urlopen(req, timeout=60) as r:
        up = r.headers["Location"]
    with open(path, "rb") as f:
        req = urllib.request.Request(up, data=f.read(), method="PUT", headers={"Content-Type": "application/zip"})
        with urllib.request.urlopen(req, timeout=600) as r:
            res = _json.loads(r.read())
    # keep only the newest 30 in Drive
    q = urllib.parse.quote(f"'{folder}' in parents and trashed=false")
    files = _gapi("GET", f"https://www.googleapis.com/drive/v3/files?q={q}&orderBy=createdTime desc&fields=files(id,name)&pageSize=100", tok).get("files", [])
    for old in files[30:]:
        try: _gapi("DELETE", f"https://www.googleapis.com/drive/v3/files/{old['id']}", tok)
        except Exception: pass
    t["last"] = now() + " · " + name; t["last_error"] = None; _meta_set("gdrive", t)
    return res

@app.route("/admin/gdrive/connect")
@admin_required
def gdrive_connect():
    if not gdrive_configured():
        flash("Admin: set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET on the server first (see README)"); return redirect(url_for("users") + "#backup")
    redirect_uri = (os.environ.get("APP_URL") or request.url_root.rstrip("/")) + "/admin/gdrive/callback"
    session["gdrive_state"] = uuid.uuid4().hex
    params = {"client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri, "response_type": "code",
              "scope": "https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/userinfo.email",
              "access_type": "offline", "prompt": "consent", "state": session["gdrive_state"]}
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params))

@app.route("/admin/gdrive/callback")
@admin_required
def gdrive_callback():
    if request.args.get("state") != session.pop("gdrive_state", None) or not request.args.get("code"):
        flash("Google Drive connection was cancelled or invalid"); return redirect(url_for("users") + "#backup")
    redirect_uri = (os.environ.get("APP_URL") or request.url_root.rstrip("/")) + "/admin/gdrive/callback"
    data = urllib.parse.urlencode({"code": request.args["code"], "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                                   "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data), timeout=20) as r:
            tok = _json.loads(r.read())
        email = ""
        try: email = _gapi("GET", "https://www.googleapis.com/oauth2/v2/userinfo", tok["access_token"]).get("email", "")
        except Exception: pass
        _meta_set("gdrive", {"refresh_token": tok["refresh_token"], "email": email})
        log_activity("connected Google Drive for backups", email, "/users"); db().commit()
        flash(f"Google Drive connected ({email}). Backups will be stored in the 'Rainflow backups' folder.")
    except Exception as e:
        flash("Google Drive connection failed: " + str(e)[:200])
    return redirect(url_for("users") + "#backup")

@app.route("/admin/gdrive/disconnect", methods=["POST"])
@admin_required
def gdrive_disconnect():
    db().execute("DELETE FROM meta WHERE key='gdrive'"); db().commit()
    flash("Google Drive disconnected"); return redirect(url_for("users") + "#backup")

def _run_gdrive_backup(include_rec, by):
    path = make_backup_zip(include_rec, by)
    name = f"rainflow-backup-{datetime.datetime.now():%Y%m%d-%H%M}{'-full' if include_rec else ''}.zip"
    try:
        gdrive_upload(path, name); return True, name
    except Exception as e:
        t = _meta_get("gdrive") or {}; t["last_error"] = f"{now()} {str(e)[:200]}"; _meta_set("gdrive", t); return False, str(e)[:200]
    finally:
        if os.path.exists(path): os.unlink(path)

@app.route("/admin/gdrive/backup", methods=["POST"])
def gdrive_backup_now():
    """Upload a backup to Drive. Called from the admin page, or by cron with X-Backup-Token (BACKUP_TOKEN env)."""
    is_cron = os.environ.get("BACKUP_TOKEN") and request.headers.get("X-Backup-Token") == os.environ["BACKUP_TOKEN"]
    if not is_cron and not session.get("is_admin"): return jsonify(ok=False), 403
    if not gdrive_status()["connected"]: return jsonify(ok=False, error="Google Drive not connected")
    ok, info = _run_gdrive_backup(request.args.get("recordings") == "1", "cron" if is_cron else session.get("user", "admin"))
    if ok and not is_cron: log_activity("backed up to Google Drive", info, "/users"); db().commit()
    return jsonify(ok=ok, info=info)

# -------------------------------------------------------------- projects ----
@app.route("/projects", methods=["POST"])
@login_required
def add_project():
    db().execute("INSERT INTO projects(name,description,created) VALUES(?,?,?)",
                 (request.form["name"], request.form.get("description", ""), now()))
    db().commit(); return redirect(url_for("dashboard"))

from pm import pm as _pm, due_reminders
app.register_blueprint(_pm)
from iot import iot as _iot, mqtt_start
app.register_blueprint(_iot)
from hw import hw as _hw
app.register_blueprint(_hw)

# old research URLs → tasks board
@app.route("/research")
@login_required
def research():
    return redirect(url_for("pm.board", type="research"))

if __name__ == "__main__":
    init_db()
    mqtt_start()
    # host 0.0.0.0 so your IoT expert can reach it on the LAN / via tunnel.
    # Note: browsers only allow camera/mic on https:// or localhost — for remote
    # use put it behind an HTTPS tunnel (e.g. `ngrok http 5000`, Cloudflare Tunnel).
    # HOST / PORT / SECRET_KEY can be set via environment (e.g. in a systemd unit).
    # Behind Nginx use HOST=127.0.0.1; set a fixed SECRET_KEY so logins survive restarts.
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 5000)),
            debug=False, threaded=True)
