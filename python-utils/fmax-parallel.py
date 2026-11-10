#!/usr/bin/env python3
"""`just fmax`, with the place-and-route seeds run in parallel.

    python3 tools/fmax_parallel.py SEEDS [--jobs N] [--build DIR] [--top K]

SEEDS is a count (24: seeds 1 to 24), a range (25-100), or a list (4,7,19;
just seed 4 is 4-4 or 4,).
Synthesizes once, then runs nextpnr for every seed at once (N at a time; the
default is the number of CPUs), and prints each seed's estimate as it finishes,
then the best seed's slowest paths as `just fmax` does.

The estimate is scripts/fmax.py's own: this file loads fmax.py's functions
(synthesis command, delay fixes, `predict`) rather than copying them, so the
numbers match `just fmax` / `just fmax-seed` for the same seed. scripts/ is the
course's and stays unchanged; this is only for searching faster. Run it from
the repository root. Output goes to build/fmax-parallel/ by default.
"""
import argparse
import glob
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FMAX = os.path.join(ROOT, 'scripts', 'fmax.py')

# fmax.py's functions, without running its main()
source = open(FMAX).read().rsplit('\nmain()', 1)[0]
fmax = {'__name__': 'fmax', '__file__': FMAX}
exec(compile(source, FMAX, 'exec'), fmax)


def parse_seeds(text):
    """'24' -> 1..24; '25-100' -> 25..100; '4,7,19' (or '4,10-12') -> those."""
    if ',' not in text and '-' not in text:
        return list(range(1, int(text) + 1))
    seeds = []
    for part in filter(None, text.split(',')):
        lo, _, hi = part.partition('-')
        seeds += range(int(lo), int(hi or lo) + 1)
    return seeds


def sources():
    rtl = os.path.join(ROOT, 'rtl')
    pkgs = sorted(glob.glob(os.path.join(rtl, 'pkg', '*.sv')))
    first = [p for p in pkgs if os.path.basename(p) == 'cpu_pkg.sv']
    return ([os.path.join(rtl, 'config_defines.vh')] + first + [p for p in pkgs if p not in first]
            + sorted(glob.glob(os.path.join(rtl, '*.sv')))
            + [os.path.join(ROOT, 'scripts', 'fmax_top.sv')])


def synthesize(build):
    """The same files and yosys run as fmax.py's main()."""
    os.makedirs(build, exist_ok=True)
    with open(os.path.join(build, 'code.hex'), 'w') as f:
        for i in range(fmax['CODE_WORDS']):
            f.write(f'{i * 2654435761 & 0xffffffff:08x}\n')
    with open(os.path.join(build, 'fmax_top.cst'), 'w') as f:
        for port, pin in fmax['PINS'].items():
            f.write(f'IO_LOC "{port}" {pin};\n')
    with open(os.path.join(build, 'sources.f'), 'w') as f:
        for s in sources():
            f.write(f'"{os.path.abspath(s)}"\n')
    fmax['run'](['yosys', '-q', '-l', 'yosys.log', '-p', '''
        plugin -i slang
        read_slang --single-unit -D TARGET_FPGA -D SYNTHESIS --top fmax_top -G CODE_INIT_FILE="code.hex" -f sources.f
        synth_gowin -family gw2a -top fmax_top
        tee -o stat.txt stat
        write_json fmax_top.json
    '''], 'yosys.out', build)


def place_and_route(build, seed, netlist):
    """-> (period, seed, needs) for one seed: fmax.py's nextpnr run and estimate."""
    result = subprocess.run(
        ['nextpnr-himbaechel', '--json', 'fmax_top.json', '--device', fmax['DEVICE'],
         '--vopt', f'family={fmax["FAMILY"]}', '--vopt', 'cst=fmax_top.cst', '--seed', str(seed),
         '--timing-allow-fail', '--sdf', f'seed{seed}.sdf'],
        cwd=build, stdout=open(os.path.join(build, f'nextpnr_seed{seed}.log'), 'w'),
        stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'nextpnr failed for seed {seed}: see {build}/nextpnr_seed{seed}.log')
    needs = fmax['predict'](os.path.join(build, f'seed{seed}.sdf'), netlist)
    return max(p for p, _ in needs.values()), seed, needs


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('seeds', help='a count (24), a range (25-100), or a list (4,7,19)')
    parser.add_argument('--jobs', type=int, default=os.cpu_count(), help='nextpnr runs at once')
    parser.add_argument('--build', default=os.path.join(ROOT, 'build', 'fmax-parallel'))
    parser.add_argument('--top', type=int, default=8, help='slowest paths to print')
    args = parser.parse_args()
    seeds = parse_seeds(args.seeds)
    build = os.path.abspath(args.build)

    print('Synthesizing...', flush=True)
    start = time.monotonic()
    synthesize(build)
    print(f'  took {time.monotonic() - start:.0f} s', flush=True)
    netlist = fmax['netlist_cells'](os.path.join(build, 'fmax_top.json'))

    print(f'Placing and routing {len(seeds)} seed(s), {args.jobs} at a time...', flush=True)
    start = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        jobs = [pool.submit(place_and_route, build, s, netlist) for s in seeds]
        for job in as_completed(jobs):
            period, seed, needs = job.result()
            results.append((period, seed, needs))
            print(f'  seed {seed:3d}: {period:6.2f} ns ({1000 / period:5.1f} MHz)', flush=True)
    print(f'  took {time.monotonic() - start:.0f} s', flush=True)

    results.sort(key=lambda r: (r[0], r[1]))
    period, seed, needs = results[0]
    print()
    print('All seeds, fastest first: ' + ' '.join(f'{s}:{p:.2f}' for p, s, _ in results))
    print(f'Best of {len(results)}: {period:.2f} ns ({1000 / period:.1f} MHz), seed {seed}')
    print()
    print('The slowest paths there, by the register (or memory) they end at, and the clock')
    print('period each one needs:')
    for end, (ns, begin) in sorted(needs.items(), key=lambda kv: -kv[1][0])[:args.top]:
        print(f'  {ns:6.2f} ns  {begin} -> {end}')
    print()
    print('Builds, logs, and timing data are in', build)


if __name__ == '__main__':
    main()
