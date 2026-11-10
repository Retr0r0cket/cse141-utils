#!/usr/bin/env bash
# Everything that checks the core, in one run, with a summary at the end (just test-all).
#
#   tools/test_all.sh [--quick] [--fuzz N] [--netlist-fuzz N] [--seed S]
#
#   1. lint                     verilator -Wall over the core
#   2. golden model             tools/thumb_iss.py against the course's own expected files
#   3. stress up to date        test_programs/stress/ is what tools/gen_stress.py makes
#   4. unit tests               every testbench/*_tb.sv but program_tb and ahb_lite_tb, e.g.
#                               alu_tb.sv (the ALU against ARM's definitions), fetch_tb.sv,
#                               decode_tb.sv, peripherals_tb.sv, and the course's manager_tb
#                               and counter_apb_tb. One for a module this repo doesn't have
#                               is skipped, so the shared testbenches work in any repo.
#   5. test programs            every program in RTL (course, custom, stress), within budget
#   6. reset and rerun          every program twice, with a reset between
#   7. AHB-Lite (RTL)           tools/ahb_suite.sh: every program with memory waits, under
#                               testbench/ahb_lite_tb.sv's protocol checker
#   8. random programs (RTL)    tools/fuzz_cores.py against the golden model
#   9. netlist                  every program on the synthesized core
#  10. AHB-Lite (netlist)       the same as 7, on the synthesized core
#  11. random programs (netlist) the same as 8, on the netlist's simulator
#
# --quick skips 9 to 11 (synthesis takes a while). Exits 1 if anything fails.

set -u
cd "$(dirname "$0")/.."

quick=0 fuzz=500 netfuzz=200 seed=1
while [ $# -gt 0 ]; do
  case "$1" in
    --quick)        quick=1 ;;
    --fuzz)         fuzz=$2; shift ;;
    --netlist-fuzz) netfuzz=$2; shift ;;
    --seed)         seed=$2; shift ;;
    *) echo "usage: $0 [--quick] [--fuzz N] [--netlist-fuzz N] [--seed S]" >&2; exit 2 ;;
  esac
  shift
done

log_dir=build/test-all
mkdir -p "$log_dir"
results=()
status=0

# step <name> <log> <command...>: run it, keep its output in the log, note PASS/FAIL
step() {
  local name=$1 log="$log_dir/$2"; shift 2
  local start=$SECONDS
  printf '%-28s ' "$name"
  if "$@" > "$log" 2>&1; then
    printf 'PASS  (%ds)\n' $((SECONDS - start))
    results+=("PASS  $name")
  else
    printf 'FAIL  (%ds; see %s)\n' $((SECONDS - start)) "$log"
    results+=("FAIL  $name  ($log)")
    status=1
  fi
}

lint() { just lint cpu_core; }

# Every testbench/*_tb.sv, but program_tb (the test programs use it) and ahb_lite_tb (the
# AHB-Lite step runs it). A testbench passes when it prints PASSED (ours) or ALL TESTS PASSED
# (the course's). One that doesn't build because a module it tests isn't in this repo's RTL
# (iverilog's "Unknown module type") is skipped, not failed.
unit_tests() {
  local f tb missing fails=0 ran=0
  for f in testbench/*_tb.sv; do
    tb=$(basename "$f" .sv)
    case "$tb" in program_tb|ahb_lite_tb) continue ;; esac
    echo "== $tb"
    just test "$tb" 2>&1 | grep -v ': sorry: ' > "$log_dir/$tb.log"
    missing=$(grep -o 'Unknown module type: [A-Za-z0-9_]*' "$log_dir/$tb.log" | sed 's/.*: //' | sort -u | tr '\n' ' ')
    if [ -n "$missing" ]; then
      echo "SKIPPED: $tb (no ${missing% } in this repo)"
      continue
    fi
    tail -20 "$log_dir/$tb.log"
    ran=$((ran + 1))
    grep -qE '^PASSED|ALL TESTS PASSED' "$log_dir/$tb.log" || { echo "FAILED: $tb"; fails=$((fails + 1)); }
  done
  echo "unit tests: $ran run, $fails failed"
  [ "$fails" -eq 0 ]
}

test_programs() {
  local out
  out=$(just test-programs 2>&1)
  echo "$out"
  echo "$out" | grep -q 'All test programs passed' && echo "$out" | grep -q '(within budget)'
}

restarts() {
  local fails=0 n=0 d p
  for d in test_programs/*/[0-9]*_*/; do
    ls "$d"*.imem > /dev/null 2>&1 || continue
    p=${d#test_programs/}; p=${p%/}; n=$((n + 1))
    if just test-program-restart "$p" 2>&1 | grep -q '^PASSED'; then :; else
      echo "FAILED restart: $p"; fails=$((fails + 1))
    fi
  done
  echo "reset and rerun: $n programs, $fails failed"
  [ "$fails" -eq 0 ]
}

ahb_rtl() { tools/ahb_suite.sh --waits '0 50' --seeds 1; }
ahb_netlist() { tools/ahb_suite.sh --netlist --waits '0 50' --seeds 1; }

fuzz_rtl() { python3 tools/fuzz_cores.py --ref iss --count "$fuzz" --seed "$seed" --keep build/fuzz-rtl; }

netlist() {
  just synthesize-mac && just test-programs-synth-mac 2>&1 | tee /dev/stderr | grep -q 'All test programs passed on the synthesized core'
}

fuzz_netlist() {
  python3 tools/fuzz_cores.py --ref iss --dut-exe build/synth-fpga/program_tb_synth \
    --count "$netfuzz" --seed $((seed + 100000)) --keep build/fuzz-netlist
}

echo "Testing the core (logs in $log_dir/)"
step 'lint'                       lint.log          lint
step 'golden model'               golden.log        python3 tools/check_iss.py
step 'stress up to date'          stress-check.log  python3 tools/gen_stress.py --check
step 'unit tests'                 unit.log          unit_tests
step 'test programs (RTL)'        programs.log      test_programs
step 'reset and rerun'            restarts.log      restarts
step 'AHB-Lite, with waits (RTL)'  ahb-rtl.log       ahb_rtl
step "random programs x$fuzz (RTL)" fuzz-rtl.log   fuzz_rtl
if [ "$quick" -eq 0 ]; then
  step 'netlist (every program)'  netlist.log       netlist
  step 'AHB-Lite, with waits (netlist)' ahb-netlist.log ahb_netlist
  step "random programs x$netfuzz (netlist)" fuzz-netlist.log fuzz_netlist
fi

echo
echo "Summary:"
for r in "${results[@]}"; do echo "  $r"; done
grep -h 'Public programs:' "$log_dir/programs.log" 2>/dev/null | sed 's/^/  /'
if [ "$status" -eq 0 ]; then echo "ALL PASSED"; else echo "SOMETHING FAILED"; fi
exit $status
