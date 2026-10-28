#!/bin/bash
# The real trigger: dispatch the record workflow when a match is actually live.
#
# GitHub's own cron is best-effort -- it had not fired a single scheduled run in
# 26 minutes on this repo -- so we poll here (every 2 min by crontab) and dispatch
# explicitly. The cron in the workflow stays as a slow fallback.
set -u
PATH=/usr/bin:/bin:/usr/local/bin
export PATH
cd "$(dirname "$0")/.." || exit 0

API=https://ott-resources.geniustv.geniussystems.com.np/public/sports/v1/namespaces/-/tournaments

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
state=$(gh run list --workflow=record.yml --limit 1 --json status -q '.[0].status' 2>/dev/null || echo "")
case "$state" in
  queued|in_progress|requested|waiting|pending) exit 0 ;;
esac

gh workflow run record.yml --ref main >/dev/null 2>&1 && echo "$(date -u +%FT%TZ) dispatched record.yml (prev state=${state:-none})"
