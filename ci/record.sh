#!/bin/bash
# Record the live NPL match, shrink it, and attach it to a GitHub release.
#
# Runs on the self-hosted runner (k2): a Nepali IP, which the CDN is known to
# serve. No login needed -- the channels are anonymous and discovery just probes
# npl_live1..8.
#
# Kept deliberately light: ffmpeg is capped to a handful of threads, mono audio,
# and the intermediates are deleted once the release is published (k2 has ~56 GB).
#
# Exit codes: 0 = nothing to do, or recorded and published; 1 = broke.
set -euo pipefail
cd "$(dirname "$0")/.."

# static toolchain on k2, so no brew/node install is needed
export PATH="$HOME/npl-ci/bin:$PATH"

API=https://ott-resources.geniustv.geniussystems.com.np/public/sports/v1/namespaces/-/tournaments
EDGE=https://ottnlive-fstream.nettvstreamz.com
OUT=${OUT:-$HOME/npl-ci/out}
BITRATE=${BITRATE:-300k}
THREADS=${THREADS:-4}
mkdir -p "$OUT"

command -v node >/dev/null || { echo "node not on PATH ($PATH)"; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg not on PATH"; exit 1; }

# ---- 1. is a match live? ---------------------------------------------------
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

# ---- 2. find a channel that is publishing ----------------------------------
# Two different layouts exist for the same league:
#   /live_hls/npl_match_<n>/playlist.m3u8    match-day feed (UNDERSCORE: npl_match_3)
#   /ottnpweb/npl_live<n>/index.m3u8         channel feed (seen on 27 Oct)
# Probe both, anonymously -- neither needs a token.
CH=""
for base in live_hls/npl_match_ ottnpweb/npl_live; do
  case "$base" in
    live_hls/*) file=playlist.m3u8 ;;
    *)          file=index.m3u8 ;;
  esac
  for i in 1 2 3 4 5 6 7 8; do
    code=$(curl -s -o /tmp/npl-master.m3u8 -w '%{http_code}' --max-time 6 "$EDGE/$base$i/$file")
    # NB: compare without a pipe -- `head | grep -q` trips `set -o pipefail` via SIGPIPE
    magic=$(head -c 7 /tmp/npl-master.m3u8 2>/dev/null || true)
    if [ "$code" = "200" ] && [ "$magic" = "#EXTM3U" ]; then
      CH="$base$i"; break 2
    fi
  done
done

# nothing live by either signal -> nothing to do
if [ -z "$live" ] && [ -z "$CH" ]; then echo "no live match and no publishing channel"; exit 0; fi
if [ -z "$CH" ]; then echo "match is live but no channel is publishing"; exit 1; fi
echo "channel: $CH"

# if the fixtures endpoint is lagging (toss, pre-show) we still record, with a generic name
if [ -z "$live" ]; then
  KEY="unknown"
  SLUG="match-$(date -u +%Y%m%d-%H%M)"
  START=$(date +%s)
fi

VAR=$(grep -A1 'RESOLUTION=854x480' /tmp/npl-master.m3u8 | tail -1 | tr -d ' \r')
if [ -z "$VAR" ] || [ "${VAR:0:1}" = "#" ]; then VAR=$(grep -m1 -v '^#' /tmp/npl-master.m3u8 | tr -d ' \r'); fi
echo "variant: $VAR"

# ---- 3. record until the feed goes quiet -----------------------------------
node ci/capture.js \
  --host "$EDGE" --path "/$CH" --variant "$VAR" \
  --out "$OUT" --name "${SLUG}_480p" --cb \
  --idle-stop 8 --max-hours 5

TS=$(ls -t "$OUT"/${SLUG}_480p_*.ts 2>/dev/null | head -1 || true)
if [ -z "$TS" ]; then echo "nothing recorded"; exit 1; fi
echo "recorded: $(du -h "$TS" | cut -f1)"

# ---- 4. shrink: lowest-bitrate codec this ffmpeg has, mono audio ------------
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
