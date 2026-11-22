#!/usr/bin/env bash
# Nightly backup: accounts DB (consistent sqlite .backup) + each user's settings and clip DB.
# Clip videos are not backed up (re-creatable and large). Keeps 14 days locally.
# Off-site copy (optional): set BACKUP_RCLONE_REMOTE (e.g. r2:virlo-backups) and configure rclone for the virlo user.
set -euo pipefail
DATA="${SAAS_DATA_DIR:-/var/lib/virlo-clips}"
OUT="$DATA/backups"
stamp=$(date -u +%Y%m%d-%H%M)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

sqlite3 "$DATA/saas.db" ".backup '$tmp/saas.db'"
for ws in "$DATA"/workspaces/u*; do
  [ -d "$ws" ] || continue
  u=$(basename "$ws")
  mkdir -p "$tmp/workspaces/$u/config" "$tmp/workspaces/$u/data/state"
  cp -r "$ws/config/." "$tmp/workspaces/$u/config/" 2>/dev/null || true
  [ -f "$ws/data/state/clipper.db" ] && sqlite3 "$ws/data/state/clipper.db" ".backup '$tmp/workspaces/$u/data/state/clipper.db'"
done
tar -C "$tmp" -czf "$OUT/virlo-clips-$stamp.tar.gz" .
find "$OUT" -name 'virlo-clips-*.tar.gz' -mtime +14 -delete
if [ -n "${BACKUP_RCLONE_REMOTE:-}" ] && command -v rclone >/dev/null; then
  rclone copy "$OUT/virlo-clips-$stamp.tar.gz" "$BACKUP_RCLONE_REMOTE" --quiet
fi
echo "backup ok: $OUT/virlo-clips-$stamp.tar.gz"
