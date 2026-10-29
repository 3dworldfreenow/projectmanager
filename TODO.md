# TODO — not yet built

## Must do before using over the internet
- [ ] **HTTPS** — camera/mic only work on https:// (or localhost). Use `ngrok http 5000`, Cloudflare Tunnel, or a server with a Let's Encrypt cert.
- [ ] **TURN server** — calls across mobile data / strict NAT need TURN. Free tier at metered.ca or self-host coturn, then add to `iceServers` in `templates/chat.html`.
- [ ] **Production server** — run `pip install waitress` then `waitress-serve --port 5000 app:app` instead of `python app.py`.
- [ ] **Login hardening** — rate-limit login attempts, lock account after N failures, session timeout.
- [ ] **Backups** — "Download backup" button (zip of `iotpm.db` + `uploads/`) and a daily cron copy.
- [ ] **Per-project access** — currently every member sees every project; add project membership so a client only sees their project.

## Calls
- [ ] 3+ person group calls (needs an SFU / media server such as LiveKit or Janus — large job).
- [ ] Browser push notifications + ringtone when the chat tab is **not** open (needs a service worker + VAPID keys).
- [ ] Screen sharing during a call (getDisplayMedia — small job).
- [ ] Server-side merge of both recordings into one file (ffmpeg).

## Chat
- [ ] Message reactions (👍 ✅), forward message, message search across all projects.
- [ ] Image paste from clipboard, drag-and-drop upload.
- [ ] Read receipts per member (who has seen a message).

## Project pages
- [ ] Hardware: barcode/QR label per part, order tracking (order date → ETA → received).
- [ ] Tests: test templates / checklists, charts of results over firmware versions.
- [ ] Firmware: diff of release notes between versions, OTA update endpoint for the ESP32.
- [ ] Milestones: invoice numbering per payee, GST field, payment reminders.
- [ ] Research: links list, "decision made" field, Kanban drag-and-drop.
- [ ] Gantt / timeline view of milestones and research tasks.

## Weather station specific
- [ ] Live sensor data page (ESP32 → MQTT/HTTP → SQLite → chart).
- [ ] Device status (last seen, battery, RSSI) and alerts.

## Nice to have
- [ ] Weekly email digest of activity.
- [ ] Dark mode toggle.
- [ ] Export whole project (chat + docs + BOM + tests) as a PDF report.
