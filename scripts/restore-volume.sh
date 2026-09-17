#!/bin/bash
#
# Restores one docker volume of the ops stack from the newest restic snapshot.
# Run on the server, as root:
#
#   sudo bash restore-volume.sh ops_n8n_data
#
# The stack is defined in Ansible, so a lost server is rebuilt with terraform
# apply + run.ps1 site.yml first; this script brings back the data those
# playbooks can't recreate. It stops the container, replaces the volume
# contents and starts it again.
set -euo pipefail

VOLUME=${1:-}
case "$VOLUME" in
  ops_n8n_data)         SERVICE=n8n;         DB=n8n/database.sqlite;      LIVE_DB=database.sqlite ;;
  ops_uptime_kuma_data) SERVICE=uptime-kuma; DB=uptime-kuma/kuma.db;      LIVE_DB=kuma.db ;;
  ops_caddy_data)       SERVICE=caddy;       DB=;                         LIVE_DB= ;;
  *) echo "usage: $0 ops_n8n_data|ops_uptime_kuma_data|ops_caddy_data" >&2; exit 2 ;;
esac

STACK=/opt/ops
VOLUME_PATH=/var/lib/docker/volumes/$VOLUME/_data
STAGE=$(mktemp -d /var/tmp/restore.XXXXXX)
trap 'rm -rf "$STAGE"' EXIT

echo "==> restoring $VOLUME from the newest snapshot"
ops-restic snapshots --latest 1
ops-restic restore latest --tag nightly --target "$STAGE"

SOURCE=$STAGE$VOLUME_PATH
[ -d "$SOURCE" ] || { echo "snapshot has no $VOLUME_PATH"; exit 1; }

# The live SQLite files are excluded from the backup; the consistent copies
# taken with .backup are restored in their place.
if [ -n "$DB" ]; then
  cp "$STAGE/var/backups/ops-sqlite/$DB" "$SOURCE/$LIVE_DB"
  rm -f "$SOURCE/$LIVE_DB-wal" "$SOURCE/$LIVE_DB-shm"
fi

echo "==> stopping $SERVICE"
docker compose --project-directory "$STACK" stop "$SERVICE"

# Let compose recreate a deleted volume, so it carries compose's own labels;
# `docker volume create` would leave it unlabelled and compose warns about it.
# With the volume still present this only creates the container, and the
# existing contents are cleared below.
docker compose --project-directory "$STACK" up -d --no-start "$SERVICE"
find "$VOLUME_PATH" -mindepth 1 -delete
cp -a "$SOURCE/." "$VOLUME_PATH/"

# Both images run as uid 1000 inside the container.
[ "$VOLUME" = ops_caddy_data ] || chown -R 1000:1000 "$VOLUME_PATH"

echo "==> starting $SERVICE"
docker compose --project-directory "$STACK" up -d "$SERVICE"

echo "==> done; check with:"
echo "    docker compose --project-directory $STACK logs -f $SERVICE"
