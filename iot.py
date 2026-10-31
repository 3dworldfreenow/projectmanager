"""
Rainflow — live IoT data module (Blueprint).

Devices get an API key. Data arrives by HTTP POST (/api/v1/data) or MQTT
(topic rainflow/<key>/data on your broker); commands go back over MQTT
(rainflow/<key>/cmd). Readings are stored in SQLite and shown live on /data.
"""
import os, json, time, threading, datetime, secrets
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, flash, abort, Response

iot = Blueprint("iot", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
  id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT, key TEXT UNIQUE, location TEXT,
  created_by TEXT, ts TEXT, last_seen TEXT, last_data TEXT, enabled INTEGER DEFAULT 1, notes TEXT);
CREATE TABLE IF NOT EXISTS readings(
  id INTEGER PRIMARY KEY, device_id INTEGER, ts TEXT, field TEXT, value REAL);
CREATE INDEX IF NOT EXISTS idx_readings ON readings(device_id, field, ts);
CREATE TABLE IF NOT EXISTS device_commands(
  id INTEGER PRIMARY KEY, device_id INTEGER, payload TEXT, sent_by TEXT, ts TEXT, delivered INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, device_id INTEGER, field TEXT, op TEXT, threshold REAL, message TEXT,
  last_fired TEXT, enabled INTEGER DEFAULT 1);
