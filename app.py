"""Laser Entry - sheet cutting log for multiple units and laser machines.

Operators log in and record each sheet they cut. Every entry keeps two times:
  * cut time   - when the sheet was actually cut (start / end)
  * entry time - when the operator saved the record (set by the server, cannot be changed)

Run:  python app.py      (then open http://<this-pc-ip>:5000)
"""
import csv
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("LASER_DB", os.path.join(BASE_DIR, "laserentry.db"))
TZ = ZoneInfo(os.environ.get("LASER_TZ", "Asia/Kolkata"))
TS_FMT = "%Y-%m-%d %H:%M:%S"

MATERIALS = ["MS", "SS", "AL", "GI", "CRCA", "HR", "BRASS", "COPPER"]
THICKNESS_SUGGESTIONS = ["10G", "12G", "14G", "16G", "18G", "20G", "22G", "24G",
                         "1MM", "1.5MM", "2MM", "3MM", "4MM", "5MM", "6MM", "8MM", "10MM"]

app = Flask(__name__)


def _secret_key():
    """Keep one key on disk so operators stay logged in after a restart."""
    if os.environ.get("LASER_SECRET"):
        return os.environ["LASER_SECRET"]
    path = os.path.join(BASE_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip()


app.config["SECRET_KEY"] = _secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=14)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024     # backup upload limit
# When hosted online with https, set LASER_HTTPS=1 so the login cookie is only sent over https.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("LASER_HTTPS") == "1"
if os.environ.get("LASER_HTTPS") == "1":
    # behind Render's proxy: use the real https address and visitor IP
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    unit_id INTEGER NOT NULL REFERENCES units(id),
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(name, unit_id)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'operator')),
    unit_id INTEGER REFERENCES units(id),
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    unit_id INTEGER NOT NULL REFERENCES units(id),
    machine_id INTEGER NOT NULL REFERENCES machines(id),
    material TEXT NOT NULL,
    thickness TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    sheet_code TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    remarks TEXT,
    mode TEXT NOT NULL CHECK(mode IN ('live', 'manual')),
    cut_start TEXT NOT NULL,
    cut_end TEXT,
    entered_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_start ON entries(cut_start);
CREATE INDEX IF NOT EXISTS idx_entries_user ON entries(user_id);
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    added_by INTEGER REFERENCES users(id),
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS stock_moves (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER NOT NULL REFERENCES units(id),
    material TEXT NOT NULL,
    thickness TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('in', 'count')),
    qty INTEGER NOT NULL,          -- 'in': sheets received; 'count': change made by a physical count
    counted INTEGER,               -- 'count': the actual number counted
    remarks TEXT,
    at TEXT NOT NULL,              -- when the material came / was counted
    entered_at TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_stock_key ON stock_moves(unit_id, material, thickness);
CREATE TABLE IF NOT EXISTS bend_models (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    added_by INTEGER REFERENCES users(id),
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS laser_requests (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,          -- server time: shows who asked first
    user_id INTEGER REFERENCES users(id),   -- who typed the request (empty = public request page)
    made_by TEXT NOT NULL,
    party TEXT NOT NULL,
    machine_id INTEGER REFERENCES machines(id),
    model TEXT NOT NULL,
    qty INTEGER NOT NULL,
    operator_id INTEGER REFERENCES users(id),
    remarks TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'done', 'cancelled')),
    closed_at TEXT,
    closed_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS loaders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    unit_id INTEGER NOT NULL REFERENCES units(id),
    active INTEGER NOT NULL DEFAULT 1,
    absent_on TEXT,                -- date this loader is marked absent (skipped that day)
    added_by INTEGER REFERENCES users(id),
    added_at TEXT,
    UNIQUE(name, unit_id)
);
CREATE TABLE IF NOT EXISTS bend_persons (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    added_by INTEGER REFERENCES users(id),
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS bending (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    unit_id INTEGER REFERENCES units(id),
    model TEXT NOT NULL,
    qty INTEGER NOT NULL,
    remarks TEXT,
    done_at TEXT NOT NULL,         -- when the bending was done
    entered_at TEXT NOT NULL       -- when it was entered (server time, cannot be changed)
);
CREATE INDEX IF NOT EXISTS idx_bending_done ON bending(done_at);
CREATE TABLE IF NOT EXISTS stock_hidden (
    unit_id INTEGER NOT NULL,
    material TEXT NOT NULL,
    thickness TEXT NOT NULL,
    hidden_at TEXT NOT NULL,       -- shown again only if new cutting / stock happens after this
    PRIMARY KEY (unit_id, material, thickness)
);
CREATE TABLE IF NOT EXISTS masters_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thicknesses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    added_by INTEGER REFERENCES users(id),
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS sheet_names (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    added_by INTEGER REFERENCES users(id),
    added_at TEXT
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    # older databases: bending had no OUT time (the OUT button on a 'given' entry fills it)
    cols = [r[1] for r in db.execute("PRAGMA table_info(bending)")]
    if "out_at" not in cols:
        db.execute("ALTER TABLE bending ADD COLUMN out_at TEXT")
        db.execute("ALTER TABLE bending ADD COLUMN out_by INTEGER REFERENCES users(id)")
    # older databases: no loaders on entries
    if "loader1" not in [r[1] for r in db.execute("PRAGMA table_info(entries)")]:
        db.execute("ALTER TABLE entries ADD COLUMN loader1 TEXT")
        db.execute("ALTER TABLE entries ADD COLUMN loader2 TEXT")
    # requests may now come from the public page without a login: user_id must allow empty
    if any(r[1] == "user_id" and r[3] for r in db.execute("PRAGMA table_info(laser_requests)")):
        cols = ", ".join(r[1] for r in db.execute("PRAGMA table_info(laser_requests)"))
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='laser_requests'").fetchone()[0]
        db.execute("ALTER TABLE laser_requests RENAME TO laser_requests_old")
        db.execute(sql.replace("user_id INTEGER NOT NULL REFERENCES users(id)", "user_id INTEGER REFERENCES users(id)"))
        db.execute(f"INSERT INTO laser_requests({cols}) SELECT {cols} FROM laser_requests_old")
        db.execute("DROP TABLE laser_requests_old")
    if "requester_ip" not in [r[1] for r in db.execute("PRAGMA table_info(laser_requests)")]:
        db.execute("ALTER TABLE laser_requests ADD COLUMN requester_ip TEXT")
    # split OUT lines remember the lot (original line) they came from
    if "lot_id" not in [r[1] for r in db.execute("PRAGMA table_info(bending)")]:
        db.execute("ALTER TABLE bending ADD COLUMN lot_id INTEGER")
    # older databases: OUT could only mean "bent"; now it can also be "sent without bending"
    if "out_kind" not in [r[1] for r in db.execute("PRAGMA table_info(bending)")]:
        db.execute("ALTER TABLE bending ADD COLUMN out_kind TEXT")
    # older databases: bending had no destination unit
    if "to_unit_id" not in [r[1] for r in db.execute("PRAGMA table_info(bending)")]:
        db.execute("ALTER TABLE bending ADD COLUMN to_unit_id INTEGER REFERENCES units(id)")
    # older databases: bending had no separate bending-person name (old rows use the login name)
    if "person" not in [r[1] for r in db.execute("PRAGMA table_info(bending)")]:
        db.execute("ALTER TABLE bending ADD COLUMN person TEXT")
    # older databases: bending had no status (all old entries were finished bending = out)
    if "status" not in [r[1] for r in db.execute("PRAGMA table_info(bending)")]:
        db.execute("ALTER TABLE bending ADD COLUMN status TEXT NOT NULL DEFAULT 'out'")
    # older databases: bending had no machine
    if "machine_id" not in [r[1] for r in db.execute("PRAGMA table_info(bending)")]:
        db.execute("ALTER TABLE bending ADD COLUMN machine_id INTEGER REFERENCES machines(id)")
    # machines: standard lot size (pieces), pre-fills qty on the bending and request forms
    if "lot_size" not in [r[1] for r in db.execute("PRAGMA table_info(machines)")]:
        db.execute("ALTER TABLE machines ADD COLUMN lot_size INTEGER")
    # older databases: machines did not record who added them
    if "added_by" not in [r[1] for r in db.execute("PRAGMA table_info(machines)")]:
        db.execute("ALTER TABLE machines ADD COLUMN added_by INTEGER REFERENCES users(id)")
    if not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        db.execute(
            "INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)",
            ("admin", "Administrator", generate_password_hash("admin123"), "admin"))
        print("Created default login  ->  username: admin   password: admin123  (change it!)")
    if not db.execute("SELECT 1 FROM materials").fetchone():
        db.executemany("INSERT INTO materials(name) VALUES (?)", [(m,) for m in MATERIALS])
    if not db.execute("SELECT 1 FROM thicknesses").fetchone():
        db.executemany("INSERT INTO thicknesses(name) VALUES (?)", [(t,) for t in THICKNESS_SUGGESTIONS])
    db.execute("INSERT OR IGNORE INTO thicknesses(name, added_at) "
               "SELECT DISTINCT thickness, MIN(entered_at) FROM entries GROUP BY thickness")
    # older databases: also remember sheet names that were already entered
    db.execute("INSERT OR IGNORE INTO sheet_names(name, added_at) "
               "SELECT DISTINCT sheet_name, MIN(entered_at) FROM entries GROUP BY sheet_name")
    db.commit()
    db.close()


# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(TZ).replace(tzinfo=None)


def now_str():
    return now().strftime(TS_FMT)


def parse_dt(value):
    """Parse a value from an <input type=datetime-local>."""
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", TS_FMT, "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def clean_code(text):
    return re.sub(r"[^A-Z0-9.\-_]", "", (text or "").upper())


THICK_HELP = "Write thickness with its unit: 16G for gauge or 2MM for millimetre (not only a number)."


def thickness_ok(t):
    """A plain number like 18 is ambiguous (18G or 18MM?) and creates a separate stock line."""
    return bool(re.search(r"[A-Z]", t or ""))


def make_sheet_code(thickness, machine_name, sheet_name):
    """16G + MACHINENAME + SHEETNAME, e.g. 16GLASER1PANELA."""
    return clean_code(thickness) + clean_code(machine_name) + clean_code(sheet_name)


def minutes_between(a, b):
    if not a or not b:
        return None
    return round((datetime.strptime(b, TS_FMT) - datetime.strptime(a, TS_FMT)).total_seconds() / 60)


def fmt_minutes(m):
    if m is None:
        return "-"
    sign = "-" if m < 0 else ""
    m = abs(m)
    if m < 60:
        return f"{sign}{m} min"
    if m < 60 * 24:
        return f"{sign}{m // 60}h {m % 60}m"
    return f"{sign}{m // 1440}d {(m % 1440) // 60}h"


app.jinja_env.globals.update(fmt_minutes=fmt_minutes, minutes_between=minutes_between, now_str=lambda: now_str())


@app.template_filter("secs")
def secs_filter(value):
    return int(datetime.strptime(value, TS_FMT).timestamp()) if value else 0


@app.template_filter("dt")
def dt_filter(value):
    if not value:
        return "-"
    try:
        d = datetime.strptime(value, TS_FMT)
    except ValueError:                      # date without time
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
        except ValueError:
            return value
    return d.strftime("%d-%m-%Y %I:%M %p")


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    if "user" not in g:
        g.user = get_db().execute(
            "SELECT u.*, un.name AS unit_name FROM users u LEFT JOIN units un ON un.id=u.unit_id "
            "WHERE u.id=? AND u.active=1", (uid,)).fetchone()
    return g.user


@app.context_processor
def inject_user():
    return {"me": current_user(), "csrf_token": csrf_token()}


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not current_user():
            session.clear()
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*a, **kw):
        if current_user()["role"] != "admin":
            abort(403)
        return view(*a, **kw)
    return wrapper


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST":
        if request.form.get("csrf") != session.get("csrf"):
            abort(400, "Form expired - go back, refresh the page and try again.")


# ---------------------------------------------------------------- auth
# Stop password guessing: after 8 wrong tries, that username is blocked for 15 minutes.
MAX_TRIES, BLOCK_MINUTES = 8, 15
_failed = {}          # username -> [count, first_failure_time]


def _blocked(username):
    rec = _failed.get(username)
    if rec and now() - rec[1] > timedelta(minutes=BLOCK_MINUTES):
        _failed.pop(username, None)
        return False
    return bool(rec and rec[0] >= MAX_TRIES)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        if _blocked(username):
            flash(f"Too many wrong passwords. Try again after {BLOCK_MINUTES} minutes "
                  "or ask admin to reset the password.", "error")
            return render_template("login.html")
        user = get_db().execute("SELECT * FROM users WHERE username=? AND active=1",
                                (username,)).fetchone()
        password = request.form.get("password", "")
        if user and check_password_hash(user["password_hash"], password):
            _failed.pop(username, None)
            csrf = session.get("csrf")
            session.clear()
            session.permanent = True
            session["uid"] = user["id"]
            session["csrf"] = csrf or secrets.token_hex(16)
            if user["role"] == "admin" and password == "admin123":
                flash("You are still using the default password admin123. "
                      "Change it now from the Password menu.", "error")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("home"))
        rec = _failed.setdefault(username, [0, now()])
        rec[0] += 1
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        me = current_user()
        if not check_password_hash(me["password_hash"], request.form.get("old", "")):
            flash("Current password is wrong.", "error")
        elif len(request.form.get("new", "")) < 4:
            flash("New password must be at least 4 characters.", "error")
        elif request.form.get("new") != request.form.get("new2"):
            flash("New passwords do not match.", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (generate_password_hash(request.form["new"]), me["id"]))
            db.commit()
            flash("Password changed.", "ok")
            return redirect(url_for("home"))
    return render_template("password.html")


