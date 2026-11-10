#!/usr/bin/env python3
"""The hardware challenge's score for this core: area x time^2 (lower is better).

    score.py [--period NS] [--area N] [--cycles N] [--groups level0,...] [--compare SCORE]
             [--tests | --test-all] [--synth] [--fmax SEEDS] [--jobs N] [--all]

From the week 6 page (https://cseweb.ucsd.edu/classes/fa26/cse141-001/project-week6.html):

  - area: cpu_core's area from `just area` (LUTs, ALUs and flip-flops; DSPs and block RAM
    are free)
  - time: the total cycles across all the assembly test programs, times the clock period
  - score: area x time^2, so 10% faster counts about as much as 20% smaller

The real ranking uses the private programs too, and the clock period measured on a real board;
this uses what's here:

  - area from the last synthesis: build/synth-fpga/1_synth_stat.json (`just synthesize-mac`
    here, `just synthesize` on Linux), counted with scripts/area.py, the autograder's own
  - cycles from the last `just test-programs`: the STATS line of each assembly program's log
    under build/sim/ (levels 0 to 5; C programs and our own don't count)
  - the period from the last `just fmax-parallel` (build/fmax-parallel/result.json)

Any of them can be given instead (--period, --area, --cycles), e.g. to ask what a change would
score before making it: `just score --cycles 4900 --period 14.5`. --compare prints how a score
compares with this one.

Or brought up to date first, so the score is the current design's:

  --tests        run `just test-programs` (the cycles); stop if a program fails
  --test-all     run the whole suite (tools/test_all.sh) instead; stop if anything fails
  --synth        run `just synthesize-mac` (the area)
  --fmax SEEDS   run tools/fmax_parallel.py with these seeds (a count, 24; a range, 25-100;
                 or a list, 4,7,19) (the period)
  --jobs N       how many seeds fmax-parallel places and routes at once (default: every core)
  --official     the period from x86-64 Linux, the official build's platform
                 (build/fmax-parallel-official/, from `just fmax-parallel-official`); with
                 --fmax, run that instead of fmax-parallel here
  --all          --tests --synth --fmax 24, e.g. `just score --all --jobs 8`
"""

import argparse
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAT = os.path.join(ROOT, 'build', 'synth-fpga', '1_synth_stat.json')
SIM = os.path.join(ROOT, 'build', 'sim')
FMAX = os.path.join(ROOT, 'build', 'fmax-parallel', 'result.json')
FMAX_OFFICIAL = os.path.join(ROOT, 'build', 'fmax-parallel-official', 'result.json')
GROUPS = 'level0,level1,level2,level3,level4,level5'  # the public assembly programs


def area_from_stat(path):
    """The area by scripts/area.py's own count."""
    spec = importlib.util.spec_from_file_location('area', os.path.join(ROOT, 'scripts', 'area.py'))
    area = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(area)
    with open(path) as f:
        result = area.fpga_area(f.read())
    if result is None:
        sys.exit(f'{path} is not a synthesis stat file (did synthesis finish?)')
    return result['total']


def cycles_from_logs(groups):
    """-> {group: (programs, cycles)}, from each program's log's STATS line."""
    found = {}
    for group in groups:
        programs = cycles = 0
        for log in sorted(glob.glob(os.path.join(SIM, group, '[0-9]*_*.log'))):
            with open(log) as f:
                m = re.search(r'^STATS \d+ instructions?, (\d+) cycles', f.read(), re.M)
            if not m:
                sys.exit(f'{log} has no STATS line: did the program pass? (just test-programs)')
            programs += 1
            cycles += int(m.group(1))
        if programs == 0:
            sys.exit(f'No logs for {group} under {SIM}: run `just test-programs` first')
        found[group] = (programs, cycles)
    return found


