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
CREATE TABLE IF NOT EXISTS modules(
  id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT, per_product INTEGER DEFAULT 1, notes TEXT,
  design_id INTEGER, position INTEGER DEFAULT 0, ts TEXT);
CREATE TABLE IF NOT EXISTS module_parts(
  id INTEGER PRIMARY KEY, module_id INTEGER, part_id INTEGER, qty INTEGER DEFAULT 1, refs TEXT,
  UNIQUE(module_id, part_id));
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
    for col, typ in [("category", "TEXT"), ("value", "TEXT"), ("package", "TEXT"), ("refs", "TEXT"), ("per_board", "INTEGER DEFAULT 1"), ("lcsc", "TEXT")]:
        try: con.execute(f"ALTER TABLE hardware ADD COLUMN {col} {typ}")
        except Exception: pass
    try: con.execute("ALTER TABLE projects ADD COLUMN boards INTEGER DEFAULT 1")
    except Exception: pass
    # L1 product → L2 modules → L3 parts: put every existing part into a "General" module once
    if not con.execute("SELECT 1 FROM meta WHERE key='modules_v1'").fetchone():
        for (p,) in con.execute("SELECT DISTINCT project_id FROM hardware").fetchall():
            cur = con.execute("INSERT INTO modules(project_id,name,per_product,position,ts) VALUES(?,?,1,0,?)", (p, "General", now()))
            for h in con.execute("SELECT id, per_board, refs FROM hardware WHERE project_id=?", (p,)).fetchall():
                con.execute("INSERT OR IGNORE INTO module_parts(module_id,part_id,qty,refs) VALUES(?,?,?,?)", (cur.lastrowid, h[0], h[1] or 1, h[2] or ""))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('modules_v1','1')")
    con.commit()

def is_img(fn):
    return bool(fn) and os.path.splitext(fn)[1].lower() in IMG

CAT_ICON = {"MCU / module": "🧠", "Sensor": "🌡️", "Passive (R, C, L, diode)": "⚡", "Semiconductor / IC": "🔲", "Connector / header": "🔌",
            "Wire & cable": "🧵", "Power (battery, solar, regulator)": "🔋", "Mechanical / enclosure": "📦", "Fastener": "🔩", "Consumable": "🧴", "Other": "🔧"}
TYPE_ICON = {k: ic for k, _, ic in TYPES}; TYPE_LABEL = {k: l for k, l, _ in TYPES}

