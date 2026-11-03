#!/bin/bash
# Push code to the site WITHOUT clobbering live.json.
#
# live.json is owned by CI (ci/record.sh republishes it every capture round). It is
# tracked, so a plain `tar | ssh` from this machine overwrites the live pointer with
# whatever stale copy is sitting locally -- which is exactly how the site ended up
# showing "no feed" while the match was streaming.
set -euo pipefail
cd "$(dirname "$0")"
tar -cz --exclude='node_modules' --exclude='._*' --exclude='live.json' . \
  | ssh -o BatchMode=yes cdjk 'cd ~/npl && tar -xz && find . -name "._*" -delete && chmod +x ci/*.sh && git pull --rebase -q origin main && git add -A && git commit -q -m "'"${1:-site update}"'" && git push -q origin main && echo "pushed $(git rev-parse --short HEAD)"'
