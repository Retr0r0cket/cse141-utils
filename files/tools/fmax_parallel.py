#!/usr/bin/env python3
"""`just fmax`, with the place-and-route seeds run in parallel.

    python3 tools/fmax_parallel.py SEEDS [--jobs N] [--build DIR] [--top K]
    python3 tools/fmax_parallel.py --time DURATION [--from S] [--jobs N] ...

SEEDS is a count (24: seeds 1 to 24), a range (25-100), or a list (4,7,19;
just seed 4 is 4-4 or 4,). Or --time: as many seeds as fit in DURATION (90,
90s, 20m, 1h30m), from seed S (default 1) up, N at a time; no seed starts
after the time is up, and the ones running then finish. Then only the best
seed's timing data is kept (each seed's is about 3 MB).
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
import hashlib
import json
import os
import re
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


def parse_duration(text):
    """'90' or '90s' -> 90; '20m' -> 1200; '1h30m' -> 5400 (seconds)."""
    m = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?', text.strip())
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(f'not a duration: {text!r} (e.g. 90s, 20m, 1h30m)')
    h, mins, secs = (int(g or 0) for g in m.groups())
    return h * 3600 + mins * 60 + secs


def sources():
    """The justfile's own list, in its order (what `just fmax` passes fmax.py): the defines,
    ahb_pkg, cpu_pkg, the other packages in rtl/cpu/pkg/, rtl/*.sv and rtl/*/*.sv (a folder
    inside one of those isn't compiled), then fmax_top."""
    rtl = os.path.join(ROOT, 'rtl')
    pkg_dir = os.path.join(rtl, 'cpu', 'pkg')
    cpu_pkg = os.path.join(pkg_dir, 'cpu_pkg.sv')
    others = [p for p in sorted(glob.glob(os.path.join(pkg_dir, '*.sv'))) if p != cpu_pkg]
    return ([os.path.join(rtl, 'config_defines.vh'), os.path.join(rtl, 'ahb', 'pkg', 'ahb_pkg.sv'),
             cpu_pkg] + others
            + sorted(glob.glob(os.path.join(rtl, '*.sv')))
            + sorted(glob.glob(os.path.join(rtl, '*', '*.sv')))
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


def timed(build, netlist, args):
    """--time: keep args.jobs seeds running, from args.first up, until the time is up; keep
    only the best one's timing data."""
    deadline = time.monotonic() + args.time
    print(f'Placing and routing seeds from {args.first} for {args.time} s, {args.jobs} at a time '
          '(the ones running when the time is up finish)...', flush=True)
    results, best = [], None
    next_seed = args.first
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        running = set()
        while True:
            while len(running) < args.jobs and time.monotonic() < deadline:
                running.add(pool.submit(place_and_route, build, next_seed, netlist))
                next_seed += 1
            if not running:
                break
            job = next(as_completed(running))
            running.remove(job)
            period, seed, needs = job.result()
            results.append((period, seed, needs))
            left = max(0, deadline - time.monotonic())
            mark = ''
            if best is None or (period, seed) < best:
                if best is not None:
                    drop(build, best[1])
                best, mark = (period, seed), '  (best so far)'
            else:
                drop(build, seed)
            print(f'  seed {seed:4d}: {period:6.2f} ns ({1000 / period:5.1f} MHz){mark}'
                  f'   [{len(results)} done, {left:.0f} s left]', flush=True)
    return results


def drop(build, seed):
    """A seed's timing data and log, once it isn't the best."""
    for name in (f'seed{seed}.sdf', f'nextpnr_seed{seed}.log'):
        try:
            os.remove(os.path.join(build, name))
        except FileNotFoundError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('seeds', nargs='?', help='a count (24), a range (25-100), or a list (4,7,19)')
    parser.add_argument('--time', type=parse_duration, metavar='DURATION',
                        help='instead of SEEDS: as many seeds as fit in this long (90s, 20m, 1h30m)')
    parser.add_argument('--from', dest='first', type=int, default=1, metavar='S',
                        help='with --time, the first seed (default 1)')
    parser.add_argument('--jobs', type=int, default=os.cpu_count(), help='nextpnr runs at once')
    parser.add_argument('--build', default=os.path.join(ROOT, 'build', 'fmax-parallel'))
    parser.add_argument('--top', type=int, default=8, help='slowest paths to print')
    args = parser.parse_args()
    if (args.seeds is None) == (args.time is None):
        parser.error('give SEEDS or --time DURATION (one of them)')
    seeds = parse_seeds(args.seeds) if args.seeds else None
    build = os.path.abspath(args.build)

    print('Synthesizing...', flush=True)
    start = time.monotonic()
    synthesize(build)
    print(f'  took {time.monotonic() - start:.0f} s', flush=True)
    netlist = fmax['netlist_cells'](os.path.join(build, 'fmax_top.json'))

    start = time.monotonic()
    results = []
    if seeds is not None:
        print(f'Placing and routing {len(seeds)} seed(s), {args.jobs} at a time...', flush=True)
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            jobs = [pool.submit(place_and_route, build, s, netlist) for s in seeds]
            for job in as_completed(jobs):
                period, seed, needs = job.result()
                results.append((period, seed, needs))
                print(f'  seed {seed:3d}: {period:6.2f} ns ({1000 / period:5.1f} MHz)', flush=True)
    else:
        results = timed(build, netlist, args)
    print(f'  took {time.monotonic() - start:.0f} s', flush=True)

    results.sort(key=lambda r: (r[0], r[1]))
    period, seed, needs = results[0]
    print()
    print('All seeds, fastest first: ' + ' '.join(f'{s}:{p:.2f}' for p, s, _ in results))
    print(f'Best of {len(results)}: {period:.2f} ns ({1000 / period:.1f} MHz), seed {seed}')
    print()
    # For tools/score.py: this run's best, or an earlier run's, if it was better and placed
    # and routed the same netlist (seeds tried so far on it add up)
    digest = hashlib.sha256(open(os.path.join(build, 'fmax_top.json'), 'rb').read()).hexdigest()
    result = {'period_ns': period, 'seed': seed, 'seeds_tried': len(results), 'netlist': digest}
    try:
        with open(os.path.join(build, 'result.json')) as f:
            earlier = json.load(f)
    except (OSError, ValueError):
        earlier = {}
    if earlier.get('netlist') == digest:
        result['seeds_tried'] += earlier['seeds_tried']
        if earlier['period_ns'] < period:
            result.update(period_ns=earlier['period_ns'], seed=earlier['seed'])
            print(f"An earlier run on this same netlist did better: seed {earlier['seed']}, "
                  f"{earlier['period_ns']:.2f} ns (kept for just score)")
            print()
    with open(os.path.join(build, 'result.json'), 'w') as f:
        json.dump(result, f)
    print('The slowest paths there, by the register (or memory) they end at, and the clock')
    print('period each one needs:')
    for end, (ns, begin) in sorted(needs.items(), key=lambda kv: -kv[1][0])[:args.top]:
        print(f'  {ns:6.2f} ns  {begin} -> {end}')
    print()
    print('Builds, logs, and timing data are in', build)


if __name__ == '__main__':
    main()
