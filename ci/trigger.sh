#!/bin/bash
# The real trigger: dispatch the record workflow when a match is actually live.
#
# GitHub's own cron is best-effort (it had not fired a single scheduled run in
# 26 minutes on this repo), so this polls every 2 minutes from whatever host it
# runs on and dispatches explicitly. The cron inside the workflow stays as a slow
# fallback.
#
# Needs either an authenticated `gh` or GH_TOKEN. Path is repo-explicit so it can
# run from anywhere (no checkout required).
set -u
REPO=${REPO:-krantiutils/npl}
API=https://ott-resources.geniustv.geniussystems.com.np/public/sports/v1/namespaces/-/tournaments

# launchd/cron hand over a minimal PATH; the static toolchain lives in ~/npl-ci/bin
export PATH="$HOME/npl-ci/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
command -v node >/dev/null || { echo "node missing on PATH ($PATH)"; exit 1; }
command -v gh >/dev/null || { echo "gh missing on PATH ($PATH)"; exit 1; }

[ -f "$HOME/npl-ci/gh-token" ] && export GH_TOKEN=$(cat "$HOME/npl-ci/gh-token")

live=$(curl -sS --max-time 15 "$API/fixtures" 2>/dev/null | node -e '
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  try {
    const j = JSON.parse(s);
    if ((j.data || []).some((m) => m.is_live || m.status === "started")) process.stdout.write("1");
  } catch (e) {}
});')

[ -z "$live" ] && exit 0

# don't stack runs: the workflow also has a concurrency group, but asking first
# keeps the runner from churning through no-op jobs
state=$(gh run list --repo "$REPO" --workflow=record.yml --limit 1 --json status -q '.[0].status' 2>/dev/null || echo "")
case "$state" in
  queued|in_progress|requested|waiting|pending) exit 0 ;;
esac

gh workflow run record.yml --repo "$REPO" --ref main >/dev/null 2>&1 \
  && echo "$(date -u +%FT%TZ) dispatched record.yml (prev state=${state:-none})"
