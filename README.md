# Rainflow

**A self-hosted workspace for small IoT / hardware teams.** Chat, calls, tasks, hardware tracking, firmware, agreements, payments — and live sensor data from your ESP32 devices — in one app you run on your own server. Built for a software owner working with an IoT engineer on a weather station; works for any small hardware project.

- Single Python/Flask app, SQLite database, no external services required
- Works as an installable mobile app (PWA) with push notifications
- Voice/video calls and group meetings with guests, recorded on your server
- Live charts from devices over HTTP or MQTT, with an API for your own tools

Repo: https://github.com/3dworldfreenow/projectmanager · Status: in active use, under development (see `TODO.md`)

---

## What's inside

| Area | What you get |
|---|---|
| **Overview** | Project pulse: tests passing, milestones, payments, tasks done, low-stock parts, device status, recent chat/activity |
| **Board / Tasks** | Kanban (Backlog → To do → In progress → Review → Done), drag-and-drop, filters. Each task has a dated log (notes, @mentions, files, images, **voice & video notes recorded in the browser**), checklist, subtasks, dependencies, time tracking, labels, links to hardware / firmware / milestone, decision box |
| **Timeline · Calendar · My tasks · Reports** | Gantt with dependency arrows, month calendar of everything dated, personal task list, progress report with PDF |
| **Chat** | Team chat with reply, pin, @mentions, voice notes, edit history, delete, search, file/image sharing, unread badges |
| **Calls & meetings** | 1:1 voice/video from chat; **meeting rooms** where anyone can join by link (no account) after the host admits them; mute / camera off / front-rear switch / full screen; **every call recorded to the server** |
| **Notifications** | In-app bell + toasts; **push to phone with the browser closed** (calls ring, messages, mentions, assignments, due reminders, sensor alerts); optional email / Telegram |
| **Hardware** | Parts list with cost, supplier, photo, datasheet, low-stock alerts, version history, BOM export to Excel |
| **Hardware tests** | Pass/fail log linked to parts and firmware versions, attachments |
| **Firmware** | Versions with release notes and binaries, dev → testing → stable |
| **Documents · Agreement · Milestones** | File store; versioned agreement each member accepts (PDF export with timestamps); milestones with amounts, due dates, paid status and invoice PDF |
| **Live data** | Devices with API keys, values and charts per sensor field, threshold alerts, commands to devices, CSV export |
| **API & ESP32 docs** | In-app docs with copy-paste ESP32 sketches (HTTP and MQTT), curl, Python |
| **Team members** | Roles, invite links, backups & export, Google Drive backup |

## How it fits together

```
 ESP32 / sensors ──HTTP POST or MQTT──▶ ┌─────────────────────────┐
                                        │  Rainflow (Flask)       │──▶ Web Push (FCM/APNs) ──▶ phones
 Browser / PWA  ◀──HTTPS (Apache/Nginx)─▶│  app.py  pm.py  iot.py  │
       │                                 │  SQLite  +  uploads/     │──▶ Google Drive backups
       └── WebRTC (peer-to-peer) ──▶ coturn (TURN relay for mobile data)
```

| File | Role |
|---|---|
| `app.py` | Core: auth, users/invites, chat, calls & meeting rooms, documents, agreement, hardware, tests, firmware, milestones, activity, backups, PDF/Excel export |
| `pm.py` | Project management: tasks/board, timeline, calendar, notifications, push subscriptions, reports |
| `iot.py` | Devices, data ingestion (HTTP + MQTT), commands, alerts, read API, live-data page |
| `webpush.py` | Web Push (RFC 8291 / VAPID) implemented on `cryptography` — no third-party push service |
| `templates/` | Jinja pages. `base.html` holds the theme, navigation, PWA shell and site-wide call/notification scripts |
| `static/` | PWA manifest, icons, service worker (`sw.js`) |
| `backup.sh` | Nightly backup script (cron) |
| `iotpm.db`, `uploads/` | **Your data.** Not in git. Back these up |

Everything is server-rendered HTML with small vanilla-JS enhancements (polling, WebRTC, charts). No build step, no Node, no frontend framework.

## Run it locally

