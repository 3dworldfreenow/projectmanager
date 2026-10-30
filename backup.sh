#!/bin/bash
# Rainflow nightly backup: consistent SQLite snapshot + uploads (incl. recordings).
# Keeps 14 days locally; optionally copies to cloud with rclone.
#
# Install (run once):
#   chmod +x /opt/rainflow/projectmanager/backup.sh
#   (crontab -l 2>/dev/null; echo "30 2 * * * /opt/rainflow/projectmanager/backup.sh >> /var/log/rainflow-backup.log 2>&1") | crontab -
set -e
APP="$(cd "$(dirname "$0")" && pwd)"
OUT="$APP/backups"; mkdir -p "$OUT"
STAMP=$(date +%Y%m%d-%H%M)
TMP=$(mktemp -d)

# consistent copy of the database while the app keeps running
"$APP/.venv/bin/python" -c "import sqlite3; s=sqlite3.connect('$APP/iotpm.db'); d=sqlite3.connect('$TMP/iotpm.db'); s.backup(d); d.close(); s.close()"

tar -czf "$OUT/rainflow-$STAMP.tar.gz" -C "$TMP" iotpm.db -C "$APP" uploads
rm -rf "$TMP"

# rotate: keep 14 days
find "$OUT" -name "rainflow-*.tar.gz" -mtime +14 -delete
echo "$(date) backup ok: $OUT/rainflow-$STAMP.tar.gz ($(du -h "$OUT/rainflow-$STAMP.tar.gz" | cut -f1))"

# Google Drive (connected in the app): ask the running app to upload a fresh backup
if [ -n "$BACKUP_TOKEN" ]; then
  curl -s -X POST -H "X-Backup-Token: $BACKUP_TOKEN" "http://127.0.0.1:${PORT:-5020}/admin/gdrive/backup?recordings=1" && echo
fi

# optional rclone copy instead. Set up once with:  rclone config   (create a Google Drive remote named "gdrive")
if command -v rclone >/dev/null 2>&1 && rclone listremotes 2>/dev/null | grep -q "^gdrive:"; then
  rclone copy "$OUT/rainflow-$STAMP.tar.gz" gdrive:rainflow-backups/ && echo "copied to Google Drive"
  rclone delete --min-age 60d gdrive:rainflow-backups/ 2>/dev/null || true
fi
