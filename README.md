# Laser Entry

Simple web system to log laser sheet cutting across multiple units and machines.
Operators log in from a phone or PC and record every sheet they cut.

## What it records

Each entry keeps:

| Field | Example |
|---|---|
| Sheet code (auto) | `16GLASER1PANELA` = thickness + machine name + sheet name |
| Unit, Machine, Operator | UNIT1, LASER1, Ramesh |
| Material, Thickness, Qty | MS, 16G, 3 |
| **Cut start / Cut end** | when the sheet was actually cut |
| **Entered at** | when the operator saved it, set by the server (operator cannot change it) |
| Entry delay | time between cut end and entry, so late entries are visible |

## Two ways for an operator to enter a sheet

1. **START CUTTING** (best): press START when the sheet goes on the machine and FINISH
   when it is done. Both times come from the server clock, so they are accurate.
2. **Already cut**: type the cut start/end time for a sheet cut earlier. The time of
   typing is still recorded, and entries typed more than 1 hour late are shown on the
   admin dashboard.

## Who sees what

- **Operator**: sees only machines of their own unit and only their own entries.
- **Admin**: dashboard (machines cutting right now, sheets per machine/operator per day,
  late entries), all entries with filters, Excel (CSV) download, delete wrong entries,
  and Setup (add units, machines, operators, reset passwords).

## Easiest way on Windows

1. Install Python from python.org (tick **"Add python.exe to PATH"** during install).
2. Download this project (GitHub: Code -> Download ZIP) and unzip it.
3. Double-click **START_LASER_ENTRY.bat**. Keep that black window open.

## Install and run by hand (on one PC / server in the office network)

Needs Python 3.9 or newer. Type these in Command Prompt (not inside Python):

```bash
pip install -r requirements.txt
python app.py
```

Open `http://<this-pc-ip>:5000` from any phone/PC on the same network.

First login: **admin / admin123** - change it right away (Password menu).

Then in **Setup**: add your units, add machines to each unit, add operator logins
and assign each operator to a unit.

### Settings (optional environment variables)

- `PORT` - default 5000
- `LASER_TZ` - time zone, default `Asia/Kolkata`
- `LASER_DB` - database file path, default `laserentry.db` next to `app.py`

All data is in the single file `laserentry.db`. Back up that file regularly.

To reach it from units in different locations, run it on a cloud server / VPS
(or any always-on PC with a public address) and open that address from every unit.