@hw.app_context_processor
def inject():
    return dict(HW_TYPES=TYPES, HW_STATUSES=STATUSES, HW_CATEGORIES=CATEGORIES, CAT_ICON=CAT_ICON, TYPE_ICON=TYPE_ICON, TYPE_LABEL=TYPE_LABEL, COMMON_PARTS=COMMON_PARTS)


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
    bm = d.execute("SELECT id FROM modules WHERE design_id=? AND project_id=?", (did, pid())).fetchone()
    if bm:
        ibom_parts = d.execute("SELECT h.id,h.name,h.value,mp.refs,h.package FROM module_parts mp JOIN hardware h ON h.id=mp.part_id "
                               "WHERE mp.module_id=? AND IFNULL(mp.refs,'')!='' ORDER BY h.category,h.name", (bm["id"],)).fetchall()
    else:
        ibom_parts = d.execute("SELECT id,name,value,refs,package FROM hardware WHERE project_id=? AND IFNULL(refs,'')!='' AND refs NOT LIKE '%: %' ORDER BY category,name", (pid(),)).fetchall()
    return render_template("design.html", des=des, versions=versions, cur=cur, pins=pins, parts=parts, specs=specs,
                           is_ibom=bool(cur) and is_ibom(cur["filename"]), ibom_parts=ibom_parts, ref=request.args.get("ref", ""),
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
def custom_values():
    """{part name (lower): [values]} = sizes the team saved + sizes already used in the BOM."""
    d = db(); out = {}
    r = d.execute("SELECT value FROM meta WHERE key='bom_values'").fetchone()
    saved = json.loads(r["value"]) if r else {}
    for k, vs in saved.items():
        out.setdefault(k, {"saved": [], "used": []})["saved"] = list(vs)
    for row in d.execute("SELECT DISTINCT name, value FROM hardware WHERE value IS NOT NULL AND value!=''"):
        k = (row["name"] or "").strip().lower()
        if not k: continue
        e = out.setdefault(k, {"saved": [], "used": []})
        if row["value"] not in e["used"] and row["value"] not in e["saved"]: e["used"].append(row["value"])
    return out

@hw.route("/hardware/values", methods=["POST"])
@login_required
def save_value():
    """Add or remove a custom size/value for a part name."""
    j = request.get_json(); name = (j.get("name") or "").strip().lower(); val = (j.get("value") or "").strip()
    if not name or not val: return jsonify(ok=False), 400
    d = db(); r = d.execute("SELECT value FROM meta WHERE key='bom_values'").fetchone()
    saved = json.loads(r["value"]) if r else {}
    lst = saved.setdefault(name, [])
    if j.get("remove"):
        if val in lst: lst.remove(val)
    elif val not in lst:
        lst.append(val)
    d.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('bom_values',?)", (json.dumps(saved),)); d.commit()
    return jsonify(ok=True, values=custom_values().get(name, {}))

@hw.route("/hardware/<int:hid>/quick", methods=["POST"])
@login_required
def part_quick(hid):
    """Inline edits from the simple table: +/- stock, status, qty per board, price."""
    j = request.get_json(silent=True) or request.form
    d = db(); r = d.execute("SELECT * FROM hardware WHERE id=?", (hid,)).fetchone() or abort(404)
    if "delta" in j:
        d.execute("UPDATE hardware SET qty=MAX(0, COALESCE(qty,0)+?) WHERE id=?", (int(j["delta"]), hid))
    if j.get("module") and "per_board" in j:
        link_part(module_of(j["module"]), hid, int(j["per_board"] or 1), (d.execute("SELECT refs FROM module_parts WHERE module_id=? AND part_id=?", (module_of(j["module"]), hid)).fetchone() or {"refs": ""})["refs"])
    for k in ("status", "per_board", "cost", "qty"):
        if k == "per_board" and j.get("module"): continue
        if k in j and j[k] != "":
            d.execute(f"UPDATE hardware SET {k}=? WHERE id=?", (j[k], hid))
    # "Have it" with nothing counted yet → assume we have what the build needs
    if j.get("status") in ("received", "tested", "installed") and not (r["qty"] or 0):
        boards = (d.execute("SELECT boards FROM projects WHERE id=?", (r["project_id"],)).fetchone() or {"boards": 1})["boards"] or 1
        d.execute("UPDATE hardware SET qty=? WHERE id=?", ((r["per_board"] or 1) * boards, hid))
    d.execute("UPDATE hardware SET updated_by=?, ts=? WHERE id=?", (session["user"], now(), hid)); d.commit()
    r = d.execute("SELECT qty, status, per_board, cost FROM hardware WHERE id=?", (hid,)).fetchone()
    return jsonify(ok=True, **dict(r))
def guess_category(name):
    """Pick a category from the part name so nobody has to choose one."""
    n = (name or "").lower()
    rules = [("Passive (R, C, L, diode)", ["resistor", "capacitor", "inductor", "diode", "led", "ohm", "kΩ", "nf", "uf", "µf", "pf"]),
             ("MCU / module", ["esp32", "esp8266", "arduino", "raspberry", "pico", "stm32", "devkit", "nodemcu", "lora", "gsm", "sim800", "module"]),
             ("Sensor", ["sensor", "bme", "bmp", "dht", "ds18b20", "gauge", "anemometer", "vane", "ldr", "uv", "pir", "soil", "gas", "mq-", "rain", "wind"]),
             ("Connector / header", ["connector", "jst", "header", "terminal", "socket", "plug", "usb", "jack", "pin "]),
             ("Wire & cable", ["wire", "cable", "awg", "jumper", "ribbon"]),
             ("Power (battery, solar, regulator)", ["battery", "solar", "li-ion", "lipo", "18650", "charger", "tp4056", "regulator", "buck", "boost", "ldo", "adapter", "supply", "ams1117", "lm7805", "78l05", "ht7333", "mcp1700", "xc6206", "mp1584", "lm2596", "ap2112"]),
             ("Semiconductor / IC", ["transistor", "mosfet", "ic ", "opamp", "relay", "2n2222", "bc547", "ne555", "74hc"]),
             ("Mechanical / enclosure", ["box", "enclosure", "case", "pole", "bracket", "mount", "standoff", "gland", "shield", "pcb"]),
             ("Fastener", ["screw", "nut", "bolt", "washer", "spacer"]),
             ("Consumable", ["solder", "heat shrink", "heatshrink", "glue", "tape", "flux", "silicone sealant"])]
    for cat, words in rules:
        if any(w in n for w in words): return cat
    return "Other"

COMMON_PARTS = ["Resistor", "Capacitor", "LED", "Diode", "Transistor", "ESP32 DevKit", "BME280 sensor", "DHT22 sensor",
                "Rain gauge", "Anemometer", "Solar panel", "18650 battery", "TP4056 charger", "Buck converter",
                "JST connector", "Pin header", "Screw terminal", "Wire", "Enclosure box", "Screws M3", "Relay module"]

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


# =================================================================== BOM files ==
# Import a BOM exported from KiCad, EasyEDA (Std/Pro), JLCPCB or any spreadsheet.
REF_TYPE = [("FB", "Ferrite bead"), ("TP", "Test point"), ("SW", "Switch"), ("BT", "Battery"), ("LED", "LED"),
            ("R", "Resistor"), ("C", "Capacitor"), ("L", "Inductor"), ("D", "Diode"), ("Q", "Transistor"),
            ("U", None), ("IC", None), ("J", "Connector"), ("P", "Connector"), ("CN", "Connector"), ("K", "Relay"),
            ("Y", "Crystal"), ("X", "Crystal"), ("F", "Fuse"), ("H", "Mounting hole"), ("M", "Motor"), ("BZ", "Buzzer"), ("LS", "Speaker")]
PASSIVE = {"Resistor", "Capacitor", "Inductor", "Ferrite bead", "Fuse", "Crystal"}
SMD_SIZE = re.compile(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)")

def _ref_type(ref, footprint=""):
    p = re.match(r"[A-Za-z]+", (ref or "").strip())
    if not p: return None
    p = p.group(0).upper()
    for k, t in REF_TYPE:
        if p == k:
            if t == "Diode" and "LED" in (footprint or "").upper(): return "LED"
            return t or ""
    return None

def _short_pkg(fp):
    """'Resistor_SMD:R_0603_1608Metric' → '0603'; 'Package_TO_SOT_SMD:SOT-23' → 'SOT-23'."""
    fp = (fp or "").strip()
    if not fp: return ""
    m = SMD_SIZE.search(fp)
    if m: return m.group(1)
    fp = fp.split(":")[-1]
    for pre in ("R_", "C_", "L_", "D_", "LED_"):
        if fp.startswith(pre): fp = fp[len(pre):]
    fp = re.sub(r"_(Vertical|Horizontal|Metric|HandSolder|Pad[\d.x]+mm.*)$", "", fp).replace("_", " ")
    return fp[:28]

def _ref_sort(r):
    m = re.match(r"([A-Za-z]+)(\d+)", r)
    return (m.group(1), int(m.group(2))) if m else (r, 0)

def _read_table(raw, filename):
    """Return list of dict rows from CSV/TSV (any encoding, incl. EasyEDA UTF-16) or XLSX."""
    import csv, io
    fn = (filename or "").lower()
    if fn.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
        rows = [[("" if c is None else str(c)).strip() for c in r] for r in ws.iter_rows(values_only=True)]
    else:
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"): txt = raw.decode("utf-16")
        else:
            try: txt = raw.decode("utf-8-sig")
            except UnicodeDecodeError: txt = raw.decode("latin-1")
        first = txt.splitlines()[0] if txt.strip() else ""
        delim = max(["\t", ",", ";"], key=first.count)
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(txt), delimiter=delim)]
    # header = first row that looks like a header (has a known column)
    known = {"reference", "references", "designator", "ref", "refs", "value", "comment", "name", "footprint", "package", "qty", "quantity"}
    for i, r in enumerate(rows[:15]):
        if sum(1 for c in r if c.lower().strip(" #") in known) >= 2:
            hdr = [c.strip() for c in r]
            return [dict(zip(hdr, x)) for x in rows[i + 1:] if any(x)]
    return []

