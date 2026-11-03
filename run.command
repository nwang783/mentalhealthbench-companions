#!/usr/bin/env bash
# Double-click this file in Finder to run the benchmark in a Terminal window.
cd "$(dirname "$0")"
./run.sh || true
echo
read -n 1 -s -r -p "Done. Press any key to close."
