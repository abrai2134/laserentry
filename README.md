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

## Operators can add new items

On the entry form an operator can add, without asking admin:

- **New machine**: choose "+ Add new machine" and type the name. It is added to the operator's own unit.
- **New material**: tap "+ New" and type it (e.g. ZINTEC).
- **New sheet name**: type it and press the orange "+ Add" button (it is also saved automatically on START/SAVE).

- **New thickness**: type it (e.g. 7MM) and press "+ Add".

Operators also have a **Masters** page (top menu) to add machines, materials, thickness
and sheet names any time. Each item has **edit** (fixes a spelling mistake everywhere, old entries and sheet
codes included) and **remove** (takes it off the list; if old entries use it they keep it,
and admin can "bring back"). Admin can edit/remove anything; operators only what they added.

## Summary (for incentive)

Menu **Summary** shows Daily / Weekly (Mon-Sun) / Monthly totals per operator,
split by material and thickness (MS 16G, MS 14G, SS 2MM ...), with total sheets,
qty and cutting time, plus a day-wise table for the week or month.
Filter by unit, machine or material and download it as Excel (CSV).
Only finished sheets are counted. Operators see their own summary only.

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

To put it online for all units, follow **DEPLOY_ONLINE.md** (step by step, PythonAnywhere).