# ---------------------------------------------------------------- operator pages
@app.route("/")
@login_required
def home():
    me = current_user()
    if me["role"] == "admin":
        return redirect(url_for("dashboard"))
    db = get_db()
    running = db.execute(
        "SELECT e.*, m.name AS machine_name FROM entries e JOIN machines m ON m.id=e.machine_id "
        "WHERE e.user_id=? AND e.cut_end IS NULL ORDER BY e.cut_start", (me["id"],)).fetchall()
    today = now().strftime("%Y-%m-%d")
    recent = db.execute(
        "SELECT e.*, m.name AS machine_name FROM entries e JOIN machines m ON m.id=e.machine_id "
        "WHERE e.user_id=? AND e.cut_end IS NOT NULL ORDER BY e.entered_at DESC LIMIT 20",
        (me["id"],)).fetchall()
    today_count = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(qty),0) FROM entries WHERE user_id=? AND substr(cut_start,1,10)=?",
        (me["id"], today)).fetchone()
    my_requests = request_rows("r.status='pending' AND r.operator_id=?", (me["id"],))
    return render_template("home.html", running=running, recent=recent, today_count=today_count,
                           my_requests=my_requests,
                           waiting=bending_waiting(me), units=db.execute("SELECT * FROM units ORDER BY name").fetchall(),
                           stock_rows=stock_balances(stock_scope(me)), low=LOW_STOCK)


def material_list(active_only=True):
    sql = "SELECT * FROM materials" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
    return get_db().execute(sql).fetchall()


def thickness_list():
    rows = get_db().execute("SELECT name FROM thicknesses WHERE active=1").fetchall()
    return sorted((r["name"] for r in rows), key=thickness_key)


# what operators can add themselves: kind -> (table, label, cleaner)
ITEM_KINDS = {
    "sheet": ("sheet_names", "sheet name", lambda v: (v or "").strip().upper()),
    "model": ("bend_models", "model", lambda v: (v or "").strip().upper()),
    "person": ("bend_persons", "bending person", lambda v: " ".join((v or "").split()).title()),
    "thickness": ("thicknesses", "thickness", clean_code),
    "material": ("materials", "material", clean_code),
}


def sheet_name_list():
    return [r["name"] for r in get_db().execute(
        "SELECT name FROM sheet_names WHERE active=1 ORDER BY name").fetchall()]


def remember(table, name, user_id):
    """Add a material / sheet name to the saved list (or switch it back on). Returns True if new."""
    db = get_db()
    row = db.execute(f"SELECT id, active FROM {table} WHERE name=?", (name,)).fetchone()
    if row:
        if not row["active"]:
            db.execute(f"UPDATE {table} SET active=1 WHERE id=?", (row["id"],))
        return False
    db.execute(f"INSERT INTO {table}(name, added_by, added_at) VALUES (?,?,?)", (name, user_id, now_str()))
    return True


def machines_for(user):
    db = get_db()
    if user["role"] == "admin" or not user["unit_id"]:
        return db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u ON u.id=m.unit_id "
                          "WHERE m.active=1 ORDER BY u.name, m.name").fetchall()
    return db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u ON u.id=m.unit_id "
                      "WHERE m.active=1 AND m.unit_id=? ORDER BY m.name", (user["unit_id"],)).fetchall()


@app.route("/entry/new", methods=["GET", "POST"])
@login_required
def new_entry():
    me = current_user()
    machines = machines_for(me)
    mode = request.values.get("mode", "live")
    if mode not in ("live", "manual"):
        mode = "live"

    if request.method == "POST":
        f = request.form
        errors = []
        db = get_db()
        new_machine = None
        if f.get("machine_id") == "new":
            # operator adds a machine; it goes into the operator's own unit
            new_machine = clean_code(f.get("new_machine"))
            unit_id = me["unit_id"] if (me["role"] != "admin" and me["unit_id"]) else f.get("new_machine_unit")
            unit = db.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone() if unit_id else None
            if not new_machine:
                errors.append("Type the new machine name.")
            elif not unit:
                errors.append("Select the unit for the new machine.")
            machine = None
        else:
            machine = next((m for m in machines if str(m["id"]) == f.get("machine_id")), None)
        material = f.get("material", "").strip().upper()
        if material == "__NEW":
            material = clean_code(f.get("new_material"))
        thickness = clean_code(f.get("thickness"))
        sheet_name = f.get("sheet_name", "").strip()
        try:
            qty = int(f.get("qty") or 1)
            if qty < 1:
                raise ValueError
        except ValueError:
            qty = None
            errors.append("Quantity must be a whole number, 1 or more.")
        if not machine and f.get("machine_id") != "new":
            errors.append("Select a machine.")
        if not material:
            errors.append("Select material.")
        if not thickness:
            errors.append("Enter thickness (e.g. 16G).")
        elif not thickness_ok(thickness):
            errors.append(THICK_HELP)
        if not clean_code(sheet_name):
            errors.append("Enter sheet name.")

        entered = now_str()
        cut_start = cut_end = None
        if mode == "live":
            cut_start = entered        # cutting starts now, operator presses Finish later
        else:
            s, e = parse_dt(f.get("cut_start")), parse_dt(f.get("cut_end"))
            if not s or not e:
                errors.append("Enter cut start and cut end time.")
            elif e < s:
                errors.append("Cut end cannot be before cut start.")
            elif e > now() + timedelta(minutes=5):
                errors.append("Cut end cannot be in the future.")
            else:
                cut_start, cut_end = s.strftime(TS_FMT), e.strftime(TS_FMT)

        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            # stop double entries (e.g. START pressed twice on a slow phone): lock, then check
            db.execute("BEGIN IMMEDIATE")
            dup = None
            if machine and mode == "live":
                busy = db.execute(
                    "SELECT e.sheet_code, e.cut_start, us.full_name FROM entries e JOIN users us ON us.id=e.user_id "
                    "WHERE e.machine_id=? AND e.cut_end IS NULL", (machine["id"],)).fetchone()
                if busy:
                    dup = (f"{machine['name']} is already cutting {busy['sheet_code']} (started by "
                           f"{busy['full_name']} at {dt_filter(busy['cut_start'])}). "
                           "Press FINISH on it first. No new entry was saved.")
            elif machine:
                if db.execute("SELECT 1 FROM entries WHERE user_id=? AND machine_id=? AND material=? AND thickness=? "
                              "AND sheet_name=? AND cut_start=? AND cut_end=?",
                              (me["id"], machine["id"], material, thickness, sheet_name.upper(),
                               cut_start, cut_end)).fetchone():
                    dup = "This same entry is already saved. No new entry was saved."
            if dup:
                db.rollback()
                flash(dup, "error")
                return redirect(url_for("home"))
            added = []
            if new_machine:
                existing = db.execute("SELECT * FROM machines WHERE name=? AND unit_id=?",
                                      (new_machine, unit["id"])).fetchone()
                if existing:
                    db.execute("UPDATE machines SET active=1 WHERE id=?", (existing["id"],))
                    machine = existing
                else:
                    try:
                        new_lot = max(int(f.get("new_machine_lot") or 0), 0)
                    except ValueError:
                        new_lot = 0
                    cur = db.execute("INSERT INTO machines(name, unit_id, added_by, lot_size) VALUES (?,?,?,?)",
                                     (new_machine, unit["id"], me["id"], new_lot or None))
                    machine = db.execute("SELECT * FROM machines WHERE id=?", (cur.lastrowid,)).fetchone()
                    added.append(f"machine {new_machine}")
            if remember("materials", material, me["id"]):
                added.append(f"material {material}")
            if remember("thicknesses", thickness, me["id"]):
                added.append(f"thickness {thickness}")
            if remember("sheet_names", sheet_name.upper(), me["id"]):
                added.append(f"sheet name {sheet_name.upper()}")
            code = make_sheet_code(thickness, machine["name"], sheet_name)
            db.execute(
                "INSERT INTO entries(user_id, unit_id, machine_id, material, thickness, sheet_name, "
                "sheet_code, qty, remarks, mode, cut_start, cut_end, entered_at, finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (me["id"], machine["unit_id"], machine["id"], material, thickness, sheet_name.upper(),
                 code, qty, f.get("remarks", "").strip(), mode, cut_start, cut_end, entered,
                 entered if mode == "manual" else None))
            db.commit()
            flash(f"Saved {code}" + (" - cutting started. Press FINISH when done." if mode == "live" else ""), "ok")
            left = stock_balance(machine["unit_id"], material, thickness)
            flash(f"Stock {material} {thickness} pending: {left} sheets.", "ok" if left > LOW_STOCK else "error")
            if added:
                flash("New " + ", ".join(added) + " added to the list.", "ok")
            return redirect(url_for("home"))

    return render_template("entry.html", machines=machines, mode=mode,
                           materials=[m["name"] for m in material_list()],
                           sheet_names=sheet_name_list(),
                           units=get_db().execute("SELECT * FROM units ORDER BY name").fetchall(),
                           thicknesses=thickness_list(), form=request.form,
                           low=LOW_STOCK,
                           stock_map={f"{d['unit_id']}|{d['material']}|{d['thickness']}": d["balance"]
                                      for d in stock_balances(stock_scope(me))},
                           now_local=now().strftime("%Y-%m-%dT%H:%M"))


@app.route("/items/add", methods=["POST"])
@app.route("/sheet-names/add", methods=["POST"])
@login_required
def add_item():
    """Add a sheet name / thickness / material to the saved list from the entry form (no page reload)."""
    kind = request.form.get("kind", "sheet")
    if kind not in ITEM_KINDS:
        abort(400)
    table, label, cleaner = ITEM_KINDS[kind]
    name = cleaner(request.form.get("name"))
    if not clean_code(name):
        return {"ok": False, "message": f"Type a {label} first."}, 400
    if kind == "thickness" and not thickness_ok(name):
        return {"ok": False, "message": THICK_HELP}, 400
    is_new = remember(table, name, current_user()["id"])
    get_db().commit()
    return {"ok": True, "name": name,
            "message": f"{name} added to {label} list." if is_new else f"{name} is already in the list."}


# ---------------------------------------------------------------- masters (operators + admin)
# kind -> (table, column in entries, label, cleaner)
MASTER_KINDS = {
    "machine": ("machines", "machine_id", "machine", clean_code),
    "material": ("materials", "material", "material", clean_code),
    "thickness": ("thicknesses", "thickness", "thickness", clean_code),
    "sheet": ("sheet_names", "sheet_name", "sheet name", lambda v: (v or "").strip().upper()),
    "model": ("bend_models", "model", "model", lambda v: (v or "").strip().upper()),
    "person": ("bend_persons", "person", "bending person", lambda v: " ".join((v or "").split()).title()),
    "loader": ("loaders", None, "loader", lambda v: " ".join((v or "").split()).title()),
}


def recompute_codes(where, params):
    """Rebuild sheet codes (thickness + machine + sheet name) after a rename."""
    db = get_db()
    rows = db.execute("SELECT e.id, e.thickness, e.sheet_name, m.name AS machine_name FROM entries e "
                      f"JOIN machines m ON m.id=e.machine_id WHERE {where}", params).fetchall()
    for r in rows:
        db.execute("UPDATE entries SET sheet_code=? WHERE id=?",
                   (make_sheet_code(r["thickness"], r["machine_name"], r["sheet_name"]), r["id"]))
    return len(rows)


