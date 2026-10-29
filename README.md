# IoT PM — team project workspace (Flask)

For a software owner working with an IoT / hardware team on a weather station and other IoT projects.

## Features
- **First-run setup** — creates your admin account + first teammate; no passwords in code
- **Team members (admin)** — add people, set roles (Project Manager, IoT Engineer, Hardware Tester, custom…), reset passwords, disable, make admin
- **Team chat** — saved forever; in-chat search; reply/quote; pin important messages (pinned bar); @mentions (highlighted + sound); voice notes (hold 🎤); edit (previous versions visible to all); delete; unread badges
- **Calls inside chat** — voice 📞 / video 🎥 (WebRTC peer-to-peer), pick who to call, ringtone + vibration, missed-call list with badge; **server-side recording**: chunks stream to the server every 2 s during the call, so the recording survives a dropped phone or closed tab. Playback in *Call Recordings*.
- **Documents** — notes + file uploads (datasheets, schematics, specs)
- **Agreement** — versioned; every member can click "I accept"; **PDF export** with acceptances + timestamps
- **Activity feed** — everything that changed, with "since your last login" highlighted
- **Hardware list** — parts, qty, cost, supplier, status, part photo, datasheet link, low-stock alert (min qty), **BOM export to Excel**; every edit keeps the previous version
- **Hardware tests** — pass/fail log linked to parts and to the firmware version tested; attach photos / CSV logs / scope shots
- **Research / tasks** — todo → doing → done, priority, owner, due, findings + attachments
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
If calls don't connect across strict NATs / mobile data, add a TURN server to `iceServers` in `templates/chat.html`.
For production, serve with `waitress-serve --port 5000 app:app` instead of the dev server.

## Data
Everything is in `iotpm.db` (SQLite) + the `uploads/` folder (files, firmware, `recordings/`) — back up both.
