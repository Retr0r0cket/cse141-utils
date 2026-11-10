# Utility scripts and programs

Our recipes are in `student.just`, which the course's `justfile` imports. Our scripts are in
`tools/`. `scripts/` is the course's and stays unchanged; our tools load its functions rather
than copying them.

## Recipes at a glance

| Recipe | Script | What it does |
|---|---|---|
| `just score [...]` | `tools/score.py` | The challenge score, area × (cycles × period)² |
| `just fmax-parallel SEEDS` or `--time T` | `tools/fmax_parallel.py` | `just fmax`, with place-and-route seeds in parallel (on this machine) |
| `just fmax-parallel-official SEEDS` or `--time T` | `tools/fmax_official.sh` | The same, in an x86-64 Linux container: the official build's seeds and periods |
| `just synthesize-mac` | `tools/area_mac.sh` | Synthesis and area where yosys has no Tcl (this Mac) |
| `just test-programs-synth-mac` | (course recipes) | Every program on the synthesized netlist |
| `just test-program-synth-mac P` | (course recipes) | One program on the netlist |
| `just area-modules` | `tools/area_by_module.sh` | Area per module (hierarchy kept) |
| `just branch-stats` | `tools/branch_stats.py` | Branch and memory statistics of the public programs |
| `just test-all [...]` | `tools/test_all.sh` | The whole test suite (see [testing.md](testing.md)) |
| `just gen-stress [--check]` | `tools/gen_stress.py` | Generate (or check) `test_programs/stress/` |
| `just ahb-suite [...]` | `tools/ahb_suite.sh` | Every program with memory waits, under the AHB-Lite checker (see [testing.md](testing.md)) |
| `just diagram` | Graphviz `dot` | Render `block_diagram.dot` to SVG and PDF |
| `just diagram-check` | `tools/check_diagram.py`, `tools/check_wiring.py` | Check the diagram's signal names, widths and wires against `rtl/` |

## Shared with other repos (cse141-utils)

These tools live in their own repo, `cse141-utils`, next to this one. They're shared with other
project repos: `student.just`, `tools/`, the shared testbenches, `test_programs/custom` and `stress`
(not `submit`: each week submits its own), and this file and `testing.md`. This repo keeps its own committed copies. A
post-commit hook copies every shared file a commit changes, or adds, back to `cse141-utils`, and
commits and pushes it there. Pulling changes made elsewhere is optional:

| Recipe | What it does |
|---|---|
| `just utils-pull [--force]` | Copy the shared files in from `cse141-utils`. One changed here since it last synced is kept, unless `--force` |
| `just utils-push [--force]` | Share this repo's committed changes now (the hook does it on every commit) |
| `just utils-status` | Every shared file that differs, and how |

For a new repo, run `../cse141-utils/setup.sh install` there, then commit. See `cse141-utils`'s
README.

## Measuring

### `tools/score.py` (`just score`)

```
score.py [--period NS] [--area N] [--cycles N] [--groups level0,...] [--compare SCORE]
         [--tests | --test-all] [--synth] [--fmax SEEDS] [--jobs N] [--all] [--official]
```

Score = area × time², with time = total cycles × clock period, in µs². Lower is better. By
default it reads the latest results:

- **area** from `build/synth-fpga/1_synth_stat.json`, counted by the course's `scripts/area.py`,
- **cycles** from the `STATS` lines of the level 0–5 logs under `build/sim/`,
- **period** from `build/fmax-parallel/result.json`, or with `--official` from
  `build/fmax-parallel-official/result.json` (`just fmax-parallel-official`). Only the official
  one predicts the graded period: see below.

`--period`, `--area` and `--cycles` substitute a value ("what would this score?").
`--compare` shows the change against another score. To bring the inputs up to date first:

- `--tests` or `--test-all` reruns the cycles,
- `--synth` reruns the area,
- `--fmax SEEDS` (with `--jobs`) reruns the period; with `--official`, in the Linux container,
- `--all` does all three, with 24 seeds.

It stops if a step fails.

### `tools/fmax_parallel.py` (`just fmax-parallel`)

