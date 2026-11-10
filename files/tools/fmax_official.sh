#!/usr/bin/env bash
# fmax-parallel on the official build's platform: x86-64 Linux with the course's pinned tools,
# in Docker (just fmax-parallel-official).
#
#   tools/fmax_official.sh SEEDS [--jobs N] [--top K]
#   tools/fmax_official.sh --time DURATION [--from S] [--jobs N] [--top K]
#
# SEEDS is a count (48), a range (49-100) or a list (4,7,19); --time tries as many seeds
# as fit in DURATION (90s, 20m, 1h30m), from seed S up (see tools/fmax_parallel.py).
#
# nextpnr's placement for a given seed differs by OS and CPU architecture, so a seed's
# period from a Mac doesn't carry over; the official build is Linux x86-64 (GitHub Actions).
# This runs tools/fmax_parallel.py in a linux/amd64 container (tools/docker/Dockerfile;
# built the first time, then reused), so the seeds and periods are the official ones.
# Output goes to build/fmax-parallel-official/ (result.json: `just score --official`).
#
# On an Arm Mac the container runs under emulation: slower than a native run per seed.

set -euo pipefail
shopt -s inherit_errexit 2>/dev/null || true
cd "$(dirname "$0")/.."

image=cse141-tools:oss-cad-suite-20261012
build=build/fmax-parallel-official

if [ $# -lt 1 ]; then
  echo "usage: $0 SEEDS | --time DURATION [--from S]  [--jobs N] [--top K]" >&2
  exit 2
fi

if ! docker info > /dev/null 2>&1; then
  echo "Docker isn't running: start Docker Desktop (open -a Docker), then try again." >&2
  exit 1
fi

if ! docker image inspect "$image" > /dev/null 2>&1; then
  echo "Building $image (once: downloads the course's tools, about 1 GB)"
  docker build --platform linux/amd64 -t "$image" tools/docker
fi

# The repository is mounted at /work, so the container sees the current RTL and writes its
# build here; it runs as you, so what it writes is yours
docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/work" -w /work \
  "$image" \
  tabbypy3 tools/fmax_parallel.py "$@" --build "/work/$build" \
  | sed "s|/work/|$PWD/|g"

# What just score --official now uses: the best seed on this netlist, from any run, and
# the score with it (if there are a synthesis and test-program logs to go with it)
echo
python3 - "$build/result.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print(f"Official best on this netlist: seed {r['seed']}, {r['period_ns']:.2f} ns "
      f"({1000 / r['period_ns']:.1f} MHz), of {r['seeds_tried']} seeds tried")
PY
python3 tools/score.py --official 2>&1 | grep -E '^(Area|Cycles|Period|Score|No )' || true
