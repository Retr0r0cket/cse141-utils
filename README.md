# cse141-utils

Utilities for ARMv6-M core being designed in CSE-141: tools, testbenches and test programs,
shared between project repos. Each repo keeps its own committed copies, so nothing it builds or
grades depends on this repo.

## Quick start

```sh
# In a project repo (this repo sits next to it, e.g. Project/cse141-utils):
../cse141-utils/setup.sh install
git add -A && git commit -m "Add cse141-utils"
just --list                  # the shared recipes are in student.just
```

`install` copies everything in `files/` to the same place in the project. For example,
`files/tools/score.py` goes to `tools/score.py`. It also adds a post-commit hook.

From then on:

- **Your changes are shared on commit.** After each commit, the hook copies every shared file
  that commit changed back into `files/` here. That includes new files where shared files live.
  It commits them here and pushes this repo, in the background. Only committed content goes up,
  not uncommitted edits.
- **Pulling others' changes is up to you.** Run `just utils-pull` (or `setup.sh install`) when
  you want them. A file you've changed locally is kept unless you pass `--force`.
- `just utils-status` lists every shared file that differs, and how.

## What's shared

| Path | What |
|---|---|
| `student.just` | The recipes (the course's `justfile` imports it): `score`, `fmax-parallel`, `fmax-parallel-official`, `test-all`, `ahb-suite`, `gen-stress`, `synthesize-mac` and the other `-mac` recipes, `area-modules`, `branch-stats`, `diagram`, `diagram-check`, `utils-*` |
| `tools/` | The golden model (`thumb_iss.py`), assembler (`thumb_asm.py`), stress generator, fuzzer, score, parallel and official (Docker) fmax, the AHB-Lite suite, `test_all.sh`, area tools, diagram checks, the LED-exact generator |
| `testbench/` | `ahb_lite_tb` (AHB-Lite protocol checker around `program_tb`), `peripherals_tb` (bus, memory, bridge, LEDs, counter), and the unit testbenches `alu_tb`, `fetch_tb`, `decode_tb` |
| `test_programs/` | `stress/` (generated, with expected outputs), `custom/`, `submit/` (the LED programs) |
| `docs/` | `testing.md`, `tools.md`: how all of the above works |

The course's own files are never shared or touched: `justfile`, `scripts/`, `rtl/`, `program_tb`,
`manager_tb`, `counter_apb_tb`, the workflows.

## Works in any repo

- **Course interfaces only.** Everything but the unit testbenches uses just what every group's
  repo has: `top_level`, `cpu_core`'s ports, the bus, the course's recipes and test-program
  formats.
- **Unit testbenches skip themselves.** `alu_tb`, `fetch_tb` and `decode_tb` test modules of one
  particular core (`arithmetic_logic_unit`, `fetch_stage`, `decode_stage`, ...). `test_all.sh`
  runs every `testbench/*_tb.sv`, and skips one whose module isn't in the repo's RTL rather than
  failing it.
- **Generated programs depend on the golden model, not on a core.** The stress programs'
  expected outputs come from `tools/thumb_iss.py`, so they're right for any correct core.
  `just gen-stress --check` confirms they're up to date.

## setup.sh

```
setup.sh install [--force] [--no-hook] [REPO]   copy the shared files into REPO (pull)
setup.sh push    [--force] [--no-remote] [REPO] copy REPO's committed changes back here
setup.sh status  [REPO]                         what differs, file by file
```

REPO defaults to the repo you're in. What each repo last synced is kept in
`REPO/.git/cse141-utils/lock`, a sha256 and a path per file. That's how `install` tells a file
you changed from one that's just older, and how `push` tells a change of yours from one that came
from another repo.

- **A file changed in both places since it last synced** isn't overwritten either way. `status`
  says "changed in both". Settle it with `install --force` (take this repo's) or `push --force`
  (take yours).
- **Turning off the automatic push to the remote:** `git config cse141-utils.autopush false` in
  the project. Commits here still happen.
- **A different location for this repo:** `install` records its path in the project's git config
  (`cse141-utils.path`). The recipes and the hook use that.
- **Deleting a shared file** isn't propagated. Remove it from `files/` here too.
