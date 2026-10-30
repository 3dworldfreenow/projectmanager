# IoT PM — team project workspace (Flask)

For a software owner working with an IoT / hardware team on a weather station and other IoT projects.

## Features
### Project management
- **Board** — Kanban (Backlog → To do → In progress → Review → Done), drag-and-drop, filters by assignee / type / label, quick-add per column
- **Task page** — description, decision/outcome, checklist, subtasks, dependencies (blocked by / blocks), time logging vs estimate, labels, links to hardware / firmware / milestone, and a **dated log**: notes with @mentions, files, images, **voice and video notes recorded in the browser**
- **My tasks**, **Timeline** (Gantt with dependency arrows, overdue, milestones), **Calendar** (due dates, milestones, firmware, tests, calls, activity per day)
- **Push notifications (browser closed)** — Web Push (VAPID, no third-party service): incoming calls ring like a phone app, new messages / mentions / assignments arrive when you're not online. Enable per device on the Notifications page. Android: Chrome (installed or not). iPhone: add to Home Screen first (iOS 16.4+). Set `VAPID_EMAIL=you@example.com` in the service file (contact address required by push services).
- **Notifications** — in-app bell + toasts for assignments, mentions, comments on your tasks, due/overdue reminders; optional **email** (SMTP_HOST/PORT/USER/PASS/FROM) and **Telegram** (TELEGRAM_BOT_TOKEN) per user
- **Reports** — progress by status and member, completed this week, overdue, upcoming, hours logged, milestones; **PDF report** download
- Old "Research" items are migrated into tasks automatically (type = research) with their logs and files

### Mobile app (PWA)
- Installable: Chrome on Android shows an **Install** banner; on iPhone use Share → *Add to Home Screen*. Opens full-screen with its own icon, bottom tab bar (Home · Chat · Board · Calendar · Alerts), offline page.
- Chat loads the latest 50 messages and fetches older ones as you scroll up; search covers the full history on the server. Activity is paginated by day.

### Team & communication
- **First-run setup** — creates your admin account + first teammate; no passwords in code
- **Invite links (admin)** — create a link with a role (Guest, IoT Engineer, …), expiry and number of uses; share via WhatsApp / share sheet; the person picks their own username & password
- **Meeting rooms (group calls)** — start from Calls / dashboard / chat, or press ➕ during a 1:1 call. Members join by link directly; **anyone else (no account) opens the link, enters a name and waits until the host admits them** (Admit / Deny banner with sound). Guests can only be in that room — nothing else in the app. Mesh WebRTC (good for up to ~5 people), every participant's view is recorded to the server and listed under Calls.
- **In-call controls** — mute, camera off, switch front/rear camera, full screen (automatic on phones); recording stays on through all of it
- **Team members (admin)** — add people, set roles (Project Manager, IoT Engineer, Hardware Tester, custom…), reset passwords, disable, make admin
- **Team chat** — saved forever; in-chat search; reply/quote; pin important messages (pinned bar); @mentions (highlighted + sound); voice notes (hold 🎤); edit (previous versions visible to all); delete; unread badges
- **Calls inside chat** — voice 📞 / video 🎥 (WebRTC peer-to-peer), pick who to call, ringtone + vibration, missed-call list with badge; **server-side recording**: chunks stream to the server every 2 s during the call, so the recording survives a dropped phone or closed tab. Playback in *Call Recordings*.
- **Documents** — notes + file uploads (datasheets, schematics, specs)
- **Agreement** — versioned; every member can click "I accept"; **PDF export** with acceptances + timestamps
- **Activity feed** — everything that changed, with "since your last login" highlighted
- **Hardware list** — parts, qty, cost, supplier, status, part photo, datasheet link, low-stock alert (min qty), **BOM export to Excel**; every edit keeps the previous version
- **Hardware tests** — pass/fail log linked to parts and to the firmware version tested; attach photos / CSV logs / scope shots
- **Firmware versions** — version, target board, release notes, .bin/.zip upload, dev → testing → stable
- **Milestones & payments** — tied to the current agreement version; amounts, due dates, paid/ref tracking, totals; **invoice PDF** once delivered/paid
- Multiple projects; Telegram-style light UI; works on phones

See `TODO.md` for what is not built yet.

## Run
```
git clone https://github.com/3dworldfreenow/projectmanager
cd projectmanager
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000 — the first visit shows a **setup page** to create the admin and the first teammate.
Everyone can change their own password (sidebar → Change password); admins manage members at **Team members**.

### Install trouble?
If `pip`/`uv` fails building **pillow** (old server, old gcc), install the core first and skip PDF:
```
uv pip install --python .venv flask werkzeug openpyxl
```
The app runs fully; PDF export falls back to a print-to-PDF page. Then try `uv pip install --python .venv "reportlab<4.3" "pillow<11"` to enable real PDFs.

## Letting the team connect
Browsers only allow camera/mic on **https://** (or localhost), so for calls over the internet run the app
behind an HTTPS tunnel (`ngrok http 5000`, Cloudflare Tunnel) or a server with a certificate.
**Calls across mobile data / strict NAT need a TURN server.** Get free credentials at metered.ca (or run coturn), then add to the systemd unit:
```
Environment=TURN_URL=turn:a.relay.metered.ca:80,turn:a.relay.metered.ca:443 TURN_USER=xxxx TURN_PASS=yyyy
```
Optional notification channels (same place): `SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USER=you@gmail.com SMTP_PASS=app-password SMTP_FROM=you@gmail.com`
and `TELEGRAM_BOT_TOKEN=123:abc` (create a bot with @BotFather; each user puts their chat id on the Notifications page). Set `APP_URL=https://iot.opirsys.com` so links in emails/Telegram work.
and `systemctl daemon-reload && systemctl restart rainflow`. Incoming calls ring on every page of the app (banner + sound), with a browser notification if the tab is in the background.
For production, serve with `waitress-serve --port 5000 app:app` instead of the dev server.

## Backups
Everything lives in `iotpm.db` (SQLite) + `uploads/` (files, firmware, `recordings/`).

**Manual** — Team members page (admin) → *Backup & export*: download a backup zip (with or without call recordings), or export all data as CSV/JSON.

**Automatic (nightly, on the server)** — keeps 14 days in `backups/`:
```
chmod +x /opt/rainflow/projectmanager/backup.sh
(crontab -l 2>/dev/null; echo "30 2 * * * /opt/rainflow/projectmanager/backup.sh >> /var/log/rainflow-backup.log 2>&1") | crontab -
```

**Google Drive (off-server copy, recommended)** — connect once in the browser, then every backup is uploaded to a *Rainflow backups* folder in that Drive (newest 30 kept):
1. Create free OAuth credentials: https://console.cloud.google.com → *APIs & Services* → *Enable APIs* → enable **Google Drive API** → *Credentials* → *Create credentials* → *OAuth client ID* → type **Web application** → Authorised redirect URI: `https://iot.opirsys.com/admin/gdrive/callback` → copy the client ID and secret. (On the *OAuth consent screen* add your Google account as a test user.)
2. Add to the service file: `GOOGLE_CLIENT_ID=… GOOGLE_CLIENT_SECRET=… BACKUP_TOKEN=<random string>` and restart.
3. Team members → *Connect Google Drive* → sign in → done. *Back up to Drive now* uploads immediately; `backup.sh` uploads nightly (it uses `BACKUP_TOKEN` — put `BACKUP_TOKEN=…` and `PORT=5020` in root's crontab environment or export them at the top of backup.sh).

**Restore** — stop the service, unzip the backup into the app folder (replacing `iotpm.db` and `uploads/`), start the service.
