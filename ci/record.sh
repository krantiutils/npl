#!/bin/bash
# Record the live NPL match, shrink it, and attach it to a GitHub release.
#
# Runs on the self-hosted runner (k2, Nepali IP). Two ways to find the feed:
#   1. authoritative: mint stream_url from the subscriber API using NETTV_TOKEN
#      (a repo secret). Gives the exact path+token for this match.
#   2. fallback: probe the two public channel layouts anonymously.
# Both are needed: (2) keeps working if the token expires, (1) survives the
# layouts changing again (they changed once already, 27 Oct -> 28 Oct).
#
# Light on purpose: threads capped, mono audio, intermediates deleted after the
# release is published (k2 has ~56 GB).
#
# Exit codes: 0 = nothing to do, or recorded and published; 1 = broke.
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="$HOME/npl-ci/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
command -v node >/dev/null || { echo "node not on PATH ($PATH)"; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg not on PATH"; exit 1; }

API=https://ott-resources.geniustv.geniussystems.com.np/public/sports/v1/namespaces/-/tournaments
EDGE=https://ottnlive-fstream.nettvstreamz.com
OUT=${OUT:-$HOME/npl-ci/out}
REPO=${REPO:-krantiutils/npl}
# SMOKE=1 records ~75s instead of a whole match, so the publish path can be proven
SMOKE=${SMOKE:-0}
if [ "$SMOKE" = "1" ]; then MAXH=0.021; else MAXH=5; fi
BITRATE=${BITRATE:-300k}
THREADS=${THREADS:-4}
mkdir -p "$OUT"

# BSD date (macOS) has no -d; GNU date has no -r. Try both.
fmt_date() { date -u -r "$1" "+$2" 2>/dev/null || date -u -d "@$1" "+$2"; }


# ---- token: cached on the runner, re-minted only when expired --------------
# The account allows a single concurrent session, so logging in on every run
# would fight the user's own device. The token lasts ~46 h; we only refresh it
# when it is missing or within 15 minutes of expiry.
TOKEN_FILE=${TOKEN_FILE:-$HOME/npl-ci/token.txt}
token_valid() {
  [ -f "$TOKEN_FILE" ] || return 1
  node -e '
    const t = require("fs").readFileSync(process.argv[1], "utf8").trim();
    const p = JSON.parse(Buffer.from(t.split(".")[1], "base64url").toString());
    process.exit(p.exp * 1000 > Date.now() + 15 * 60000 ? 0 : 1);
  ' "$TOKEN_FILE" 2>/dev/null
}
if ! token_valid; then
  if [ -n "${NETTV_USER:-}" ] && [ -n "${NETTV_PASS:-}" ]; then
    ./ci/login.sh || echo "login failed -- continuing without a token (anonymous probing)"
  else
    echo "no cached token and no credentials -- continuing anonymously"
  fi
fi
if [ -z "${NETTV_TOKEN:-}" ] && [ -f "$TOKEN_FILE" ]; then NETTV_TOKEN=$(cat "$TOKEN_FILE"); fi
export NETTV_TOKEN

# ---- 1. is a match live? (public endpoint) --------------------------------
# the trigger resolves the match and passes its key; fall back to the live fixture
live=$(if [ -n "${MATCH_KEY:-}" ]; then
  curl -sS --max-time 20 "$API/fixtures" | MATCH_KEY="$MATCH_KEY" node -e '
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  try {
    const want = process.env.MATCH_KEY;
    const m = (JSON.parse(s).data || []).find((x) => x.key === want);
    const slug = m ? `${m.team_a?.code || "?"}-vs-${m.team_b?.code || "?"}` : "live";
    process.stdout.write(`${want}|${slug}|${m ? m.start_at : 0}`);
  } catch (e) {}
});'
else
  curl -sS --max-time 20 "$API/fixtures" | node -e '
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  try {
    const j = JSON.parse(s);
    const rows = j.data || [];
    let m = rows.find((x) => x.is_live || x.status === "started");
    if (!m) {
      // fixtures lag the feed (stayed not_started through 28 Oct). Fall back to the
      // nearest scheduled match so the minted path is still authoritative.
      const now = Date.now() / 1000;
      m = rows.filter((x) => x.status === "not_started" && Math.abs(x.start_at - now) <= 3 * 3600)
              .sort((a, b) => Math.abs(a.start_at - now) - Math.abs(b.start_at - now))[0];
    }
    if (!m) return;
    const slug = `${m.team_a?.code || "?"}-vs-${m.team_b?.code || "?"}`;
    process.stdout.write(`${m.key}|${slug}|${m.start_at}`);
  } catch (e) {}
});')
fi)