```
fmax_parallel.py SEEDS [--jobs N] [--build DIR] [--top K]
fmax_parallel.py --time DURATION [--from S] [--jobs N] [--build DIR] [--top K]
```

It synthesizes once, then runs nextpnr for every seed, `--jobs` at a time (the default is every
CPU). It prints each seed's estimate as it finishes, then the best seed's slowest `--top` paths.
SEEDS is a count (`24`), a range (`25-100`) or a list (`4,7,19`).

`--time` tries as many seeds as fit in DURATION (`90s`, `20m`, `1h30m`), from seed `--from`
(default 1) up, keeping `--jobs` running. No seed starts after the time is up; the ones running
then finish, so it runs over by up to one seed's time. Each seed's timing data is about 3 MB, so
in this mode only the best seed's `.sdf` and log are kept.

The estimate is `scripts/fmax.py`'s own: its functions are loaded rather than copied, so the
numbers match `just fmax-seed` for the same seed. The best result goes to `result.json` in the
build folder, for `score.py`, with a hash of the synthesized netlist. A later run on the same
netlist keeps the earlier best if it was better, and adds up the seeds tried; a changed netlist
starts over.

### `tools/fmax_official.sh` (`just fmax-parallel-official`)

```
just fmax-parallel-official 48                  # seeds 1-48 (or a range, or a list)
just fmax-parallel-official --time 30m          # as many seeds as fit in 30 minutes
just fmax-parallel-official --time 1h --from 200
just score --official
```

Place and route depends on the OS and CPU architecture: the same seed lands somewhere else on a
Mac, and even synthesis gives a different netlist there. The official build is x86-64 Linux, so
this runs `fmax_parallel.py` in a `linux/amd64` Docker container with the course's pinned tools
(`tools/docker/Dockerfile`: Ubuntu 24.04, oss-cad-suite 2026-10-12 and just 1.58.0, the same
downloads as `.github/workflows/fmax.yml`). The repository is mounted into the container, so it
measures the current RTL; output goes to `build/fmax-parallel-official/`.

- Docker Desktop has to be running (`open -a Docker`). The image is built on the first run, in
  about 4 minutes, and reused after that.
- On an Arm Mac it runs under emulation: synthesis takes about 4 s, and a batch of 12 seeds
  about 40–60 s.
- Each seed's line prints as it finishes. At the end it prints the best seed kept for this
  netlist (from any run) and the score `just score --official` gives with it.

Any change to the RTL's text can change the netlist, and with it every seed's result: renaming
local wires alone (same logic) once moved the official best from 12.42 to 13.27 ns. Rerun this
after every RTL edit before trusting the score, and put the best seed in `REPORT.md`
(`fmax seed: N`). A seed's number means nothing from one netlist to the next: seed 61 gave
15.43 ns on the renamed netlist and 12.25 ns, the best found, on the one we kept.

### `tools/area_mac.sh`, `tools/area_by_module.sh`

This Mac's yosys has no Tcl, so the course's `just synthesize`, `just area` and
`test-programs-synth` stop with an error here.

- **`area_mac.sh`** runs the same commands as `scripts/synth.tcl` through `yosys -p`, writing the
  same files into `build/synth-fpga/`. It then prints `scripts/area.py`'s count. The `*-mac`
  recipes reuse the course's netlist recipes after it.
- **`area_by_module.sh`** synthesizes with `-noflatten` and prints the LUT, flip-flop and ALU
  counts per module. Its total is higher than the flat synthesis's, so compare modules, not
  totals.

ABC mapping varies by about ±5% between small changes, so treat small area differences as noise.

### `tools/branch_stats.py` (`just branch-stats`)

For each program, this walks the `.pcs` trace against the `.imem` and reports:

- instructions, loads and stores (and back-to-back ones),
- conditional branches (taken, backward), and B, BL, BX and PC writes,
- how many conditional branches simple predictors would get wrong: never taken, BTFN, a per-branch
  counter, and a 16-entry 2-bit table.

We use it to judge whether a predictor would pay off before building one.

## Testing tools

### `tools/thumb_iss.py`: the golden model

