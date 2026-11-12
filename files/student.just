# Our own recipes. The course's justfile imports this file.

# tools/fmax_parallel.py: seeds are a count (24), a range (25-100), or a list
# (4,7,19); or --time 20m: as many seeds as fit in that long (90s, 20m, 1h30m;
# --from S starts at seed S). More flags: --jobs N, --build DIR, --top K.
#
# Estimate fmax like `just fmax`, running seeds in parallel (or for a time)
[group("Synthesis")]
[positional-arguments]
fmax-parallel seeds="2" *args:
  "{{_python}}" tools/fmax_parallel.py "$@"

# fmax-parallel on the official build's platform (x86-64 Linux, the course's pinned
# tools) in Docker, so the seeds are the ones the official build sees. Output in
# build/fmax-parallel-official/ (just score --official).
#
# Estimate fmax as the official build would: N seeds (48), or for a time (--time 30m)
[group("Synthesis")]
[positional-arguments]
fmax-parallel-official seeds="2" *args:
  tools/fmax_official.sh "$@"

_dot := require("dot")  # oss-cad-suite's own Graphviz

# Render block_diagram.dot to block_diagram.svg and block_diagram.pdf
[group("Docs")]
diagram:
  "{{_dot}}" -Tsvg block_diagram.dot -o block_diagram.svg
  "{{_dot}}" -Tpdf block_diagram.dot -o block_diagram.pdf

_python3 := require("python3")  # the system's: tabbypy3 has no PyYAML

# Check the block diagram against rtl/: names and widths, then wiring
[group("Docs")]
diagram-check:
  "{{_python3}}" tools/check_diagram.py
  "{{_python3}}" tools/check_wiring.py --external ext_bus --external ext_mem --external ext_bridge --external ext_led --external ext_counter --external out_leds
  "{{_python3}}" tools/check_wiring.py --top top_level -D TARGET_FPGA

# This Mac's Yosys has no Tcl, so `just synthesize` (and the recipes that
# need it: area, test-programs-synth) stop with an error here. These run the
# same synthesis (scripts/synth.tcl's commands) with yosys -p instead, into
# the same build/synth-fpga/ files, then reuse the course's recipes.

# Synthesize cpu_core into build/synth-fpga/ and print its area
[group("Mac")]
synthesize-mac:
  tools/area_mac.sh . build/synth-fpga

# Run every test program on the synthesized core (like test-programs-synth)
[group("Mac")]
test-programs-synth-mac: synthesize-mac
  just --no-deps _build-synth
  just --no-deps test-programs-synth

# Run one test program on the synthesized core (like test-program-synth)
[group("Mac")]
test-program-synth-mac program: synthesize-mac
  just --no-deps _build-synth
  just --no-deps test-program-synth {{program}}

# (Synthesis keeps the hierarchy here, so the total is higher than `just
# area`'s: compare modules, not totals.)
#
# Area per module
[group("Analysis")]
area-modules:
  tools/area_by_module.sh . build/area-modules

# Branch and memory statistics for every public test program
[group("Analysis")]
branch-stats:
  "{{_python3}}" tools/branch_stats.py test_programs/

# The hardware challenge's score, area x time^2, from the last synthesis,
# test-programs and fmax-parallel (or --area, --cycles, --period), or
# brought up to date first: --tests (or --test-all), --synth, --fmax SEEDS
# with --jobs N (cores), or --all (all three, 24 seeds); see tools/score.py
[group("Analysis")]
[positional-arguments]
score *args:
  "{{_python3}}" tools/score.py "$@"

# Everything that checks the core, with a summary: lint, the golden model,
# unit tests, every program (RTL, restarts, netlist), and random programs
# against the golden model. --quick skips the netlist; --fuzz N,
# --netlist-fuzz N, --seed S. See tools/test_all.sh
[group("Analysis")]
[positional-arguments]
test-all *args:
  tools/test_all.sh "$@"

# Waits in memory are 0, 25, 50, 75% by default: --waits "0 50", --seeds N,
# --groups "stress", --jobs N, --netlist (the synthesized core). See
# tools/ahb_suite.sh
#
# Every test program with memory waits, under an AHB-Lite protocol checker
[group("Analysis")]
[positional-arguments]
ahb-suite *args:
  tools/ahb_suite.sh "$@"

# Make test_programs/stress/ (directed programs with expected outputs from
# the golden model); `just gen-stress --check` checks it's up to date
[group("Generate")]
[positional-arguments]
gen-stress *args:
  "{{_python3}}" tools/gen_stress.py "$@"

# The shared tools (cse141-utils: tools/, the shared testbenches and test programs, this file)
# live in their own repo, next to this one unless `git config cse141-utils.path` says where.
# Committing here shares your changes to them (a post-commit hook); pulling others' is up to you.
_utils := `git config cse141-utils.path 2>/dev/null || echo ../cse141-utils`

# Copy the shared tools in from cse141-utils (keeps ones changed here; --force)
[group("Utils")]
[positional-arguments]
utils-pull *args:
  "{{_utils}}/141-utils-mgr.sh" install "$@" .

# Share this repo's committed changes to the shared tools (the hook does this on commit)
[group("Utils")]
[positional-arguments]
utils-push *args:
  "{{_utils}}/141-utils-mgr.sh" push "$@" .

# Which shared files differ between here and cse141-utils
[group("Utils")]
utils-status:
  "{{_utils}}/141-utils-mgr.sh" status .