```bash
git clone https://github.com/3dworldfreenow/projectmanager
cd projectmanager
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000 — the first visit shows a **setup page** to create the admin and the first teammate. Nothing else to configure for local use (calls work on localhost).

> Old server / old gcc and `pillow` fails to build? Install the core first: `pip install flask werkzeug openpyxl cryptography` — PDFs fall back to print-to-PDF pages.

## Deploy on a server (how the reference install runs)

The production instance runs on a CentOS VPS behind Apache with Let's Encrypt, as a systemd service.

1. **App** in `/opt/rainflow/projectmanager`, venv in `.venv`, service `rainflow` on `127.0.0.1:5020`:
   ```ini
   # /etc/systemd/system/rainflow.service
   [Service]
   WorkingDirectory=/opt/rainflow/projectmanager
   Environment=HOST=127.0.0.1 PORT=5020 SECRET_KEY=<long random> APP_URL=https://your.domain
   ExecStart=/opt/rainflow/projectmanager/.venv/bin/python app.py
   Restart=always
   ```
2. **Reverse proxy with HTTPS** (required: browsers only allow camera/mic/push on https). Apache: `ProxyPass / http://127.0.0.1:5020/`, `LimitRequestBody 524288000`, then `certbot --apache`.
3. **TURN server** so calls work over mobile data: `coturn` on the same VPS, then `TURN_URL=turn:your.domain:3478 TURN_USER=… TURN_PASS=…` in the service file.
4. **Update**: `git pull && systemctl restart rainflow` (don't restart during a meeting — rooms are in memory).

### All settings (environment variables in the service file)

| Variable | Purpose |
|---|---|
| `HOST`, `PORT`, `SECRET_KEY`, `APP_URL` | Bind address, port, session secret, public URL used in links |
| `TURN_URL`, `TURN_USER`, `TURN_PASS` | TURN relay for calls across NAT / mobile data |
| `VAPID_EMAIL` | Contact address sent with push messages (any valid email) |
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`, `MQTT_TLS` | MQTT broker for device data (`pip install paho-mqtt`) |
| `API_READ_TOKEN` | Lets external tools read device data (`Authorization: Bearer …`) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` | Email notifications (per-user opt-in) |
| `TELEGRAM_BOT_TOKEN` | Telegram notifications (per-user opt-in with chat id) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `BACKUP_TOKEN` | Google Drive backups (connect once in the browser) |

## Devices & API (ESP32)

1. Live data → *Add a device* → copy its key (`rf_…`).
2. Send readings — any JSON fields become charts:
   ```bash
   curl -X POST https://your.domain/api/v1/data -H "X-Device-Key: rf_xxx" \
        -H "Content-Type: application/json" -d '{"temp":24.3,"hum":58,"rain_mm":0.2}'
   ```
   or publish the same JSON to MQTT topic `rainflow/<key>/data`.
3. Commands to the device: `POST /api/v1/command {"device":1,"cmd":"relay","value":1}` → MQTT `rainflow/<key>/cmd`, or returned in the next HTTP response.
4. Read data: `/api/v1/devices`, `/api/v1/latest`, `/api/v1/readings?device=1&field=temp&from=…&format=csv`.

Full reference with complete ESP32 sketches (HTTP deep-sleep node, MQTT two-way node) and the Mosquitto setup commands: **API & ESP32 docs** page inside the app (`/api/docs`).

## Backups

Your data is `iotpm.db` + `uploads/`.
- **Manual**: Team members → *Backup & export* (zip with/without recordings; CSV/JSON export).
- **Nightly**: `chmod +x backup.sh` and add to cron: `30 2 * * * /opt/rainflow/projectmanager/backup.sh >> /var/log/rainflow-backup.log 2>&1` (keeps 14 days in `backups/`).
- **Google Drive**: create an OAuth client (Google Cloud Console → Drive API → OAuth client ID, web application, redirect URI `https://your.domain/admin/gdrive/callback`), set `GOOGLE_CLIENT_ID/SECRET` + `BACKUP_TOKEN`, then *Connect Google Drive* in the app. Backups land in a *Rainflow backups* folder (newest 30 kept).
- **Restore**: stop the service, unzip the backup over `iotpm.db` and `uploads/`, start the service.

## Limits to know

- Group calls are mesh WebRTC — fine up to ~5 participants; more needs an SFU (LiveKit/Janus).
- Meeting rooms and call signaling live in memory: a service restart ends active calls.
- Push notifications on iPhone require the app to be added to the Home Screen (iOS 16.4+).
- SQLite is plenty for a team; move to Postgres only if you reach hundreds of users or millions of readings.

## Contributing / extending

- Add a page: route in the matching blueprint (`app.py` core, `pm.py` tasks, `iot.py` devices), a template extending `base.html`, and a nav entry in the `groups` list in `base.html`.
- Database changes: add `CREATE TABLE IF NOT EXISTS` to the module's `SCHEMA`, and an `ALTER TABLE` line in `init_db()`'s migration list for existing installs.
- Keep pages dependency-free (no CDN requirement) so the app works on a LAN without internet.
- Test on a phone width — every page is checked at 390 px.

Roadmap and known gaps: `TODO.md`.

## License

MIT — use it, change it, run it for your own team.
