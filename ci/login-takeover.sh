#!/bin/bash
# Move the NetTV account's one allowed session to this host.
#
# The platform counts devices, not logins: presenting the device_unique_id of the
# device that currently holds the session takes it over instead of being refused
# with "Number of logged in sessions exceeded". k2 uses this to own the session so
# the site can mint its own licenses (see README, "Holding the session on k2").
#
#   NETTV_DEVICE_UUID=BFC14D2C-... NETTV_DEVICE_ID=iPad ci/login-takeover.sh
#
# Credentials are read from ~/npl-ci/nettv.env (or the environment) and never
# printed. Only the new token's expiry is written to the terminal; the token
# itself goes to ~/npl-ci/token.txt (0600).
set -u
export PATH="$HOME/npl-ci/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

AUTH=${AUTH:-https://ott-auth.geniustv.geniussystems.com.np}
ENV_FILE=${ENV_FILE:-$HOME/npl-ci/nettv.env}
TOKEN_FILE=${TOKEN_FILE:-$HOME/npl-ci/token.txt}

if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

DEVICE_UUID=${NETTV_DEVICE_UUID:-}
DEVICE_ID=${NETTV_DEVICE_ID:-iPad}
if [ -z "$DEVICE_UUID" ]; then
  echo "NETTV_DEVICE_UUID is required (the device_unique_id from the login error)" >&2
  exit 2
fi
if [ -z "${NETTV_USER:-}" ] || [ -z "${NETTV_PASS:-}" ]; then
  echo "NETTV_USER/NETTV_PASS missing from $ENV_FILE" >&2
  exit 2
fi
command -v node >/dev/null || { echo "node missing" >&2; exit 1; }

body=$(node -e '
  process.stdout.write(JSON.stringify({
    username: process.env.NETTV_USER, password: process.env.NETTV_PASS, reseller_id: 5,
    device_id: process.argv[1], device_unique_id: process.argv[2],
  }));
' "$DEVICE_ID" "$DEVICE_UUID")

resp=$(curl -sS --max-time 25 -X POST -H 'Content-Type: application/json' \
  -d "$body" -w '\n%{http_code}' "$AUTH/v2/subscribers/login")

code=$(printf '%s' "$resp" | tail -1)
text=$(printf '%s' "$resp" | sed '$d')

if [ "$code" != "200" ]; then
  echo "login failed: HTTP $code" >&2
  printf '%s' "$text" | head -c 600 >&2
  echo >&2
  exit 1
fi

token=$(printf '%s' "$text" | node -e '
  let s = "";
  process.stdin.on("data", (d) => (s += d)).on("end", () => {
    const find = (v) => {
      if (!v || typeof v !== "object") return null;
      for (const [k, x] of Object.entries(v)) {
        if (k === "access_token" && typeof x === "string") return x;
        const r = find(x);
        if (r) return r;
      }
      return null;
    };
    try {
      const t = find(JSON.parse(s));
      if (t) process.stdout.write(t);
    } catch { /* not JSON: reported as a failure below */ }
  });')

if [ -z "$token" ]; then
  echo "login returned no access_token" >&2
  exit 1
fi

umask 077
printf '%s' "$token" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

node -e '
  const p = JSON.parse(Buffer.from(process.argv[1].split(".")[1], "base64url").toString());
  console.log("session taken over; token expires", new Date(p.exp * 1000).toISOString());
' "$token"
