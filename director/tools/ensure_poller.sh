#!/bin/bash
# Make sure the Director poller is running for the match that is about to start.
#
# The GitHub Action records the video; this is the other half -- the ball-by-ball
# poller that feeds the game. Without it the channel has nothing to play even when
# the feed is up.
#
# Runs from launchd (or a loop) every minute. Starts the poller when a match is
# within START_WINDOW minutes of its start, and never starts a second one.
set -u
export PATH="$HOME/npl-ci/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/../.." || exit 1     # director/tools -> repo root

SEASON=2026
START_WINDOW=${START_WINDOW:-90}
API=https://nplt20league.com/api/v1
PY=${PY:-/tmp/npl26-orch/venv/bin/python}
LOG=/tmp/npl26-orch/ensure-poller.log

[ -x "$PY" ] || PY=python3

fixtures=$(curl -sS --max-time 15 "$API/matches?season=$SEASON" 2>/dev/null) || exit 0

next=$(printf '%s' "$fixtures" | START_WINDOW="$START_WINDOW" node -e '
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  try {
    const win = Number(process.env.START_WINDOW || 90) * 60000;
    const now = Date.now();
    const rows = (JSON.parse(s).data || []).filter((m) => m.status === "start_in");
    const soon = rows
      .map((m) => ({ m, t: Date.parse(m.playedAt) }))
      .filter((x) => Number.isFinite(x.t) && x.t - now <= win && now - x.t <= 3 * 3600 * 1000)
      .sort((a, b) => a.t - b.t)[0];
    if (!soon) return;
    process.stdout.write(`${soon.m.matchNumber}|${(soon.m.playedAt || "").slice(0, 10)}`);
  } catch (e) {}
});')

[ -z "$next" ] && exit 0
IFS='|' read -r NUMBER DATE <<<"$next"
N="${NUMBER#Match }"
DIR="director/live/match$N"

# already running for this match?
if pgrep -f "live_npl.py poll" >/dev/null; then exit 0; fi

mkdir -p "$DIR"
echo "$(date -u +%FT%TZ) starting poller for $NUMBER ($DATE) -> $DIR" >> "$LOG"
nohup "$PY" director/tools/live_npl.py poll --match "$N" --out "$DIR/" >> "$LOG" 2>&1 &