def rename_master(kind, item_id, raw):
    """Fix a name everywhere, old entries included. If the new name already exists, merge into it."""
    table, col, label, cleaner = MASTER_KINDS[kind]
    db = get_db()
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    new = cleaner(raw)
    if not row or not clean_code(new):
        return "error", f"Type the new {label}."
    old = row["name"]
    if new == old:
        return "ok", "No change."
    if kind == "thickness" and not thickness_ok(new):
        return "error", THICK_HELP
    if kind == "loader":
        other = db.execute("SELECT * FROM loaders WHERE name=? AND unit_id=? AND id<>?",
                           (new, row["unit_id"], row["id"])).fetchone()
        if other:
            db.execute("UPDATE loaders SET active=1 WHERE id=?", (other["id"],))
            db.execute("DELETE FROM loaders WHERE id=?", (row["id"],))
        else:
            db.execute("UPDATE loaders SET name=? WHERE id=?", (new, row["id"]))
        n = 0
        for c in ("loader1", "loader2"):
            n += db.execute(f"UPDATE entries SET {c}=? WHERE {c}=? AND unit_id=?", (new, old, row["unit_id"])).rowcount
        return "ok", f"Loader {old} changed to {new}" + (f" ({n} old entries updated)." if n else ".")
    if kind == "machine":
        other = db.execute("SELECT * FROM machines WHERE name=? AND unit_id=? AND id<>?",
                           (new, row["unit_id"], row["id"])).fetchone()
        if other:
            db.execute("UPDATE entries SET machine_id=? WHERE machine_id=?", (other["id"], row["id"]))
            db.execute("UPDATE machines SET active=1 WHERE id=?", (other["id"],))
            db.execute("DELETE FROM machines WHERE id=?", (row["id"],))
            target = other["id"]
        else:
            db.execute("UPDATE machines SET name=? WHERE id=?", (new, row["id"]))
            target = row["id"]
        n = recompute_codes("e.machine_id=?", (target,))
    else:
        etab = "bending" if kind in ("model", "person") else "entries"
        ids = [r[0] for r in db.execute(f"SELECT id FROM {etab} WHERE {col}=?", (old,))]
        other = db.execute(f"SELECT * FROM {table} WHERE name=? AND id<>?", (new, row["id"])).fetchone()
        if other:
            db.execute(f"UPDATE {table} SET active=1 WHERE id=?", (other["id"],))
            db.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
        else:
            db.execute(f"UPDATE {table} SET name=? WHERE id=?", (new, row["id"]))
        db.execute(f"UPDATE {etab} SET {col}=? WHERE {col}=?", (new, old))
        if kind in ("material", "thickness"):
            db.execute(f"UPDATE stock_moves SET {col}=? WHERE {col}=?", (new, old))
        n = len(ids)
        if ids and kind not in ("material", "model", "person"):
            recompute_codes(f"e.id IN ({','.join('?' * len(ids))})", ids)
    msg = f"{label.capitalize()} {old} changed to {new}"
    return "ok", msg + (f" ({n} old entries updated)." if n else ".")


def remove_master(kind, item_id):
    """Delete if never used; if old entries use it, hide it so past records stay correct."""
    table, col, label, _ = MASTER_KINDS[kind]
    db = get_db()
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    if not row:
        return "error", "Already removed."
    if kind == "loader":
        used = db.execute("SELECT COUNT(*) FROM entries WHERE unit_id=? AND (loader1=? OR loader2=?)",
                          (row["unit_id"], row["name"], row["name"])).fetchone()[0]
    else:
        etab = "bending" if kind in ("model", "person") else "entries"
        used = db.execute(f"SELECT COUNT(*) FROM {etab} WHERE {col}=?",
                          (row["id"] if kind == "machine" else row["name"],)).fetchone()[0]
        if kind in ("material", "thickness"):
            used += db.execute(f"SELECT COUNT(*) FROM stock_moves WHERE {col}=?", (row["name"],)).fetchone()[0]
    if used:
        db.execute(f"UPDATE {table} SET active=0 WHERE id=?", (row["id"],))
        return "ok", (f"{row['name']} removed from the list. It is used in {used} old entries, "
                      "so those entries (and stock) keep it. If it was a mistake for another name "
                      "(e.g. 18 instead of 18G), use ✎ edit and type the correct name: the entries and "
                      "stock are then moved into it.")
    db.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
    return "ok", f"{row['name']} removed."

@app.route("/masters", methods=["GET", "POST"])
@login_required
def masters():
    """Machines, materials, thickness and sheet names. Everyone can add; only admin can hide."""
    me = current_user()
    is_admin = me["role"] == "admin"
    db = get_db()
    if request.method == "POST":
        f = request.form
        action = f.get("action", "")

        def done(msg, cat="ok", log=True):
            flash(msg, cat)
            if cat == "ok" and log:
                db.execute("INSERT INTO masters_log(at, user_id, text) VALUES (?,?,?)", (now_str(), me["id"], msg))

        def lot_value():
            try:
                v = int(f.get("lot_size") or 0)
            except ValueError:
                v = -1
            return v

        if action == "set_lot":
            row = db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u ON u.id=m.unit_id "
                             "WHERE m.id=?", (f.get("id"),)).fetchone()
            lot = lot_value()
            if not row or (not is_admin and me["unit_id"] and row["unit_id"] != me["unit_id"]):
                flash("You cannot change this machine.", "error")
            elif lot < 0:
                flash("Lot size must be a whole number (0 or empty = no lot size).", "error")
            elif (row["lot_size"] or 0) != lot:
                db.execute("UPDATE machines SET lot_size=? WHERE id=?", (lot or None, row["id"]))
                done(f"Lot size of {row['name']} ({row['unit_name']}) set to {lot or 'none'}.")
        elif action == "add_machine":
            name = clean_code(f.get("name"))
            unit_id = f.get("unit_id") if (is_admin or not me["unit_id"]) else me["unit_id"]
            unit = db.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone() if unit_id else None
            if not name or not unit:
                flash("Type the machine name and choose the unit.", "error")
            else:
                row = db.execute("SELECT * FROM machines WHERE name=? AND unit_id=?", (name, unit["id"])).fetchone()
                if row:
                    db.execute("UPDATE machines SET active=1 WHERE id=?", (row["id"],))
                    done(f"{name} is already in {unit['name']}.", log=bool(not row["active"]))
                else:
                    lot = max(lot_value(), 0)
                    db.execute("INSERT INTO machines(name, unit_id, added_by, lot_size) VALUES (?,?,?,?)",
                               (name, unit["id"], me["id"], lot or None))
                    done(f"Machine {name} added to {unit['name']}" + (f" with lot size {lot}." if lot else "."))
        elif action == "add_loader":
            name = MASTER_KINDS["loader"][3](f.get("name"))
            unit_id = f.get("unit_id") if (is_admin or not me["unit_id"]) else me["unit_id"]
            unit = db.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone() if unit_id else None
            if not name or not unit:
                flash("Type the loader name and choose the unit.", "error")
            else:
                row = db.execute("SELECT * FROM loaders WHERE name=? AND unit_id=?", (name, unit["id"])).fetchone()
                if row:
                    db.execute("UPDATE loaders SET active=1 WHERE id=?", (row["id"],))
                    done(f"{name} is already a loader in {unit['name']}.", log=bool(not row["active"]))
                else:
                    db.execute("INSERT INTO loaders(name, unit_id, added_by, added_at) VALUES (?,?,?,?)",
                               (name, unit["id"], me["id"], now_str()))
                    done(f"Loader {name} added to {unit['name']}.")
        elif action == "absent_loader":
            row = db.execute("SELECT * FROM loaders WHERE id=?", (f.get("id"),)).fetchone()
            if row and (is_admin or not me["unit_id"] or row["unit_id"] == me["unit_id"]):
                today = now().strftime("%Y-%m-%d")
                absent = row["absent_on"] == today
                db.execute("UPDATE loaders SET absent_on=? WHERE id=?", (None if absent else today, row["id"]))
                done(f"Loader {row['name']} marked {'present' if absent else 'absent'} today.")
        elif action.startswith("add_") and action[4:] in ITEM_KINDS:
            table, label, cleaner = ITEM_KINDS[action[4:]]
            name = cleaner(f.get("name"))
            if action == "add_thickness" and name and not thickness_ok(name):
                flash(THICK_HELP, "error")
            elif clean_code(name):
                is_new = remember(table, name, me["id"])
                done(f"{label.capitalize()} {name} added." if is_new else f"{name} is already in the list.", log=is_new)
        elif action.split("_", 1)[0] in ("rename", "remove") and action.split("_", 1)[-1] in MASTER_KINDS:
            verb, kind = action.split("_", 1)
            table = MASTER_KINDS[kind][0]
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (f.get("id"),)).fetchone()
            # operators may change any item, but machines only in their own unit
            other_unit = (kind in ("machine", "loader") and not is_admin and me["unit_id"]
                          and row and row["unit_id"] != me["unit_id"])
            if not row or other_unit or (not is_admin and not row["active"]):
                flash("You cannot change this item. Ask admin.", "error")
            else:
                cat, msg = (rename_master(kind, row["id"], f.get("name")) if verb == "rename"
                            else remove_master(kind, row["id"]))
                done(msg, cat, log=msg != "No change.")
        elif action.startswith("toggle_") and is_admin:
            table = {"machine": "machines", "material": "materials", "thickness": "thicknesses",
                     "sheet": "sheet_names", "model": "bend_models",
                     "person": "bend_persons", "loader": "loaders"}.get(action[7:])
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (f.get("id"),)).fetchone() if table else None
            if row:
                db.execute(f"UPDATE {table} SET active=1-active WHERE id=?", (row["id"],))
                done(f"{row['name']} {'brought back' if not row['active'] else 'removed from the list'}.")
        db.commit()
        return redirect(url_for("masters") + "#" + f.get("anchor", ""))

    def items(table, order="name"):
        where = "" if is_admin else " WHERE t.active=1"
        return db.execute(f"SELECT t.*, u.full_name AS added_by_name FROM {table} t "
                          f"LEFT JOIN users u ON u.id=t.added_by{where} ORDER BY t.active DESC, t.{order}").fetchall()

    if is_admin or not me["unit_id"]:
        machines = db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u ON u.id=m.unit_id "
                              + ("" if is_admin else "WHERE m.active=1 ") + "ORDER BY u.name, m.name").fetchall()
    else:
        machines = db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u ON u.id=m.unit_id "
                              "WHERE m.active=1 AND m.unit_id=? ORDER BY m.name", (me["unit_id"],)).fetchall()
    thick = sorted(items("thicknesses"), key=lambda r: (-r["active"], thickness_key(r["name"])))
    return render_template("masters.html", machines=machines, materials=items("materials", "id"),
                           thicknesses=thick, sheets=items("sheet_names"), models=items("bend_models"), persons=items("bend_persons"),
                           loaders=db.execute("SELECT l.*, u.name AS unit_name, l.absent_on=? AS absent FROM loaders l "
                                              "JOIN units u ON u.id=l.unit_id "
                                              + ("" if (is_admin or not me["unit_id"]) else
                                                 f"WHERE l.unit_id={int(me['unit_id'])} AND l.active=1 ")
                                              + "ORDER BY u.name, l.active DESC, l.name",
                                              (now().strftime("%Y-%m-%d"),)).fetchall(),
                           units=db.execute("SELECT * FROM units ORDER BY name").fetchall(),
                           log=db.execute("SELECT l.*, u.full_name FROM masters_log l LEFT JOIN users u "
                                          "ON u.id=l.user_id ORDER BY l.id DESC LIMIT 50").fetchall()
                           if is_admin else [])


# ---------------------------------------------------------------- settings + Google Sheet lists
def get_setting(key, default=""):
    r = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r and r["value"] is not None else default


def set_setting(key, value):
    get_db().execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))


_sheet_cache = {"at": None, "data": None}
SHEET_CACHE_MIN = 10


