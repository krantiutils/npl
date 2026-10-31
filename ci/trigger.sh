#!/bin/bash
# Dispatch the record workflow when a match is genuinely on air.
#
# The old version probed anonymous HLS paths. That stopped working on 31 Oct: the
# rendition is now /npl/live_match<N>/ on a rotating host and every playlist and
# segment needs a signed wmsAuthSign. So we mint with the account instead, exactly
# like the recorder does, and only dispatch when that URL actually serves a playlist.
#
# Runs from launchd every 2 minutes on k2. Credentials live in ~/npl-ci/nettv.env
# (0600) and the dispatch token in ~/npl-ci/gh-token (0600).
set -u
REPO=${REPO:-krantiutils/npl}
RES=https://ott-resources.geniustv.geniussystems.com.np
AUTH=https://ott-auth.geniustv.geniussystems.com.np
DEVICE_UUID=${NETTV_DEVICE_UUID:-2258e2c2-9438-469c-8dd8-b31f911da230}

export PATH="$HOME/npl-ci/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
command -v node >/dev/null || { echo "node missing"; exit 1; }
command -v gh >/dev/null || { echo "gh missing"; exit 1; }

# creds + token
[ -f "$HOME/npl-ci/nettv.env" ] && . "$HOME/npl-ci/nettv.env"
[ -f "$HOME/npl-ci/gh-token" ] && export GH_TOKEN=$(cat "$HOME/npl-ci/gh-token")
[ -z "${NETTV_USER:-}" ] || [ -z "${NETTV_PASS:-}" ] && { echo "no nettv creds"; exit 0; }

# 1. which match does the platform consider live?
fixtures=$(curl -sS --max-time 15 "$RES/public/sports/v1/namespaces/-/tournaments/fixtures" 2>/dev/null)
live=$(printf '%s' "$fixtures" | node -e '
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  try {
    const rows = JSON.parse(s).data || [];
    let m = rows.find((x) => x.is_live || x.status === "started");
    if (!m) {
      // The platform flag lags (it never went live for the 31 Oct 17:00 game). If a
      // a fixture start time has passed and it is still not_started, that is a match
      // that should be on air -- let the mint below decide whether a feed exists.
      const now = Date.now() / 1000;
      m = rows.filter((x) => x.status === "not_started" && x.start_at <= now + 300 &&
                               now - x.start_at <= 3 * 3600)
              .sort((a, b) => b.start_at - a.start_at)[0];
    }
    if (!m) return;
    const slug = `${m.team_a?.code || "?"}-vs-${m.team_b?.code || "?"}`;
    process.stdout.write(`${m.key}|${slug}`);
  } catch (e) {}
});')
[ -z "$live" ] && exit 0
IFS='|' read -r KEY SLUG <<<"$live"

# 2. already published this match?
if gh release list --repo "$REPO" --limit 30 2>/dev/null | grep -q "$SLUG"; then exit 0; fi

# 3. a run already going?
state=$(gh run list --repo "$REPO" --workflow=record.yml --limit 1 --json status -q '.[0].status' 2>/dev/null || echo "")
case "$state" in queued|in_progress|requested|waiting|pending) exit 0 ;; esac

# 4. mint and prove the feed actually serves a playlist
body=$(node -e 'process.stdout.write(JSON.stringify({username:process.env.NETTV_USER,password:process.env.NETTV_PASS,reseller_id:5,device_id:"MacOS|Chrome",device_unique_id:process.argv[1]}))' "$DEVICE_UUID")
token=$(curl -sS --max-time 20 -X POST -H "Content-Type: application/json" -d "$body" "$AUTH/v2/subscribers/login" 2>/dev/null \
  | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{const w=v=>{if(!v||typeof v!=="object")return null;for(const[k,x]of Object.entries(v)){if(k==="access_token"&&typeof x==="string")return x;const r=w(x);if(r)return r}return null};const t=w(JSON.parse(s));if(t)process.stdout.write(t)}catch(e){}})')
[ -z "$token" ] && { echo "login failed"; exit 0; }

su=$(curl -sS --max-time 15 -H "Authorization: Bearer $token" -H "Accept: application/json" \
  "$RES/subscriber/sports/v1/namespaces/5/subscribers/${NETTV_SUBSCRIBER:-9337709}/stbs/${NETTV_STB:-voidash-5}/cricket/matches/$KEY/stream-url" 2>/dev/null \
  | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{process.stdout.write(JSON.parse(s).data.stream_url||"")}catch(e){}})')
[ -z "$su" ] && { echo "no stream_url yet for $SLUG"; exit 0; }

code=$(curl -sSL -o /tmp/npl-trigger.m3u8 -w '%{http_code}' --max-time 12 "$su")
magic=$(head -c 7 /tmp/npl-trigger.m3u8 2>/dev/null || true)
if [ "$code" != "200" ] || [ "$magic" != "#EXTM3U" ]; then
  echo "feed not serving yet for $SLUG (HTTP $code)"; exit 0
fi

gh workflow run record.yml --repo "$REPO" --ref main -f match="$KEY" >/dev/null 2>&1 \
  && echo "$(date -u +%FT%TZ) dispatched for $SLUG ($KEY)"