if [ -n "$live" ]; then
  IFS='|' read -r KEY SLUG START <<<"$live"
  echo "live: $SLUG ($KEY)"
else
  echo "fixtures: no match marked live yet"
fi

# ---- 2a. authoritative path via the minted stream_url ---------------------
CH=""
TOKENQS=""
if [ -n "${NETTV_TOKEN:-}" ] && [ -n "$live" ]; then
  SU=$(curl -sS --max-time 12 \
        -H "Authorization: Bearer $NETTV_TOKEN" -H "Accept: application/json" \
        "https://ott-resources.geniustv.geniussystems.com.np/subscriber/sports/v1/namespaces/5/subscribers/${NETTV_SUBSCRIBER:-9337709}/stbs/${NETTV_STB:-voidash-5}/cricket/matches/$KEY/stream-url" \
       | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{process.stdout.write(JSON.parse(s).data.stream_url||"")}catch(e){}})')
  if [ -n "$SU" ]; then
    # Follow the LB redirect first: the real rendition is not on the pinned edge, the
    # host rotates (ott-anycaststream NN) and the filename is not always index.m3u8
    # (it has been playlist.m3u8). Trust the final URL rather than guessing.
    read -r code final <<<"$(curl -sSL -o /tmp/npl-master.m3u8 -w '%{http_code} %{url_effective}' --max-time 12 "$SU")"
    magic=$(head -c 7 /tmp/npl-master.m3u8 2>/dev/null || true)
    if [ "$magic" = "#EXTM3U" ]; then
      HOST=$(printf '%s' "$final" | cut -d/ -f3)
      PB=$(printf '%s' "$final" | sed -E 's#^https?://[^/]+##; s#\?.*$##; s#/[^/]+$##')
      QS=$(printf '%s' "$SU" | sed -n 's/.*\(wmsAuthSign=.*\)/\1/p')
      CH="${PB#/}"; EDGE_HOST="https://$HOST"; TOKENQS="$QS"
      echo "minted channel: $EDGE_HOST/$CH"
    else
      echo "minted url did not serve a playlist (HTTP $code), falling back to probing"
    fi
  fi
fi