def sheet_csv_url(link):
    """Turn a normal Google Sheet link into its CSV download link (keeps the tab if the link has gid=)."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", link or "")
    if not m:
        return None
    g = re.search(r"[#&?]gid=(\d+)", link)
    return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
            + (f"&gid={g.group(1)}" if g else ""))


def fetch_sheet_lists(force=False):
    """Made-by and party names from the Google Sheet set in Setup. Cached 10 minutes; the last good copy
    is kept in the database so the form still works if Google cannot be reached."""
    import json
    import urllib.request
    now_ = now()
    if not force and _sheet_cache["data"] and now_ - _sheet_cache["at"] < timedelta(minutes=SHEET_CACHE_MIN):
        return _sheet_cache["data"]
    url = sheet_csv_url(get_setting("sheet_url"))
    def key(h):          # compare headings ignoring case, spaces (incl. hidden ones) and symbols
        return re.sub(r"[^a-z0-9]", "", (h or "").lower())
    made_col = key(get_setting("sheet_madeby_col", "Made By"))
    party_col = key(get_setting("sheet_party_col", "PARTY"))
    data = {"made_by": [], "party": [], "error": None, "at": None}
    if not url:
        data["error"] = "Google Sheet link is not set (admin: Setup page)."
    else:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "laserentry"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode("utf-8-sig", errors="replace")
            if text.lstrip().startswith("<"):
                raise ValueError("Google sent a login page: share the sheet as 'Anyone with the link: Viewer'.")
            rows = csv.reader(io.StringIO(text))
            raw_header = next(rows, [])
            header = [key(h) for h in raw_header]
            if made_col not in header or party_col not in header:
                raise ValueError("Columns not found. Sheet has: "
                                 + ", ".join(" ".join(h.split()) for h in raw_header[:25] if h.strip()))
            mi, pi = header.index(made_col), header.index(party_col)
            made, party = set(), set()
            for r in rows:
                if len(r) > mi and r[mi].strip():
                    made.add(" ".join(r[mi].split()).upper())
                if len(r) > pi and r[pi].strip():
                    party.add(" ".join(r[pi].split()).upper())
            data.update(made_by=sorted(made), party=sorted(party), at=now_str())
            set_setting("sheet_cache", json.dumps({"made_by": data["made_by"], "party": data["party"], "at": data["at"]}))
            get_db().commit()
        except Exception as ex:                     # network / sharing / column problems
            data["error"] = f"Could not read the Google Sheet: {ex}"
            try:
                old = json.loads(get_setting("sheet_cache") or "{}")
                data.update(made_by=old.get("made_by", []), party=old.get("party", []), at=old.get("at"))
            except ValueError:
                pass
    _sheet_cache.update(at=now_, data=data)
    return data


# ---------------------------------------------------------------- laser cutting requests
def request_lists():
    db = get_db()
    sheet = fetch_sheet_lists()
    used_made = [r[0] for r in db.execute("SELECT DISTINCT made_by FROM laser_requests")]
    used_party = [r[0] for r in db.execute("SELECT DISTINCT party FROM laser_requests")]
    return dict(
        sheet=sheet,
        made_by=sorted(set(sheet["made_by"]) | set(used_made)),
        parties=sorted(set(sheet["party"]) | set(used_party)),
        models=db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u ON u.id=m.unit_id "
                          "WHERE m.active=1 ORDER BY m.name").fetchall(),
        operators=db.execute("SELECT us.*, un.name AS unit_name FROM users us LEFT JOIN units un ON un.id=us.unit_id "
                             "WHERE us.active=1 AND us.role='operator' ORDER BY us.full_name").fetchall())


def request_rows(where="1=1", params=()):
    return get_db().execute(
        "SELECT r.*, us.full_name AS requested_by, op.full_name AS operator_name, un.name AS unit_name, "
        "cl.full_name AS closed_by_name FROM laser_requests r LEFT JOIN users us ON us.id=r.user_id "
        "LEFT JOIN users op ON op.id=r.operator_id LEFT JOIN units un ON un.id=op.unit_id "
        f"LEFT JOIN users cl ON cl.id=r.closed_by WHERE {where} ORDER BY r.created_at, r.id", params).fetchall()


def save_request(f, lists, user_id, ip=None):
    """Validate and save one request (used by the staff page and the public page). Returns new id or None."""
    db = get_db()
    made_by = " ".join(f.get("made_by", "").split()).upper()
    party = " ".join(f.get("party", "").split()).upper()
    machine = next((m for m in lists["models"] if str(m["id"]) == f.get("machine_id")), None)
    operator = next((o for o in lists["operators"] if str(o["id"]) == f.get("operator_id")), None)
    try:
        qty = int(f.get("qty") or "")
    except ValueError:
        qty = 0
    for ok, text in ((made_by, "Select Request By."), (party, "Select the party."), (machine, "Select the model."),
                     (qty >= 1, "Enter quantity (1 or more)."), (operator, "Select the operator.")):
        if not ok:
            flash(text, "error")
            return None
    db.execute("BEGIN IMMEDIATE")
    if db.execute("SELECT 1 FROM laser_requests WHERE made_by=? AND party=? AND machine_id=? AND qty=? "
                  "AND operator_id=? AND (julianday(?)-julianday(created_at))*86400 < 20",
                  (made_by, party, machine["id"], qty, operator["id"], now_str())).fetchone():
        db.rollback()
        flash("This request was just saved. Not saved again.", "error")
        return None
    cur = db.execute("INSERT INTO laser_requests(created_at, user_id, made_by, party, machine_id, model, qty, "
                     "operator_id, remarks, requester_ip) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (now_str(), user_id, made_by, party, machine["id"], machine["name"], qty,
                      operator["id"], f.get("remarks", "").strip()[:300], ip))
    db.commit()
    pos = db.execute("SELECT COUNT(*) FROM laser_requests WHERE status='pending' AND operator_id=? AND id<=?",
                     (operator["id"], cur.lastrowid)).fetchone()[0]
    flash(f"Request #{cur.lastrowid} saved: {party} - {machine['name']} x {qty} for "
          f"{operator['full_name']}. Position in the queue: {pos}.", "ok")
    return cur.lastrowid


# public page: limits so the open link cannot be misused
PUBLIC_MAX_PER_HOUR = 30
_public_hits = {}


def public_allowed():
    """Logged-in users always; otherwise the PIN (if admin set one) must have been entered."""
    if current_user():
        return True
    pin = get_setting("request_pin")
    return not pin or session.get("request_pin_ok") == pin


@app.route("/request", methods=["GET", "POST"])
def public_request():
    """Request form that works without a login. Admin can protect it with a PIN in Setup."""
    pin = get_setting("request_pin")
    if request.method == "POST" and request.form.get("action") == "pin":
        if request.form.get("pin", "").strip() == pin:
            session["request_pin_ok"] = pin
        else:
            flash("Wrong PIN.", "error")
        return redirect(url_for("public_request"))
    if not public_allowed():
        return render_template("request_public.html", need_pin=True)
    lists = request_lists()
    if request.method == "POST":
        ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
        hour_ago = now() - timedelta(hours=1)
        hits = [t for t in _public_hits.get(ip, []) if t > hour_ago]
        if len(hits) >= PUBLIC_MAX_PER_HOUR and not current_user():
            flash("Too many requests from this device in the last hour. Please try later.", "error")
        else:
            rid = save_request(request.form, lists, current_user()["id"] if current_user() else None, ip)
            if rid:
                hits.append(now())
                session["last_request_by"] = " ".join(request.form.get("made_by", "").split()).upper()
        _public_hits[ip] = hits
        return redirect(url_for("public_request"))
    return render_template("request_public.html", need_pin=False, made_by=lists["made_by"], models=lists["models"],
                           operators=lists["operators"], last_by=session.get("last_request_by", ""))


BOARD_DONE_DAYS = 3


def request_board():
    """Pending requests (oldest first) + requests done in the last 3 days, for the open status list
    on the login and request pages. None when a PIN is set and not entered on this device."""
    if not public_allowed():
        return None
    since = (now() - timedelta(days=BOARD_DONE_DAYS)).strftime(TS_FMT)
    pending = request_rows("r.status='pending'")
    done = sorted(request_rows("r.status='done' AND r.closed_at >= ?", (since,)),
                  key=lambda r: r["closed_at"], reverse=True)
    return {"pending": pending, "done": done, "at": now_str()}


app.jinja_env.globals["request_board"] = request_board


@app.route("/request/party")
def party_search():
    """Party names matching what is typed (2+ letters, max 15) - the full list is never sent."""
    if not public_allowed():
        abort(403)
    q = " ".join(request.args.get("q", "").split()).upper()
    if len(q) < 2:
        return {"parties": []}
    parties = request_lists()["parties"]
    starts = [p for p in parties if p.startswith(q)]
    contains = [p for p in parties if q in p and not p.startswith(q)]
    return {"parties": (starts + contains)[:15]}


@app.route("/requests", methods=["GET", "POST"])
@login_required
def requests_page():
    me = current_user()
    is_admin = me["role"] == "admin"
    db = get_db()
    lists = request_lists()
    if request.method == "POST":
        f = request.form
        action = f.get("action", "add")
        if action in ("done", "cancel", "reopen"):
            r = db.execute("SELECT * FROM laser_requests WHERE id=?", (f.get("id"),)).fetchone()
            if not r:
                abort(404)
            if not (is_admin or r["operator_id"] == me["id"] or r["user_id"] == me["id"]):
                flash("Only the operator, the person who asked, or admin can change this request.", "error")
            else:
                st = {"done": "done", "cancel": "cancelled", "reopen": "pending"}[action]
                db.execute("UPDATE laser_requests SET status=?, closed_at=?, closed_by=? WHERE id=?",
                           (st, None if st == "pending" else now_str(), None if st == "pending" else me["id"], r["id"]))
                db.commit()
                flash(f"Request #{r['id']} ({r['party']} - {r['model']} x {r['qty']}) marked {st}.", "ok")
            return redirect(request.referrer or url_for("requests_page"))
        save_request(f, lists, me["id"])
        return redirect(url_for("requests_page"))

    status = request.args.get("status", "pending")
    where, params = [], []
    if status in ("pending", "done", "cancelled"):
        where.append("r.status=?")
        params.append(status)
    if request.args.get("operator"):
        where.append("r.operator_id=?")
        params.append(request.args["operator"])
    if request.args.get("party"):
        where.append("r.party=?")
        params.append(request.args["party"])
    if status != "pending":
        try:
            d1 = datetime.strptime(request.args.get("from", ""), "%Y-%m-%d").date()
        except ValueError:
            d1 = now().date() - timedelta(days=30)
        where.append("substr(r.created_at,1,10) >= ?")
        params.append(d1.isoformat())
    rows = request_rows(" AND ".join(where) or "1=1", params)
    return render_template("requests.html", rows=rows, status=status, args=request.args, **lists)


@app.route("/requests.csv")
@login_required
def requests_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Request no", "Date / time", "Request by", "Party", "Model", "Qty", "Operator", "Unit", "Status",
                "Closed at", "Closed by", "Entered by", "Remarks"])
    for r in request_rows():
        w.writerow([r["id"], r["created_at"], r["made_by"], r["party"], r["model"], r["qty"], r["operator_name"] or "",
                    r["unit_name"] or "", r["status"], r["closed_at"] or "", r["closed_by_name"] or "",
                    r["requested_by"] or "Request page (no login)", r["remarks"] or ""])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=laser_requests_{now().strftime('%Y%m%d_%H%M')}.csv"})


# ---------------------------------------------------------------- loaders
def pick_loaders(unit_id, n=2):
    """Fair rotation: loaders of the unit (not absent today) with the fewest sheets today go first;
    on a tie, the one who has waited longest since his last sheet. Returns up to n names."""
    db = get_db()
    today = now().strftime("%Y-%m-%d")
    names = [r["name"] for r in db.execute(
        "SELECT name FROM loaders WHERE unit_id=? AND active=1 AND (absent_on IS NULL OR absent_on<>?)",
        (unit_id, today))]
    if not names:
        return []
    load = {nm: [0, ""] for nm in names}
    for r in db.execute("SELECT loader1, loader2, finished_at FROM entries WHERE unit_id=? AND "
                        "(loader1 IS NOT NULL OR loader2 IS NOT NULL) ORDER BY finished_at", (unit_id,)):
        for nm in (r["loader1"], r["loader2"]):
            if nm in load:
                if (r["finished_at"] or "")[:10] == today:
                    load[nm][0] += 1
                load[nm][1] = max(load[nm][1], r["finished_at"] or "")
    return sorted(names, key=lambda nm: (load[nm][0], load[nm][1], nm))[:n]


def loader_counts(start, end, unit_id=None, machine_id=None):
    where, params = ["substr(e.finished_at,1,10) BETWEEN ? AND ?"], [start.isoformat(), end.isoformat()]
    if unit_id:
        where.append("e.unit_id=?")
        params.append(unit_id)
    if machine_id:
        where.append("e.machine_id=?")
        params.append(machine_id)
    out = {}
    for r in get_db().execute("SELECT e.loader1, e.loader2, un.name AS unit_name FROM entries e "
                              f"JOIN units un ON un.id=e.unit_id WHERE {' AND '.join(where)}", params):
        for nm in (r["loader1"], r["loader2"]):
            if nm:
                d = out.setdefault((nm, r["unit_name"]), {"loader": nm, "unit": r["unit_name"], "sheets": 0})
                d["sheets"] += 1
    return sorted(out.values(), key=lambda d: (d["unit"], -d["sheets"], d["loader"]))


@app.route("/entry/<int:eid>/finish", methods=["POST"])
@login_required
def finish_entry(eid):
    me = current_user()
    db = get_db()
    e = db.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    if not e or (e["user_id"] != me["id"] and me["role"] != "admin"):
        abort(404)
    if e["cut_end"] is None:
        db.execute("BEGIN IMMEDIATE")          # two machines finishing together get different loaders
        t = now_str()
        names = pick_loaders(e["unit_id"])
        db.execute("UPDATE entries SET cut_end=?, finished_at=?, loader1=?, loader2=? WHERE id=? AND cut_end IS NULL",
                   (t, t, names[0] if names else None, names[1] if len(names) > 1 else None, eid))
        db.commit()
        flash(f"{e['sheet_code']} finished.", "ok")
        if names:
            flash("🧑‍🔧 Loaders: " + " & ".join(names), "loaders")
        else:
            flash("No loaders set up for this unit. Add them on the Masters page.", "error")
    return redirect(request.referrer or url_for("home"))


@app.route("/entry/<int:eid>/cancel", methods=["POST"])
@login_required
def cancel_entry(eid):
    """Operator may cancel a running cut started by mistake; admin may delete any entry."""
    me = current_user()
    db = get_db()
    e = db.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    if not e:
        abort(404)
    allowed = me["role"] == "admin" or (e["user_id"] == me["id"] and e["cut_end"] is None)
    if not allowed:
        abort(403)
    db.execute("DELETE FROM entries WHERE id=?", (eid,))
    db.commit()
    flash(f"Deleted {e['sheet_code']}.", "ok")
    return redirect(request.referrer or url_for("home"))


# ---------------------------------------------------------------- reports
def query_entries(args, me, limit=None):
    sql = ("SELECT e.*, m.name AS machine_name, un.name AS unit_name, us.full_name AS operator, "
           "us.username FROM entries e JOIN machines m ON m.id=e.machine_id "
           "JOIN units un ON un.id=e.unit_id JOIN users us ON us.id=e.user_id WHERE 1=1")
    params = []
    if me["role"] != "admin":
        sql += " AND e.user_id=?"
        params.append(me["id"])
    if args.get("from"):
        sql += " AND substr(e.cut_start,1,10) >= ?"
        params.append(args["from"])
    if args.get("to"):
        sql += " AND substr(e.cut_start,1,10) <= ?"
        params.append(args["to"])
    for key, col in (("unit", "e.unit_id"), ("machine", "e.machine_id"), ("user", "e.user_id")):
        if args.get(key):
            sql += f" AND {col}=?"
            params.append(args[key])
    if args.get("material"):
        sql += " AND e.material=?"
        params.append(args["material"])
    if args.get("thickness"):
        sql += " AND e.thickness=?"
        params.append(clean_code(args["thickness"]))
    if args.get("q"):
        sql += " AND (e.sheet_code LIKE ? OR e.sheet_name LIKE ? OR e.remarks LIKE ?)"
        params += [f"%{args['q']}%"] * 3
    sql += " ORDER BY e.cut_start DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return get_db().execute(sql, params).fetchall()


def default_range(args):
    args = dict(args)
    if not args.get("from") and not args.get("to"):
        args["from"] = args["to"] = now().strftime("%Y-%m-%d")
    return args


@app.route("/entries")
@login_required
def entries():
    me = current_user()
    args = default_range(request.args)
    rows = query_entries(args, me, limit=2000)
    db = get_db()
    totals = {"sheets": len(rows), "qty": sum(r["qty"] for r in rows),
              "minutes": sum(minutes_between(r["cut_start"], r["cut_end"]) or 0 for r in rows)}
    return render_template(
        "entries.html", rows=rows, args=args, totals=totals,
        materials=[m["name"] for m in material_list(active_only=False)],
        units=db.execute("SELECT * FROM units ORDER BY name").fetchall(),
        machines=db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u ON u.id=m.unit_id "
                            "ORDER BY u.name, m.name").fetchall(),
        users=db.execute("SELECT * FROM users WHERE role='operator' ORDER BY full_name").fetchall())


def _blank(v):
    return "" if v is None else v


@app.route("/entries.csv")
@login_required
def entries_csv():
    rows = query_entries(default_range(request.args), current_user())
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID", "Sheet Code", "Unit", "Machine", "Material", "Thickness", "Sheet Name", "Qty",
                "Operator", "Cut Start", "Cut End", "Cut Minutes", "Entered At",
                "Entry Delay (min)", "Mode", "Loader 1", "Loader 2", "Remarks"])
    for r in rows:
        w.writerow([r["id"], r["sheet_code"], r["unit_name"], r["machine_name"], r["material"],
                    r["thickness"], r["sheet_name"], r["qty"], r["operator"], r["cut_start"],
                    r["cut_end"] or "", _blank(minutes_between(r["cut_start"], r["cut_end"])),
                    r["entered_at"], minutes_between(r["cut_end"], r["entered_at"]) if r["mode"] == "manual" else 0,
                    "Live (Start/Finish)" if r["mode"] == "live" else "Manual (typed later)",
                    r["loader1"] or "", r["loader2"] or "", r["remarks"] or ""])
    name = f"laser_entries_{now().strftime('%Y%m%d_%H%M')}.csv"
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


# ---------------------------------------------------------------- summary (for incentive)
def period_range(period, day):
    """Return (from_date, to_date, label, prev_day, next_day) for day / week / month."""
    if period == "week":
        start = day - timedelta(days=day.weekday())            # Monday
        end = start + timedelta(days=6)
        label = f"Week {start.strftime('%d %b')} - {end.strftime('%d %b %Y')}"
        return start, end, label, start - timedelta(days=7), start + timedelta(days=7)
    if period == "month":
        start = day.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        end = nxt - timedelta(days=1)
        prev = (start - timedelta(days=1)).replace(day=1)
        return start, end, start.strftime("%B %Y"), prev, nxt
    return day, day, day.strftime("%d %b %Y (%A)"), day - timedelta(days=1), day + timedelta(days=1)


def thickness_key(t):
    """Order thickness thick -> thin: gauges (10G before 16G) then mm (larger first)."""
    m = re.match(r"^(\d+(?:\.\d+)?)(G|MM)?$", t or "")
    if not m:
        return (2, 0, t)
    num = float(m.group(1))
    return (0, num, t) if m.group(2) == "G" else (1, -num, t)


def build_summary(args, me):
    period = args.get("period") if args.get("period") in ("day", "week", "month") else "month"
    try:
        day = datetime.strptime(args.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        day = now().date()
    start, end, label, prev_day, next_day = period_range(period, day)

    where = "e.cut_end IS NOT NULL AND substr(e.cut_start,1,10) BETWEEN ? AND ?"
    params = [start.isoformat(), end.isoformat()]
    if me["role"] != "admin":
        where += " AND e.user_id=?"
        params.append(me["id"])
    elif args.get("unit"):
        where += " AND e.unit_id=?"
        params.append(args["unit"])
    if args.get("material"):
        where += " AND e.material=?"
        params.append(args["material"])
    if args.get("machine"):
        where += " AND e.machine_id=?"
        params.append(args["machine"])

    db = get_db()
    rows = db.execute(
        "SELECT e.user_id, us.full_name AS operator, un.name AS unit_name, e.material, e.thickness, "
        "COUNT(*) AS sheets, SUM(e.qty) AS qty, "
        "SUM((julianday(e.cut_end)-julianday(e.cut_start))*1440) AS minutes "
        "FROM entries e JOIN users us ON us.id=e.user_id JOIN units un ON un.id=e.unit_id "
        f"WHERE {where} GROUP BY e.user_id, e.material, e.thickness", params).fetchall()

    mat_order = {m["name"]: i for i, m in enumerate(material_list(active_only=False))}
    columns = sorted({(r["material"], r["thickness"]) for r in rows},
                     key=lambda c: (mat_order.get(c[0], 999), c[0], thickness_key(c[1])))
    ops = {}
    for r in rows:
        o = ops.setdefault(r["user_id"], {"operator": r["operator"], "unit": r["unit_name"], "cells": {},
                                          "sheets": 0, "qty": 0, "minutes": 0})
        o["cells"][(r["material"], r["thickness"])] = {"sheets": r["sheets"], "qty": r["qty"]}
        o["sheets"] += r["sheets"]
        o["qty"] += r["qty"]
        o["minutes"] += r["minutes"] or 0
    operators = sorted(ops.values(), key=lambda o: -o["sheets"])
    col_totals = {c: {"sheets": sum(o["cells"].get(c, {}).get("sheets", 0) for o in operators),
                      "qty": sum(o["cells"].get(c, {}).get("qty", 0) for o in operators)} for c in columns}
    grand = {"sheets": sum(o["sheets"] for o in operators), "qty": sum(o["qty"] for o in operators),
             "minutes": sum(o["minutes"] for o in operators)}

    # day-by-day sheets per operator (for week / month)
    daily, days = {}, []
    if period != "day":
        for r in db.execute(
                "SELECT substr(e.cut_start,1,10) AS d, e.user_id, COUNT(*) AS sheets "
                f"FROM entries e WHERE {where} GROUP BY d, e.user_id", params).fetchall():
            daily.setdefault(r["d"], {})[r["user_id"]] = r["sheets"]
        d = start
        while d <= end:
            days.append(d.isoformat())
            d += timedelta(days=1)
    op_ids = [uid for uid, _ in sorted(ops.items(), key=lambda kv: -kv[1]["sheets"])]
    groups = []                                   # material header spans, same order as columns
    for m, _t in columns:
        if groups and groups[-1][0] == m:
            groups[-1][1] += 1
        else:
            groups.append([m, 1])
    return dict(period=period, date=day.isoformat(), label=label, start=start, end=end, groups=groups,
                where=where, params=params,
                prev_day=prev_day.isoformat(), next_day=next_day.isoformat(),
                columns=columns, operators=operators, col_totals=col_totals, grand=grand,
                daily=daily, days=days, op_ids=op_ids, ops=ops)


def machine_lots(s, b):
    """Per machine in the period: lot size, laser sheets / qty / lots, bending out qty / lots."""
    db = get_db()
    rows = {}

    def row(mid, name, unit, lot):
        return rows.setdefault(mid, {"machine": name or "-", "unit": unit or "", "lot": lot, "sheets": 0, "qty": 0,
                                     "bend_out": 0})
    for r in db.execute("SELECT e.machine_id, m.name, m.lot_size, un.name AS unit_name, COUNT(*) AS sheets, "
                        "SUM(e.qty) AS qty FROM entries e JOIN machines m ON m.id=e.machine_id "
                        f"JOIN units un ON un.id=e.unit_id WHERE {s['where']} GROUP BY e.machine_id", s["params"]):
        d = row(r["machine_id"], r["name"], r["unit_name"], r["lot_size"])
        d["sheets"], d["qty"] = r["sheets"], r["qty"] or 0
    lots = {m["id"]: m for m in db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u ON u.id=m.unit_id")}
    for r in b["out_list"]:
        if r["kind"] == "direct" or not r.get("machine_id"):
            continue
        m = lots.get(r["machine_id"])
        d = row(r["machine_id"], m["name"] if m else r["machine_name"], m["unit_name"] if m else r["unit_name"],
                m["lot_size"] if m else None)
        d["bend_out"] += r["qty"]
    for d in rows.values():
        d["lots"] = round(d["qty"] / d["lot"], 1) if d["lot"] else None
        d["bend_lots"] = round(d["bend_out"] / d["lot"], 1) if d["lot"] else None
    return sorted(rows.values(), key=lambda d: (d["unit"], d["machine"]))


@app.route("/summary")
@login_required
def summary():
    me = current_user()
    data = build_summary(request.args, me)
    db = get_db()
    if me["role"] == "admin" or not me["unit_id"]:
        machines = db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u "
                              "ON u.id=m.unit_id ORDER BY u.name, m.name").fetchall()
    else:
        machines = db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u "
                              "ON u.id=m.unit_id WHERE m.unit_id=? ORDER BY m.name", (me["unit_id"],)).fetchall()
    bend = bending_data({"period": data["period"], "date": data["date"], "unit": request.args.get("unit", ""),
                         "machine": request.args.get("machine", "")}, me)
    unit_scope = int(request.args["unit"]) if (me["role"] == "admin" and request.args.get("unit")) else None
    lscope = unit_scope if me["role"] == "admin" else me["unit_id"]
    return render_template("summary.html", s=data, args=request.args, machines=machines, b=bend,
                           mlots=machine_lots(data, bend),
                           loader_rows=loader_counts(data["start"], data["end"], lscope, request.args.get("machine")),
                           waiting=bending_waiting(me, unit_scope),
                           units=db.execute("SELECT * FROM units ORDER BY name").fetchall(),
                           materials=[m["name"] for m in material_list(active_only=False)])


@app.route("/summary.csv")
@login_required
def summary_csv():
    s = build_summary(request.args, current_user())
    out = io.StringIO()
    w = csv.writer(out)
    title = f"Laser cutting summary - {s['label']}"
    if request.args.get("machine"):
        m = get_db().execute("SELECT name FROM machines WHERE id=?", (request.args["machine"],)).fetchone()
        title += f" - Machine {m['name'] if m else ''}"
    if request.args.get("material"):
        title += f" - Material {request.args['material']}"
    w.writerow([title])
    w.writerow(["Numbers = sheets cut (finished entries only)."])
    w.writerow([])
    w.writerow(["Operator", "Unit"] + [f"{m} {t}" for m, t in s["columns"]] +
               ["Total sheets", "Total qty", "Cutting hours"])
    for o in s["operators"]:
        w.writerow([o["operator"], o["unit"]] +
                   [o["cells"].get(c, {}).get("sheets", 0) for c in s["columns"]] +
                   [o["sheets"], o["qty"], round(o["minutes"] / 60, 2)])
    w.writerow(["TOTAL", ""] + [s["col_totals"][c]["sheets"] for c in s["columns"]] +
               [s["grand"]["sheets"], s["grand"]["qty"], round(s["grand"]["minutes"] / 60, 2)])
    if s["days"]:
        w.writerow([])
        w.writerow(["Day-wise sheets"])
        w.writerow(["Date"] + [s["ops"][u]["operator"] for u in s["op_ids"]] + ["Total"])
        for d in s["days"]:
            vals = [s["daily"].get(d, {}).get(u, 0) for u in s["op_ids"]]
            w.writerow([datetime.strptime(d, "%Y-%m-%d").strftime("%d-%m-%Y")] + vals + [sum(vals)])
    b = bending_data({"period": s["period"], "date": s["date"], "unit": request.args.get("unit", ""),
                      "machine": request.args.get("machine", "")}, current_user())
    w.writerow([])
    me = current_user()
    lscope = (request.args.get("unit") or None) if me["role"] == "admin" else me["unit_id"]
    w.writerow([])
    w.writerow(["Machine-wise (lot size)"])
    w.writerow(["Unit", "Machine", "Lot size", "Laser sheets", "Laser qty", "Laser lots", "Bending out qty",
                "Bending lots"])
    for d in machine_lots(s, b):
        w.writerow([d["unit"], d["machine"], d["lot"] or "", d["sheets"], d["qty"],
                    d["lots"] if d["lots"] is not None else "", d["bend_out"],
                    d["bend_lots"] if d["bend_lots"] is not None else ""])
    w.writerow([])
    w.writerow(["Loaders - sheets unloaded"])
    w.writerow(["Loader", "Unit", "Sheets"])
    for d in loader_counts(s["start"], s["end"], lscope, request.args.get("machine")):
        w.writerow([d["loader"], d["unit"], d["sheets"]])
    w.writerow([])
    write_bending_csv(w, b)
    name = f"laser_summary_{s['period']}_{s['start'].isoformat()}.csv"
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


# ---------------------------------------------------------------- bending
PERSON_SQL = "COALESCE(NULLIF(b.person,''), us.full_name)"   # old rows: the login name


def bending_query(args, me, start, end):
    # an entry belongs to the period if it was given / sent OR taken out in it
    where = "(substr(b.done_at,1,10) BETWEEN ? AND ? OR substr(b.out_at,1,10) BETWEEN ? AND ?)"
    params = [start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()]
    if me["role"] != "admin":
        where += " AND b.user_id=?"
        params.append(me["id"])
    else:
        if args.get("unit"):
            where += " AND b.unit_id=?"
            params.append(args["unit"])
        if args.get("user"):
            where += " AND b.user_id=?"
            params.append(args["user"])
    if args.get("model"):
        where += " AND b.model=?"
        params.append(args["model"])
    if args.get("machine"):
        where += " AND b.machine_id=?"
        params.append(args["machine"])
    if args.get("person"):
        where += f" AND {PERSON_SQL}=?"
        params.append(args["person"])
    return where, params


BEND_STATUS = {"in": "Given to bending", "out": "Out from bending", "direct": "Sent without bending"}
NEEDS_TO_UNIT = ("out", "direct")       # these must say which unit the parts went to
# Flow: an operator saves "Given to bending" (IN); when bending is finished someone presses OUT on that line,
# which stores the OUT time and the unit it went to. Older separate "out" rows still count as out.


def in_period(ts, start, end):
    return bool(ts) and start.isoformat() <= ts[:10] <= end.isoformat()


def bending_status(unit_id=None, start=None, end=None):
    """Per unit: qty given / out / sent without bending in the period, and pending in bending now."""
    db = get_db()
    where, params = ("WHERE b.unit_id=?", [unit_id]) if unit_id else ("", [])
    rows = db.execute("SELECT b.unit_id, un.name AS unit_name, b.status, b.qty, b.done_at, b.out_at, b.out_kind "
                      "FROM bending b "
                      f"LEFT JOIN units un ON un.id=b.unit_id {where}", params).fetchall()
    out = {}
    for r in rows:
        d = out.setdefault(r["unit_id"], {"unit_id": r["unit_id"], "unit_name": r["unit_name"],
                                          "given": 0, "done": 0, "direct": 0, "pending": 0})
        if r["status"] == "in":
            if not r["out_at"]:
                d["pending"] += r["qty"]
            if start and in_period(r["done_at"], start, end):
                d["given"] += r["qty"]
            if start and in_period(r["out_at"], start, end):
                d["direct" if r["out_kind"] == "direct" else "done"] += r["qty"]
        elif start and in_period(r["done_at"], start, end):
            d["done" if r["status"] == "out" else "direct"] += r["qty"]
    return sorted((d for d in out.values() if d["pending"] or d["given"] or d["done"] or d["direct"]),
                  key=lambda d: d["unit_name"] or "")


def bending_waiting(me, unit_id=None):
    """'Given to bending' entries still waiting for OUT (operators see their unit)."""
    where, params = "b.status='in' AND b.out_at IS NULL", []
    scope = unit_id or (me["unit_id"] if me["role"] != "admin" else None)
    if scope:
        where += " AND b.unit_id=?"
        params.append(scope)
    return get_db().execute(
        f"SELECT b.*, {PERSON_SQL} AS person_name, un.name AS unit_name, m.name AS machine_name FROM bending b "
        "JOIN users us ON us.id=b.user_id LEFT JOIN units un ON un.id=b.unit_id "
        f"LEFT JOIN machines m ON m.id=b.machine_id WHERE {where} ORDER BY b.done_at", params).fetchall()


def out_lots(rows, start, end):
    """Lines that left bending in the period (OUT or sent without bending from a given lot), with the
    lot they came from: a lot given as 12 and sent out as 8 + 4 shows '8 of 12'."""
    db = get_db()
    lots = {}
    for r in db.execute("SELECT COALESCE(lot_id, id) AS lot, qty, out_at FROM bending WHERE status='in'"):
        d = lots.setdefault(r["lot"], {"total": 0, "waiting": 0})
        d["total"] += r["qty"]
        if not r["out_at"]:
            d["waiting"] += r["qty"]
    out = []
    for r in rows:
        if r["status"] == "in" and r["out_at"] and in_period(r["out_at"], start, end):
            lot = lots.get(r["lot_id"] or r["id"], {"total": r["qty"], "waiting": 0})
            out.append(dict(r, kind=r["out_kind"] or "out", when=r["out_at"], lot_qty=lot["total"],
                            lot_waiting=lot["waiting"]))
        elif r["status"] in ("out", "direct") and in_period(r["done_at"], start, end):
            out.append(dict(r, kind=r["status"], when=r["done_at"], lot_qty=None, lot_waiting=0))
    return sorted(out, key=lambda d: d["when"], reverse=True)


def bending_data(args, me):
    period = args.get("period") if args.get("period") in ("day", "week", "month") else "day"
    try:
        day = datetime.strptime(args.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        day = now().date()
    start, end, label, prev_day, next_day = period_range(period, day)
    where, params = bending_query(args, me, start, end)
    db = get_db()
    rows = db.execute(f"SELECT b.*, us.full_name AS operator, {PERSON_SQL} AS person_name, "
                      "un.name AS unit_name, tu.name AS to_unit_name, m.name AS machine_name "
                      "FROM bending b JOIN users us ON us.id=b.user_id LEFT JOIN units un ON un.id=b.unit_id "
                      "LEFT JOIN units tu ON tu.id=b.to_unit_id LEFT JOIN machines m ON m.id=b.machine_id "
                      f"WHERE {where} ORDER BY b.done_at DESC, b.id DESC", params).fetchall()
    people, sent = {}, {}
    tot = {"in": 0, "out": 0, "direct": 0}

    def add(r, key, sent_key=None):
        p = people.setdefault(r["person_name"], {"person": r["person_name"], "unit": r["unit_name"] or "",
                                                 "in": 0, "out": 0, "direct": 0, "mins": 0, "timed": 0})
        p[key] += r["qty"]
        tot[key] += r["qty"]
        if sent_key:
            k = (r["unit_name"] or "-", r["to_unit_name"] or "-")
            d = sent.setdefault(k, {"from": k[0], "to": k[1], "out": 0, "direct": 0})
            d[sent_key] += r["qty"]
        return p

    for r in rows:
        if r["status"] == "in":
            if in_period(r["done_at"], start, end):
                add(r, "in")
            if in_period(r["out_at"], start, end):
                if r["out_kind"] == "direct":          # given to bending, then sent on without bending
                    add(r, "direct", "direct")
                else:
                    p = add(r, "out", "out")
                    p["mins"] += minutes_between(r["done_at"], r["out_at"]) or 0
                    p["timed"] += 1
        elif in_period(r["done_at"], start, end):
            add(r, r["status"], r["status"])
    for p in people.values():
        p["avg"] = round(p["mins"] / p["timed"]) if p["timed"] else None
    unit_scope = (me["unit_id"] if me["role"] != "admin" and me["unit_id"] else
                  (int(args["unit"]) if me["role"] == "admin" and args.get("unit") else None))
    return dict(period=period, date=day.isoformat(), label=label, start=start, end=end,
                prev_day=prev_day.isoformat(), next_day=next_day.isoformat(), rows=rows,
                people=sorted(people.values(), key=lambda p: -p["out"]),
                sent=sorted(sent.values(), key=lambda d: (d["from"], d["to"])),
                status_rows=bending_status(unit_scope, start, end),
                given_total=tot["in"], grand=tot["out"], direct_total=tot["direct"],
                out_list=out_lots(rows, start, end))


@app.route("/bending/<int:bid>/out", methods=["POST"])
@login_required
def bending_out(bid):
    """OUT / without-bending button on a 'Given to bending' line. The qty can be less than given:
    then that part is recorded as out and the rest stays in bending on the same line."""
    me = current_user()
    db = get_db()
    to_unit = db.execute("SELECT * FROM units WHERE id=?", (request.form.get("to_unit_id"),)).fetchone()
    back = request.form.get("back") or url_for("bending")
    kind = "direct" if request.args.get("kind") == "direct" else "out"
    db.execute("BEGIN IMMEDIATE")              # two people pressing OUT on the same line at once
    row = db.execute("SELECT * FROM bending WHERE id=?", (bid,)).fetchone()
    try:
        qty = int(request.form.get("out_qty") or (row["qty"] if row else 0))
    except ValueError:
        qty = 0
    if not row or row["status"] != "in":
        db.rollback()
        abort(404)
    if me["role"] != "admin" and me["unit_id"] and row["unit_id"] != me["unit_id"]:
        msg = ("This entry belongs to another unit.", "error")
    elif row["out_at"]:
        msg = ("Already marked OUT.", "error")
    elif not to_unit:
        msg = ("Select to which unit it is sent, then press OUT.", "error")
    elif qty < 1 or qty > row["qty"]:
        msg = (f"OUT qty must be between 1 and {row['qty']}.", "error")
    else:
        t = now_str()
        left = row["qty"] - qty
        if left:
            # split: the part going out becomes its own line, the rest stays waiting on this line
            db.execute("INSERT INTO bending(user_id, unit_id, machine_id, model, qty, status, person, remarks, done_at, "
                       "entered_at, out_at, out_by, to_unit_id, out_kind, lot_id) "
                       "VALUES (?,?,?,?,?,'in',?,?,?,?,?,?,?,?,?)",
                       (row["user_id"], row["unit_id"], row["machine_id"], row["model"] or "", qty, row["person"],
                        row["remarks"], row["done_at"], row["entered_at"], t, me["id"], to_unit["id"], kind,
                        row["lot_id"] or row["id"]))
            db.execute("UPDATE bending SET qty=? WHERE id=?", (left, bid))
        else:
            db.execute("UPDATE bending SET out_at=?, out_by=?, to_unit_id=?, out_kind=? WHERE id=?",
                       (t, me["id"], to_unit["id"], kind, bid))
        txt = (f"Sent without bending: {qty} pcs ({row['person'] or ''}) to {to_unit['name']}."
               if kind == "direct" else
               f"OUT from bending: {qty} pcs ({row['person'] or ''}) sent to {to_unit['name']}. "
               f"Bending took {fmt_minutes(minutes_between(row['done_at'], t))}.")
        msg = (txt + (f" {left} pcs still in bending." if left else ""), "ok")
    db.commit()
    flash(*msg)
    return redirect(back)


@app.route("/bending", methods=["GET", "POST"])
@login_required
def bending():
    me = current_user()
    is_admin = me["role"] == "admin"
    db = get_db()
    units = db.execute("SELECT * FROM units ORDER BY name").fetchall()
    if request.method == "POST":
        f = request.form
        if f.get("action") == "delete":
            row = db.execute("SELECT * FROM bending WHERE id=?", (f.get("id"),)).fetchone()
            today = now().strftime("%Y-%m-%d")
            if row and (is_admin or (row["user_id"] == me["id"] and row["entered_at"][:10] == today)):
                db.execute("DELETE FROM bending WHERE id=?", (row["id"],))
                db.commit()
                flash(f"Deleted {BEND_STATUS.get(row['status'], '')} x {row['qty']}.", "ok")
            else:
                flash("You can delete only your own entries made today. Ask admin.", "error")
            return redirect(request.referrer or url_for("bending"))
        person = ITEM_KINDS["person"][2](f.get("person"))
        try:
            qty = int(f.get("qty") or "")
        except ValueError:
            qty = 0
        done = parse_dt(f.get("done_at") or "") or now()
        status = f.get("status")
        machine = next((m for m in machines_for(me) if str(m["id"]) == f.get("machine_id")), None)
        to_unit = next((u for u in units if str(u["id"]) == f.get("to_unit_id")), None)
        if status not in ("in", "direct"):
            flash("Choose: Given to bending or Sent without bending.", "error")
        elif not machine:
            flash("Select the machine.", "error")
        elif not person:
            flash("Enter the bending person name.", "error")
        elif status in NEEDS_TO_UNIT and not to_unit:
            flash("Select to which unit the parts are sent.", "error")
        elif qty < 1:
            flash("Enter quantity (1 or more).", "error")
        elif done > now() + timedelta(minutes=5):
            flash("Date / time cannot be in the future.", "error")
        else:
            to_id = to_unit["id"] if (to_unit and status in NEEDS_TO_UNIT) else None
            db.execute("BEGIN IMMEDIATE")      # stop double saves from a double tap
            dup = db.execute("SELECT 1 FROM bending WHERE user_id=? AND machine_id=? AND qty=? AND status=? "
                             "AND person=? AND (julianday(?)-julianday(entered_at))*86400 < 20",
                             (me["id"], machine["id"], qty, status, person, now_str())).fetchone()
            if dup:
                db.rollback()
                flash(f"{BEND_STATUS[status]} x {qty} was just saved. Not saved again.", "error")
            else:
                new_person = remember("bend_persons", person, me["id"])
                cur = db.execute("INSERT INTO bending(user_id, unit_id, machine_id, model, qty, status, person, to_unit_id, "
                           "remarks, done_at, entered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (me["id"], machine["unit_id"], machine["id"], "", qty, status, person, to_id,
                            f.get("remarks", "").strip(), done.strftime(TS_FMT), now_str()))
                db.commit()
                session["bend_machine"] = machine["id"]          # pre-select next time
                session["bend_person"] = person
                if new_person:
                    flash(f"New bending person {person} added to the list.", "ok")
                pend = sum(r["pending"] for r in bending_status(machine["unit_id"]))
                # shown right under the form (the page jumps there), not at the top
                session["bend_saved"] = {
                    "id": cur.lastrowid,
                    "text": f"Saved: {BEND_STATUS[status]} - {qty} pcs by {person} on {machine['name']}"
                            + (f", sent to {to_unit['name']}" if to_id else "")
                            + f". Pending in bending now: {pend} pcs."
                            + (" Press OUT on this line when bending is finished." if status == "in" else "")}
                return redirect(url_for("bending") + "#recent")
        return redirect(url_for("bending"))

    data = bending_data(request.args, me)
    persons = [r["name"] for r in db.execute("SELECT name FROM bend_persons WHERE active=1 ORDER BY name")]
    recent = db.execute(f"SELECT b.*, {PERSON_SQL} AS person_name, tu.name AS to_unit_name, m.name AS machine_name, "
                        "un.name AS unit_name "
                        "FROM bending b JOIN users us ON us.id=b.user_id LEFT JOIN units tu ON tu.id=b.to_unit_id "
                        "LEFT JOIN units un ON un.id=b.unit_id "
                        "LEFT JOIN machines m ON m.id=b.machine_id WHERE b.user_id=? "
                        "ORDER BY b.id DESC LIMIT 5", (me["id"],)).fetchall()
    return render_template("bending.html", b=data, args=request.args, person_list=persons,
                           recent=recent, saved=session.pop("bend_saved", None), waiting=bending_waiting(me),
                           last_person=session.get("bend_person", ""), statuses=BEND_STATUS,
                           machines=machines_for(me), last_machine=session.get("bend_machine"),
                           all_machines=db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u "
                                                   "ON u.id=m.unit_id ORDER BY u.name, m.name").fetchall()
                           if is_admin else machines_for(me),
                           today=now().strftime("%Y-%m-%d"), now_local=now().strftime("%Y-%m-%dT%H:%M"),
                           units=units,
                           users=db.execute("SELECT * FROM users WHERE role='operator' ORDER BY full_name").fetchall())


def write_bending_csv(w, b):
    w.writerow([f"Bending - {b['label']}: given {b['given_total']}, out {b['grand']}, "
                f"sent without bending {b['direct_total']}"])
    w.writerow(["Bending person", "Unit", "Given to bending", "Out from bending", "Sent without bending",
                "Avg bending time (min)"])
    for p in b["people"]:
        w.writerow([p["person"], p["unit"], p["in"], p["out"], p["direct"], p["avg"] if p["avg"] is not None else ""])
    w.writerow(["TOTAL", "", b["given_total"], b["grand"], b["direct_total"]])
    w.writerow([])
    w.writerow(["Sent to units"])
    w.writerow(["From unit", "To unit", "Out from bending", "Sent without bending"])
    for d in b["sent"]:
        w.writerow([d["from"], d["to"], d["out"], d["direct"]])
    w.writerow([])
    w.writerow(["Unit", "Given (period)", "Out (period)", "Sent without bending (period)", "Pending in bending now"])
    for r in b["status_rows"]:
        w.writerow([r["unit_name"] or "", r["given"], r["done"], r["direct"], r["pending"]])


@app.route("/bending.csv")
@login_required
def bending_csv():
    b = bending_data(request.args, current_user())
    out = io.StringIO()
    w = csv.writer(out)
    write_bending_csv(w, b)
    w.writerow([])
    w.writerow(["All entries"])
    w.writerow(["Given / sent at", "Entered at", "Status", "OUT at", "Bending time (min)", "Bending person",
                "Entered by", "Unit", "Machine", "Sent to unit", "Qty", "Model (old entries)", "Remarks"])
    for r in b["rows"]:
        st = (("Sent without bending" if r["out_kind"] == "direct" else "Out from bending") if r["out_at"]
              else "In bending (waiting)") if r["status"] == "in" \
            else BEND_STATUS.get(r["status"], r["status"])
        w.writerow([r["done_at"], r["entered_at"], st, r["out_at"] or "",
                    "" if r["out_kind"] == "direct" else _blank(minutes_between(r["done_at"], r["out_at"])),
                    r["person_name"],
                    r["operator"], r["unit_name"] or "", r["machine_name"] or "", r["to_unit_name"] or "",
                    r["qty"], r["model"] or "", r["remarks"] or ""])
    name = f"bending_{b['period']}_{b['start'].isoformat()}.csv"
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


# ---------------------------------------------------------------- stock
# Stock = sheets received + physical-count corrections - sheets cut.
# Every cutting entry uses 1 sheet of its unit / material / thickness (deleting the entry gives it back).
LOW_STOCK = 5


def stock_balances(unit_id=None):
    db = get_db()
    today = now().strftime("%Y-%m-%d")
    where, params = ("WHERE unit_id=?", [unit_id]) if unit_id else ("", [])
    data = {}

    def row(key):
        return data.setdefault(key, {"unit_id": key[0], "material": key[1], "thickness": key[2],
                                     "received": 0, "counted": 0, "used": 0, "used_today": 0, "last": ""})
    for r in db.execute("SELECT unit_id, material, thickness, "
                        "SUM(CASE WHEN kind='in' THEN qty ELSE 0 END) AS received, "
                        "SUM(CASE WHEN kind='count' THEN qty ELSE 0 END) AS counted "
                        f"FROM stock_moves {where} GROUP BY unit_id, material, thickness", params):
        d = row((r["unit_id"], r["material"], r["thickness"]))
        d["received"], d["counted"] = r["received"], r["counted"]
    for r in db.execute("SELECT unit_id, material, thickness, COUNT(*) AS used, "
                        "SUM(substr(cut_start,1,10)=?) AS used_today, MAX(entered_at) AS last "
                        f"FROM entries {where} GROUP BY unit_id, material, thickness", [today] + params):
        d = row((r["unit_id"], r["material"], r["thickness"]))
        d["used"], d["used_today"], d["last"] = r["used"], r["used_today"], r["last"] or ""
    units = {u["id"]: u["name"] for u in db.execute("SELECT * FROM units")}
    mat_order = {m["name"]: i for i, m in enumerate(material_list(active_only=False))}
    hidden = {(h["unit_id"], h["material"], h["thickness"]): h["hidden_at"]
              for h in db.execute("SELECT * FROM stock_hidden")}
    for r in db.execute(f"SELECT unit_id, material, thickness, MAX(entered_at) AS last FROM stock_moves {where} "
                        "GROUP BY unit_id, material, thickness", params):
        d = data.get((r["unit_id"], r["material"], r["thickness"]))
        if d:
            d["last"] = max(d["last"], r["last"] or "")
    out = []
    for key, d in data.items():
        if key in hidden and d["last"] <= hidden[key]:
            continue                       # deleted by admin and nothing new since
        d["balance"] = d["received"] + d["counted"] - d["used"]
        d["unit_name"] = units.get(d["unit_id"], "?")
        out.append(d)
    out.sort(key=lambda d: (d["unit_name"], mat_order.get(d["material"], 999), d["material"],
                            thickness_key(d["thickness"])))
    return out


def stock_balance(unit_id, material, thickness):
    for d in stock_balances(unit_id):
        if d["material"] == material and d["thickness"] == thickness:
            return d["balance"]
    return 0


def stock_scope(me):
    """Operators see their own unit's stock; admin (or an operator without a unit) sees all."""
    return None if (me["role"] == "admin" or not me["unit_id"]) else me["unit_id"]


