#!/bin/bash
# Serve the NPL page locally. ES modules need http://, not file://.
cd "$(dirname "$0")" || exit 1
PORT=${1:-8123}
echo "http://127.0.0.1:$PORT/"
exec python3 -m http.server "$PORT" --bind 127.0.0.1
