#!/bin/bash
# The real trigger: dispatch the record workflow when a match is actually on.
#
# Two signals, because neither alone is reliable:
#   - fixtures.is_live  : lags badly (stayed "not_started" through the whole of
#                         28 Oct's match, and flipped only after it ended)
#   - channel publishing: authoritative -- the CDN only serves a playlist when
#                         there is a feed. Costs 16 tiny GETs, so we only probe
#                         when a fixture is within an hour of its start time.
#
# Runs from launchd every 2 minutes on k2 (no github cron: it never fired).
set -u
REPO=${REPO:-krantiutils/npl}
API=https://ott-resources.geniustv.geniussystems.com.np/public/sports/v1/namespaces/-/tournaments
EDGE=https://ottnlive-fstream.nettvstreamz.com
WINDOW_MIN=${WINDOW_MIN:-45}

export PATH="$HOME/npl-ci/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
command -v node >/dev/null || { echo "node missing on PATH ($PATH)"; exit 1; }
command -v gh >/dev/null || { echo "gh missing on PATH ($PATH)"; exit 1; }
[ -f "$HOME/npl-ci/gh-token" ] && export GH_TOKEN=$(cat "$HOME/npl-ci/gh-token")

fixtures=$(curl -sS --max-time 15 "$API/fixtures" 2>/dev/null)

# "live" or "starting soon" -> worth probing the channels.
# Emits the match slug (e.g. KNY-vs-BK) so the guard below can be per-match:
# NPL runs two games a day and the day-wide guard would swallow the second one.
interest=$(printf '%s' "$fixtures" | WINDOW_MIN="$WINDOW_MIN" node -e '
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  try {
    const win = Number(process.env.WINDOW_MIN || 45) * 60000;
    const now = Date.now();
    const rows = JSON.parse(s).data || [];
    const hot = rows.filter((m) => {
      if (m.is_live || m.status === "started") return true;
      if (m.status !== "not_started") return false;
      const start = Number(m.start_at) * 1000;
      return Number.isFinite(start) && Math.abs(now - start) <= win;
    });
    if (hot.length) {
      const m = hot[0];
      process.stdout.write(`${m.team_a?.code || "?"}-vs-${m.team_b?.code || "?"}`);
    }
  } catch (e) {}
});')

[ -z "$interest" ] && exit 0

# is a feed actually publishing?
publishing=""
for base in live_hls/npl_match_ ottnpweb/npl_live; do
  case "$base" in
    live_hls/*) file=playlist.m3u8 ;;
    *)          file=index.m3u8 ;;
  esac
  for i in 1 2 3 4 5 6 7 8; do
    code=$(curl -s -o /tmp/npl-trigger.m3u8 -w '%{http_code}' --max-time 6 "$EDGE/$base$i/$file")
    # no pipe: `head | grep -q` trips pipefail/SIGPIPE
    magic=$(head -c 7 /tmp/npl-trigger.m3u8 2>/dev/null || true)
    if [ "$code" = "200" ] && [ "$magic" = "#EXTM3U" ]; then publishing="$base$i"; break 2; fi
  done
done

[ -z "$publishing" ] && exit 0


# Already published this match? Then stop. Matched on the release *title*, because
# NPL runs two games per day and a day-wide guard would skip the second one.
if gh release list --repo "$REPO" --limit 30 2>/dev/null | grep -q "$interest"; then
  exit 0
fi

# don't stack runs (the workflow also has a concurrency group)
state=$(gh run list --repo "$REPO" --workflow=record.yml --limit 1 --json status -q '.[0].status' 2>/dev/null || echo "")
case "$state" in
  queued|in_progress|requested|waiting|pending) exit 0 ;;
esac

gh workflow run record.yml --repo "$REPO" --ref main >/dev/null 2>&1 \
  && echo "$(date -u +%FT%TZ) dispatched for $publishing (prev state=${state:-none})"