@app.route("/stock", methods=["GET", "POST"])
@login_required
def stock():
    me = current_user()
    is_admin = me["role"] == "admin"
    db = get_db()
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        unit_id = f.get("unit_id") if (is_admin or not me["unit_id"]) else me["unit_id"]
        unit = db.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone() if unit_id else None
        material = clean_code(f.get("material"))
        thickness = clean_code(f.get("thickness"))
        when = parse_dt(f.get("at") or "") or now()
        try:
            qty = int(f.get("qty") or "")
        except ValueError:
            qty = None
        if action in ("in", "count"):
            if thickness and not thickness_ok(thickness):
                flash(THICK_HELP, "error")
            elif not unit or not material or not thickness:
                flash("Choose unit, material and thickness.", "error")
            elif action == "in" and (qty is None or qty < 1):
                flash("Enter how many sheets came (1 or more).", "error")
            elif action == "count" and not is_admin:
                abort(403)
            elif action == "count" and (qty is None or qty < 0):
                flash("Enter the counted number of sheets (0 or more).", "error")
            else:
                remember("materials", material, me["id"])
                remember("thicknesses", thickness, me["id"])
                if action == "in":
                    db.execute("INSERT INTO stock_moves(unit_id, material, thickness, kind, qty, remarks, at, "
                               "entered_at, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
                               (unit["id"], material, thickness, "in", qty, f.get("remarks", "").strip(),
                                when.strftime(TS_FMT), now_str(), me["id"]))
                    db.commit()
                    flash(f"Received {qty} sheets {material} {thickness} at {unit['name']}. "
                          f"Stock now {stock_balance(unit['id'], material, thickness)}.", "ok")
                else:
                    current = stock_balance(unit["id"], material, thickness)
                    db.execute("INSERT INTO stock_moves(unit_id, material, thickness, kind, qty, counted, remarks, "
                               "at, entered_at, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (unit["id"], material, thickness, "count", qty - current, qty,
                                f.get("remarks", "").strip() or "Physical count", when.strftime(TS_FMT),
                                now_str(), me["id"]))
                    db.commit()
                    flash(f"{material} {thickness} at {unit['name']} set to {qty} sheets "
                          f"(system showed {current}, change {qty - current:+d}).", "ok")
        elif action == "delete" and is_admin:
            db.execute("DELETE FROM stock_moves WHERE id=?", (f.get("id"),))
            db.commit()
            flash("Stock record deleted. Balance recalculated.", "ok")
        elif action in ("line_delete", "line_move") and is_admin:
            key = (int(f.get("unit_id") or 0), f.get("material", ""), f.get("thickness", ""))
            uname = (db.execute("SELECT name FROM units WHERE id=?", (key[0],)).fetchone() or {"name": "?"})["name"]
            if action == "line_delete":
                n = db.execute("DELETE FROM stock_moves WHERE unit_id=? AND material=? AND thickness=?", key).rowcount
                db.execute("INSERT INTO stock_hidden(unit_id, material, thickness, hidden_at) VALUES (?,?,?,?) "
                           "ON CONFLICT(unit_id, material, thickness) DO UPDATE SET hidden_at=excluded.hidden_at",
                           key + (now_str(),))
                db.commit()
                flash(f"Stock line {uname} {key[1]} {key[2]} deleted ({n} received/count records removed). "
                      "Cutting entries are kept.", "ok")
            else:
                parts = (f.get("to", "") or "").upper().split()
                new_mat = clean_code(parts[0]) if parts else ""
                new_thk = clean_code("".join(parts[1:])) if len(parts) > 1 else ""
                if not new_mat or not new_thk:
                    flash("Type material and thickness, e.g. MS 18G.", "error")
                elif not thickness_ok(new_thk):
                    flash(THICK_HELP, "error")
                else:
                    ids = [r[0] for r in db.execute("SELECT id FROM entries WHERE unit_id=? AND material=? AND thickness=?", key)]
                    db.execute("UPDATE entries SET material=?, thickness=? WHERE unit_id=? AND material=? AND thickness=?",
                               (new_mat, new_thk) + key)
                    db.execute("UPDATE stock_moves SET material=?, thickness=? WHERE unit_id=? AND material=? AND thickness=?",
                               (new_mat, new_thk) + key)
                    if ids:
                        recompute_codes(f"e.id IN ({','.join('?' * len(ids))})", ids)
                    remember("materials", new_mat, me["id"])
                    remember("thicknesses", new_thk, me["id"])
                    db.commit()
                    flash(f"{uname}: {key[1]} {key[2]} moved into {new_mat} {new_thk} "
                          f"({len(ids)} cutting entries and their sheet codes updated).", "ok")
        return redirect(url_for("stock", unit=request.args.get("unit", "")))

    scope = stock_scope(me)
    unit_filter = request.args.get("unit") if scope is None else scope
    rows = stock_balances(int(unit_filter) if unit_filter else None)
    mwhere, mparams = ("WHERE s.unit_id=?", [unit_filter]) if unit_filter else ("", [])
    moves = db.execute("SELECT s.*, u.name AS unit_name, us.full_name FROM stock_moves s "
                       "JOIN units u ON u.id=s.unit_id LEFT JOIN users us ON us.id=s.user_id "
                       f"{mwhere} ORDER BY s.at DESC, s.id DESC LIMIT 100", mparams).fetchall()
    return render_template("stock.html", rows=rows, moves=moves, low=LOW_STOCK, unit_filter=str(unit_filter or ""),
                           bend_rows=bending_status(int(unit_filter) if unit_filter else None, now().date(), now().date()),
                           waiting=bending_waiting(me, int(unit_filter) if unit_filter else None),
                           units=db.execute("SELECT * FROM units ORDER BY name").fetchall(),
                           materials=[m["name"] for m in material_list()], thicknesses=thickness_list(),
                           now_local=now().strftime("%Y-%m-%dT%H:%M"))