```
thumb_iss.py <program> [--out DIR]       (<program> without .imem)
```

An instruction-set simulator for the in-scope ARMv6-M Thumb instructions, written from the
architecture's rules. It runs a program as the test bench does and writes `.pcs`, `.after.dmem`
and `.bus` in `check-program`'s formats:

- PC reads as + 4; ADR and LDR literal use `Align(PC, 4)`.
- BL is counted once, at its first halfword.
- The LED keeps 6 bits.
- `UDF #0` halts; any other UDF or SVC is an error.

It refuses anything it can't predict: reading SRAM nobody wrote, or reading the cycle counter. A
test built on it can't depend on timing. The one exception is opt-in: set `machine.counter` to a
function and COUNT reads return its value (`cleared_at` is the instruction count at the last
CLEAR). `gen_stress.py` does this for its `counter` program, which only checks ranges. The LEDs
answer anywhere in their 4 KB, as `led_apb` does, and a store to an unmapped peripheral address
is an error. Python code can use it as a library: `Machine`,
`load_program`, `write_outputs`.

### `tools/check_iss.py`

Runs the golden model on every course program that has expected files and compares. It exits 1
on any difference. The stress programs are left out, since their expected files come from the
model.

### `tools/thumb_asm.py`: a small assembler

`Asm` has labels, every in-scope instruction, and branch offsets resolved at the end (`B<cond>`
±256 B, B ±2 KiB, BL ±4 MiB). Helpers:

- `const(rd, value)` builds any 32-bit constant.
- `prologue()` sets r6 = SRAM base and r8–r12 = 8, 4, 2, 1, 0 for flag capture.
- `capture_flags(rd)` captures NZCV with branches over flag-neutral high-register ADD and MOV.
- `save(rt)` stores and advances r6.
- `trap()` emits `UDF #1`, an error the core doesn't halt on.
- `ldr_label` and `word_label` load addresses.

`imem()` writes the `.imem`, with a header comment.

### `tools/gen_stress.py` (`just gen-stress`)

Builds the 36 stress programs (listed in [testing.md](testing.md)) with `thumb_asm`. For each, it
writes:

- the `.imem`,
- a `.mem_sizes` (Code rounded up to a power of two of at least 1 KiB; SRAM 4 KiB),
- the expected files, by running the golden model.

`--check` builds into a temporary folder and exits 1 if `test_programs/stress/` differs. The
generator writes in place and rewrites only changed files, so iCloud sync doesn't leave copies
behind.

### `tools/fuzz_cores.py`

```
fuzz_cores.py --ref <repo>|iss [--dut <repo>] [--dut-exe SIM] [--count N] [--seed S]
              [--runs N] [--keep DIR]
```

Random differential programs (see [testing.md](testing.md)). It builds `program_tb` once per core,
or uses `--dut-exe`, such as the netlist's `build/synth-fpga/program_tb_synth`. It compares the PC
traces and the final SRAM. Failing programs and both outputs stay in `--keep`.

### `tools/ahb_suite.sh` (`just ahb-suite`)

```
ahb_suite.sh [--waits "0 25 50 75"] [--seeds N] [--groups "level0 ... submit"] [--jobs N] [--netlist]
```

Every test program on `testbench/ahb_lite_tb.sv` (`program_tb` inside a protocol checker), with
`ahb_mem`'s random waits, in parallel. It builds a simulator per set of memory sizes, and with
`--netlist` it builds them from the synthesized core in `build/synth-fpga/`. Each run's output is
in `build/ahb-suite/<rtl|netlist>/<group>/<program>/w<waits>s<seed>/` (`sim.log`, `check.log`),
with a summary line per run. It exits 1 if any run fails. See [testing.md](testing.md).

### `tools/test_all.sh` (`just test-all`)

The whole suite, with logs in `build/test-all/` and a summary; see [testing.md](testing.md).

## Generators for the LED programs

### `tools/gen_led_exact.py`

```
python3 tools/gen_led_exact.py test_programs/submit/02_ledExact/ledExact.imem
```