def refresh(args):
    """Run what --tests, --test-all, --synth and --fmax ask for, in that order; stop on a failure."""
    def run(flag, cmd, ok=None):
        print(f'== --{flag}: {" ".join(cmd)}', flush=True)
        r = subprocess.run(cmd, cwd=ROOT, capture_output=ok is not None, text=True)
        if ok is not None:
            sys.stdout.write(r.stdout[-3000:])
        if r.returncode != 0 or (ok is not None and not ok(r.stdout)):
            sys.exit(f'--{flag} failed: no score (fix that first, or score without --{flag})')
    if args.test_all:
        run('test-all', [os.path.join(ROOT, 'tools', 'test_all.sh')])
    elif args.tests:
        run('tests', ['just', 'test-programs'], ok=lambda out: 'All test programs passed' in out)
    if args.synth:
        run('synth', ['just', 'synthesize-mac'])
    if args.fmax:
        if args.official:
            cmd = [os.path.join(ROOT, 'tools', 'fmax_official.sh'), args.fmax]
        else:
            # The interpreter `just fmax-parallel` uses (oss-cad-suite's), when there is one
            python = shutil.which('tabbypy3') or sys.executable
            cmd = [python, os.path.join(ROOT, 'tools', 'fmax_parallel.py'), args.fmax]
        if args.jobs:
            cmd += ['--jobs', str(args.jobs)]
        run('fmax', cmd)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--period', type=float, help='clock period, ns (default: the last fmax-parallel)')
    parser.add_argument('--area', type=int, help='area (default: the last synthesis)')
    parser.add_argument('--cycles', type=int, help='total cycles (default: the last test-programs)')
    parser.add_argument('--groups', default=GROUPS, help=f'program groups to count (default {GROUPS})')
    parser.add_argument('--compare', type=float, help='another score, to compare this one with')
    fresh = parser.add_argument_group('bring the inputs up to date first')
    tests = fresh.add_mutually_exclusive_group()
    tests.add_argument('--tests', action='store_true', help='run `just test-programs` (the cycles)')
    tests.add_argument('--test-all', action='store_true', help='run the whole suite (tools/test_all.sh)')
    fresh.add_argument('--synth', action='store_true', help='run `just synthesize-mac` (the area)')
    fresh.add_argument('--fmax', metavar='SEEDS', help='run fmax-parallel with these seeds (the period)')
    fresh.add_argument('--jobs', type=int, help='fmax-parallel runs at once (default: every core)')
    fresh.add_argument('--all', action='store_true', help='--tests --synth --fmax 24')
    parser.add_argument('--official', action='store_true',
                        help='the period from x86-64 Linux (just fmax-parallel-official)')
    args = parser.parse_args()
    if args.all:
        if not args.test_all:
            args.tests = True
        args.synth = True
        args.fmax = args.fmax or '24'
    if args.jobs is not None and not args.fmax:
        parser.error('--jobs is for --fmax (or --all)')
    if args.jobs is not None and args.jobs < 1:
        parser.error('--jobs must be at least 1')
    if (args.tests or args.test_all) and args.cycles is not None:
        parser.error('--tests and --test-all measure the cycles: leave out --cycles')
    if args.synth and args.area is not None:
        parser.error('--synth measures the area: leave out --area')
    if args.fmax and args.period is not None:
        parser.error('--fmax measures the period: leave out --period')
    refresh(args)

    if args.area is None:
        if not os.path.exists(STAT):
            sys.exit(f'No synthesis yet ({STAT}): run `just synthesize-mac` (or `just synthesize`)')
        area, area_from = area_from_stat(STAT), 'the last synthesis'
    else:
        area, area_from = args.area, 'given'

    if args.cycles is None:
        per_group = cycles_from_logs(args.groups.split(','))
        cycles = sum(c for _, c in per_group.values())
        programs = sum(p for p, _ in per_group.values())
        cycles_from = f'{programs} programs in {args.groups} (the last test-programs)'
    else:
        per_group, cycles, cycles_from = {}, args.cycles, 'given'

    if args.period is None:
        path = FMAX_OFFICIAL if args.official else FMAX
        recipe = 'fmax-parallel-official' if args.official else 'fmax-parallel'
        if not os.path.exists(path):
            sys.exit(f'No fmax result yet ({path}): run `just {recipe} <seeds>`, '
                     'or give one with --period NS')
        with open(path) as f:
            fmax = json.load(f)
        period = fmax['period_ns']
        period_from = f"the last {recipe} (seed {fmax['seed']} of {fmax['seeds_tried']})"
    else:
        period, period_from = args.period, 'given'

    time_us = cycles * period / 1000
    score = area * time_us ** 2

    print(f'Area:    {area:>10}            ({area_from})')
    for group, (p, c) in per_group.items():
        print(f'           {group:8} {c:>6} cycles ({p} programs)')
    print(f'Cycles:  {cycles:>10}            ({cycles_from})')
    print(f'Period:  {period:>10.2f} ns         ({period_from}; {1000 / period:.1f} MHz)')
    print(f'Time:    {time_us:>10.2f} us')
    print(f'Score:   {score:>10.0f}            (area x time^2, in us^2; lower is better)')
    if args.compare:
        change = score / args.compare - 1
        print(f'         {"better" if change < 0 else "worse"} than {args.compare:.0f} by '
              f'{abs(change) * 100:.1f}%')


if __name__ == '__main__':
    main()
