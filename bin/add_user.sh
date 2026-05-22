#!/usr/bin/env bash
# Provision a new isolated user instance of the kite dashboard.
#
# Run on the VM as: sudo bash /opt/kite/bin/add_user.sh <slug>
#
# What it does (idempotent — safe to re-run):
#   1. /opt/kite-data-<slug>/             — per-user data dir (.env, CSVs, etc.)
#   2. /etc/systemd/system/kite-monitor-<slug>.service
#   3. /etc/nginx/sites-enabled/kite-<slug>
#   4. /etc/nginx/.htpasswd-<slug>        — basic-auth credentials
#   5. systemd reload + start
#   6. nginx reload
#
# Each user gets their own port pair:
#   - PUBLIC_PORT (nginx listens here, exposed to the internet)
#   - FLASK_PORT  (Flask listens here, localhost only)
# Ports are auto-picked starting at 5002 / 8001 (first user keeps 80 / 5001).

set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

if [ $# -lt 1 ]; then
  cat >&2 <<EOF
Usage: $0 <slug>

  <slug>   lowercase alphanumeric (a-z, 0-9, -), e.g. priya or charlie-test

Example:
  sudo bash /opt/kite/bin/add_user.sh priya
EOF
  exit 1
fi

SLUG="$1"
if ! [[ "${SLUG}" =~ ^[a-z0-9-]+$ ]]; then
  echo "Slug must be lowercase a-z, 0-9, dash only." >&2
  exit 1
fi
if [ "${SLUG}" = "omkar" ]; then
  echo "Slug 'omkar' is reserved for the primary instance — already set up." >&2
  exit 1
fi

# ── Paths
CODE_DIR=/opt/kite
DATA_DIR="/opt/kite-data-${SLUG}"
TMPL_DIR="${CODE_DIR}/deploy/templates"
SYSTEMD_UNIT="/etc/systemd/system/kite-monitor-${SLUG}.service"
NGINX_CONF="/etc/nginx/sites-enabled/kite-${SLUG}"
HTPASSWD="/etc/nginx/.htpasswd-${SLUG}"

# ── Auto-pick ports
# Existing user 1: PUBLIC=80, FLASK=5001 (untouched)
# Subsequent users get 8001/5002, 8002/5003, ...
pick_port() {
  local start=$1
  local p=${start}
  while ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE ":${p}\$" \
     || grep -qE "listen\s+${p}\b" /etc/nginx/sites-enabled/* 2>/dev/null \
     || grep -qE "PORT=${p}\b" /etc/systemd/system/kite-monitor-*.service 2>/dev/null; do
    p=$((p + 1))
  done
  echo "${p}"
}
PUBLIC_PORT=$(pick_port 8001)
FLASK_PORT=$(pick_port 5002)

echo "==> Provisioning user '${SLUG}'"
echo "    DATA_DIR     : ${DATA_DIR}"
echo "    PUBLIC_PORT  : ${PUBLIC_PORT}  (nginx, internet-facing)"
echo "    FLASK_PORT   : ${FLASK_PORT}  (Flask, localhost-only)"
echo

# ── 1) Per-user data dir + .env
mkdir -p "${DATA_DIR}/data/csv"
chown -R ubuntu:ubuntu "${DATA_DIR}"

if [ ! -f "${DATA_DIR}/.env" ]; then
  sed \
    -e "s|%%USER_SLUG%%|${SLUG}|g" \
    -e "s|%%DATA_DIR%%|${DATA_DIR}|g" \
    "${TMPL_DIR}/env.tmpl" > "${DATA_DIR}/.env"
  chown ubuntu:ubuntu "${DATA_DIR}/.env"
  chmod 600 "${DATA_DIR}/.env"
  echo "==> Created ${DATA_DIR}/.env (chmod 600). Fill in Kite + Telegram creds before starting:"
  echo "      sudo -u ubuntu nano ${DATA_DIR}/.env"
fi

# ── 2) systemd unit
sed \
  -e "s|%%USER_SLUG%%|${SLUG}|g" \
  -e "s|%%DATA_DIR%%|${DATA_DIR}|g" \
  -e "s|%%FLASK_PORT%%|${FLASK_PORT}|g" \
  "${TMPL_DIR}/kite-monitor-user.service.tmpl" > "${SYSTEMD_UNIT}"

# ── 3) nginx server block
sed \
  -e "s|%%USER_SLUG%%|${SLUG}|g" \
  -e "s|%%PUBLIC_PORT%%|${PUBLIC_PORT}|g" \
  -e "s|%%FLASK_PORT%%|${FLASK_PORT}|g" \
  "${TMPL_DIR}/nginx-user.conf.tmpl" > "${NGINX_CONF}"

# ── 4) basic-auth password
if [ ! -f "${HTPASSWD}" ]; then
  echo "==> Set a basic-auth password for user '${SLUG}' (this is what they enter in the browser):"
  htpasswd -c "${HTPASSWD}" "${SLUG}"
  chown root:www-data "${HTPASSWD}"
  chmod 640 "${HTPASSWD}"
fi

# ── 5) systemd + nginx reload
systemctl daemon-reload
nginx -t

# ── 6) Touch the log file so systemd can append
LOG="/var/log/kite-monitor-${SLUG}.log"
touch "${LOG}" && chown ubuntu:ubuntu "${LOG}"

# ── 7) Open AWS-side port — we can only print the instruction; security group
#       is configured in the AWS console, not via SSH.
echo
echo "==> Almost done. Two manual steps remain:"
echo
echo "  (a) Fill in ${DATA_DIR}/.env with this user's Kite credentials."
echo "      sudo -u ubuntu nano ${DATA_DIR}/.env"
echo
echo "  (b) Open inbound TCP ${PUBLIC_PORT} on the AWS security group attached to this VM."
echo "      AWS Console → EC2 → Security Groups → Inbound rules → Add rule (Custom TCP, ${PUBLIC_PORT}, 0.0.0.0/0)."
echo
echo "  Then start the service:"
echo "      sudo systemctl enable --now kite-monitor-${SLUG}"
echo "      sudo systemctl reload nginx"
echo
echo "  User will access at: http://<vm-ip>:${PUBLIC_PORT}/   (login: ${SLUG} / <password you set>)"