Assembles `ledExact`, which turns LED 5 on when COUNT is exactly 1000. The script runs once, on
our machine. The `.imem` it writes is what we submit, the same for every core: the program
adapts to each core at run time, by itself. It's graded on every
group's correct core and the course's baseline, so it measures the core it's on and assumes
nothing about its timing:

- One copy of the timed code runs every time. It clears COUNT, loops N times, enters a sled of
  harmless APB writes at entry m and a sled of NOPs at entry j, and ends with the timed store and
  a load of COUNT. Only registers change between runs: the store writes 0 to the LEDs while
  measuring, and 32 (LED 5) for real.
- With the store aimed at CLEAR, COUNT at the end gives the tail's length. So the program knows
  the COUNT at the end that means the store was at 1000. The one assumption is that a store to
  CLEAR takes as long as a store to the LEDs.
- Each measurement is the second of two identical runs (warm predictors). It measures the
  loop's period P and what 8 NOP entries cost. It then scans the NOP entries by arithmetic for
  each m, and measures only those that should fit.
- The real write is one turn of a loop with identical code between runs. LED 5 is written only
  after two runs in a row both end exactly on target.
- The APB-write sled gives a second step size, for a core where every NOP-and-loop time has
  the same parity (one fetching a word every 2 cycles, for example).

It was tested on 21 cores. All hit 1000 except two of the fetch-starved copies: one fetch every 3
cycles (CPI about 2.0) and one every 7 (CPI about 3.8), which run out of cycles while searching.
The cores:

- the 13 cores on our branches, from week 5's `main` to `area-buffer`, with and without
  prediction and a BTB,
- three copies of our core where decode waits 1–3 extra cycles per instruction,
- five where fetch waits 1–6 cycles after each fetch.

Each core finishes in 4,300–7,900 cycles, within `program_tb`'s default limit of 10,000.

## The block diagram

- **`block_diagram.yaml`** is the spec: every block, pipeline-register field, port and width,
  plus timing, connections and what changed since the last diagram. **`block_diagram.dot`** is
  the picture, drawn by hand in Graphviz (one HTML table per block, one port per signal) and kept
  in step with the YAML.
- **`just diagram`** renders the `.dot` to `block_diagram.svg` and `block_diagram.pdf`.
- **`just diagram-check`** runs two checkers:
  - **`tools/check_diagram.py`** checks that every `name [width]` in the `.dot` and the YAML is
    declared in `rtl/` with that width. Widths come from declarations and ports, with localparams,
    the packages' localparams and types (`ctrl_t`, `pc_t`, the enums) worked out from their
    definitions, and parameters taking the values they're instantiated with
    (`#(.BufferDepth(8))`). `--missing` lists RTL names the YAML never mentions.
  - **`tools/check_wiring.py`** reads `rtl/` into Yosys (slang), flattens it, and checks that every
    wire drawn in the `.dot` is a real connection: the same net, in the head's combinational
    fan-in, or loaded by the head when it's a register (one register at most). It runs twice: on
    `cpu_core` (the nodes outside it named with `--external`) and on `top_level`, which checks the
    bus too. Wires whose cells only say `[w]` (an unnamed D input) go unchecked.
- Both need PyYAML or Yosys and Graphviz, so they run with the system's `python3`; the course's
  `tabbypy3` lacks PyYAML. `check_wiring.py` finds `yosys` and `dot` on PATH or in
  `/opt/oss-cad-suite/latest/bin`.
- **`tools/diagram_from_yaml.py`** was an earlier way to draw the `.dot` from the YAML. Running it
  **overwrites the hand-drawn `block_diagram.dot`**, so don't, unless you mean to replace it.
- After an RTL change: run `just diagram-check` (stale names, widths and wires), then
  `python3 tools/check_diagram.py --missing` (new signals), update both files, and `just diagram`.

## GitHub workflows (`.github/workflows/`)

| Workflow | What it runs |
|---|---|
| `area.yml`, `fmax.yml` | The course's: the official area and fmax on Linux |

An untracked `fmax-parallel.yml` in the working tree is a near-copy of `fmax.yml` (it runs
`just fmax`). For official seeds, use `just fmax-parallel-official`.
