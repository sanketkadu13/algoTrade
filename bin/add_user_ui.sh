#!/usr/bin/env bash
# Non-interactive variant of add_user.sh — called by the dashboard's admin
# endpoint after an invited user submits their creds + chosen password.
#
# All inputs come through env vars (avoids leaking secrets into the process
# command line / ps auxf):
#   SLUG            (required)  e.g. priya
#   DASH_PASSWORD   (required)  basic-auth password the user picked
#   API_KEY         (required)  Kite Connect
#   API_SECRET      (required)
#   ACCESS_TOKEN    (required)
#   KITE_USER_ID    (optional)  for auto-refresh
#   KITE_PASSWORD   (optional)
#   KITE_TOTP_SECRET (optional)
#   TELEGRAM_TOKEN  (optional)
#   TELEGRAM_CHAT_ID (optional)
#
# Output: JSON on stdout: {"ok": true, "slug": "...", "public_port": N}
# Or:     {"ok": false, "error": "..."}
#
# Exit 0 on success, non-zero on failure.

set -euo pipefail

# ── JSON-safe failure helper. Pipes structured errors back to the caller. ──
fail() {
  local msg=$1
  printf '{"ok": false, "error": %s}\n' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "${msg}")"
  exit 1
}

if [ "${EUID}" -ne 0 ]; then
  fail "must run as root (sudo)"
fi

: "${SLUG:?SLUG required}"
: "${DASH_PASSWORD:?DASH_PASSWORD required}"
: "${API_KEY:?API_KEY required}"
: "${API_SECRET:?API_SECRET required}"
# ACCESS_TOKEN is OPTIONAL — invitees can leave it blank at signup and
# generate one from their dashboard's Refresh Token modal at first login.
ACCESS_TOKEN="${ACCESS_TOKEN:-}"

# ── Validate slug
if ! [[ "${SLUG}" =~ ^[a-z0-9-]+$ ]]; then
  fail "slug must match ^[a-z0-9-]+\$"
fi
if [ "${SLUG}" = "omkar" ] || [ "${SLUG}" = "admin" ] || [ "${SLUG}" = "root" ]; then
  fail "slug '${SLUG}' is reserved"
fi

CODE_DIR=/opt/kite
DATA_DIR="/opt/kite-data-${SLUG}"
TMPL_DIR="${CODE_DIR}/deploy/templates"
SYSTEMD_UNIT="/etc/systemd/system/kite-monitor-${SLUG}.service"
NGINX_CONF="/etc/nginx/sites-enabled/kite-${SLUG}"
HTPASSWD="/etc/nginx/.htpasswd-${SLUG}"

# ── Refuse to overwrite an existing user
if [ -d "${DATA_DIR}" ] || [ -f "${SYSTEMD_UNIT}" ]; then
  fail "user '${SLUG}' already exists"
fi

# ── Port picking, restricted to the AWS pre-opened range 8001-8010
pick_port() {
  local start=$1 end=$2
  for p in $(seq "${start}" "${end}"); do
    if ! ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE ":${p}\$" \
       && ! grep -qE "listen\s+${p}\b" /etc/nginx/sites-enabled/* 2>/dev/null \
       && ! grep -qE "PORT=${p}\b" /etc/systemd/system/kite-monitor-*.service 2>/dev/null; then
      echo "${p}"
      return 0
    fi
  done
  return 1
}
PUBLIC_PORT=$(pick_port 8001 8010) || fail "no free public port in 8001-8010 (max users reached or AWS SG not opened)"
FLASK_PORT=$(pick_port 5002 5020) || fail "no free flask port in 5002-5020"

# ── Per-user data dir + .env (chmod 600, secrets never touch ps/log)
mkdir -p "${DATA_DIR}/data/csv"
chown -R ubuntu:ubuntu "${DATA_DIR}"

cat > "${DATA_DIR}/.env" <<EOF
API_KEY=${API_KEY}
API_SECRET=${API_SECRET}
ACCESS_TOKEN=${ACCESS_TOKEN}
KITE_USER_ID=${KITE_USER_ID:-}
KITE_PASSWORD=${KITE_PASSWORD:-}
KITE_TOTP_SECRET=${KITE_TOTP_SECRET:-}
TELEGRAM_TOKEN=${TELEGRAM_TOKEN:-}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID:-}
EOF
chown ubuntu:ubuntu "${DATA_DIR}/.env"
chmod 600 "${DATA_DIR}/.env"

# ── systemd unit
sed \
  -e "s|%%USER_SLUG%%|${SLUG}|g" \
  -e "s|%%DATA_DIR%%|${DATA_DIR}|g" \
  -e "s|%%FLASK_PORT%%|${FLASK_PORT}|g" \
  "${TMPL_DIR}/kite-monitor-user.service.tmpl" > "${SYSTEMD_UNIT}"

# ── nginx server block
sed \
  -e "s|%%USER_SLUG%%|${SLUG}|g" \
  -e "s|%%PUBLIC_PORT%%|${PUBLIC_PORT}|g" \
  -e "s|%%FLASK_PORT%%|${FLASK_PORT}|g" \
  "${TMPL_DIR}/nginx-user.conf.tmpl" > "${NGINX_CONF}"

# ── htpasswd (batch mode, no prompts; password from env)
htpasswd -B -b -c "${HTPASSWD}" "${SLUG}" "${DASH_PASSWORD}" > /dev/null 2>&1 \
  || fail "htpasswd failed"
chown root:www-data "${HTPASSWD}"
chmod 640 "${HTPASSWD}"

# ── Logfile
LOG="/var/log/kite-monitor-${SLUG}.log"
touch "${LOG}" && chown ubuntu:ubuntu "${LOG}"

# ── Reload + start
systemctl daemon-reload
nginx -t > /dev/null 2>&1 || fail "nginx -t failed; aborting before reload"
systemctl enable --now "kite-monitor-${SLUG}" > /dev/null 2>&1 \
  || fail "systemctl enable/start failed"
systemctl reload nginx

# ── Success
printf '{"ok": true, "slug": "%s", "public_port": %d, "flask_port": %d}\n' \
  "${SLUG}" "${PUBLIC_PORT}" "${FLASK_PORT}"