def parse_bom(raw, filename):
    """Normalise any BOM file into grouped parts: name, value, package, refs, per_board, lcsc, part_no, maker, supplier, datasheet, cost."""
    out = {}
    for row in _read_table(raw, filename):
        low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        def g(*keys):
            for k in keys:
                for col, v in low.items():
                    if v and (col == k or col.replace(" ", "_") == k or col.replace(" ", "") == k.replace("_", "")): return v
            return ""
        refs = g("reference", "references", "designator", "designators", "ref", "refs", "ref_des")
        if refs.lower() in ("", "total") and not g("name", "value", "comment"): continue
        refl = [r for r in re.split(r"[,\s;]+", refs) if r]
        value = g("value", "comment", "val")
        fp = g("footprint", "package", "case", "pcb_footprint")
        lcsc = g("lcsc", "lcsc_part", "lcsc_part_#", "lcsc_part_number", "jlcpcb_part_#", "jlcpcb_part", "supplier_part", "lcsc#")
        if lcsc and not re.fullmatch(r"C\d{2,9}", lcsc, re.I): lcsc = ""
        mpn = g("manufacturer_part", "mpn", "mfr_part", "manufacturer_part_number", "part_number", "part_no", "mfr._part_#")
        maker = g("manufacturer", "mfr", "brand")
        qty = g("qty", "quantity", "qty_per_board", "count")
        name = g("name", "description", "part")
        t = _ref_type(refl[0], fp) if refl else None
        if t in PASSIVE or t in ("LED", "Diode", "Connector", "Switch", "Test point", "Mounting hole"):
            if not value: value = name                       # EasyEDA puts the value (10kΩ) in "Name"
            pname = t
        else:
            pname = (name if name and name != value else value) or (t or "Part"); value = "" if pname == value else value
        pkg = _short_pkg(fp)
        key = (pname.lower(), value.lower(), pkg.lower(), lcsc.upper())
        e = out.setdefault(key, dict(name=pname, value=value, package=pkg, refs=[], per_board=0, lcsc=lcsc.upper(), part_no=mpn,
                                     maker=maker, supplier="LCSC" if lcsc else g("supplier", "vendor"), datasheet=g("datasheet") if g("datasheet").startswith("http") else "",
                                     cost=g("price", "unit_price", "cost") or (str(float(re.sub(r"[^\d.]", "", g("lcsc_price")) or 0) * inr_rate()) if g("lcsc_price") else "")))
        e["refs"] += refl
        e["per_board"] += len(refl) if refl else int(float(qty or 1))
        if refl and qty and len(refl) == 1 and int(float(qty)) > 1: e["per_board"] += int(float(qty)) - 1
    parts = []
    for e in out.values():
        if any(r.upper().startswith(("TP", "H", "MH", "FID")) for r in e["refs"]) and not e["lcsc"] and e["name"] in ("Test point", "Mounting hole", "Part"):
            continue                                            # board features, not parts to buy
        e["refs"] = ",".join(sorted(set(e["refs"]), key=_ref_sort))
        try: e["cost"] = float(re.sub(r"[^\d.]", "", str(e["cost"])) or 0)
        except ValueError: e["cost"] = 0
        e["category"] = guess_category(e["name"] + " " + e["value"])
        parts.append(e)
    return sorted(parts, key=lambda e: _ref_sort(e["refs"].split(",")[0]) if e["refs"] else ("~", 0))