@app.route("/stock.csv")
@login_required
def stock_csv():
    me = current_user()
    scope = stock_scope(me)
    unit_filter = request.args.get("unit") if scope is None else scope
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"Sheet stock on {now().strftime('%d-%m-%Y %I:%M %p')}"])
    w.writerow(["Unit", "Material", "Thickness", "Received", "Count corrections", "Used (cut)",
                "Used today", "Stock pending"])
    for d in stock_balances(int(unit_filter) if unit_filter else None):
        w.writerow([d["unit_name"], d["material"], d["thickness"], d["received"], d["counted"],
                    d["used"], d["used_today"], d["balance"]])
    name = f"laser_stock_{now().strftime('%Y%m%d_%H%M')}.csv"
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.route("/dashboard")
@admin_required
def dashboard():
    db = get_db()
    day = request.args.get("day") or now().strftime("%Y-%m-%d")
    by_machine = db.execute(
        "SELECT un.name AS unit_name, m.name AS machine_name, COUNT(e.id) AS sheets, "
        "COALESCE(SUM(e.qty),0) AS qty, "
        "COALESCE(SUM((julianday(e.cut_end)-julianday(e.cut_start))*1440),0) AS minutes "
        "FROM machines m JOIN units un ON un.id=m.unit_id "
        "LEFT JOIN entries e ON e.machine_id=m.id AND substr(e.cut_start,1,10)=? "
        "WHERE m.active=1 GROUP BY m.id ORDER BY un.name, m.name", (day,)).fetchall()
    by_operator = db.execute(
        "SELECT us.full_name, un.name AS unit_name, COUNT(e.id) AS sheets, COALESCE(SUM(e.qty),0) AS qty "
        "FROM entries e JOIN users us ON us.id=e.user_id JOIN units un ON un.id=e.unit_id "
        "WHERE substr(e.cut_start,1,10)=? GROUP BY us.id ORDER BY sheets DESC", (day,)).fetchall()
    running = db.execute(
        "SELECT e.*, m.name AS machine_name, un.name AS unit_name, us.full_name AS operator "
        "FROM entries e JOIN machines m ON m.id=e.machine_id JOIN units un ON un.id=e.unit_id "
        "JOIN users us ON us.id=e.user_id WHERE e.cut_end IS NULL ORDER BY e.cut_start").fetchall()
    late = db.execute(
        "SELECT e.*, m.name AS machine_name, un.name AS unit_name, us.full_name AS operator "
        "FROM entries e JOIN machines m ON m.id=e.machine_id JOIN units un ON un.id=e.unit_id "
        "JOIN users us ON us.id=e.user_id WHERE e.mode='manual' AND substr(e.cut_start,1,10)=? "
        "AND (julianday(e.entered_at)-julianday(e.cut_end))*1440 > 60 "
        "ORDER BY e.entered_at DESC", (day,)).fetchall()
    dday = datetime.strptime(day, "%Y-%m-%d").date()
    bend_rows = bending_status(None, dday, dday)
    dday_ = datetime.strptime(day, "%Y-%m-%d").date()
    loader_rows = loader_counts(dday_, dday_)
    bend_day = bending_data({"period": "day", "date": day}, current_user())
    bend_ops = bend_day["people"]
    bend_waiting = bending_waiting(current_user())
    doubles = db.execute(
        "SELECT a.id AS id1, b.id AS id2, a.sheet_code, a.cut_start AS start1, b.cut_start AS start2, "
        "a.entered_at AS ent1, b.entered_at AS ent2, m.name AS machine_name, un.name AS unit_name, "
        "us.full_name AS operator FROM entries a JOIN entries b ON b.machine_id=a.machine_id "
        "AND b.sheet_code=a.sheet_code AND b.id>a.id "
        "AND abs(julianday(b.cut_start)-julianday(a.cut_start))*1440 <= 2 "
        "JOIN machines m ON m.id=a.machine_id JOIN units un ON un.id=a.unit_id JOIN users us ON us.id=a.user_id "
        "WHERE substr(a.cut_start,1,10)=? ORDER BY a.cut_start", (day,)).fetchall()
    return render_template("dashboard.html", day=day, by_machine=by_machine, doubles=doubles,
                           bend_rows=bend_rows, bend_ops=bend_ops, waiting=bend_waiting, loader_rows=loader_rows,
                           units=db.execute("SELECT * FROM units ORDER BY name").fetchall(),
                           stock_rows=stock_balances(), low=LOW_STOCK,
                           by_operator=by_operator, running=running, late=late,
                           now_ts=now_str())


