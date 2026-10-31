#!/bin/bash
# Keep director/live/current pointed at the match on air, so the broadcast page has
# ONE stable URL across games instead of /match3, /match8, …
#
# Choosing by file mtime is wrong: rebuilding an old match (build --out match3)
# touches its script and would steal the pointer. The poller, however, appends to
# <match>/poll.log every few seconds for the match it is recording, so that is the
# live signal. When nothing is being polled, fall back to the highest match number
# that has a script (the most recent game that can still be played).
set -u
cd "$(dirname "$0")/../live" || exit 1

best=""
for d in match*/; do
  d="${d%/}"
  [ -f "$d/poll.log" ] || continue
  [ -f "$d/script.txt" ] || continue          # polled but nothing to play yet
  if [ -n "$(find "$d/poll.log" -newermt '-5 minutes' 2>/dev/null)" ]; then
    best="$d"
    break
  fi
done

if [ -z "$best" ]; then
  highest=-1
  for d in match*/; do
    d="${d%/}"
    [ -f "$d/script.txt" ] || continue
    n="${d#match}"
    case "$n" in ''*[!0-9]*) continue ;; esac
    if [ "$n" -gt "$highest" ]; then highest="$n"; best="$d"; fi
  done
fi

[ -z "$best" ] && exit 0

now=$(readlink current 2>/dev/null || true)
if [ "$now" != "$best" ]; then
  ln -sfn "$best" current
  echo "$(date -u +%FT%TZ) current -> $best"
fi