def import_bom(raw, filename, user, module_id=None):
    """Import into one module. Parts already in the library (same LCSC code or name+value+package) are re-used
    — that is how one resistor is shared by many modules. Re-importing a module's board updates its counts and
    designators and drops PCB parts that are no longer on that board."""
    d = db(); added = shared = 0; module_id = module_id or general_module(); seen = set()
    for e in parse_bom(raw, filename):
        ex = find_same(e["name"], e["value"], e["package"], e["lcsc"])
        if ex:
            d.execute("UPDATE hardware SET lcsc=COALESCE(NULLIF(lcsc,''),NULLIF(?,'')), part_no=COALESCE(NULLIF(part_no,''),NULLIF(?,'')), "
                      "datasheet=COALESCE(NULLIF(datasheet,''),NULLIF(?,'')), updated_by=?, ts=? WHERE id=?",
                      (e["lcsc"], e["part_no"], e["datasheet"], user, now(), ex)); pid_ = ex; shared += 1
        else:
            pid_ = d.execute("INSERT INTO hardware(project_id,name,category,value,package,refs,per_board,qty,cost,supplier,part_no,lcsc,datasheet,notes,status,updated_by,ts) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (pid(), e["name"], e["category"], e["value"], e["package"], e["refs"], e["per_board"] or 1, 0, e["cost"], e["supplier"],
                              e["part_no"], e["lcsc"], e["datasheet"], e["maker"], "planned", user, now())).lastrowid; added += 1
        link_part(module_id, pid_, e["per_board"] or 1, e["refs"]); seen.add(pid_)
    gone = [r["part_id"] for r in d.execute("SELECT part_id FROM module_parts WHERE module_id=? AND IFNULL(refs,'')!=''", (module_id,)) if r["part_id"] not in seen]
    for g in gone:
        d.execute("DELETE FROM module_parts WHERE module_id=? AND part_id=?", (module_id, g))
    sync_parts(gone); d.commit()
    return added, shared, len(gone)


# ======================================================================= LCSC ==
LCSC_URL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={}"

def inr_rate():
    try: return float(os.environ.get("LCSC_INR_RATE", "88"))
    except ValueError: return 88.0