# ---- 2b. anonymous fallback: probe both known layouts ---------------------
if [ -z "$CH" ]; then
  for base in live_hls/npl_match_ ottnpweb/npl_live; do
    case "$base" in
      live_hls/*) file=playlist.m3u8 ;;
      *)          file=index.m3u8 ;;
    esac
    for i in 1 2 3 4 5 6 7 8; do
      code=$(curl -s -o /tmp/npl-master.m3u8 -w '%{http_code}' --max-time 6 "$EDGE/$base$i/$file")
      # NB: no pipe here -- `head | grep -q` trips pipefail via SIGPIPE
      magic=$(head -c 7 /tmp/npl-master.m3u8 2>/dev/null || true)
      if [ "$code" = "200" ] && [ "$magic" = "#EXTM3U" ]; then CH="$base$i"; break 2; fi
    done
  done
fi

if [ -z "$live" ] && [ -z "$CH" ]; then echo "no live match and no publishing channel"; exit 0; fi
if [ -z "$CH" ]; then echo "match is live but no channel is publishing"; exit 1; fi
echo "channel: $CH"

# publish the resolved path for the website (public repo -> Pages). No token here:
# the channel is served anonymously, so the page only needs the path.
live_json() {
  node -e '
    const [path, checked, live] = process.argv.slice(1);
    process.stdout.write(JSON.stringify({ checked, live: live === "1", path: live === "1" ? path : null }, null, 2) + "\n");
  ' "/$CH" "$(date -u +%FT%TZ)" "$([ -n "$CH" ] && echo 1 || echo 0)"
}
if [ "$(cat live.json 2>/dev/null || true)" != "$(live_json)" ]; then
  live_json > live.json
  git add live.json
  git -c user.name="npl-ci" -c user.email="npl-ci@users.noreply.github.com" \
      commit -q -m "ci: live url -> /$CH" || true
  git push -q origin HEAD:main || echo "warn: could not push live.json"
  echo "published live.json: /$CH"
else
  echo "live.json unchanged"
fi

# fixtures can lag the feed (toss / pre-show): still record, with a generic name
if [ -z "$live" ]; then
  KEY="unknown"
  SLUG="match-$(date -u +%Y%m%d-%H%M)"
  START=$(date +%s)
fi

VAR=$(grep -A1 'RESOLUTION=854x480' /tmp/npl-master.m3u8 | tail -1 | tr -d ' \r')
if [ -z "$VAR" ] || [ "${VAR:0:1}" = "#" ]; then VAR=$(grep -m1 -v '^#' /tmp/npl-master.m3u8 | tr -d ' \r'); fi
echo "variant: $VAR"

# ---- 3. record until the feed goes quiet -----------------------------------
# The feed keeps streaming after the match (post-match show), so silence alone
# means recording filler until the 5 h cap. This watchdog polls the fixtures API
# and stops the capture once the match is marked completed. Backgrounded; if the
# API lags it simply never fires and idle-stop/max-hours still apply.
if [ -n "${KEY:-}" ] && [ "$KEY" != "unknown" ]; then
  (
    for _ in $(seq 1 360); do
      sleep 60
      st=$(curl -sS --max-time 10 "$API/fixtures" 2>/dev/null | KEY="$KEY" node -e '
        let s = "";
        process.stdin.on("data", (d) => (s += d)).on("end", () => {
          try {
            const m = (JSON.parse(s).data || []).find((x) => x.key === process.env.KEY);
            if (m) process.stdout.write(m.status || "");
          } catch (e) {}
        });')
      if [ "$st" = "completed" ]; then
        echo "watchdog: match completed -> stopping capture"
        pkill -TERM -f "capture.cjs" && break
      fi
    done
  ) &
  WD=$!
fi

node ci/capture.cjs \
  --host "$EDGE" --path "/$CH" --variant "$VAR" \
  ${TOKENQS:+--token "$TOKENQS"} \
  --out "$OUT" --name "${SLUG}_480p" --cb \
  --idle-stop 8 --max-hours "$MAXH"

kill "${WD:-}" 2>/dev/null || true

TS=$(ls -t "$OUT"/${SLUG}_480p_*.ts 2>/dev/null | head -1 || true)
if [ -z "$TS" ]; then echo "nothing recorded"; exit 1; fi
echo "recorded: $(basename "$TS") $(du -h "$TS" | cut -f1)"

# ---- 4. shrink: lowest-bitrate codec available, mono audio ----------------
ENC=$(ffmpeg -hide_banner -encoders 2>/dev/null || true)
if grep -q libsvtav1 <<<"$ENC"; then
  VOPTS=(-c:v libsvtav1 -b:v "$BITRATE" -svtav1-params "lp=${THREADS}")
elif grep -q libx265 <<<"$ENC"; then
  VOPTS=(-c:v libx265 -b:v 500k -x265-params "pools=${THREADS}")
else
  VOPTS=(-c:v libx264 -b:v 900k)
fi
if grep -q libopus <<<"$ENC"; then AOPTS=(-c:a libopus -b:a 48k); else AOPTS=(-c:a aac -b:a 64k); fi

OUTV="${TS%.ts}.mp4"
ffmpeg -hide_banner -loglevel error -y -i "$TS" \
  -vf "scale=854:480" -threads "$THREADS" -filter_threads 2 \
  "${VOPTS[@]}" "${AOPTS[@]}" -ac 1 -movflags +faststart "$OUTV"
echo "encoded: $(du -h "$OUTV" | cut -f1)"

# ---- 5. publish as a release asset ----------------------------------------
STAMP=$(basename "${TS%.ts}" | sed -E 's/.*_([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2})-([0-9]{2})-([0-9]{2})$/\1T\2\3\4/')
if [ "$SMOKE" = "1" ]; then TAG="smoke-$(date -u +%H%M%S)"; else TAG="npl-${STAMP:-$(fmt_date "${START:-$(date +%s)}" %Y%m%d-%H%M)}"; fi
TITLE="NPL ${SLUG} — $(fmt_date "${START:-$(date +%s)}" "%d %b %Y")"
if gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
  gh release upload "$TAG" -R "$REPO" "$OUTV" --clobber 2>&1 | tail -2
else
gh release create "$TAG" -R "$REPO" "$OUTV" --title "$TITLE" \
  --notes "Auto-recorded from the live feed. ${BITRATE} video, mono audio." --latest 2>&1 | tail -2
fi

# ---- 6. reclaim disk (the release asset is the archive now) ---------------
rm -f "$TS" "$OUTV"
echo "done: $TAG"