"""

# ------------------------------------------------------------------ helpers --
def db():
    from app import db as _db
    return _db()

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def pid():
    return session.get("pid", 1)

def login_required(f):
    from functools import wraps
    @wraps(f)
    def w(*a, **k):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w

def init_iot(con):
    con.executescript(SCHEMA); con.commit()


# --------------------------------------------------------------- ingestion --
def store_reading(con, device, data, ts=None):
    """Store a dict of numeric fields for a device. Non-numeric values are kept in last_data only."""
    ts = ts or now()
    numeric = {}
    for k, v in (data or {}).items():
        if isinstance(v, bool): v = int(v)
        if isinstance(v, (int, float)):
            numeric[k] = float(v)
        else:
            try: numeric[k] = float(v)
            except (TypeError, ValueError): pass
    con.executemany("INSERT INTO readings(device_id,ts,field,value) VALUES(?,?,?,?)",
                    [(device["id"], ts, k, v) for k, v in numeric.items()])
    con.execute("UPDATE devices SET last_seen=?, last_data=? WHERE id=?", (ts, json.dumps(data)[:4000], device["id"]))
    con.commit()
    check_alerts(con, device, numeric)
    return numeric

def check_alerts(con, device, numeric):
    """Simple threshold alerts → in-app + push notification (max once per 10 min per alert)."""
    import sqlite3
    try:
        rows = con.execute("SELECT * FROM alerts WHERE device_id=? AND enabled=1", (device["id"],)).fetchall()
    except sqlite3.OperationalError:
        return
    for a in rows:
        v = numeric.get(a["field"])
        if v is None: continue
        hit = (a["op"] == ">" and v > a["threshold"]) or (a["op"] == "<" and v < a["threshold"]) or (a["op"] == "=" and v == a["threshold"])
        if not hit: continue
        if a["last_fired"] and (datetime.datetime.now() - datetime.datetime.strptime(a["last_fired"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 600:
            continue
        con.execute("UPDATE alerts SET last_fired=? WHERE id=?", (now(), a["id"]))
        msg = a["message"] or f"{device['name']}: {a['field']} is {v:g} ({a['op']} {a['threshold']:g})"
        users = [r["username"] for r in con.execute("SELECT username FROM users WHERE active=1")]
        for u in users:
            con.execute("INSERT INTO notifications(username,text,link,ts) VALUES(?,?,?,?)", (u, "⚠ " + msg, "/data", now()))
        con.commit()
        try:
            from pm import push
            push(users, "⚠ Sensor alert", msg, "/data", urgent=True, even_if_online=True)
        except Exception:
            pass

def device_by_key(con, key):
    return con.execute("SELECT * FROM devices WHERE key=? AND enabled=1", (key,)).fetchone() if key else None


# ------------------------------------------------------------------- MQTT ----
MQTT_STATE = {"connected": False, "error": None, "received": 0, "last": None, "lib": True, "host": os.environ.get("MQTT_HOST")}
_mqtt_client = None

def mqtt_start():
    """Background MQTT subscriber: rainflow/<device_key>/data  (JSON payload)."""
    global _mqtt_client
    host = os.environ.get("MQTT_HOST")
    if not host: return
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        MQTT_STATE.update(lib=False, error="paho-mqtt not installed (uv pip install --python .venv paho-mqtt)"); return
    import sqlite3
    from app import DB

    def on_connect(c, u, flags, rc, *a):
        MQTT_STATE.update(connected=(rc == 0), error=None if rc == 0 else f"connect rc={rc}")
        c.subscribe("rainflow/+/data")

    def on_disconnect(c, u, rc, *a):
        MQTT_STATE["connected"] = False

    def on_message(c, u, msg):
        try:
            key = msg.topic.split("/")[1]
            data = json.loads(msg.payload.decode())
            if not isinstance(data, dict): return
            con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
            dev = device_by_key(con, key)
            if dev:
                store_reading(con, dev, data.get("data", data) if "data" in data and isinstance(data.get("data"), dict) else data)
                MQTT_STATE["received"] += 1; MQTT_STATE["last"] = now()
            con.close()
        except Exception as e:
            MQTT_STATE["error"] = str(e)[:120]

    try:
        try:
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rainflow-server")
        except Exception:
            cl = mqtt.Client(client_id="rainflow-server")
        if os.environ.get("MQTT_USER"):
            cl.username_pw_set(os.environ["MQTT_USER"], os.environ.get("MQTT_PASS", ""))
        if os.environ.get("MQTT_TLS") == "1":
            cl.tls_set()
        cl.on_connect = on_connect; cl.on_message = on_message; cl.on_disconnect = on_disconnect
        cl.connect_async(host, int(os.environ.get("MQTT_PORT", 1883)), 30)
        cl.loop_start(); _mqtt_client = cl
    except Exception as e:
        MQTT_STATE["error"] = str(e)[:120]

def mqtt_publish(topic, payload):
    if _mqtt_client and MQTT_STATE["connected"]:
        _mqtt_client.publish(topic, payload, qos=1); return True
    return False


# ------------------------------------------------------------- public API ----
def _api_device():
    key = request.headers.get("X-Device-Key") or request.args.get("key") or (request.get_json(silent=True) or {}).get("key")
    dev = device_by_key(db(), key)
    if not dev:
        abort(Response(json.dumps({"ok": False, "error": "invalid or missing device key"}), 401, {"Content-Type": "application/json"}))
    return dev

@iot.route("/api/v1/data", methods=["POST"])
def api_data():
    """Device → server. JSON body: {"temp": 22.5, "hum": 61} or {"data": {...}, "ts": "2026-10-31 14:00:00"}.
    Auth: header X-Device-Key: <key>  (or ?key=)."""
    dev = _api_device()
    j = request.get_json(silent=True)
    if j is None:  # allow form / query values too
        j = {k: v for k, v in request.form.items()} or {k: v for k, v in request.args.items() if k != "key"}
    data = j.get("data") if isinstance(j.get("data"), dict) else {k: v for k, v in j.items() if k not in ("key", "ts")}
    stored = store_reading(db(), dev, data, j.get("ts"))
    # pending commands piggy-back on the response so HTTP-only devices can receive them
    cmds = db().execute("SELECT id,payload FROM device_commands WHERE device_id=? AND delivered=0 ORDER BY id", (dev["id"],)).fetchall()
    if cmds:
        db().execute("UPDATE device_commands SET delivered=1 WHERE device_id=? AND delivered=0", (dev["id"],)); db().commit()
    return jsonify(ok=True, stored=list(stored.keys()), commands=[json.loads(c["payload"]) for c in cmds])

@iot.route("/api/v1/commands", methods=["GET"])
def api_commands():
    """Device polls for commands (HTTP devices). Returns and marks them delivered."""
    dev = _api_device()
    cmds = db().execute("SELECT id,payload,ts FROM device_commands WHERE device_id=? AND delivered=0 ORDER BY id", (dev["id"],)).fetchall()
    db().execute("UPDATE device_commands SET delivered=1 WHERE device_id=? AND delivered=0", (dev["id"],)); db().commit()
    return jsonify(ok=True, commands=[dict(id=c["id"], ts=c["ts"], **json.loads(c["payload"])) for c in cmds])

def _reader_auth():
    """Read endpoints: logged-in user, or a device key, or ?token=READ_TOKEN env."""
    if "user" in session: return True
    key = request.headers.get("X-Device-Key") or request.args.get("key")
    if key and device_by_key(db(), key): return True
    tok = os.environ.get("API_READ_TOKEN")
    if tok and (request.args.get("token") == tok or request.headers.get("Authorization") == "Bearer " + tok): return True
    abort(Response(json.dumps({"ok": False, "error": "login, device key or API_READ_TOKEN required"}), 401, {"Content-Type": "application/json"}))

@iot.route("/api/v1/devices")
def api_devices():
    _reader_auth()
    rows = db().execute("SELECT id,name,location,last_seen,last_data,enabled FROM devices ORDER BY id").fetchall()
    return jsonify(ok=True, devices=[dict(r, last_data=json.loads(r["last_data"]) if r["last_data"] else None) for r in rows])

@iot.route("/api/v1/latest")
def api_latest():
    _reader_auth()
    dev_id = request.args.get("device")
    q = "SELECT id,name,last_seen,last_data FROM devices" + (" WHERE id=?" if dev_id else "")
    rows = db().execute(q, (dev_id,) if dev_id else ()).fetchall()
    return jsonify(ok=True, devices=[dict(r, last_data=json.loads(r["last_data"]) if r["last_data"] else None) for r in rows])

@iot.route("/api/v1/readings")
def api_readings():
    """?device=<id>&field=temp&from=2026-10-30&to=2026-10-31&limit=1000  (newest first)."""
    _reader_auth()
    dev_id = request.args.get("device") or abort(400); field = request.args.get("field")
    q = "SELECT ts,field,value FROM readings WHERE device_id=?"; a = [dev_id]
    if field: q += " AND field=?"; a.append(field)
    if request.args.get("from"): q += " AND ts>=?"; a.append(request.args["from"])
    if request.args.get("to"): q += " AND ts<=?"; a.append(request.args["to"] + (" 23:59:59" if len(request.args["to"]) == 10 else ""))
    q += " ORDER BY ts DESC LIMIT ?"; a.append(min(int(request.args.get("limit", 1000)), 20000))
    rows = db().execute(q, a).fetchall()
    if request.args.get("format") == "csv":
        out = "ts,field,value\n" + "".join(f"{r['ts']},{r['field']},{r['value']}\n" for r in rows)
        return Response(out, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=readings.csv"})
    return jsonify(ok=True, count=len(rows), readings=[dict(r) for r in rows])

@iot.route("/api/v1/command", methods=["POST"])
@login_required
def api_command():
    """Server → device. JSON: {"device": 1, "cmd": "relay", "value": 1}. Sent over MQTT (rainflow/<key>/cmd) and queued for HTTP polling."""
    j = request.get_json(silent=True) or request.form.to_dict()
    dev = db().execute("SELECT * FROM devices WHERE id=?", (j.get("device"),)).fetchone() or abort(404)
    payload = {k: v for k, v in j.items() if k != "device"}
    db().execute("INSERT INTO device_commands(device_id,payload,sent_by,ts) VALUES(?,?,?,?)", (dev["id"], json.dumps(payload), session["user"], now())); db().commit()
    via_mqtt = mqtt_publish(f"rainflow/{dev['key']}/cmd", json.dumps(payload))
    return jsonify(ok=True, via="mqtt" if via_mqtt else "queued for HTTP poll")


# ------------------------------------------------------------- UI pages ----
@iot.route("/data")
@login_required
def data_page():
    devs = db().execute("SELECT * FROM devices WHERE project_id=? ORDER BY id", (pid(),)).fetchall()
    out = []
    for d in devs:
        fields = [r["field"] for r in db().execute("SELECT DISTINCT field FROM readings WHERE device_id=? ORDER BY field", (d["id"],))]
        online = bool(d["last_seen"]) and (datetime.datetime.now() - datetime.datetime.strptime(d["last_seen"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 600
        out.append(dict(d, fields=fields, online=online, last=json.loads(d["last_data"]) if d["last_data"] else {}))
    alerts = db().execute("SELECT a.*, d.name dname FROM alerts a JOIN devices d ON d.id=a.device_id WHERE d.project_id=?", (pid(),)).fetchall()
    return render_template("data.html", devices=out, mqtt=MQTT_STATE, alerts=alerts,
                           base_url=os.environ.get("APP_URL") or request.url_root.rstrip("/"))

@iot.route("/data/series")
@login_required
def data_series():
    """Chart data: ?device=1&hours=24 → {field: [[ts, value], ...]} (downsampled to ~300 points)."""
    dev_id = int(request.args.get("device")); hours = float(request.args.get("hours", 24))
    since = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db().execute("SELECT ts,field,value FROM readings WHERE device_id=? AND ts>=? ORDER BY ts", (dev_id, since)).fetchall()
    series = {}
    for r in rows: series.setdefault(r["field"], []).append([r["ts"], r["value"]])
    for f, pts in series.items():
        if len(pts) > 300:
            step = len(pts) / 300.0
            series[f] = [pts[int(i * step)] for i in range(300)] + [pts[-1]]
    return jsonify(series)

@iot.route("/data/devices", methods=["POST"])
@login_required
def device_new():
    f = request.form
    key = "rf_" + secrets.token_hex(12)
    db().execute("INSERT INTO devices(project_id,name,key,location,created_by,ts,notes) VALUES(?,?,?,?,?,?,?)",
                 (pid(), f["name"].strip(), key, f.get("location", ""), session["user"], now(), f.get("notes", "")))
    db().commit()
    from app import log_activity; log_activity("added device", f["name"], "/data"); db().commit()
    flash(f"Device added. Its key: {key} — put it in the firmware (see API docs).")
    return redirect(url_for("iot.data_page"))

@iot.route("/data/devices/<int:did>/<action>", methods=["POST"])
@login_required
def device_action(did, action):
    d = db()
    if action == "toggle": d.execute("UPDATE devices SET enabled=1-enabled WHERE id=?", (did,))
    elif action == "newkey": d.execute("UPDATE devices SET key=? WHERE id=?", ("rf_" + secrets.token_hex(12), did))
    elif action == "delete":
        d.execute("DELETE FROM readings WHERE device_id=?", (did,)); d.execute("DELETE FROM alerts WHERE device_id=?", (did,))
        d.execute("DELETE FROM device_commands WHERE device_id=?", (did,)); d.execute("DELETE FROM devices WHERE id=?", (did,))
    elif action == "clear": d.execute("DELETE FROM readings WHERE device_id=?", (did,))
    elif action == "alert":
        f = request.form
        d.execute("INSERT INTO alerts(device_id,field,op,threshold,message) VALUES(?,?,?,?,?)",
                  (did, f["field"].strip(), f["op"], float(f["threshold"]), f.get("message", "")))
    elif action == "alertdel": d.execute("DELETE FROM alerts WHERE id=? AND device_id=?", (request.form["alert_id"], did))
    d.commit(); return redirect(url_for("iot.data_page"))

@iot.route("/api/docs")
@login_required
def api_docs():
    devs = db().execute("SELECT id,name,key FROM devices WHERE project_id=? ORDER BY id", (pid(),)).fetchall()
    return render_template("api_docs.html", devices=devs, mqtt=MQTT_STATE,
                           base_url=os.environ.get("APP_URL") or request.url_root.rstrip("/"),
                           mqtt_host=os.environ.get("MQTT_HOST") or "iot.opirsys.com", mqtt_port=os.environ.get("MQTT_PORT", "1883"),
                           read_token=bool(os.environ.get("API_READ_TOKEN")))

@iot.route("/api/v1/mqtt/status")
@login_required
def mqtt_status():
    return jsonify(MQTT_STATE)
