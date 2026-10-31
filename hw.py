"""
Rainflow — hardware design module (Blueprint).

Designs (sketch / schematic / PCB / enclosure / wiring / photos) with versions,
tap-to-annotate pins on images linked to BOM parts, enclosure specs,
and a connections (wiring) table.
"""
import os, json, datetime, re
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, abort, flash, Response

hw = Blueprint("hw", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS designs(
  id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, type TEXT, status TEXT DEFAULT 'draft',
  notes TEXT, specs TEXT, created_by TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS design_versions(
  id INTEGER PRIMARY KEY, design_id INTEGER, version INTEGER, filename TEXT, orig_name TEXT,
  note TEXT, by TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS design_pins(
  id INTEGER PRIMARY KEY, version_id INTEGER, x REAL, y REAL, label TEXT, hardware_id INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS connections(
  id INTEGER PRIMARY KEY, project_id INTEGER, from_part INTEGER, from_pin TEXT, to_part INTEGER, to_pin TEXT,
  color TEXT, note TEXT, position INTEGER DEFAULT 0);
"""

TYPES = [("sketch", "Sketch / idea", "✏️"), ("schematic", "Schematic", "🔌"), ("pcb", "PCB layout", "🟩"),
         ("enclosure", "Enclosure / box", "📦"), ("wiring", "Wiring diagram", "🧵"), ("photo", "Build photos", "📷"), ("other", "Other", "📄")]
STATUSES = [("draft", "Draft"), ("review", "In review"), ("approved", "Approved"), ("old", "Superseded")]
IMG = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

CATEGORIES = ["MCU / module", "Sensor", "Passive (R, C, L, diode)", "Semiconductor / IC", "Connector / header",
              "Wire & cable", "Power (battery, solar, regulator)", "Mechanical / enclosure", "Fastener", "Consumable", "Other"]


def db():
    from app import db as _db
    return _db()

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

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

def init_hw(con):
    con.executescript(SCHEMA)
    for col, typ in [("category", "TEXT"), ("value", "TEXT"), ("package", "TEXT"), ("refs", "TEXT"), ("per_board", "INTEGER DEFAULT 1")]:
        try: con.execute(f"ALTER TABLE hardware ADD COLUMN {col} {typ}")
        except Exception: pass
    try: con.execute("ALTER TABLE projects ADD COLUMN boards INTEGER DEFAULT 1")
    except Exception: pass
    con.commit()

def is_img(fn):
    return bool(fn) and os.path.splitext(fn)[1].lower() in IMG

CAT_ICON = {"MCU / module": "🧠", "Sensor": "🌡️", "Passive (R, C, L, diode)": "⚡", "Semiconductor / IC": "🔲", "Connector / header": "🔌",
            "Wire & cable": "🧵", "Power (battery, solar, regulator)": "🔋", "Mechanical / enclosure": "📦", "Fastener": "🔩", "Consumable": "🧴", "Other": "🔧"}
TYPE_ICON = {k: ic for k, _, ic in TYPES}; TYPE_LABEL = {k: l for k, l, _ in TYPES}

@hw.app_context_processor
def inject():
    return dict(HW_TYPES=TYPES, HW_STATUSES=STATUSES, HW_CATEGORIES=CATEGORIES, CAT_ICON=CAT_ICON, TYPE_ICON=TYPE_ICON, TYPE_LABEL=TYPE_LABEL)


# ----------------------------------------------------------------- designs --
@hw.route("/designs")
@login_required
def designs():
    d = db()
    rows = d.execute("SELECT * FROM designs WHERE project_id=? ORDER BY status='old', id DESC", (pid(),)).fetchall()
    out = []
    for r in rows:
        v = d.execute("SELECT * FROM design_versions WHERE design_id=? ORDER BY version DESC LIMIT 1", (r["id"],)).fetchone()
        nv = d.execute("SELECT COUNT(*) FROM design_versions WHERE design_id=?", (r["id"],)).fetchone()[0]
        npins = d.execute("SELECT COUNT(*) FROM design_pins WHERE version_id=?", (v["id"],)).fetchone()[0] if v else 0
        out.append(dict(r, latest=v, nv=nv, npins=npins, thumb=v["filename"] if v and is_img(v["filename"]) else None))
    return render_template("designs.html", designs=out)

@hw.route("/designs/new", methods=["POST"])
@login_required
def design_new():
    from app import save_upload, log_activity
    f = request.form
    cur = db().execute("INSERT INTO designs(project_id,title,type,notes,specs,created_by,ts) VALUES(?,?,?,?,?,?,?)",
                       (pid(), f["title"].strip(), f.get("type", "sketch"), f.get("notes", ""), "{}", session["user"], now()))
    did = cur.lastrowid
    up = request.files.get("file")
    if up and up.filename:
        fn, orig = save_upload(up)
        db().execute("INSERT INTO design_versions(design_id,version,filename,orig_name,note,by,ts) VALUES(?,?,?,?,?,?,?)",
                     (did, 1, fn, orig, "first version", session["user"], now()))
    log_activity("added design", f["title"], f"/designs/{did}"); db().commit()
    return redirect(url_for("hw.design", did=did))

@hw.route("/designs/<int:did>")
@login_required
def design(did):
    d = db()
    des = d.execute("SELECT * FROM designs WHERE id=?", (did,)).fetchone() or abort(404)
    versions = d.execute("SELECT * FROM design_versions WHERE design_id=? ORDER BY version DESC", (did,)).fetchall()
    vid = request.args.get("v")
    cur = next((v for v in versions if str(v["id"]) == vid), versions[0] if versions else None)
    pins = d.execute("SELECT p.*, h.name AS part_name, h.refs AS part_refs FROM design_pins p LEFT JOIN hardware h ON h.id=p.hardware_id "
                     "WHERE p.version_id=? ORDER BY p.id", (cur["id"],)).fetchall() if cur else []
    parts = d.execute("SELECT id,name,refs,value FROM hardware WHERE project_id=? ORDER BY category,name", (pid(),)).fetchall()
    specs = json.loads(des["specs"] or "{}")
    return render_template("design.html", des=des, versions=versions, cur=cur, pins=pins, parts=parts, specs=specs,
                           is_img=is_img(cur["filename"]) if cur else False,
                           is_pdf=bool(cur) and cur["filename"].lower().endswith(".pdf"))

@hw.route("/designs/<int:did>/version", methods=["POST"])
@login_required
def design_version(did):
    from app import save_upload, log_activity
    d = db(); des = d.execute("SELECT * FROM designs WHERE id=?", (did,)).fetchone() or abort(404)
    up = request.files.get("file")
    if not up or not up.filename:
        flash("Choose a file to upload"); return redirect(url_for("hw.design", did=did))
    fn, orig = save_upload(up)
    v = (d.execute("SELECT MAX(version) FROM design_versions WHERE design_id=?", (did,)).fetchone()[0] or 0) + 1
    cur = d.execute("INSERT INTO design_versions(design_id,version,filename,orig_name,note,by,ts) VALUES(?,?,?,?,?,?,?)",
                    (did, v, fn, orig, request.form.get("note", ""), session["user"], now()))
    # carry pins forward if asked (same image geometry)
    if request.form.get("copy_pins") and v > 1:
        prev = d.execute("SELECT id FROM design_versions WHERE design_id=? AND version=?", (did, v - 1)).fetchone()
        if prev:
            for p in d.execute("SELECT * FROM design_pins WHERE version_id=?", (prev["id"],)).fetchall():
                d.execute("INSERT INTO design_pins(version_id,x,y,label,hardware_id,note) VALUES(?,?,?,?,?,?)",
                          (cur.lastrowid, p["x"], p["y"], p["label"], p["hardware_id"], p["note"]))
    log_activity(f"uploaded design v{v}", des["title"], f"/designs/{did}"); d.commit()
    return redirect(url_for("hw.design", did=did))

@hw.route("/designs/<int:did>/update", methods=["POST"])
@login_required
def design_update(did):
    d = db(); f = request.form
    des = d.execute("SELECT * FROM designs WHERE id=?", (did,)).fetchone() or abort(404)
    if "status" in f and f["status"] in dict(STATUSES):
        d.execute("UPDATE designs SET status=? WHERE id=?", (f["status"], did))
        from app import log_activity; log_activity(f"design {dict(STATUSES)[f['status']].lower()}", des["title"], f"/designs/{did}")
    else:
        specs = json.loads(des["specs"] or "{}")
        for k in ["length_mm", "width_mm", "height_mm", "material", "ip_rating", "color", "mounting", "glands", "weight_g", "finish", "board_w_mm", "board_h_mm", "layers", "thickness_mm", "fab", "order_status"]:
            if k in f: specs[k] = f[k].strip()
        d.execute("UPDATE designs SET title=?, type=?, notes=?, specs=? WHERE id=?",
                  (f.get("title", des["title"]).strip(), f.get("type", des["type"]), f.get("notes", des["notes"]), json.dumps(specs), did))
    d.commit(); return redirect(url_for("hw.design", did=did))

@hw.route("/designs/<int:did>/delete", methods=["POST"])
@login_required
def design_delete(did):
    d = db()
    for v in d.execute("SELECT id FROM design_versions WHERE design_id=?", (did,)).fetchall():
        d.execute("DELETE FROM design_pins WHERE version_id=?", (v["id"],))
    d.execute("DELETE FROM design_versions WHERE design_id=?", (did,)); d.execute("DELETE FROM designs WHERE id=?", (did,)); d.commit()
    return redirect(url_for("hw.designs"))

# pins (annotations)
@hw.route("/designs/pins/<int:vid>", methods=["POST"])
@login_required
def pin_add(vid):
    j = request.get_json()
    cur = db().execute("INSERT INTO design_pins(version_id,x,y,label,hardware_id,note) VALUES(?,?,?,?,?,?)",
                       (vid, float(j["x"]), float(j["y"]), (j.get("label") or "").strip(), j.get("hardware_id") or None, (j.get("note") or "").strip()))
    db().commit(); return jsonify(ok=True, id=cur.lastrowid)

@hw.route("/designs/pins/<int:pin_id>/update", methods=["POST"])
@login_required
def pin_update(pin_id):
    j = request.get_json()
    if j.get("delete"):
        db().execute("DELETE FROM design_pins WHERE id=?", (pin_id,))
    else:
        db().execute("UPDATE design_pins SET label=?, hardware_id=?, note=?, x=COALESCE(?,x), y=COALESCE(?,y) WHERE id=?",
                     ((j.get("label") or "").strip(), j.get("hardware_id") or None, (j.get("note") or "").strip(), j.get("x"), j.get("y"), pin_id))
    db().commit(); return jsonify(ok=True)


# ------------------------------------------------------------- connections --
@hw.route("/connections", methods=["GET", "POST"])
@login_required
def connections():
    d = db()
    if request.method == "POST":
        f = request.form
        if f.get("delete"):
            d.execute("DELETE FROM connections WHERE id=? AND project_id=?", (f["delete"], pid()))
        else:
            pos = (d.execute("SELECT MAX(position) FROM connections WHERE project_id=?", (pid(),)).fetchone()[0] or 0) + 1
            d.execute("INSERT INTO connections(project_id,from_part,from_pin,to_part,to_pin,color,note,position) VALUES(?,?,?,?,?,?,?,?)",
                      (pid(), f.get("from_part") or None, f.get("from_pin", "").strip(), f.get("to_part") or None, f.get("to_pin", "").strip(),
                       f.get("color", "").strip(), f.get("note", "").strip(), pos))
        d.commit(); return redirect(url_for("hw.connections"))
    rows = d.execute("SELECT c.*, a.name AS from_name, a.refs AS from_refs, b.name AS to_name, b.refs AS to_refs FROM connections c "
                     "LEFT JOIN hardware a ON a.id=c.from_part LEFT JOIN hardware b ON b.id=c.to_part WHERE c.project_id=? ORDER BY c.position, c.id", (pid(),)).fetchall()
    parts = d.execute("SELECT id,name,refs FROM hardware WHERE project_id=? ORDER BY category,name", (pid(),)).fetchall()
    if request.args.get("format") == "csv":
        out = "from,from_pin,to,to_pin,wire,note\n" + "".join(f'"{r["from_name"] or ""}",{r["from_pin"]},"{r["to_name"] or ""}",{r["to_pin"]},{r["color"]},"{r["note"]}"\n' for r in rows)
        return Response(out, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=connections.csv"})
    return render_template("connections.html", rows=rows, parts=parts)


# ------------------------------------------------------------ BOM helpers --
def parse_quick(line):
    """Quick-add: 'R 10k 0603 x3 R1,R4,R7'  |  'C 100nF 50V 0805 x2 C1,C2'  |  'ESP32 DevKit x1'"""
    t = line.strip()
    out = {"name": t, "category": "Other", "value": "", "package": "", "refs": "", "per_board": 1}
    m = re.search(r"\bx\s*(\d+)\b", t, re.I)
    if m: out["per_board"] = int(m.group(1)); t = (t[:m.start()] + t[m.end():]).strip()
    m = re.search(r"\b([RCLDUJQ]\d+(?:\s*,\s*[RCLDUJQ]\d+)*)\s*$", t)
    if m: out["refs"] = re.sub(r"\s+", "", m.group(1)); t = t[:m.start()].strip()
    m = re.search(r"\b(0201|0402|0603|0805|1206|1210|2512|SOT-?\d+|SOIC-?\d+|TQFP-?\d+|QFN-?\d+|DIP-?\d+|TO-?\d+|TH)\b", t, re.I)
    if m: out["package"] = m.group(1).upper(); t = (t[:m.start()] + t[m.end():]).strip()
    head = t.split()[0].upper() if t.split() else ""
    kinds = {"R": ("Resistor", "Passive (R, C, L, diode)"), "C": ("Capacitor", "Passive (R, C, L, diode)"), "L": ("Inductor", "Passive (R, C, L, diode)"),
             "D": ("Diode", "Passive (R, C, L, diode)"), "LED": ("LED", "Passive (R, C, L, diode)"), "Q": ("Transistor", "Semiconductor / IC"),
             "U": ("IC", "Semiconductor / IC"), "J": ("Connector", "Connector / header"), "W": ("Wire", "Wire & cable")}
    if head in kinds:
        out["name"], out["category"] = kinds[head]; out["value"] = t[len(t.split()[0]):].strip()
        if out["value"]:
            # passives read as "Resistor 10k"; modules / connectors keep their own name ("ESP32 DevKit", "JST-XH 2pin")
            out["name"] = out["value"] if head in ("U", "J", "Q", "W") else f"{kinds[head][0]} {out['value']}"
            if head == "J" and "connector" not in out["value"].lower(): out["name"] = out["value"] + " connector"
    else:
        out["name"] = t
    return out