def lcsc_info(code, fresh=False):
    """Look up an LCSC part (C14663) → dict(name, maker, package, desc, price_usd, price_inr, ladder, stock, datasheet, url) or {error}."""
    import urllib.request, time
    code = (code or "").strip().upper()
    if not re.fullmatch(r"C\d{2,9}", code): return {"error": "LCSC codes look like C14663"}
    d = db(); key = "lcsc:" + code
    r = d.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if r and not fresh:
        c = json.loads(r["value"])
        if time.time() - c.get("_t", 0) < 86400: return c
    try:
        req = urllib.request.Request(LCSC_URL.format(code), headers={"User-Agent": "Mozilla/5.0 (Rainflow BOM)", "Accept": "application/json"})
        j = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
        res = j.get("result") or {}
        if not res: return {"error": f"{code} not found on LCSC"}
        prices = res.get("productPriceList") or []
        def usd(p): return float(p.get("usdPrice") or p.get("productPrice") or 0)
        ladder = [{"qty": int(p.get("ladder") or 1), "usd": usd(p), "inr": round(usd(p) * inr_rate(), 2)} for p in prices]
        info = dict(code=code, name=res.get("productModel") or "", maker=res.get("brandNameEn") or "", package=res.get("encapStandard") or "",
                    desc=(res.get("productIntroEn") or res.get("productDescEn") or "")[:200], category=res.get("catalogName") or "",
                    stock=int(res.get("stockNumber") or 0), datasheet=res.get("pdfUrl") or "",
                    price_usd=ladder[0]["usd"] if ladder else 0, price_inr=ladder[0]["inr"] if ladder else 0, ladder=ladder,
                    url=f"https://www.lcsc.com/product-detail/{code}.html", _t=time.time())
        d.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, json.dumps(info))); d.commit()
        return info
    except Exception as ex:
        return {"error": f"Could not reach LCSC ({type(ex).__name__}). Fill the fields by hand.", "url": f"https://www.lcsc.com/product-detail/{code}.html"}

def price_for(info, qty):
    """Best ladder price (₹) for buying qty pieces."""
    best = info.get("price_inr") or 0
    for s in info.get("ladder") or []:
        if qty >= s["qty"]: best = s["inr"]
    return best

@hw.route("/hardware/lcsc")
@login_required
def lcsc_lookup():
    return jsonify(lcsc_info(request.args.get("code", "")))

@hw.route("/hardware/lcsc/refresh", methods=["POST"])
@login_required
def lcsc_refresh():
    """Update price + stock note of every part that has an LCSC code."""
    from app import log_activity
    d = db(); ok = fail = 0
    boards = (d.execute("SELECT boards FROM projects WHERE id=?", (pid(),)).fetchone() or {"boards": 1})["boards"] or 1
    for h in d.execute("SELECT * FROM hardware WHERE project_id=? AND IFNULL(lcsc,'')!=''", (pid(),)).fetchall():
        info = lcsc_info(h["lcsc"], fresh=True)
        if info.get("error"): fail += 1; continue
        d.execute("UPDATE hardware SET cost=?, datasheet=COALESCE(NULLIF(datasheet,''),?), part_no=COALESCE(NULLIF(part_no,''),?) WHERE id=?",
                  (price_for(info, (h["per_board"] or 1) * boards), info["datasheet"], info["name"], h["id"])); ok += 1
    d.commit(); log_activity("refreshed LCSC prices", f"{ok} parts", "/hardware")
    flash(f"LCSC prices updated for {ok} part(s)" + (f" · {fail} could not be reached" if fail else "") + f" · ₹ = ${inr_rate():g}")
    return redirect(url_for("hardware"))

@hw.route("/hardware/export/jlc")
@login_required
def export_jlc():
    """BOM in the format JLCPCB asks for when ordering assembled boards."""
    import csv, io
    d = db(); mid = request.args.get("m")
    mods = [module_of(mid)] if mid else [m["id"] for m in modules_list()]
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
    name = "all"
    for m in mods:
        mm = d.execute("SELECT name FROM modules WHERE id=?", (m,)).fetchone(); name = mm["name"] if mid else name
        for r in d.execute("SELECT h.*, mp.refs AS mrefs FROM module_parts mp JOIN hardware h ON h.id=mp.part_id WHERE mp.module_id=? AND IFNULL(mp.refs,'')!='' ORDER BY h.category, h.name", (m,)):
            w.writerow([r["value"] or r["name"], r["mrefs"], r["package"] or "", r["lcsc"] or ""])
    from werkzeug.utils import secure_filename
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=BOM_JLCPCB_{secure_filename(name)}.csv"})


