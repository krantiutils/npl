#!/bin/bash
# Record the live NPL match, shrink it, and attach it to a GitHub release.
# Runs on the self-hosted runner (Nepali IP: the CDN is not reachable from
# GitHub-hosted runners as far as we know). No login needed -- channels are
# served anonymously and discovery just probes npl_live1..8.
#
# Exit codes: 0 = nothing to do or recorded fine; 1 = something broke.
set -euo pipefail
cd "$(dirname "$0")/.."

API=https://ott-resources.geniustv.geniussystems.com.np/public/sports/v1/namespaces/-/tournaments
EDGE=https://ottnlive-fstream.nettvstreamz.com
OUT=${OUT:-$HOME/npl-out}
BITRATE=${BITRATE:-300k}
mkdir -p "$OUT"

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

if [ -z "$live" ]; then echo "no live match"; exit 0; fi
IFS='|' read -r KEY SLUG START <<<"$live"
echo "live: $SLUG ($KEY)"

# ---- 2. find a channel that is publishing ----------------------------------
CH=""
for i in 1 2 3 4 5 6 7 8; do
  code=$(curl -s -o /tmp/npl-master.m3u8 -w '%{http_code}' --max-time 6 "$EDGE/ottnpweb/npl_live$i/index.m3u8")
  if [ "$code" = "200" ] && head -c 7 /tmp/npl-master.m3u8 | grep -q EXTM3U; then CH="npl_live$i"; break; fi
done
if [ -z "$CH" ]; then echo "match is live but no channel is publishing"; exit 1; fi
echo "channel: $CH"

# ---- 3. prefer the 480p variant (no downscale needed later) ----------------
VAR=$(grep -A1 'RESOLUTION=854x480' /tmp/npl-master.m3u8 | tail -1 | tr -d '\r' | tr -d ' ')
if [ -z "$VAR" ] || [ "${VAR:0:1}" = "#" ]; then VAR=$(grep -m1 -v '^#' /tmp/npl-master.m3u8 | tr -d ' \r'); fi
echo "variant: $VAR"

# ---- 4. record until the feed goes quiet -----------------------------------
node ci/capture.js \
  --host "$EDGE" --path "/ottnpweb/$CH" --variant "$VAR" \
  --out "$OUT" --name "${SLUG}_480p" --cb \
  --idle-stop 8 --max-hours 5

TS=$(ls -t "$OUT"/${SLUG}_480p_*.ts 2>/dev/null | head -1 || true)
if [ -z "$TS" ]; then echo "nothing recorded"; exit 1; fi
echo "recorded: $TS ($(du -h "$TS" | cut -f1))"

# ---- 5. shrink: AV1 480p, stereo -> mono opus ------------------------------
OUTV="${TS%.ts}.av1.mp4"
ffmpeg -hide_banner -loglevel error -y -i "$TS" \
  -vf scale=854:480 -c:v libsvtav1 -b:v "$BITRATE" -c:a libopus -b:a 48k -ac 1 \
  -movflags +faststart "$OUTV"
echo "encoded: $OUTV ($(du -h "$OUTV" | cut -f1))"

# ---- 6. publish as a release asset ----------------------------------------
TAG="npl-$(date -u -d "@${START:-$(date +%s)}" +%Y%m%d)-$(echo "$KEY" | tail -c 7)"
TITLE="NPL ${SLUG} — $(date -u -d "@${START:-$(date +%s)}" +%d %b %Y)"
gh release create "$TAG" "$OUTV" \
  --title "$TITLE" \
  --notes "Auto-recorded from the live feed. AV1 480p, ${BITRATE}." \
  --latest 2>&1 | tail -2

echo "done: $TAG"
