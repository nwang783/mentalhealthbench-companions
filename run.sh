#!/usr/bin/env bash
# One command to run the benchmark. Extra flags pass through, e.g. ./run.sh --mock
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

if [ ! -f .env ] && [[ " $* " != *" --mock "* ]]; then
  cp .env.example .env
  echo "Created .env. Put your two API keys in it, then run this again."
  exit 1
fi

exec "$PY" -m mhb "$@"