# ================================================================ iBOM viewer ==
IBOM_BRIDGE = b"""<script>/* Rainflow bridge: highlight a part when the parent page asks */
(function(){function look(r){var i=document.getElementById('reflookup');if(!i)return;i.value=r||'';i.dispatchEvent(new Event('input',{bubbles:true}));}
window.addEventListener('message',function(e){if(e.data&&typeof e.data.rfRef==='string')look(e.data.rfRef);});
window.addEventListener('load',function(){var m=location.search.match(/[?&]ref=([^&]+)/);if(m)setTimeout(function(){look(decodeURIComponent(m[1]));},300);});})();</script>"""

def is_ibom(fn):
    return bool(fn) and fn.lower().endswith((".html", ".htm"))

@hw.route("/designs/ibom/<int:vid>")
@login_required
def ibom(vid):
    """Serve an uploaded InteractiveHtmlBom page inside a sandbox (no access to Rainflow cookies or pages)."""
    from app import UPLOADS
    v = db().execute("SELECT * FROM design_versions WHERE id=?", (vid,)).fetchone() or abort(404)
    if not is_ibom(v["filename"]): abort(404)
    raw = open(os.path.join(UPLOADS, v["filename"]), "rb").read()
    i = raw.lower().rfind(b"</body>")
    raw = raw[:i] + IBOM_BRIDGE + raw[i:] if i >= 0 else raw + IBOM_BRIDGE
    resp = Response(raw, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-popups; default-src 'self' 'unsafe-inline' data: blob:"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp



# ================================================================== modules ==
# L1 = the project (product), L2 = modules / sub-assemblies, L3 = parts (one shared library).
# module_parts links a part to every module that uses it, with the qty + designators on that module.
# hardware.per_board / hardware.refs are kept as a cache = total per product, so stock, shopping list
# and cost keep working everywhere.

def modules_list(project=None):
    d = db(); p = project or pid()
    return d.execute("""SELECT m.*, COUNT(mp.id) AS nparts, COALESCE(SUM(mp.qty * COALESCE(h.cost,0)),0) AS cost
        FROM modules m LEFT JOIN module_parts mp ON mp.module_id=m.id LEFT JOIN hardware h ON h.id=mp.part_id
        WHERE m.project_id=? GROUP BY m.id ORDER BY m.position, m.id""", (p,)).fetchall()

def general_module(project=None):
    d = db(); p = project or pid()
    r = d.execute("SELECT id FROM modules WHERE project_id=? ORDER BY (name='General') DESC, position, id LIMIT 1", (p,)).fetchone()
    if r: return r["id"]
    return d.execute("INSERT INTO modules(project_id,name,per_product,position,ts) VALUES(?,?,1,0,?)", (p, "General", now())).lastrowid

def module_of(mid):
    """Module id from a form value, only if it belongs to this project; otherwise the default module."""
    try: mid = int(mid)
    except (TypeError, ValueError): return general_module()
    r = db().execute("SELECT id FROM modules WHERE id=? AND project_id=?", (mid, pid())).fetchone()
    return r["id"] if r else general_module()

def find_same(name, value="", package="", lcsc=""):
    """An existing library part that is the same thing (same LCSC code, or same name + value + package)."""
    d = db()
    if lcsc:
        r = d.execute("SELECT id FROM hardware WHERE project_id=? AND UPPER(IFNULL(lcsc,''))=?", (pid(), lcsc.upper())).fetchone()
        if r: return r["id"]
    r = d.execute("SELECT id FROM hardware WHERE project_id=? AND LOWER(TRIM(name))=? AND LOWER(TRIM(IFNULL(value,'')))=? AND LOWER(TRIM(IFNULL(package,'')))=?",
                  (pid(), (name or "").strip().lower(), (value or "").strip().lower(), (package or "").strip().lower())).fetchone()
    return r["id"] if r else None

def link_part(module_id, part_id, qty=1, refs="", add=False):
    d = db(); qty = max(0, int(qty or 0))
    ex = d.execute("SELECT * FROM module_parts WHERE module_id=? AND part_id=?", (module_id, part_id)).fetchone()
    if ex:
        if add:
            allr = [r for r in (ex["refs"] or "").split(",") + (refs or "").split(",") if r.strip()]
            d.execute("UPDATE module_parts SET qty=qty+?, refs=? WHERE id=?", (qty, ",".join(dict.fromkeys(allr)), ex["id"]))
        else:
            d.execute("UPDATE module_parts SET qty=?, refs=? WHERE id=?", (qty, refs or "", ex["id"]))
    else:
        d.execute("INSERT INTO module_parts(module_id,part_id,qty,refs) VALUES(?,?,?,?)", (module_id, part_id, qty or 1, refs or ""))
    sync_parts([part_id])

def sync_parts(ids=None):
    """Recompute hardware.per_board (= qty per product across all modules) and hardware.refs from module links."""
    d = db()
    if ids is None:
        ids = [r["id"] for r in d.execute("SELECT id FROM hardware WHERE project_id=?", (pid(),))]
    for i in ids:
        rows = d.execute("SELECT mp.qty, mp.refs, m.per_product, m.name FROM module_parts mp JOIN modules m ON m.id=mp.module_id WHERE mp.part_id=? ORDER BY m.position, m.id", (i,)).fetchall()
        if not rows: continue                                   # not in any module: leave as it was (spare / library only)
        total = sum((r["qty"] or 0) * (r["per_product"] or 1) for r in rows)
        refs = [r["refs"] for r in rows if r["refs"]]
        d.execute("UPDATE hardware SET per_board=?, refs=? WHERE id=?", (total, (" · ".join(f"{r['name']}: {r['refs']}" for r in rows if r["refs"]) if len(refs) > 1 else (refs[0] if refs else "")), i))

def used_in(project=None):
    """{part_id: [(module_id, module_name, qty, refs)]}"""
    out = {}
    for r in db().execute("SELECT mp.part_id, m.id, m.name, mp.qty, mp.refs FROM module_parts mp JOIN modules m ON m.id=mp.module_id "
                          "WHERE m.project_id=? ORDER BY m.position, m.id", (project or pid(),)):
        out.setdefault(r["part_id"], []).append((r["id"], r["name"], r["qty"], r["refs"] or ""))
    return out

@hw.route("/modules", methods=["POST"])
@login_required
def module_new():
    from app import log_activity
    name = (request.form.get("name") or "").strip()
    if not name: flash("Give the module a name"); return redirect(url_for("hardware"))
    d = db(); pos = (d.execute("SELECT MAX(position) FROM modules WHERE project_id=?", (pid(),)).fetchone()[0] or 0) + 1
    mid = d.execute("INSERT INTO modules(project_id,name,per_product,notes,position,ts) VALUES(?,?,?,?,?,?)",
                    (pid(), name, max(1, int(request.form.get("per_product") or 1)), request.form.get("notes", ""), pos, now())).lastrowid
    d.commit(); log_activity("added module", name, f"/hardware?m={mid}")
    return redirect(url_for("hardware", m=mid))

@hw.route("/modules/<int:mid>", methods=["POST"])
@login_required
def module_update(mid):
    d = db(); m = d.execute("SELECT * FROM modules WHERE id=? AND project_id=?", (mid, pid())).fetchone() or abort(404)
    f = request.form
    d.execute("UPDATE modules SET name=?, per_product=?, notes=?, design_id=? WHERE id=?",
              ((f.get("name") or m["name"]).strip(), max(1, int(f.get("per_product") or m["per_product"] or 1)), f.get("notes", m["notes"] or ""),
               int(f["design_id"]) if f.get("design_id") else None, mid))
    sync_parts([r["part_id"] for r in d.execute("SELECT part_id FROM module_parts WHERE module_id=?", (mid,))]); d.commit()
    flash("Module saved"); return redirect(url_for("hardware", m=mid))

@hw.route("/modules/<int:mid>/link", methods=["POST"])
@login_required
def module_link(mid):
    """Add a library part to a module, or change its qty/designators there (qty 0 = remove from this module)."""
    d = db(); m = d.execute("SELECT * FROM modules WHERE id=? AND project_id=?", (mid, pid())).fetchone() or abort(404)
    j = request.get_json(silent=True) or request.form
    part = int(j["part_id"]); qty = int(j.get("qty") or 0)
    d.execute("SELECT 1 FROM hardware WHERE id=? AND project_id=?", (part, pid())).fetchone() or abort(404)
    if "delta" in j:
        ex = d.execute("SELECT qty FROM module_parts WHERE module_id=? AND part_id=?", (mid, part)).fetchone()
        qty = max(0, (ex["qty"] if ex else 0) + int(j["delta"]))
    if qty <= 0 and not j.get("refs"):
        d.execute("DELETE FROM module_parts WHERE module_id=? AND part_id=?", (mid, part)); sync_parts([part])
        left = d.execute("SELECT COUNT(*) FROM module_parts WHERE part_id=?", (part,)).fetchone()[0]
        if not left: d.execute("UPDATE hardware SET per_board=0, refs='' WHERE id=?", (part,))
    else:
        refs = j.get("refs")
        if refs is None:
            ex = d.execute("SELECT refs FROM module_parts WHERE module_id=? AND part_id=?", (mid, part)).fetchone()
            refs = ex["refs"] if ex else ""
        link_part(mid, part, qty or 1, refs)
    d.commit()
    h = d.execute("SELECT per_board, qty FROM hardware WHERE id=?", (part,)).fetchone()
    if request.is_json:
        return jsonify(ok=True, module_qty=qty, per_product=h["per_board"], have=h["qty"])
    return redirect(request.referrer or url_for("hardware", m=mid))

@hw.route("/modules/<int:mid>/delete", methods=["POST"])
@login_required
def module_delete(mid):
    """Delete a module. Parts are never lost: a part used nowhere else moves to General."""
    from app import log_activity
    d = db(); m = d.execute("SELECT * FROM modules WHERE id=? AND project_id=?", (mid, pid())).fetchone() or abort(404)
    links = d.execute("SELECT * FROM module_parts WHERE module_id=?", (mid,)).fetchall()
    if links and not session.get("is_admin"):
        flash("Only an admin can delete a module that still has parts"); return redirect(url_for("hardware", m=mid))
    d.execute("DELETE FROM module_parts WHERE module_id=?", (mid,)); d.execute("DELETE FROM modules WHERE id=?", (mid,))
    orphans = [l for l in links if not d.execute("SELECT 1 FROM module_parts WHERE part_id=?", (l["part_id"],)).fetchone()]
    if orphans:
        g = d.execute("SELECT id FROM modules WHERE project_id=? AND name='General'", (pid(),)).fetchone()
        g = g["id"] if g else d.execute("INSERT INTO modules(project_id,name,per_product,position,ts) VALUES(?,?,1,0,?)", (pid(), "General", now())).lastrowid
        for l in orphans:
            d.execute("INSERT OR IGNORE INTO module_parts(module_id,part_id,qty,refs) VALUES(?,?,?,?)", (g, l["part_id"], l["qty"], l["refs"]))
    sync_parts([l["part_id"] for l in links]); d.commit()
    log_activity("deleted module", m["name"], "/hardware")
    flash(f"Module {m['name']} deleted" + (f" — {len(orphans)} part(s) moved to General" if orphans else ""))
    return redirect(url_for("hardware"))


@hw.route("/hardware/delete", methods=["POST"])
@login_required
def parts_delete():
    """Delete parts from the library (any team member) — or, with module=, only take them out of that module."""
    from app import DELETABLE, UPLOADS, log_activity
    d = db(); ids = [int(x) for x in (request.form.getlist("ids") or request.form.get("id", "").split(",")) if str(x).strip().isdigit()]
    rows = [r for r in (d.execute("SELECT * FROM hardware WHERE id=? AND project_id=?", (i, pid())).fetchone() for i in ids) if r]
    back = request.form.get("next") or url_for("hardware")
    if not rows: flash("Nothing selected"); return redirect(back)
    if request.form.get("module"):
        mid = module_of(request.form["module"]); mname = d.execute("SELECT name FROM modules WHERE id=?", (mid,)).fetchone()["name"]
        for r in rows:
            d.execute("DELETE FROM module_parts WHERE module_id=? AND part_id=?", (mid, r["id"]))
            if not d.execute("SELECT 1 FROM module_parts WHERE part_id=?", (r["id"],)).fetchone():
                d.execute("UPDATE hardware SET per_board=0, refs='' WHERE id=?", (r["id"],))
        sync_parts([r["id"] for r in rows]); d.commit()
        log_activity(f"removed {len(rows)} part(s) from module", mname + ": " + ", ".join(r["name"] for r in rows)[:120], f"/hardware?m={mid}")
        flash(f"Removed from {mname}: " + ", ".join(r["name"] for r in rows)[:200] + " — still in the parts library")
        return redirect(back)
    stmts, _ = DELETABLE["hardware"]
    for r in rows:
        for sql, filecol in stmts:
            if filecol and r[filecol]:
                try: os.unlink(os.path.join(UPLOADS, r[filecol]))
                except Exception: pass
            d.execute(sql, (r["id"], r["id"]) if sql.count("?") == 2 else (r["id"],))
    d.commit()
    names = ", ".join(r["name"] + (f" {r['value']}" if r["value"] else "") for r in rows)
    log_activity(f"deleted {len(rows)} part(s)", names[:150], "/hardware")
    flash(f"Deleted {len(rows)} part(s): {names[:200]}")
    return redirect(back if "/hardware/" not in back else url_for("hardware"))