# ---------------------------------------------------------------- admin setup
@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    db = get_db()
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        try:
            if action == "add_unit" and f.get("name", "").strip():
                db.execute("INSERT INTO units(name) VALUES (?)", (f["name"].strip().upper(),))
                flash("Unit added.", "ok")
            elif action == "add_user":
                username = re.sub(r"\s+", "", f.get("username", "").lower())
                if not username or not f.get("full_name", "").strip() or len(f.get("password", "")) < 4:
                    flash("Username, name and password (min 4 chars) are required.", "error")
                else:
                    role = "admin" if f.get("role") == "admin" else "operator"
                    db.execute("INSERT INTO users(username, full_name, password_hash, role, unit_id) "
                               "VALUES (?,?,?,?,?)",
                               (username, f["full_name"].strip(), generate_password_hash(f["password"]),
                                role, f.get("unit_id") or None))
                    flash(f"User {username} created.", "ok")
            elif action == "reset_password" and len(f.get("password", "")) >= 4:
                db.execute("UPDATE users SET password_hash=? WHERE id=?",
                           (generate_password_hash(f["password"]), f["id"]))
                u = db.execute("SELECT username FROM users WHERE id=?", (f["id"],)).fetchone()
                if u:
                    _failed.pop(u["username"], None)
                flash("Password reset.", "ok")
            elif action == "toggle_user":
                if int(f["id"]) == current_user()["id"]:
                    flash("You cannot disable yourself.", "error")
                else:
                    db.execute("UPDATE users SET active=1-active WHERE id=?", (f["id"],))
            elif action == "sheet_settings":
                link = f.get("sheet_url", "").strip()
                if link and not sheet_csv_url(link):
                    flash("That does not look like a Google Sheet link.", "error")
                else:
                    set_setting("sheet_url", link)
                    set_setting("sheet_madeby_col", f.get("madeby_col", "").strip() or "Made By")
                    set_setting("sheet_party_col", f.get("party_col", "").strip() or "PARTY")
                    set_setting("request_pin", f.get("request_pin", "").strip())
                    db.commit()
                    d = fetch_sheet_lists(force=True)
                    if d["error"]:
                        flash(d["error"], "error")
                    else:
                        flash(f"Google Sheet read: {len(d['made_by'])} 'Request by' names and {len(d['party'])} parties.", "ok")
            elif action == "set_user_unit":
                db.execute("UPDATE users SET unit_id=? WHERE id=?", (f.get("unit_id") or None, f["id"]))
                flash("Unit updated.", "ok")
            db.commit()
        except sqlite3.IntegrityError:
            flash("That name already exists.", "error")
        return redirect(url_for("admin"))
    units = db.execute("SELECT * FROM units ORDER BY name").fetchall()
    machines = db.execute("SELECT m.*, u.name AS unit_name FROM machines m JOIN units u ON u.id=m.unit_id "
                          "ORDER BY u.name, m.name").fetchall()
    users = db.execute("SELECT us.*, un.name AS unit_name FROM users us LEFT JOIN units un "
                       "ON un.id=us.unit_id ORDER BY us.role, us.full_name").fetchall()
    return render_template("admin.html", units=units, machines=machines, users=users,
                           sheet_url=get_setting("sheet_url"), madeby_col=get_setting("sheet_madeby_col", "Made By"),
                           party_col=get_setting("sheet_party_col", "PARTY"), request_pin=get_setting("request_pin"),
                           public_link=url_for("public_request", _external=True))


