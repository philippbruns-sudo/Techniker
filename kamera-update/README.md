# Camera Update (ARVOO Ruby)

Updates ARVOO cameras on the network to the latest firmware via their API and logs
when each camera received which update.

```
[Browser] ── http://localhost:8080 ──> [server.py] ── HTTP :577 ──> [Cameras]
                                          │
                                          └── data/update_log.csv (log)
```

Why a small server instead of a plain HTML page? Browsers block requests from a web
page directly to the cameras (CORS), especially with a password. `server.py` handles
the communication with the cameras and writes the log.

## Getting started

Only Python 3 is required (Windows, Linux, Raspberry Pi) – no additional packages.

```bash
python3 server.py                  # http://localhost:8080
python3 server.py --demo           # try it out with simulated cameras
python3 server.py --host 0.0.0.0   # also reachable from other devices on the network
python3 server.py --port 8090      # different port
```

On Windows you can simply double-click **`start.bat`** (normal mode) or
**`start-demo.bat`** (demo). The browser opens automatically; close the black window
to stop. Python 3 must be installed (python.org – tick “Add Python to PATH” during setup).

On first start, enter the camera password under **⚙ Settings**.

The interface can be switched between **German and English** with the **DE | EN**
button in the top right corner. The choice is remembered in the browser.

## Usage

1. **＋ Add IPs** – one camera per line, as a link or IP, optionally with a name.
   Without a port, 577 is used; the same IP with a different port is a separate camera.
   Duplicates are merged. New cameras are checked automatically afterwards.
   ```
   http://10.32.75.8:577/
   http://10.32.75.8:566/
   10.32.47.135
   10.32.47.135 Car park North
   10.32.47.135; Car park North; password    (camera-specific password)
   10.32.47.135; Car park North;             (back to the default password)
   ```
2. **🔍 Check all** – reads the version and serial number and asks the ARVOO server
   whether an update is available. Does not change anything on the cameras.
3. Update – a single camera with **⬆** in its row, several via the checkboxes and
   **⬆ Update selected**, **⬆ Update all with update** or **⬆ Update all**.
   Cameras that are already up to date are only logged as “up to date”.
   At most 3 updates run at the same time (configurable).
   Clicking an IP opens the camera’s web interface.
4. The **Update log** at the bottom lists every update with time, version before/after
   and result. **⬇ Download CSV** opens it in Excel.

## How an update works

| Step | Camera API |
|---|---|
| Read version and serial number | `GET /system/info` |
| Ask the ARVOO server for an update | `POST /system/actions/check_update` |
| Start the update | `POST /system/actions/perform_update` with `{"version": "<new version>"}` |
| Track progress | `GET /system/info/update_progress` |
| Wait for the reboot, confirm the new version | `GET /system/info` |

The tool runs **two rounds**: ask for an update, and if one is offered, install it.
Version numbers are ignored – whatever ARVOO offers is installed. After the second round
the tool stops checking.

An update is considered finished once the camera reports “finished” and is reachable again;
otherwise it is aborted after 30 minutes.

The cameras need **internet access** to the ARVOO server, because they download the
firmware themselves.

## Settings

Saved in `config.json` next to `server.py` (created on first save). Confirmed working:

| Setting | Value |
|---|---|
| User | `user` |
| Port | `577` |
| API path | `/api/v2` |
| Authentication | Basic |
| Parallel updates | `3` (takes effect after restarting `server.py`) |
| Format perform_update | JSON `{"version": "4.12.0"}` |

Other formats for `perform_update` and “Detect automatically” are still available in
the settings in case a future firmware expects something different.

## Log

`data/update_log.csv` (semicolon-separated, opens directly in Excel):

| time | ip | name | serial | version_before | version_after | result | message |
|---|---|---|---|---|---|---|---|
| 2026-10-27 09:10:00 | 10.32.47.124:577 | Car park North | RB-24-00817 | 4.101.0 | 4.103.2 | success | Format: json |

`result` is `success`, `up to date` or `failed` (with the reason in `message`).

A log from an earlier German version is converted automatically on start;
the original is kept as `data/update_log_backup_de.csv`.

## Files

| File | Content | In repo |
|---|---|---|
| `server.py` | Server and camera logic | ✅ |
| `index.html` | User interface (German / English) | ✅ |
| `config.example.json` | Example settings | ✅ |
| `start.bat`, `start-demo.bat` | Double-click start on Windows | ✅ |
| `config.json` | Settings including the password | ❌ (`.gitignore`) |
| `data/` | Camera list and log | ❌ (`.gitignore`) |
| `data-demo/` | Demo mode data | ❌ (`.gitignore`) |
