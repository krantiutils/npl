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
BITRATE=${BITRATE:-300k}
THREADS=${THREADS:-4}
mkdir -p "$OUT"

# ---- 1. is a match live? (public endpoint) --------------------------------
live=$(curl -sS --max-time 20 "$API/fixtures" | node -e '
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  try {
    const j = JSON.parse(s);
    const m = (j.data || []).find((x) => x.is_live || x.status === "started");
    if (!m) return;
    const slug = `${m.team_a?.code || "?"}-vs-${m.team_b?.code || "?"}`;
    process.stdout.write(`${m.key}|${slug}|${m.start_at}`);
  } catch (e) {}
});')

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
    PB=$(printf '%s' "$SU" | sed -E 's#^https?://[^/]+##; s#\?.*$##; s#/[^/]+$##; s#^/#+#')
    FN=$(printf '%s' "$SU" | sed -E 's#^https?://[^/]+##; s#\?.*$##; s#.*/##')
    QS=$(printf '%s' "$SU" | sed -n 's/.*\(wmsAuthSign=.*\)/\1/p')
    code=$(curl -s -o /tmp/npl-master.m3u8 -w '%{http_code}' --max-time 8 "$EDGE/$PB/$FN?$QS")
    magic=$(head -c 7 /tmp/npl-master.m3u8 2>/dev/null || true)
    if [ "$magic" = "#EXTM3U" ]; then
      CH="$PB"; TOKENQS="$QS"
      echo "minted channel: $CH"
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

# fixtures can lag the feed (toss / pre-show): still record, with a generic name
if [ -z "$live" ]; then
  KEY="unknown"
  SLUG="match-$(date -u +%Y%m%d-%H%M)"
  START=$(date +%s)
fi

VAR=$(grep -A1 'RESOLUTION=854x480' /tmp/npl-master.m3u8 | tail -1 | tr -d ' \r')
if [ -z "$VAR" ] || [ "${VAR:0:1}" = "#" ]; then VAR=$(grep -m1 -v '^#' /tmp/npl-master.m3u8 | tr -d ' \r'); fi
echo "variant: $VAR"

# ---- 3. record until the feed goes quiet ----------------------------------
node ci/capture.cjs \
  --host "$EDGE" --path "/$CH" --variant "$VAR" \
  ${TOKENQS:+--token "$TOKENQS"} \
  --out "$OUT" --name "${SLUG}_480p" --cb \
  --idle-stop 8 --max-hours 5

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
TAG="npl-$(date -u -d "@${START:-$(date +%s)}" +%Y%m%d)-$(echo "$KEY" | tail -c 7)"
TITLE="NPL ${SLUG} — $(date -u -d "@${START:-$(date +%s)}" +%d\ %b\ %Y)"
gh release create "$TAG" "$OUTV" --title "$TITLE" \
  --notes "Auto-recorded from the live feed. ${BITRATE} video, mono audio." --latest 2>&1 | tail -2

# ---- 6. reclaim disk (the release asset is the archive now) ---------------
rm -f "$TS" "$OUTV"
echo "done: $TAG"
