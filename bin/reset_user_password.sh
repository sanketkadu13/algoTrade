#!/usr/bin/env bash
# Reset a provisioned user's basic-auth password.
#
# Called by the admin panel when an invited user forgets their dashboard
# password. Overwrites the existing /etc/nginx/.htpasswd-<slug> entry with
# a new bcrypt hash. nginx re-reads the file on each request, so no reload
# is needed.
#
# Inputs (env vars, to keep secrets off the process command line):
#   SLUG          (required)  e.g. sanket
#   DASH_PASSWORD (required)  new basic-auth password
#
# Output: JSON on stdout: {"ok": true, "slug": "..."}
# Or:     {"ok": false, "error": "..."}

set -euo pipefail

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

if ! [[ "${SLUG}" =~ ^[a-z0-9-]+$ ]]; then
  fail "slug must match ^[a-z0-9-]+\$"
fi

HTPASSWD="/etc/nginx/.htpasswd-${SLUG}"
if [ ! -f "${HTPASSWD}" ]; then
  fail "no htpasswd file for slug '${SLUG}' — user not provisioned"
fi

# -B bcrypt, -b batch (password on command line — but we're root and the
# password came in via env, not argv from outside this process).
htpasswd -B -b "${HTPASSWD}" "${SLUG}" "${DASH_PASSWORD}" > /dev/null 2>&1 \
  || fail "htpasswd failed"

# Ownership/perms should already be right from add_user_ui.sh, but be safe.
chown root:www-data "${HTPASSWD}"
chmod 640 "${HTPASSWD}"

printf '{"ok": true, "slug": "%s"}\n' "${SLUG}"
