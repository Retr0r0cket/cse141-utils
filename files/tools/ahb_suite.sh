#!/usr/bin/env bash
# The AHB-Lite suite (just ahb-suite): every test program on the whole system with random wait
# states in memory, under testbench/ahb_lite_tb.sv's protocol checker, and checked as
# `just check-program` checks it.
#
#   tools/ahb_suite.sh [--waits "0 25 50 75"] [--seeds N] [--groups "level0 ... submit"]
#                      [--jobs N] [--netlist]
#
# Each program runs once with no waits, then --seeds times (default 2, with different
# +MEM_WAIT_SEEDs) at each nonzero percentage in --waits: each data phase cycle in memory
# waits with that chance (ahb_mem's +MEM_WAITS), so data phases of 1 to many cycles, held
# address phases, and held store data all come up. A run passes when the checker finds no
# violation and the program's outputs (.pcs, .after.dmem, .bus) are right. Cycle counts
# change with waits, so a .led_on (when the LEDs turn on, in cycles) is only checked
# without waits.
#
# --netlist runs the synthesized core instead (build/synth-fpga/, from
# `just test-programs-synth-mac` or `just _build-synth`). Output: build/ahb-suite/.

set -euo pipefail
cd "$(dirname "$0")/.."

waits="0 25 50 75" seeds=2 jobs=$(sysctl -n hw.ncpu 2>/dev/null || nproc) netlist=0
groups="level0 level1 level2 level3 level4 level5 c custom stress submit"

# One run: <simulator> <program dir> <waits> <seed> <output dir>
if [ "${1:-}" = --one ]; then
  sim=$2 dir=$3 w=$4 s=$5 out=$6 root=$PWD
  name=$(basename "$dir" | sed -E 's/^[0-9]+_//')
  program=${dir#test_programs/}
  mkdir -p "$out"
  ( cd "$out" && "$(just --evaluate _vvp)" "$sim" "+TEST=$root/$dir/$name" \
      +MEM_WAITS="$w" +MEM_WAIT_SEED="$s" +MAX_CYCLES=1000000 ) > "$out/sim.log" 2>&1 || true
  just check-program "$program" "$out" > "$out/check.log" 2>&1 || true
  ok=1
  grep -q '^AHB-LITE PASSED' "$out/sim.log" || ok=0
  if [ "$w" = 0 ]; then
    grep -q '^FAIL  ' "$out/check.log" && ok=0
  else
    grep '^FAIL  ' "$out/check.log" | grep -qv '\.led_on' && ok=0
  fi
  summary=$(grep -o '^AHB-LITE [A-Z]*: .*' "$out/sim.log" | sed -E 's/^AHB-LITE [A-Z]+: //' || echo 'no summary: see sim.log')
  if [ $ok = 1 ]; then
    printf 'PASS  %-34s waits %2s%% seed %s  %s\n' "$program" "$w" "$s" "$summary"
  else
    printf 'FAIL  %-34s waits %2s%% seed %s  (%s)\n' "$program" "$w" "$s" "$out"
  fi
  exit 0
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --waits)   waits=$2; shift ;;
    --seeds)   seeds=$2; shift ;;
    --groups)  groups=$2; shift ;;
    --jobs)    jobs=$2; shift ;;
    --netlist) netlist=1 ;;
    *) echo "usage: $0 [--waits \"0 25 50 75\"] [--seeds N] [--groups \"...\"] [--jobs N] [--netlist]" >&2
       exit 2 ;;
  esac
  shift
done

build=build/ahb-suite/$([ $netlist = 1 ] && echo netlist || echo rtl)
rm -rf "$build"
mkdir -p "$build/bin"
iverilog=$(just --evaluate _iverilog)
defines=$(just --evaluate _config_defines)
packages=$(just --evaluate _packages)
rtl=$(just --evaluate _rtl)
if [ $netlist = 1 ]; then
  synth=build/synth-fpga
  for f in "$synth/cells_sim.v" "$synth/1_synth_netlist.v"; do
    [ -f "$f" ] || { echo "no $f: run just test-programs-synth-mac (or just _build-synth) first" >&2; exit 1; }
  done
  # The netlist in place of rtl/cpu/ and the core's own folders; the rest of the system as RTL
  sources="\"$synth/cells_sim.v\" \"$synth/1_synth_netlist.v\" rtl/ahb/ahb_mem.sv rtl/ahb/ahb_interconnect.sv rtl/ahb/ahb_to_apb.sv rtl/apb/led_apb.sv rtl/apb/cycle_counter.sv rtl/top_level.sv"
  flags=-DGATE_LEVEL_SIM
else
  sources=$rtl
  flags=
fi

# A simulator per set of memory sizes (they're parameters, fixed when it's built)
simulator() {
  local params=$1 key
  key=$(echo "default $params" | sed 's/-Pprogram_tb\.//g; s/[ =]/_/g')
  if [ ! -f "$build/bin/$key" ]; then
    eval "\"$iverilog\" -o \"$build/bin/$key\" -g2012 -s ahb_lite_tb $flags \
      $(echo "$params" | sed 's/-Pprogram_tb\./-Pahb_lite_tb./g') \"$defines\" $packages $sources \
      testbench/program_tb.sv testbench/ahb_lite_tb.sv" >&2
  fi
  echo "$PWD/$build/bin/$key"
}

runs=()
for g in $groups; do
  for dir in test_programs/$g/*/; do
    dir=${dir%/}
    name=$(basename "$dir" | sed -E 's/^[0-9]+_//')
    [ -f "$dir/$name.imem" ] || continue
    sim=$(simulator "$(just _mem-sizes "${dir#test_programs/}")")
    for w in $waits; do
      if [ "$w" = 0 ]; then list=1; else list=$(seq 1 "$seeds"); fi
      for s in $list; do
        runs+=("$sim" "$dir" "$w" "$s" "$build/$(basename "$g")/$(basename "$dir")/w${w}s$s")
      done
    done
  done
done

echo "AHB-Lite suite ($([ $netlist = 1 ] && echo netlist || echo RTL)): $(( ${#runs[@]} / 5 )) runs, waits {$waits}%, $seeds seeds each, $jobs at a time"
printf '%s\0' "${runs[@]}" | xargs -0 -n 5 -P "$jobs" "$0" --one | tee "$build/summary.log"

passed=$(grep -c '^PASS' "$build/summary.log" || true)
failed=$(grep -c '^FAIL' "$build/summary.log" || true)
echo
echo "AHB-Lite suite: $passed passed, $failed failed (logs in $build/)"
[ "$failed" = 0 ]
