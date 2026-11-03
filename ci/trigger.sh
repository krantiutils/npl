#!/bin/bash
# Publish the live session to live.json, every 2 minutes from launchd on k2.
#
# 3 Nov 2026: NetTV moved the NPL feed to DASH + Widevine, which retired the old
# job. It recorded HLS and published a playlist path; the playlist is gone (it is
# /npl/live_match/index.mpd now, no match number, no token) and the picture is
# CENC-encrypted, so a recording without a license is worthless. What the website
# needs is the manifest plus the 15-minute Widevine license, so that is all this
# does. Recording is not attempted: it is not possible without the license keys.
#
# live.json is written through the GitHub contents API -- no clone here, so this
# cannot fight with a push of the site -- and only when the payload changed, or
# when the published heartbeat is more than ten minutes old. A match therefore
# costs a handful of commits, not hundreds.
set -u
REPO=${REPO:-krantiutils/npl}
WORK=${WORK:-$HOME/npl-ci}

export PATH="$HOME/npl-ci/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
command -v node >/dev/null || { echo "node missing"; exit 1; }
command -v gh >/dev/null || { echo "gh missing"; exit 1; }

[ -f "$WORK/nettv.env" ] && { set -a; . "$WORK/nettv.env"; set +a; }
[ -f "$WORK/gh-token" ] && export GH_TOKEN=$(cat "$WORK/gh-token")

# The account allows one concurrent session, so NETTV_USER/NETTV_PASS are only used
# when the cached 46-hour token has run out; if a phone or TV is signed in, the
# login fails and this exits without touching the published file.
if ! node "$(dirname "$0")/publish.cjs" > /tmp/npl-live.json; then
  echo "mint failed, leaving live.json alone"
  exit 0
fi

# what is published right now?
remote=$(gh api "repos/$REPO/contents/live.json" --jq .content 2>/dev/null | tr -d '\n')
SHA=$(gh api "repos/$REPO/contents/live.json" --jq .sha 2>/dev/null)
REMOTE_JSON=""
[ -n "$remote" ] && REMOTE_JSON=$(printf '%s' "$remote" | base64 -d 2>/dev/null)

need=0
if [ -z "$REMOTE_JSON" ]; then
  need=1
else
  verdict=$(REMOTE_JSON="$REMOTE_JSON" NEW="$(cat /tmp/npl-live.json)" node -e '
    const a = JSON.parse(process.env.REMOTE_JSON || "{}");
    const b = JSON.parse(process.env.NEW || "{}");
    const key = (x) => JSON.stringify([x.live, x.match, x.mpd, x.license_url, x.expires, x.mint]);
    if (key(a) !== key(b)) { process.stdout.write("changed"); process.exit(0); }
    const age = (Date.now() - new Date(a.checked).getTime()) / 60000;
    process.stdout.write(age > 10 ? "stale" : "same");
  ')
  [ "$verdict" = "same" ] || need=1
fi

if [ "$need" = "0" ]; then
  echo "live.json unchanged ($(node -e 'const j=require("/tmp/npl-live.json");process.stdout.write(j.live?`lic exp ${j.expires}`:"no match")'))"
  exit 0
fi

CONTENT=$(base64 < /tmp/npl-live.json | tr -d '\n')
SLUG=$(node -e 'const j=require("/tmp/npl-live.json");process.stdout.write(j.match||"no match")')
args=(-X PUT "repos/$REPO/contents/live.json"
      -f message="live.json: $SLUG $(date -u +%FT%TZ)"
      -f content="$CONTENT" -f branch=main)
[ -n "$SHA" ] && args+=(-f sha="$SHA")
gh api "${args[@]}" >/dev/null 2>&1 \
  && echo "$(date -u +%FT%TZ) published live.json: $SLUG" \
  || echo "warn: could not publish live.json"
