"""
Rainflow — project-management module (Blueprint).

Tasks (Kanban + detail page with dated log of notes / voice / video / files,
checklists, subtasks, dependencies, time tracking, labels), Gantt timeline,
Calendar, Notifications (in-app bell; optional email / Telegram), Reports.
"""
import os, json, datetime, calendar as _cal, smtplib, urllib.request, urllib.parse
from email.mime.text import MIMEText
from flask import (Blueprint, g, render_template, request, redirect, url_for, session,
                   jsonify, abort, flash, make_response)

pm = Blueprint("pm", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, description TEXT,
  type TEXT DEFAULT 'task', status TEXT DEFAULT 'todo', priority TEXT DEFAULT 'medium',
  assignee TEXT, labels TEXT DEFAULT '', start TEXT, due TEXT, estimate_h REAL,
  parent_id INTEGER, position INTEGER DEFAULT 0, milestone_id INTEGER, hardware_id INTEGER,
  firmware_id INTEGER, created_by TEXT, ts TEXT, done_ts TEXT, decision TEXT);
CREATE TABLE IF NOT EXISTS task_comments(
  id INTEGER PRIMARY KEY, task_id INTEGER, author TEXT, body TEXT, filename TEXT, orig_name TEXT,
  kind TEXT DEFAULT 'note', ts TEXT);
