# Roadmap / TODO

What exists is described in README.md. This is what is **not** built yet, roughly by priority.

## Must do before using over the internet
- [ ] **HTTPS** — camera/mic only work on https:// (or localhost). Use `ngrok http 5000`, Cloudflare Tunnel, or a server with a Let's Encrypt cert.
- [ ] **TURN server** — calls across mobile data / strict NAT need TURN. Free tier at metered.ca or self-host coturn, then add to `iceServers` in `templates/chat.html`.
- [ ] **Production server** — run `pip install waitress` then `waitress-serve --port 5000 app:app` instead of `python app.py`.
- [ ] **Login hardening** — rate-limit login attempts, lock account after N failures, session timeout.
- [ ] **Backups** — "Download backup" button (zip of `iotpm.db` + `uploads/`) and a daily cron copy.
- [ ] **Per-project access** — currently every member sees every project; add project membership so a client only sees their project.

## Calls
- [x] Group calls (mesh, up to ~5). For 6+ people an SFU (LiveKit/Janus) would be needed.
- [ ] Meeting rooms are kept in memory — a server restart ends active rooms (fine for now).
- [ ] Browser push notifications + ringtone when the chat tab is **not** open (needs a service worker + VAPID keys).
- [ ] Screen sharing during a call (getDisplayMedia — small job).
- [ ] Server-side merge of both recordings into one file (ffmpeg).

## Chat
- [ ] Message reactions (👍 ✅), forward message, message search across all projects.
- [ ] Image paste from clipboard, drag-and-drop upload.
- [ ] Read receipts per member (who has seen a message).

## Project management (next)
- [ ] Drag-and-drop on touch screens (currently long-press → prompt on mobile).
- [ ] Gantt: drag bars to change dates; critical path.
- [ ] Recurring tasks, task templates (e.g. "new sensor bring-up" checklist).
- [ ] Per-project membership / client (read-only) role.
- [ ] Weekly digest email (cron: `curl $APP_URL/reports/pdf`).

## Project pages
- [ ] Hardware: barcode/QR label per part, order tracking (order date → ETA → received).
- [ ] Tests: test templates / checklists, charts of results over firmware versions.
- [ ] Firmware: diff of release notes between versions, OTA update endpoint for the ESP32.
- [ ] Milestones: invoice numbering per payee, GST field, payment reminders.

## Devices / live data
- [x] Live sensor data (HTTP + MQTT), charts, alerts, commands, API.
- [ ] Per-field units and display names (e.g. show "Temperature °C" instead of `temp`).
- [ ] Downsampling / retention policy for old readings (keep raw 30 days, hourly averages after).
- [ ] OTA firmware update endpoint for ESP32 (serve the "stable" binary from the Firmware page).
- [ ] Device offline alert (no data for N minutes).

## Nice to have
- [ ] Weekly email digest of activity.
- [ ] Dark mode toggle.
- [ ] Export whole project (chat + docs + BOM + tests) as a PDF report.
