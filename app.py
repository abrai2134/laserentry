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
    if not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        db.execute(
            "INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)",
            ("admin", "Administrator", generate_password_hash("admin123"), "admin"))
        print("Created default login  ->  username: admin   password: admin123  (change it!)")
    if not db.execute("SELECT 1 FROM materials").fetchone():
        db.executemany("INSERT INTO materials(name) VALUES (?)", [(m,) for m in MATERIALS])
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


app.jinja_env.globals.update(fmt_minutes=fmt_minutes, minutes_between=minutes_between)


@app.template_filter("dt")
def dt_filter(value):
    if not value:
        return "-"
    d = datetime.strptime(value, TS_FMT)
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
    return render_template("home.html", running=running, recent=recent, today_count=today_count)


def material_list(active_only=True):
    sql = "SELECT * FROM materials" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
    return get_db().execute(sql).fetchall()


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
            added = []
            if new_machine:
                existing = db.execute("SELECT * FROM machines WHERE name=? AND unit_id=?",
                                      (new_machine, unit["id"])).fetchone()
                if existing:
                    db.execute("UPDATE machines SET active=1 WHERE id=?", (existing["id"],))
                    machine = existing
                else:
                    cur = db.execute("INSERT INTO machines(name, unit_id) VALUES (?,?)", (new_machine, unit["id"]))
                    machine = db.execute("SELECT * FROM machines WHERE id=?", (cur.lastrowid,)).fetchone()
                    added.append(f"machine {new_machine}")
            if remember("materials", material, me["id"]):
                added.append(f"material {material}")
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
            if added:
                flash("New " + ", ".join(added) + " added to the list.", "ok")
            return redirect(url_for("home"))

    return render_template("entry.html", machines=machines, mode=mode,
                           materials=[m["name"] for m in material_list()],
                           sheet_names=sheet_name_list(),
                           units=get_db().execute("SELECT * FROM units ORDER BY name").fetchall(),
                           thicknesses=THICKNESS_SUGGESTIONS, form=request.form,
                           now_local=now().strftime("%Y-%m-%dT%H:%M"))


@app.route("/sheet-names/add", methods=["POST"])
@login_required
def add_sheet_name():
    """Operator adds a sheet name to the saved list from the entry form (no page reload)."""
    name = request.form.get("name", "").strip().upper()
    if not clean_code(name):
        return {"ok": False, "message": "Type a sheet name first."}, 400
    db = get_db()
    is_new = remember("sheet_names", name, current_user()["id"])
    db.commit()
    return {"ok": True, "name": name,
            "message": f"{name} added to sheet list." if is_new else f"{name} is already in the list."}


@app.route("/entry/<int:eid>/finish", methods=["POST"])
@login_required
def finish_entry(eid):
    me = current_user()
    db = get_db()
    e = db.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    if not e or (e["user_id"] != me["id"] and me["role"] != "admin"):
        abort(404)
    if e["cut_end"] is None:
        t = now_str()
        db.execute("UPDATE entries SET cut_end=?, finished_at=? WHERE id=?", (t, t, eid))
        db.commit()
        flash(f"{e['sheet_code']} finished.", "ok")
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
                "Entry Delay (min)", "Mode", "Remarks"])
    for r in rows:
        w.writerow([r["id"], r["sheet_code"], r["unit_name"], r["machine_name"], r["material"],
                    r["thickness"], r["sheet_name"], r["qty"], r["operator"], r["cut_start"],
                    r["cut_end"] or "", _blank(minutes_between(r["cut_start"], r["cut_end"])),
                    r["entered_at"], minutes_between(r["cut_end"], r["entered_at"]) if r["mode"] == "manual" else 0,
                    "Live (Start/Finish)" if r["mode"] == "live" else "Manual (typed later)",
                    r["remarks"] or ""])
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
                prev_day=prev_day.isoformat(), next_day=next_day.isoformat(),
                columns=columns, operators=operators, col_totals=col_totals, grand=grand,
                daily=daily, days=days, op_ids=op_ids, ops=ops)


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
    return render_template("summary.html", s=data, args=request.args, machines=machines,
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
    name = f"laser_summary_{s['period']}_{s['start'].isoformat()}.csv"
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
    return render_template("dashboard.html", day=day, by_machine=by_machine,
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
            elif action == "add_machine" and clean_code(f.get("name")) and f.get("unit_id"):
                db.execute("INSERT INTO machines(name, unit_id) VALUES (?,?)",
                           (clean_code(f["name"]), f["unit_id"]))
                flash("Machine added.", "ok")
            elif action == "toggle_machine":
                db.execute("UPDATE machines SET active=1-active WHERE id=?", (f["id"],))
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
            elif action in ("add_material", "add_sheet_name"):
                table = "materials" if action == "add_material" else "sheet_names"
                name = clean_code(f.get("name")) if table == "materials" else f.get("name", "").strip().upper()
                if name:
                    remember(table, name, current_user()["id"])
                    flash(f"{name} added.", "ok")
            elif action in ("toggle_material", "toggle_sheet_name"):
                table = "materials" if action == "toggle_material" else "sheet_names"
                db.execute(f"UPDATE {table} SET active=1-active WHERE id=?", (f["id"],))
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
    materials = db.execute("SELECT * FROM materials ORDER BY active DESC, name").fetchall()
    sheets = db.execute("SELECT s.*, u.full_name AS added_by_name FROM sheet_names s "
                        "LEFT JOIN users u ON u.id=s.added_by ORDER BY s.active DESC, s.name").fetchall()
    return render_template("admin.html", units=units, machines=machines, users=users,
                           materials=materials, sheets=sheets)


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
