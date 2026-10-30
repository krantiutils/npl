#!/bin/bash
# Obtain a NetTV access token from the account credentials.
#
# No browser: the storefront's login is a plain JSON POST to ott-auth, captured
# from the app itself:
#   POST /v2/subscribers/login
#   {"username","password","reseller_id":5,"device_id":"MacOS|Chrome","device_unique_id":"<uuid>"}
# The response carries access_token (+ refresh_token), which is exactly what the
# `ntv_u` cookie holds. That token is what the subscriber API wants as a Bearer.
#
# Tokens last ~46 h, and the account allows ONE concurrent session, so the token
# is cached in $TOKEN_FILE and only re-minted when it is missing or about to
# expire. Logging in on every run would fight the user's own device for the slot.
#
#   NETTV_USER=... NETTV_PASS=... ./ci/login.sh
set -euo pipefail

API=${NETTV_AUTH:-https://ott-auth.geniustv.geniussystems.com.np}
TOKEN_FILE=${TOKEN_FILE:-$HOME/npl-ci/token.txt}
# fixed per-installation device id so a re-login reuses the same session record
DEVICE_UUID_FILE=${DEVICE_UUID_FILE:-$HOME/npl-ci/device-uuid}

export PATH="$HOME/npl-ci/bin:/usr/bin:/bin"

if [ -z "${NETTV_USER:-}" ] || [ -z "${NETTV_PASS:-}" ]; then
  echo "login: NETTV_USER/NETTV_PASS not set"
  exit 1
fi

mkdir -p "$(dirname "$TOKEN_FILE")"
# The account allows ONE concurrent session, and the server keys sessions by
# device_unique_id -- so logging in with the SAME id replaces our session instead
# of adding one (verified: a random id -> 400 "sessions exceeded", this pinned id
# -> 200). Keep it stable or every login will fight for the slot.
UUID=${NETTV_DEVICE_UUID:-2258e2c2-9438-469c-8dd8-b31f911da230}

body=$(node -e '
  process.stdout.write(JSON.stringify({
    username: process.env.NETTV_USER,
    password: process.env.NETTV_PASS,
    reseller_id: 5,
    device_id: "MacOS|Chrome",
    device_unique_id: process.argv[1],
  }));
' "$UUID")

resp=$(curl -sS --max-time 25 -X POST -H "Content-Type: application/json" -d "$body" \
        "$API/v2/subscribers/login" -w '\n%{http_code}') || { echo "login: request failed"; exit 1; }
code=$(printf '%s' "$resp" | tail -1)
payload=$(printf '%s' "$resp" | sed '$d')

if [ "$code" != "200" ]; then
  echo "login: HTTP $code"
  printf '%s\n' "$payload" | head -c 400
  echo
  exit 1
fi

token=$(printf '%s' "$payload" | node -e '
  let s = "";
  process.stdin.on("data", (d) => (s += d)).on("end", () => {
    const walk = (v) => {
      if (!v || typeof v !== "object") return null;
      for (const [k, x] of Object.entries(v)) {
        if (k === "access_token" && typeof x === "string" && x.split(".").length === 3) return x;
        const r = walk(x);
        if (r) return r;
      }
      return null;
    };
    try { const t = walk(JSON.parse(s)); if (t) process.stdout.write(t); } catch (e) {}
  });')

if [ -z "$token" ]; then echo "login: no access_token in response"; printf '%s\n' "$payload" | head -c 300; exit 1; fi

umask 077
printf '%s\n' "$token" > "$TOKEN_FILE"
exp=$(node -e '
  const t = require("fs").readFileSync(process.argv[1], "utf8").trim();
  const p = JSON.parse(Buffer.from(t.split(".")[1], "base64url").toString());
  console.log(new Date(p.exp * 1000).toISOString());
' "$TOKEN_FILE")
echo "login: token written to $TOKEN_FILE, expires $exp"