CREATE TABLE IF NOT EXISTS checklist(
  id INTEGER PRIMARY KEY, task_id INTEGER, text TEXT, done INTEGER DEFAULT 0, position INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS task_deps(task_id INTEGER, depends_on INTEGER, PRIMARY KEY(task_id, depends_on));
CREATE TABLE IF NOT EXISTS time_entries(
  id INTEGER PRIMARY KEY, task_id INTEGER, username TEXT, minutes INTEGER, note TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS labels(
  id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT, color TEXT, UNIQUE(project_id, name));
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY, username TEXT, text TEXT, link TEXT, read INTEGER DEFAULT 0, ts TEXT);
CREATE TABLE IF NOT EXISTS user_prefs(
  username TEXT PRIMARY KEY, email TEXT, telegram_chat_id TEXT, notify_email INTEGER DEFAULT 0,
  notify_telegram INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS push_subs(
  endpoint TEXT PRIMARY KEY, username TEXT, sub TEXT, ua TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS presence(username TEXT PRIMARY KEY, last_seen REAL);
"""

STATUSES = [("backlog", "Backlog"), ("todo", "To do"), ("doing", "In progress"), ("review", "Review"), ("done", "Done")]
TYPES = ["task", "research", "bug", "feature", "hardware", "firmware"]
LABEL_COLORS = ["#2f7de1", "#0f9d8a", "#e09f1f", "#d64545", "#7f6bd0", "#d35d64", "#4f9a3c", "#3a86c9"]


# ------------------------------------------------------------------ helpers --
def db():
    from app import db as _db
    return _db()

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

def today():
    return datetime.date.today().isoformat()

def pid():
    return session.get("pid", 1)

def login_required(f):
    from functools import wraps
    @wraps(f)
    def w(*a, **k):
        if not db().execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return redirect(url_for("setup"))
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w

def all_users():
    from app import all_users as au
    return au()

def log_activity(action, detail="", link=""):
    from app import log_activity as la
    la(action, detail, link)

def save_upload(f):
    from app import save_upload as su
    return su(f)

def is_image(fn):
    from app import is_image as ii
    return ii(fn)


def init_pm(con):
    """Create tables and migrate old research items into tasks (runs once)."""
    con.executescript(SCHEMA)
    if not con.execute("SELECT 1 FROM meta WHERE key='research_migrated'").fetchone():
        try:
            rows = con.execute("SELECT * FROM research").fetchall()
            for r in rows:
                cur = con.execute(
                    "INSERT INTO tasks(project_id,title,description,type,status,priority,assignee,due,created_by,ts,decision) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (r["project_id"], r["title"], r["details"], "research", r["status"], r["priority"], r["owner"],
                     r["due"], r["owner"], r["ts"], r["decision"] if "decision" in r.keys() else None))
                tid = cur.lastrowid
                try:
                    for l in con.execute("SELECT * FROM research_log WHERE research_id=?", (r["id"],)).fetchall():
                        con.execute("INSERT INTO task_comments(task_id,author,body,filename,orig_name,kind,ts) VALUES(?,?,?,?,?,?,?)",
                                    (tid, l["author"], l["body"], l["filename"], l["orig_name"], l["kind"], l["ts"]))
                except Exception:
                    pass
                for f in con.execute("SELECT * FROM research_files WHERE research_id=?", (r["id"],)).fetchall():
                    con.execute("INSERT INTO task_comments(task_id,author,body,filename,orig_name,kind,ts) VALUES(?,?,?,?,?,?,?)",
                                (tid, f["uploaded_by"], "", f["filename"], f["orig_name"], "file", f["ts"]))
        except Exception:
            pass
        con.execute("INSERT OR REPLACE INTO meta VALUES('research_migrated','1')")
    con.commit()


# ------------------------------------------------------------- web push --
def vapid_keys():
    d = db()
    row = d.execute("SELECT value FROM meta WHERE key='vapid'").fetchone()
    if row:
        return json.loads(row["value"])
    import webpush
    pem, pub = webpush.generate_vapid()
    d.execute("INSERT INTO meta VALUES('vapid',?)", (json.dumps({"pem": pem, "pub": pub}),)); d.commit()
    return {"pem": pem, "pub": pub}

def touch_presence():
    import time as _t
    if "user" in session:
        db().execute("INSERT INTO presence VALUES(?,?) ON CONFLICT(username) DO UPDATE SET last_seen=excluded.last_seen",
                     (session["user"], _t.time())); db().commit()

def is_online(username, within=25):
    import time as _t
    r = db().execute("SELECT last_seen FROM presence WHERE username=?", (username,)).fetchone()
    return bool(r) and _t.time() - r["last_seen"] < within

def push(users, title, body, url="/", urgent=False, even_if_online=False):
    """Web Push to every device of each user (works with the browser closed)."""
    try:
        import webpush
    except Exception:
        return
    d = db(); keys = vapid_keys(); subs = []
    for u in set(users):
        if not u or u == session.get("user"): continue
        if not even_if_online and is_online(u): continue
        for r in d.execute("SELECT sub FROM push_subs WHERE username=?", (u,)):
            try: subs.append(json.loads(r["sub"]))
            except Exception: pass
    if not subs: return
    subject = "mailto:" + os.environ.get("VAPID_EMAIL", "admin@example.com")
    import sqlite3 as _sq
    from app import DB
    def on_dead(endpoint):
        c = _sq.connect(DB); c.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,)); c.commit(); c.close()
    webpush.send_async(subs, {"title": title, "body": body, "url": url, "urgent": urgent}, keys["pem"], subject, on_dead)

@pm.route("/push/key")
@login_required
def push_key():
    return jsonify(key=vapid_keys()["pub"])

@pm.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    j = request.get_json()
    db().execute("INSERT INTO push_subs(endpoint,username,sub,ua,ts) VALUES(?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET username=excluded.username, sub=excluded.sub, ts=excluded.ts",
                 (j["endpoint"], session["user"], json.dumps(j), request.headers.get("User-Agent", "")[:120], now()))
    db().commit(); return jsonify(ok=True)

@pm.route("/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    j = request.get_json()
    db().execute("DELETE FROM push_subs WHERE endpoint=?", (j.get("endpoint"),)); db().commit(); return jsonify(ok=True)

@pm.route("/push/test", methods=["POST"])
@login_required
def push_test():
    push([session["user"]], "Rainflow", "Push notifications are working on this device 🎉", "/notifications", even_if_online=True)
    return jsonify(ok=True)

@pm.route("/push/status")
@login_required
def push_status():
    n = db().execute("SELECT COUNT(*) FROM push_subs WHERE username=?", (session["user"],)).fetchone()[0]
    return jsonify(devices=n)


def notify(users, text, link=""):
    """In-app notification (+ push / email / Telegram if the user enabled them)."""
    d = db()
    push(users, "Rainflow", text, link)
    for u in set(users):
        if not u or u == session.get("user"):
            continue
        d.execute("INSERT INTO notifications(username,text,link,ts) VALUES(?,?,?,?)", (u, text, link, now()))
        p = d.execute("SELECT * FROM user_prefs WHERE username=?", (u,)).fetchone()
        if p:
            if p["notify_email"] and p["email"]:
                send_email(p["email"], "Rainflow: " + text[:60], text + "\n\n" + (os.environ.get("APP_URL", "") + link))
            if p["notify_telegram"] and p["telegram_chat_id"]:
                send_telegram(p["telegram_chat_id"], text + "\n" + (os.environ.get("APP_URL", "") + link))
    d.commit()


def send_email(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host: return False
    try:
        msg = MIMEText(body); msg["Subject"] = subject
        msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "rainflow@localhost")); msg["To"] = to
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=10) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"): s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            s.send_message(msg)
        return True
    except Exception:
        return False


def send_telegram(chat_id, text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token: return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=10)
        return True
    except Exception:
        return False


def mentions(text):
    return [u for u in all_users() if f"@{u}" in (text or "")]


def task_or_404(tid):
    t = db().execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t: abort(404)
    return t


def label_map():
    return {r["name"]: r["color"] for r in db().execute("SELECT * FROM labels WHERE project_id=?", (pid(),))}


def ensure_labels(names):
    d = db(); existing = label_map()
    for n in [x.strip() for x in names.split(",") if x.strip()]:
        if n not in existing:
            d.execute("INSERT OR IGNORE INTO labels(project_id,name,color) VALUES(?,?,?)",
                      (pid(), n, LABEL_COLORS[len(existing) % len(LABEL_COLORS)])); existing[n] = 1


@pm.app_context_processor
def inject_pm():
    if "user" not in session:
        return {}
    n = db().execute("SELECT COUNT(*) FROM notifications WHERE username=? AND read=0", (session["user"],)).fetchone()[0]
    return dict(notif_unread=n, STATUSES=STATUSES, TYPES=TYPES, today=today())


# ------------------------------------------------------------------- board --
@pm.route("/board")
@login_required
def board():
    d = db(); f = request.args
    q = "SELECT * FROM tasks WHERE project_id=? AND parent_id IS NULL"; args = [pid()]
    if f.get("assignee"): q += " AND assignee=?"; args.append(f["assignee"])
    if f.get("type"): q += " AND type=?"; args.append(f["type"])
    if f.get("label"): q += " AND (','||labels||',') LIKE ?"; args.append(f"%,{f['label']},%")
    if f.get("q"): q += " AND title LIKE ?"; args.append(f"%{f['q']}%")
    q += " ORDER BY position, id"
    tasks = d.execute(q, args).fetchall()
    cols = {s: [] for s, _ in STATUSES}
    for t in tasks: cols.setdefault(t["status"], []).append(t)
    sub = {r["parent_id"]: (r["n"], r["done"]) for r in d.execute(
        "SELECT parent_id, COUNT(*) n, SUM(status='done') done FROM tasks WHERE project_id=? AND parent_id IS NOT NULL GROUP BY parent_id", (pid(),))}
    chk = {r["task_id"]: (r["n"], r["done"]) for r in d.execute(
        "SELECT task_id, COUNT(*) n, SUM(done) done FROM checklist GROUP BY task_id")}
    cmt = {r["task_id"]: r["n"] for r in d.execute("SELECT task_id, COUNT(*) n FROM task_comments GROUP BY task_id")}
    return render_template("board.html", cols=cols, users=all_users(), labels=label_map(), sub=sub, chk=chk, cmt=cmt, f=f)


@pm.route("/tasks/new", methods=["POST"])
@login_required
def task_new():
    d = db(); f = request.form
    ensure_labels(f.get("labels", ""))
    pos = (d.execute("SELECT MAX(position) FROM tasks WHERE project_id=? AND status=?", (pid(), f.get("status", "todo"))).fetchone()[0] or 0) + 1
    cur = d.execute(
        "INSERT INTO tasks(project_id,title,description,type,status,priority,assignee,labels,start,due,estimate_h,parent_id,milestone_id,hardware_id,firmware_id,created_by,ts,position) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid(), f["title"].strip(), f.get("description", ""), f.get("type", "task"), f.get("status", "todo"),
         f.get("priority", "medium"), f.get("assignee") or None, ",".join(x.strip() for x in f.get("labels", "").split(",") if x.strip()),
         f.get("start") or None, f.get("due") or None, f.get("estimate_h") or None, f.get("parent_id") or None,
         f.get("milestone_id") or None, f.get("hardware_id") or None, f.get("firmware_id") or None, session["user"], now(), pos))
    tid = cur.lastrowid
    log_activity("created task", f["title"], f"/tasks/{tid}")
    if f.get("assignee") and f["assignee"] != session["user"]:
        notify([f["assignee"]], f"{session['user']} assigned you: {f['title']}", f"/tasks/{tid}")
    notify(mentions(f.get("description", "")), f"{session['user']} mentioned you in task: {f['title']}", f"/tasks/{tid}")
    d.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, id=tid)
    return redirect(f.get("next") or url_for("pm.task_item", tid=tid))


@pm.route("/tasks/move", methods=["POST"])
@login_required
def tasks_move():
    """Kanban drag-drop: {id, status, order:[ids in new column order]}"""
    j = request.get_json(); d = db()
    t = task_or_404(j["id"])
    if j["status"] not in dict(STATUSES): return jsonify(ok=False), 400
    done_ts = now() if j["status"] == "done" and t["status"] != "done" else (None if j["status"] != "done" else t["done_ts"])
    d.execute("UPDATE tasks SET status=?, done_ts=? WHERE id=?", (j["status"], done_ts, j["id"]))
    for i, tid in enumerate(j.get("order", [])):
        d.execute("UPDATE tasks SET position=? WHERE id=?", (i, tid))
    if t["status"] != j["status"]:
        log_activity(f"moved task to {dict(STATUSES)[j['status']]}", t["title"], f"/tasks/{t['id']}")
        if t["assignee"] and t["assignee"] != session["user"]:
            notify([t["assignee"]], f"{session['user']} moved '{t['title']}' to {dict(STATUSES)[j['status']]}", f"/tasks/{t['id']}")
    d.commit(); return jsonify(ok=True)


# --------------------------------------------------------------- task page --
@pm.route("/tasks/<int:tid>")
@login_required
def task_item(tid):
    d = db(); t = task_or_404(tid)
    comments = d.execute("SELECT * FROM task_comments WHERE task_id=? ORDER BY id DESC", (tid,)).fetchall()
    checks = d.execute("SELECT * FROM checklist WHERE task_id=? ORDER BY position, id", (tid,)).fetchall()
    subs = d.execute("SELECT * FROM tasks WHERE parent_id=? ORDER BY status='done', id", (tid,)).fetchall()
    deps = d.execute("SELECT t.* FROM task_deps dp JOIN tasks t ON t.id=dp.depends_on WHERE dp.task_id=?", (tid,)).fetchall()
    blocks = d.execute("SELECT t.* FROM task_deps dp JOIN tasks t ON t.id=dp.task_id WHERE dp.depends_on=?", (tid,)).fetchall()
    time = d.execute("SELECT * FROM time_entries WHERE task_id=? ORDER BY id DESC", (tid,)).fetchall()
    spent = sum(x["minutes"] for x in time)
    parent = d.execute("SELECT id,title FROM tasks WHERE id=?", (t["parent_id"],)).fetchone() if t["parent_id"] else None
    others = d.execute("SELECT id,title FROM tasks WHERE project_id=? AND id!=? ORDER BY id DESC LIMIT 200", (pid(), tid)).fetchall()
    hw = d.execute("SELECT id,name FROM hardware WHERE project_id=?", (pid(),)).fetchall()
    fw = d.execute("SELECT id,version FROM firmware WHERE project_id=? ORDER BY id DESC", (pid(),)).fetchall()
    ms = d.execute("SELECT id,title FROM milestones WHERE project_id=?", (pid(),)).fetchall()
    link = {}
    if t["hardware_id"]: link["hardware"] = d.execute("SELECT id,name FROM hardware WHERE id=?", (t["hardware_id"],)).fetchone()
    if t["firmware_id"]: link["firmware"] = d.execute("SELECT id,version FROM firmware WHERE id=?", (t["firmware_id"],)).fetchone()
    if t["milestone_id"]: link["milestone"] = d.execute("SELECT id,title FROM milestones WHERE id=?", (t["milestone_id"],)).fetchone()
    return render_template("task.html", t=t, comments=comments, checks=checks, subs=subs, deps=deps, blocks=blocks,
                           time=time, spent=spent, parent=parent, others=others, hw=hw, fw=fw, ms=ms, link=link,
                           users=all_users(), labels=label_map())


@pm.route("/tasks/<int:tid>/update", methods=["POST"])
@login_required
def task_update(tid):
    d = db(); t = task_or_404(tid); f = request.form
    ensure_labels(f.get("labels", ""))
    new_assignee = f.get("assignee") or None
    done_ts = t["done_ts"]
    if f.get("status") == "done" and t["status"] != "done": done_ts = now()
    if f.get("status") != "done": done_ts = None
    d.execute("UPDATE tasks SET title=?,description=?,type=?,status=?,priority=?,assignee=?,labels=?,start=?,due=?,estimate_h=?,milestone_id=?,hardware_id=?,firmware_id=?,decision=?,done_ts=? WHERE id=?",
              (f["title"].strip(), f.get("description", ""), f.get("type", t["type"]), f.get("status", t["status"]), f.get("priority", "medium"),
               new_assignee, ",".join(x.strip() for x in f.get("labels", "").split(",") if x.strip()), f.get("start") or None, f.get("due") or None,
               f.get("estimate_h") or None, f.get("milestone_id") or None, f.get("hardware_id") or None, f.get("firmware_id") or None,
               f.get("decision", ""), done_ts, tid))
    log_activity("updated task", f["title"], f"/tasks/{tid}")
    if new_assignee and new_assignee != t["assignee"]:
        notify([new_assignee], f"{session['user']} assigned you: {f['title']}", f"/tasks/{tid}")
    d.commit(); return redirect(url_for("pm.task_item", tid=tid))


@pm.route("/tasks/<int:tid>/status/<status>", methods=["POST"])
@login_required
def task_status(tid, status):
    t = task_or_404(tid)
    if status in dict(STATUSES):
        db().execute("UPDATE tasks SET status=?, done_ts=? WHERE id=?", (status, now() if status == "done" else None, tid))
        log_activity(f"moved task to {dict(STATUSES)[status]}", t["title"], f"/tasks/{tid}"); db().commit()
    return redirect(request.referrer or url_for("pm.task_item", tid=tid))


@pm.route("/tasks/<int:tid>/delete", methods=["POST"])
@login_required
def task_delete(tid):
    d = db(); t = task_or_404(tid)
    for tbl in ("task_comments", "checklist", "time_entries"):
        d.execute(f"DELETE FROM {tbl} WHERE task_id=?", (tid,))
    d.execute("DELETE FROM task_deps WHERE task_id=? OR depends_on=?", (tid, tid))
    d.execute("UPDATE tasks SET parent_id=NULL WHERE parent_id=?", (tid,))
    d.execute("DELETE FROM tasks WHERE id=?", (tid,))
    log_activity("deleted task", t["title"], "/board"); d.commit()
    return redirect(url_for("pm.board"))


@pm.route("/tasks/<int:tid>/comment", methods=["POST"])
@login_required
def task_comment(tid):
    """Dated log entry: text and/or file / image / voice / video."""
    d = db(); t = task_or_404(tid); body = (request.form.get("body") or "").strip()
    f = request.files.get("file"); fn = orig = None; kind = request.form.get("kind", "note")
    if f and f.filename:
        fn, orig = save_upload(f)
        if kind == "note": kind = "image" if is_image(fn) else "file"
    if not body and not fn:
        return redirect(url_for("pm.task_item", tid=tid))
    d.execute("INSERT INTO task_comments(task_id,author,body,filename,orig_name,kind,ts) VALUES(?,?,?,?,?,?,?)",
              (tid, session["user"], body, fn, orig, kind, now()))
    log_activity(f"added {kind} to task", t["title"], f"/tasks/{tid}")
    watchers = {t["assignee"], t["created_by"]} | set(r["author"] for r in d.execute("SELECT DISTINCT author FROM task_comments WHERE task_id=?", (tid,)))
    notify(watchers - {session["user"]}, f"{session['user']} added a {kind} on '{t['title']}'", f"/tasks/{tid}")
    notify(mentions(body), f"{session['user']} mentioned you on '{t['title']}'", f"/tasks/{tid}")
    d.commit()
    if request.headers.get("X-Requested-With") == "fetch": return jsonify(ok=True)
    return redirect(url_for("pm.task_item", tid=tid))


@pm.route("/tasks/<int:tid>/comment/<int:cid>/delete", methods=["POST"])
@login_required
def task_comment_delete(tid, cid):
    db().execute("DELETE FROM task_comments WHERE id=? AND task_id=? AND author=?", (cid, tid, session["user"])); db().commit()
    return redirect(url_for("pm.task_item", tid=tid))


@pm.route("/tasks/<int:tid>/check", methods=["POST"])
@login_required
def task_check(tid):
    d = db(); f = request.form
    if f.get("text"):
        pos = (d.execute("SELECT MAX(position) FROM checklist WHERE task_id=?", (tid,)).fetchone()[0] or 0) + 1
        d.execute("INSERT INTO checklist(task_id,text,position) VALUES(?,?,?)", (tid, f["text"].strip(), pos))
    elif f.get("toggle"):
        d.execute("UPDATE checklist SET done=1-done WHERE id=? AND task_id=?", (f["toggle"], tid))
    elif f.get("remove"):
        d.execute("DELETE FROM checklist WHERE id=? AND task_id=?", (f["remove"], tid))
    d.commit(); return redirect(url_for("pm.task_item", tid=tid))


@pm.route("/tasks/<int:tid>/dep", methods=["POST"])
@login_required
def task_dep(tid):
    d = db(); f = request.form
    if f.get("add") and int(f["add"]) != tid:
        d.execute("INSERT OR IGNORE INTO task_deps VALUES(?,?)", (tid, f["add"]))
    if f.get("remove"):
        d.execute("DELETE FROM task_deps WHERE task_id=? AND depends_on=?", (tid, f["remove"]))
    d.commit(); return redirect(url_for("pm.task_item", tid=tid))


@pm.route("/tasks/<int:tid>/time", methods=["POST"])
@login_required
def task_time(tid):
    d = db(); f = request.form
    if f.get("remove"):
        d.execute("DELETE FROM time_entries WHERE id=? AND username=?", (f["remove"], session["user"]))
    else:
        mins = int(float(f.get("hours") or 0) * 60) + int(f.get("minutes") or 0)
        if mins > 0:
            d.execute("INSERT INTO time_entries(task_id,username,minutes,note,ts) VALUES(?,?,?,?,?)",
                      (tid, session["user"], mins, f.get("note", ""), now()))
    d.commit(); return redirect(url_for("pm.task_item", tid=tid))


# ---------------------------------------------------------------- timeline --
@pm.route("/timeline")
@login_required
def timeline():
    d = db()
    tasks = d.execute("SELECT * FROM tasks WHERE project_id=? AND parent_id IS NULL ORDER BY COALESCE(start,due,ts), id", (pid(),)).fetchall()
    ms = d.execute("SELECT * FROM milestones WHERE project_id=? AND due!='' ORDER BY due", (pid(),)).fetchall()
    deps = d.execute("SELECT * FROM task_deps").fetchall()
    dates = [x for t in tasks for x in (t["start"], t["due"]) if x] + [m["due"] for m in ms] + [today()]
    dmin = min(dates); dmax = max(dates)
    dmin = (datetime.date.fromisoformat(dmin) - datetime.timedelta(days=3)); dmax = (datetime.date.fromisoformat(dmax) + datetime.timedelta(days=10))
    if (dmax - dmin).days < 42: dmax = dmin + datetime.timedelta(days=42)
    rows = []
    for t in tasks:
        s = t["start"] or t["due"] or t["ts"][:10]; e = t["due"] or t["start"] or t["ts"][:10]
        if e < s: s, e = e, s
        rows.append(dict(id=t["id"], title=t["title"], status=t["status"], assignee=t["assignee"], priority=t["priority"],
                         s=(datetime.date.fromisoformat(s) - dmin).days, e=(datetime.date.fromisoformat(e) - dmin).days + 1,
                         start=s, due=e, overdue=(t["status"] != "done" and t["due"] and t["due"] < today())))
    idx = {r["id"]: i for i, r in enumerate(rows)}
    arrows = [(idx[dp["depends_on"]], idx[dp["task_id"]]) for dp in deps if dp["depends_on"] in idx and dp["task_id"] in idx]
    msr = [dict(title=m["title"], x=(datetime.date.fromisoformat(m["due"]) - dmin).days, status=m["status"]) for m in ms]
    weeks = []; dcur = dmin
    while dcur <= dmax:
        weeks.append(dict(x=(dcur - dmin).days, label=dcur.strftime("%d %b"))); dcur += datetime.timedelta(days=7)
    return render_template("timeline.html", rows=rows, arrows=arrows, ms=msr, weeks=weeks, days=(dmax - dmin).days,
                           today_x=(datetime.date.today() - dmin).days, dmin=dmin)


# ---------------------------------------------------------------- calendar --
@pm.route("/calendar")
@login_required
def calendar_view():
    d = db()
    y = int(request.args.get("y", datetime.date.today().year)); m = int(request.args.get("m", datetime.date.today().month))
    first = datetime.date(y, m, 1); last = datetime.date(y, m, _cal.monthrange(y, m)[1])
    lo, hi = first.isoformat(), last.isoformat()
    ev = {}
    def add(day, kind, text, link):
        if day and lo <= day[:10] <= hi: ev.setdefault(day[:10], []).append(dict(kind=kind, text=text, link=link))
    for t in d.execute("SELECT * FROM tasks WHERE project_id=? AND (due BETWEEN ? AND ? OR start BETWEEN ? AND ?)", (pid(), lo, hi, lo, hi)):
        if t["due"]: add(t["due"], "due" if t["status"] != "done" else "done", t["title"], f"/tasks/{t['id']}")
        if t["start"] and t["start"] != t["due"]: add(t["start"], "start", "start: " + t["title"], f"/tasks/{t['id']}")
    for x in d.execute("SELECT * FROM milestones WHERE project_id=? AND due BETWEEN ? AND ?", (pid(), lo, hi)):
        add(x["due"], "milestone", "◆ " + x["title"] + (" (paid)" if x["status"] == "paid" else ""), "/milestones")
    for x in d.execute("SELECT * FROM firmware WHERE project_id=? AND ts BETWEEN ? AND ?", (pid(), lo, hi + " 23:59")):
        add(x["ts"], "firmware", "firmware " + x["version"], "/firmware")
    for x in d.execute("SELECT * FROM tests WHERE project_id=? AND ts BETWEEN ? AND ?", (pid(), lo, hi + " 23:59")):
        add(x["ts"], "pass" if x["passed"] else "fail", ("✓ " if x["passed"] else "✗ ") + x["title"], "/tests")
    for x in d.execute("SELECT * FROM calls WHERE project_id=? AND started BETWEEN ? AND ?", (pid(), lo, hi + " 23:59")):
        add(x["started"], "call", f"call {x['caller']}→{x['callee']}", "/calls")
    act = {r["day"]: r["n"] for r in d.execute("SELECT substr(ts,1,10) day, COUNT(*) n FROM activity WHERE project_id=? AND ts BETWEEN ? AND ? GROUP BY day", (pid(), lo, hi + " 23:59"))}
    msgs = {r["day"]: r["n"] for r in d.execute("SELECT substr(ts,1,10) day, COUNT(*) n FROM messages WHERE project_id=? AND ts BETWEEN ? AND ? GROUP BY day", (pid(), lo, hi + " 23:59"))}
    weeks = _cal.Calendar(firstweekday=0).monthdatescalendar(y, m)
    prev = (first - datetime.timedelta(days=1)); nxt = (last + datetime.timedelta(days=1))
    sel = request.args.get("d")
    day_items = []
    if sel:
        day_items = ev.get(sel, []) + [dict(kind="activity", text=f"{a['who']} {a['action']} {a['detail']}", link=a["link"])
                                       for a in d.execute("SELECT * FROM activity WHERE project_id=? AND ts LIKE ? ORDER BY id", (pid(), sel + "%"))]
    return render_template("calendar.html", y=y, m=m, weeks=weeks, ev=ev, act=act, msgs=msgs, first=first,
                           prev=prev, nxt=nxt, sel=sel, day_items=day_items, month_name=first.strftime("%B %Y"))


# ----------------------------------------------------------- notifications --
@pm.route("/notifications")
@login_required
def notifications():
    d = db()
    rows = d.execute("SELECT * FROM notifications WHERE username=? ORDER BY id DESC LIMIT 100", (session["user"],)).fetchall()
    d.execute("UPDATE notifications SET read=1 WHERE username=?", (session["user"],)); d.commit()
    prefs = d.execute("SELECT * FROM user_prefs WHERE username=?", (session["user"],)).fetchone()
    return render_template("notifications.html", rows=rows, prefs=prefs,
                           email_ok=bool(os.environ.get("SMTP_HOST")), tg_ok=bool(os.environ.get("TELEGRAM_BOT_TOKEN")))


@pm.route("/notifications/prefs", methods=["POST"])
@login_required
def notif_prefs():
    f = request.form
    db().execute("INSERT INTO user_prefs(username,email,telegram_chat_id,notify_email,notify_telegram) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(username) DO UPDATE SET email=excluded.email, telegram_chat_id=excluded.telegram_chat_id, "
                 "notify_email=excluded.notify_email, notify_telegram=excluded.notify_telegram",
                 (session["user"], f.get("email", ""), f.get("telegram_chat_id", ""), 1 if f.get("notify_email") else 0, 1 if f.get("notify_telegram") else 0))
    db().commit(); flash("Notification settings saved")
    return redirect(url_for("pm.notifications"))


@pm.route("/notifications/poll")
@login_required
def notif_poll():
    touch_presence()
    rows = db().execute("SELECT id,text,link,ts FROM notifications WHERE username=? AND read=0 ORDER BY id DESC LIMIT 5", (session["user"],)).fetchall()
    n = db().execute("SELECT COUNT(*) FROM notifications WHERE username=? AND read=0", (session["user"],)).fetchone()[0]
    return jsonify(count=n, items=[dict(r) for r in rows])


def due_reminders():
    """Called on each dashboard load (cheap): notify assignees of tasks due today/tomorrow, once per day."""
    d = db(); key = "reminders_" + today()
    if d.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone(): return
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    for t in d.execute("SELECT * FROM tasks WHERE status!='done' AND due IN (?,?) AND assignee IS NOT NULL", (today(), tomorrow)):
        d.execute("INSERT INTO notifications(username,text,link,ts) VALUES(?,?,?,?)",
                  (t["assignee"], f"'{t['title']}' is due {'today' if t['due']==today() else 'tomorrow'}", f"/tasks/{t['id']}", now()))
    for t in d.execute("SELECT * FROM tasks WHERE status!='done' AND due<? AND assignee IS NOT NULL", (today(),)):
        d.execute("INSERT INTO notifications(username,text,link,ts) VALUES(?,?,?,?)",
                  (t["assignee"], f"Overdue: '{t['title']}' was due {t['due']}", f"/tasks/{t['id']}", now()))
    d.execute("INSERT OR REPLACE INTO meta VALUES(?, '1')", (key,)); d.commit()


# ----------------------------------------------------------------- reports --
def report_data():
    d = db(); p = pid()
    tasks = d.execute("SELECT * FROM tasks WHERE project_id=?", (p,)).fetchall()
    by_status = {s: sum(1 for t in tasks if t["status"] == s) for s, _ in STATUSES}
    by_user = {}
    for t in tasks:
        u = t["assignee"] or "unassigned"; by_user.setdefault(u, {"total": 0, "done": 0, "overdue": 0})
        by_user[u]["total"] += 1; by_user[u]["done"] += t["status"] == "done"
        by_user[u]["overdue"] += (t["status"] != "done" and bool(t["due"]) and t["due"] < today())
    week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    done_week = [t for t in tasks if t["done_ts"] and t["done_ts"] >= week_ago]
    overdue = [t for t in tasks if t["status"] != "done" and t["due"] and t["due"] < today()]
    upcoming = [t for t in tasks if t["status"] != "done" and t["due"] and today() <= t["due"] <= (datetime.date.today() + datetime.timedelta(days=14)).isoformat()]
    time = {r["username"]: r["m"] for r in d.execute("SELECT te.username, SUM(minutes) m FROM time_entries te JOIN tasks t ON t.id=te.task_id WHERE t.project_id=? GROUP BY te.username", (p,))}
    ms = d.execute("SELECT * FROM milestones WHERE project_id=? ORDER BY due", (p,)).fetchall()
    tests = d.execute("SELECT SUM(passed) p, COUNT(*) n FROM tests WHERE project_id=?", (p,)).fetchone()
    hw = d.execute("SELECT COUNT(*) n, SUM(cost*qty) c FROM hardware WHERE project_id=?", (p,)).fetchone()
    act = d.execute("SELECT * FROM activity WHERE project_id=? AND ts>=? ORDER BY id DESC LIMIT 40", (p, week_ago)).fetchall()
    project = d.execute("SELECT * FROM projects WHERE id=?", (p,)).fetchone()
    return dict(tasks=tasks, by_status=by_status, by_user=by_user, done_week=done_week, overdue=overdue, upcoming=upcoming,
                time=time, ms=ms, tests=tests, hw=hw, act=act, project=project, week_ago=week_ago)


@pm.route("/reports")
@login_required
def reports():
    return render_template("reports.html", **report_data())


@pm.route("/reports/pdf")
@login_required
def reports_pdf():
    from app import _pdf
    r = report_data(); total = len(r["tasks"]) or 1
    lines = [f"Progress: {r['by_status']['done']} of {len(r['tasks'])} tasks done ({round(100*r['by_status']['done']/total)}%).",
             [["Status"] + [l for _, l in STATUSES], ["Tasks"] + [str(r["by_status"][s]) for s, _ in STATUSES]],
             "Team:", [["Member", "Tasks", "Done", "Overdue", "Hours logged"]] +
             [[u, v["total"], v["done"], v["overdue"], f"{r['time'].get(u,0)/60:.1f}"] for u, v in r["by_user"].items()],
             "Completed in the last 7 days:"] + [f"• {t['title']} ({t['assignee'] or '—'}, {t['done_ts']})" for t in r["done_week"]] or ["(none)"]
    lines += ["Overdue:"] + ([f"• {t['title']} — due {t['due']} ({t['assignee'] or '—'})" for t in r["overdue"]] or ["(none)"])
    lines += ["Due in the next 14 days:"] + ([f"• {t['title']} — {t['due']} ({t['assignee'] or '—'})" for t in r["upcoming"]] or ["(none)"])
    lines += ["Milestones:", [["Milestone", "Amount", "Due", "Status"]] + [[m["title"], f"{m['amount'] or 0:,.0f}", m["due"] or "", m["status"]] for m in r["ms"]] or [["(none)", "", "", ""]]]
    lines += [f"Hardware: {r['hw']['n'] or 0} parts, BOM ≈ ₹{r['hw']['c'] or 0:,.0f}. Tests: {r['tests']['p'] or 0} passed of {r['tests']['n'] or 0}."]
    return _pdf(f"Project report — {r['project']['name']}", lines,
                [("Generated", now()), ("By", session["user"]), ("Period", f"{r['week_ago']} to {today()}")])


@pm.route("/my")
@login_required
def my_tasks():
    d = db()
    rows = d.execute("SELECT t.*, p.name pname FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.assignee=? AND t.status!='done' "
                     "ORDER BY CASE WHEN t.due IS NULL THEN 1 ELSE 0 END, t.due, CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END", (session["user"],)).fetchall()
    return render_template("my.html", rows=rows)
