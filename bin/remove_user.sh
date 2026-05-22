#!/usr/bin/env bash
# Remove a user's isolated instance.
#
# Stops the service, removes systemd + nginx config, archives data to a
# timestamped tar.gz, then deletes the live data dir. Recoverable from the
# archive if you ever need it back.
#
# Run on the VM as: sudo bash /opt/kite/bin/remove_user.sh <slug>

set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

if [ $# -lt 1 ]; then
  echo "Usage: $0 <slug>" >&2
  exit 1
fi

SLUG="$1"
if [ "${SLUG}" = "omkar" ]; then
  echo "Refusing to remove the primary 'omkar' instance via this script." >&2
  echo "If you really mean to, do it manually." >&2
  exit 1
fi

DATA_DIR="/opt/kite-data-${SLUG}"
SYSTEMD_UNIT="/etc/systemd/system/kite-monitor-${SLUG}.service"
NGINX_CONF="/etc/nginx/sites-enabled/kite-${SLUG}"
HTPASSWD="/etc/nginx/.htpasswd-${SLUG}"
LOG="/var/log/kite-monitor-${SLUG}.log"

if [ ! -d "${DATA_DIR}" ] && [ ! -f "${SYSTEMD_UNIT}" ]; then
  echo "No traces of user '${SLUG}' found. Nothing to do." >&2
  exit 0
fi

echo "==> Removing user '${SLUG}'"
read -rp "Type the slug again to confirm: " confirm
if [ "${confirm}" != "${SLUG}" ]; then
  echo "Aborted." >&2
  exit 1
fi

# Stop + disable the service
if [ -f "${SYSTEMD_UNIT}" ]; then
  systemctl stop "kite-monitor-${SLUG}" || true
  systemctl disable "kite-monitor-${SLUG}" || true
  rm -f "${SYSTEMD_UNIT}"
  systemctl daemon-reload
fi

# nginx
if [ -f "${NGINX_CONF}" ]; then
  rm -f "${NGINX_CONF}"
  nginx -t && systemctl reload nginx
fi
rm -f "${HTPASSWD}"

# Archive data dir before deletion (recoverable later)
if [ -d "${DATA_DIR}" ]; then
  TS=$(date +%Y%m%d-%H%M%S)
  ARCHIVE="/opt/kite-archive-${SLUG}-${TS}.tar.gz"
  tar -czf "${ARCHIVE}" -C /opt "kite-data-${SLUG}"
  chmod 600 "${ARCHIVE}"
  rm -rf "${DATA_DIR}"
  echo "==> Data archived: ${ARCHIVE}"
fi

# Log file
if [ -f "${LOG}" ]; then
  mv "${LOG}" "${LOG}.removed-$(date +%Y%m%d-%H%M%S)"
fi

echo "==> Done. User '${SLUG}' removed."
echo "    AWS security group: don't forget to close the port you opened for them."