# ---------------------------------------------------------------- backup / restore
@app.route("/admin/backup")
@admin_required
def backup_download():
    """Download a consistent copy of the whole database (all units, users, entries)."""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        get_db().backup(dst)
        dst.close()
        with open(tmp, "rb") as fh:
            data = fh.read()
    finally:
        os.remove(tmp)
    name = f"laserentry_backup_{now().strftime('%Y%m%d_%H%M')}.db"
    return Response(data, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.route("/admin/restore", methods=["POST"])
@admin_required
def backup_restore():
    """Replace ALL data with an uploaded backup file (e.g. laserentry.db from the office PC)."""
    up = request.files.get("file")
    if not up or not up.filename:
        flash("Choose the backup file (.db) first.", "error")
        return redirect(url_for("admin"))
    data_dir = os.path.dirname(os.path.abspath(DB_PATH))
    tmp = os.path.join(data_dir, "restore_upload.tmp")
    up.save(tmp)
    try:
        src = sqlite3.connect(tmp)
        try:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"users", "entries", "machines", "units"} <= tables:
                raise ValueError
            if not src.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
                flash("That backup has no active admin login, so it was not loaded.", "error")
                return redirect(url_for("admin"))
        except (sqlite3.DatabaseError, ValueError):
            flash("That file is not a Laser Entry backup. Nothing was changed.", "error")
            return redirect(url_for("admin"))
        # keep a copy of the current data first, just in case
        safety = os.path.join(data_dir, f"before_restore_{now().strftime('%Y%m%d_%H%M%S')}.db")
        keep = sqlite3.connect(safety)
        get_db().backup(keep)
        keep.close()
        src.backup(get_db())
        src.close()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    init_db()              # adds any newer tables the old backup does not have
    session.clear()
    flash("Backup loaded. Log in again with a login from that backup.", "ok")
    return redirect(url_for("login"))


@app.errorhandler(403)
def forbidden(_e):
    return render_template("message.html", text="You do not have access to this page."), 403


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
