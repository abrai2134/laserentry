# Put Laser Entry online

Two options. Pick ONE:

- **Option A - PythonAnywhere** (free plan works). Steps right below.
- **Option B - Render** (needs Starter plan, about USD 7-8/month). Steps at the end of this file.

---

# Option A - PythonAnywhere

After this, every unit opens the same address, for example
**https://lasermalbar.pythonanywhere.com**, from any phone or PC with internet.
Your office PC does not need to stay on.

Time needed: about 15 minutes. Cost: free plan is enough to start.

---

## 1. Create the account

1. Go to **https://www.pythonanywhere.com** and click **Pricing & signup**.
2. Click **Create a Beginner account** (free).
3. The **username** you choose becomes your web address:
   `https://USERNAME.pythonanywhere.com`. Pick something short, e.g. `lasermalbar`.
4. Confirm your email.

## 2. Download the code onto the server

1. In PythonAnywhere, open the **Consoles** tab and click **Bash**.
2. A black console opens. Paste these lines one by one and press Enter after each:

```bash
git clone -b claude/confident-bohr-ju830a https://github.com/abrai2134/laserentry.git
```

```bash
python3 -m pip install --user -r laserentry/requirements.txt
```

## 3. Create the website

1. Open the **Web** tab and click **Add a new web app** -> **Next**.
2. Choose **Manual configuration** (NOT "Flask").
3. Choose the **newest Python version** in the list -> **Next**.
4. On the Web page that opens, scroll to **Code**:
   - **Source code:** `/home/USERNAME/laserentry`
   - **Working directory:** `/home/USERNAME/laserentry`
5. Click the link next to **WSGI configuration file**. Delete everything in that file
   and paste these 5 lines (replace USERNAME with your username), then click **Save**:

```python
import os, sys
os.environ["LASER_HTTPS"] = "1"
os.environ["LASER_TZ"] = "Asia/Kolkata"
sys.path.insert(0, "/home/USERNAME/laserentry")
from app import app as application
```

6. Back on the Web tab, under **Static files**, add:
   - URL: `/static/`   Directory: `/home/USERNAME/laserentry/static`
7. Under **Security**, switch **Force HTTPS** ON.
8. Click the big green **Reload** button at the top.

## 4. Open it

Go to **https://USERNAME.pythonanywhere.com**.

- Starting fresh: log in with `admin` / `admin123` and **change the password at once**
  (Password menu). Then add units, machines and operators in **Setup**.
- To keep the data you already entered on your office PC, do step 5 first.

## 5. (Optional) Move your existing data from the office PC

Easiest: on the office PC open Laser Entry -> **Setup** -> **Download backup**.
Then on the online site -> **Setup** -> **Restore from a backup file** -> choose that file.

Or by hand:

1. On the office PC, close the black Laser Entry window.
2. In PythonAnywhere open the **Files** tab -> go into the `laserentry` folder.
3. Click **Upload a file** and choose `laserentry.db` from your PC's Laser Entry folder.
4. Go to the **Web** tab and click **Reload**.

All your operators, machines and entries are now online, with the same passwords.
From now on use only the online address (do not keep entering on the office PC copy).

---

## Updating to a new version later

**Consoles** -> **Bash**, then:

```bash
cd ~/laserentry && git pull
```

Then **Web** tab -> **Reload**. Your data is not touched.

## Backup (do this every week)

In Laser Entry: **Setup** -> **Download backup**. (Or PythonAnywhere **Files** tab ->
`laserentry` folder -> download `laserentry.db`.)
Keep these files safe; this one file holds all your data.

## Free plan notes

- Every 3 months PythonAnywhere emails you; open the **Web** tab and click
  **"Run until 3 months from today"**, or the site is switched off.
- For your own address (e.g. `laser.yourcompany.com`) and no 3-month renewal,
  upgrade to the paid "Hacker" plan (about USD 5 per month).

## Security

- Change the `admin` password on day one. The system warns you while it is still `admin123`.
- Give every operator their own login. Disable a login in **Setup** when someone leaves.
- After 8 wrong passwords a username is blocked for 15 minutes (admin can reset it in Setup).

---

# Option B - Render

**Important:** Render's free plan has no permanent disk. The server restarts often and
all data would be lost. Use the **Starter** plan with a 1 GB disk (the setup file below
does this for you; about USD 7 + 0.25 per month).

1. Log in to **https://dashboard.render.com**.
2. Click **New +** -> **Blueprint**.
3. Connect GitHub if asked, and select the repository **abrai2134/laserentry**.
4. Choose branch **claude/confident-bohr-ju830a** (the file `render.yaml` there has all settings).
5. Render shows: web service `laserentry`, Starter plan, Singapore region, disk `laser-data`.
   Click **Apply** / **Deploy Blueprint**.
6. Wait about 3-5 minutes until it says **Live**. Your address is shown at the top, like
   `https://laserentry-xxxx.onrender.com`.
7. Open it and log in with `admin` / `admin123`, then **change the password** at once.
8. To bring your office PC data: on the office PC **Setup** -> **Download backup**, then on
   the Render site **Setup** -> **Restore from a backup file**.

**Updates:** Render redeploys automatically whenever new code is pushed to that branch.
Your data on the disk is kept.

**Own address** (e.g. `laser.yourcompany.com`): Render service -> **Settings** ->
**Custom Domains**.

**Backup:** Setup -> **Download backup**, every week.
