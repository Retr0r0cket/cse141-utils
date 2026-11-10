#!/usr/bin/env python3
"""Differential testing: random programs, run on two cores, which must agree exactly.

    fuzz_cores.py --ref <repo> [--dut <repo>] [--count N] [--seed S] [--keep DIR]

Each program is random but always halts: forward branches anywhere (conditional and not),
backward branches only as counted loops (nested two deep), BL calls to subroutines that return
with BX LR, loads and stores to a block of SRAM, and NOPs to land branches in either halfword
of a word (and BLs across two words). A halt (udf #0) follows every unconditional branch and
sits in every skipped stretch, so running anything off the right path stops the program early.
At the end every register is stored, so the final memory holds the whole machine state.

Both cores run every program in RTL simulation (program_tb, built once per core), or the DUT
on a simulator built already (--dut-exe: the synthesized netlist's, say). They must
agree on the PCs that reach execute (program_tb's trace, as `just check-program` reads it) and
on SRAM at the halt, and both must halt. The DUT defaults to this repository.
"""

import argparse
import os
import random
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = [0, 1, 2, 3, 4]          # registers the random code works on
R_INNER, R_OUTER, R_BASE = 7, 5, 6


# ----------------------------------------------------------------------------------------------
# Thumb encodings (only what the generator uses)

def movs_imm(rd, imm):  return 0x2000 | rd << 8 | imm
def adds_imm(rd, imm):  return 0x3000 | rd << 8 | imm
def subs_imm(rd, imm):  return 0x3800 | rd << 8 | imm
def cmp_imm(rn, imm):   return 0x2800 | rn << 8 | imm
def adds_reg(rd, rn, rm): return 0x1800 | rm << 6 | rn << 3 | rd
def subs_reg(rd, rn, rm): return 0x1A00 | rm << 6 | rn << 3 | rd
def dp(op, rdn, rm):    return 0x4000 | op << 6 | rm << 3 | rdn
def lsls_imm(rd, rm, n): return 0x0000 | n << 6 | rm << 3 | rd
def lsrs_imm(rd, rm, n): return 0x0800 | n << 6 | rm << 3 | rd
def asrs_imm(rd, rm, n): return 0x1000 | n << 6 | rm << 3 | rd
def str_imm(rt, rn, k): return 0x6000 | k << 6 | rn << 3 | rt
def ldr_imm(rt, rn, k): return 0x6800 | k << 6 | rn << 3 | rt
NOP, HALT, BX_LR = 0xBF00, 0xDE00, 0x4770
DP = {'ands': 0, 'eors': 1, 'lsls': 2, 'lsrs': 3, 'asrs': 4, 'adcs': 5, 'sbcs': 6, 'rors': 7,
      'tst': 8, 'rsbs': 9, 'cmp': 10, 'cmn': 11, 'orrs': 12, 'bics': 14, 'mvns': 15}


class Program:
    """Halfwords and labels, resolved into an .imem at the end."""

    def __init__(self):
        self.items = []     # ('h', value, text) | ('label', name) | ('b', cond, label) | ('bl', label)
        self.n = 0

    def label(self):
        self.n += 1
        return f'L{self.n}'

    def emit(self, value, text):
        self.items.append(('h', value, text))

    def place(self, name):
        self.items.append(('label', name))

    def branch(self, label, cond=None):
        self.items.append(('b', cond, label))

    def bl(self, label):
        self.items.append(('bl', label))

    def resolve(self):
        addr, labels = 0, {}
        for it in self.items:
            if it[0] == 'label':
                labels[it[1]] = addr
            else:
                addr += 4 if it[0] == 'bl' else 2
        out, addr = [], 0
        for it in self.items:
            if it[0] == 'label':
                out.append((None, None, f'{it[1]}:'))
            elif it[0] == 'h':
                out.append((addr, it[1], it[2]))
                addr += 2
            elif it[0] == 'b':
                cond, target = it[1], labels[it[2]]
                off = (target - (addr + 4)) // 2
                if cond is None:
                    assert -1024 <= off < 1024
                    out.append((addr, 0xE000 | (off & 0x7FF), f'b      {it[2]}'))
                else:
                    assert -128 <= off < 128, (addr, target)
                    out.append((addr, 0xD000 | cond << 8 | (off & 0xFF), f'b{COND[cond]}    {it[2]}'))
                addr += 2
            else:
                target = labels[it[1]]
                off = target - (addr + 4)
                s = (off >> 24) & 1
                i1, i2 = (off >> 23) & 1, (off >> 22) & 1
                imm10, imm11 = (off >> 12) & 0x3FF, (off >> 1) & 0x7FF
                j1, j2 = (1 - i1) ^ s, (1 - i2) ^ s
                out.append((addr, 0xF000 | s << 10 | imm10, f'bl     {it[1]}'))
                out.append((addr + 2, 0xD000 | j1 << 13 | j2 << 11 | imm11, '(bl, second half)'))
                addr += 4
        return out

    def imem(self, header):
        lines = [f'// {line}' for line in header] + ['@00000000']
        for addr, value, text in self.resolve():
            if addr is None:
                lines.append(f'// {text}')
            else:
                lines.append(f'{value & 0xFF:02X} {value >> 8:02X}        // {addr:04x}: {text}')
        return '\n'.join(lines) + '\n'


COND = ['eq', 'ne', 'cs', 'cc', 'mi', 'pl', 'vs', 'vc', 'hi', 'ls', 'ge', 'lt', 'gt', 'le']


# ----------------------------------------------------------------------------------------------
# The generator

def gen_op(p, rng, data=DATA):
    """One random instruction on the data registers (sets flags, mostly)."""
    rd, rn, rm = rng.choice(data), rng.choice(data), rng.choice(data)
    kind = rng.randrange(12)
    if kind == 0:   p.emit(movs_imm(rd, rng.randrange(256)), f'movs   r{rd}, #...')
    elif kind == 1: p.emit(adds_imm(rd, rng.randrange(256)), f'adds   r{rd}, #...')
    elif kind == 2: p.emit(subs_imm(rd, rng.randrange(256)), f'subs   r{rd}, #...')
    elif kind == 3: p.emit(adds_reg(rd, rn, rm), f'adds   r{rd}, r{rn}, r{rm}')
    elif kind == 4: p.emit(subs_reg(rd, rn, rm), f'subs   r{rd}, r{rn}, r{rm}')
    elif kind == 5:
        name = rng.choice(['ands', 'eors', 'orrs', 'bics', 'mvns', 'adcs', 'sbcs', 'rsbs'])
        p.emit(dp(DP[name], rd, rm), f'{name:6} r{rd}, r{rm}')
    elif kind == 6:
        name = rng.choice(['lsls', 'lsrs', 'asrs', 'rors'])
        p.emit(dp(DP[name], rd, rm), f'{name:6} r{rd}, r{rm}')
    elif kind == 7:
        n = rng.randrange(32)
        f = rng.choice([lsls_imm, lsrs_imm, asrs_imm])
        p.emit(f(rd, rm, n), f'{f.__name__[:4]}   r{rd}, r{rm}, #{n}')
    elif kind == 8:
        name = rng.choice(['cmp', 'cmn', 'tst'])
        p.emit(dp(DP[name], rn, rm), f'{name:6} r{rn}, r{rm}')
    elif kind == 9: p.emit(cmp_imm(rn, rng.randrange(256)), f'cmp    r{rn}, #...')
    elif kind == 10:
        k = rng.randrange(16)
        p.emit(str_imm(rd, R_BASE, k), f'str    r{rd}, [r6, #{4 * k}]')
    else:
        k = rng.randrange(16)
        p.emit(ldr_imm(rd, R_BASE, k), f'ldr    r{rd}, [r6, #{4 * k}]')


def gen_pad(p, rng):
    """0 or 1 NOP, so what follows lands in either half of a word."""
    if rng.random() < 0.5:
        p.emit(NOP, 'nop')


def gen_straight(p, rng, n):
    for _ in range(n):
        gen_op(p, rng)


def gen_skip(p, rng):
    """A forward B<cond> (or B) over a few instructions; a halt among them if it's a B."""
    skip = p.label()
    gen_op(p, rng)                       # (sets flags, usually)
    gen_pad(p, rng)
    if rng.random() < 0.25:
        p.branch(skip)                   # always taken: what it skips must never run
        p.emit(HALT, 'udf    #0          @ never runs')
        for _ in range(rng.randrange(3)):
            p.emit(HALT, 'udf    #0          @ never runs')
    else:
        p.branch(skip, cond=rng.randrange(14))
        gen_straight(p, rng, rng.randrange(1, 4))
    p.place(skip)


def gen_loop(p, rng, depth, subs):
    """A counted loop (counter r7, or r5 for an outer one), its body random code."""
    counter = R_INNER if depth == 0 else R_OUTER
    p.emit(movs_imm(counter, rng.randrange(1, 10)), f'movs   r{counter}, #n')
    gen_pad(p, rng)
    top = p.label()
    p.place(top)
    gen_body(p, rng, depth + 1, subs, rng.randrange(1, 5))
    p.emit(subs_imm(counter, 1), f'subs   r{counter}, #1')
    gen_pad(p, rng)
    p.branch(top, cond=1)                # bne top


def gen_body(p, rng, depth, subs, blocks):
    for _ in range(blocks):
        r = rng.random()
        if r < 0.35:
            gen_straight(p, rng, rng.randrange(1, 4))
        elif r < 0.6:
            gen_skip(p, rng)
        elif r < 0.75 and depth < 2:
            gen_loop(p, rng, depth, subs)
        elif r < 0.9 and subs:
            gen_pad(p, rng)
            p.bl(rng.choice(subs))
        else:
            gen_pad(p, rng)


def generate(seed):
    """A program that fits in Code (1 KiB): a seed whose program doesn't tries its variants."""
    for variant in range(100):
        text, halfwords = generate_once(seed, variant)
        if halfwords <= 500:
            return text
    raise RuntimeError(f'seed {seed}: no program small enough')


def generate_once(seed, variant):
    rng = random.Random(seed * 1000 + variant)
    p = Program()
    # r6 = 0x20000000; data registers random; the SRAM block written before any load reads it
    p.emit(movs_imm(R_BASE, 32), 'movs   r6, #32')
    p.emit(lsls_imm(R_BASE, R_BASE, 24), 'lsls   r6, r6, #24')
    for r in DATA:
        p.emit(movs_imm(r, rng.randrange(256)), f'movs   r{r}, #...')
    for k in range(16):
        p.emit(str_imm(DATA[k % 5], R_BASE, k), f'str    r{k % 5}, [r6, #{4 * k}]')
    subs = [p.label() for _ in range(rng.randrange(0, 3))]
    gen_body(p, rng, 0, subs, rng.randrange(6, 16))
    # The machine state, then the halt
    for i, r in enumerate(DATA + [R_INNER, R_OUTER]):
        p.emit(str_imm(r, R_BASE, 16 + i), f'str    r{r}, [r6, #{4 * (16 + i)}]')
    p.emit(HALT, 'udf    #0')
    # Subroutines (they don't call others: LR)
    for s in subs:
        p.place(s)
        gen_pad(p, rng)
        gen_body(p, rng, 2, [], rng.randrange(1, 3))
        p.emit(BX_LR, 'bx     lr')
        p.emit(HALT, 'udf    #0          @ never runs')
    halfwords = sum(1 for addr, _, _ in p.resolve() if addr is not None)
    return p.imem([f'Random program, seed {seed} (tools/fuzz_cores.py)']), halfwords


# ----------------------------------------------------------------------------------------------
# Running

def build(repo, out):
    """program_tb for a repository's core, as `just test` builds it."""
    os.makedirs(out, exist_ok=True)
    rtl = os.path.join(repo, 'rtl')
    pkgs = [os.path.join(rtl, 'ahb', 'pkg', 'ahb_pkg.sv'), os.path.join(rtl, 'cpu', 'pkg', 'cpu_pkg.sv')]
    pkgs += sorted(p for p in (os.path.join(rtl, 'cpu', 'pkg', f) for f in os.listdir(os.path.join(rtl, 'cpu', 'pkg')))
                   if p.endswith('.sv') and not p.endswith('/cpu_pkg.sv'))
    files = [os.path.join(rtl, 'config_defines.vh')] + pkgs
    files += sorted(os.path.join(rtl, f) for f in os.listdir(rtl) if f.endswith('.sv'))
    for d in sorted(os.listdir(rtl)):
        if os.path.isdir(os.path.join(rtl, d)):
            files += sorted(os.path.join(rtl, d, f) for f in os.listdir(os.path.join(rtl, d)) if f.endswith('.sv'))
    files.append(os.path.join(repo, 'testbench', 'program_tb.sv'))
    exe = os.path.join(out, 'program_tb')
    r = subprocess.run(['iverilog', '-o', exe, '-g2012', '-s', 'program_tb'] + files,
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f'iverilog failed for {repo}:\n{r.stderr[-2000:]}')
    return exe


def run(exe, prog_dir, name, cwd, runs=1):
    """-> (halted, pcs, dmem) for one program."""
    os.makedirs(cwd, exist_ok=True)
    subprocess.run(['vvp', exe, f'+TEST={os.path.join(prog_dir, name)}', f'+RUNS={runs}'],
                   cwd=cwd, capture_output=True, text=True)
    trace = open(os.path.join(cwd, f'{name}.trace')).read().splitlines()
    halted = bool(trace) and trace[-1].endswith(' halt')
    pcs, last, started = [], None, False
    for line in trace:
        if line.startswith('reset'):
            started, last = False, None
            pcs.append('reset')
            continue
        f = line.split()
        pc = f[2].lower()
        if not started and pc != 'pc=00000000':
            continue
        started = True
        if pc == 'pc=ffffffff' or pc == last:
            continue
        last = pc
        pcs.append(pc)
    dmem = open(os.path.join(cwd, f'{name}.after.dmem')).read()
    return halted, pcs, dmem


def run_iss(prog_dir, name, runs=1):
    """-> (halted, pcs, dmem) from the golden model, as run() reads them from a core."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import thumb_iss
    m = thumb_iss.load_program(os.path.join(prog_dir, name))
    m.run()
    pcs = [f'pc={p:08x}' for p in m.pcs]
    pcs = [pc for i, pc in enumerate(pcs) if i == 0 or pc != pcs[i - 1]]   # (as the trace merges them)
    pcs = (pcs + ['reset']) * (runs - 1) + pcs
    dmem = ''.join(f'@{o:08x} {b:02x}\n' for o, b in sorted(m.sram.items()))
    return True, pcs, dmem


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--ref', required=True, help='the reference: a repository (or worktree) with a '
                    'core, or `iss` for the golden model (tools/thumb_iss.py)')
    ap.add_argument('--dut', default=ROOT, help='the core under test (default: this repository)')
    ap.add_argument('--dut-exe', help='a simulator already built for the DUT, e.g. the netlist\'s '
                    '(build/synth-fpga/program_tb_synth, after just test-programs-synth-mac)')
    ap.add_argument('--count', type=int, default=100)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--runs', type=int, default=1, help='runs per program (resets between, as test-program-restart)')
    ap.add_argument('--keep', default=os.path.join(ROOT, 'build', 'fuzz'), help='where programs and outputs go')
    args = ap.parse_args()

    work = os.path.abspath(args.keep)
    shutil.rmtree(work, ignore_errors=True)
    progs = os.path.join(work, 'programs')
    os.makedirs(progs)
    ref_exe = None if args.ref == 'iss' else build(os.path.abspath(args.ref), os.path.join(work, 'ref'))
    dut_exe = (os.path.abspath(args.dut_exe) if args.dut_exe
               else build(os.path.abspath(args.dut), os.path.join(work, 'dut')))

    failures = 0
    for i in range(args.count):
        seed = args.seed + i
        name = f'f{seed:05d}'
        with open(os.path.join(progs, f'{name}.imem'), 'w') as f:
            f.write(generate(seed))
        ref = (run_iss(progs, name, args.runs) if ref_exe is None
               else run(ref_exe, progs, name, os.path.join(work, 'ref', 'out'), args.runs))
        dut = run(dut_exe, progs, name, os.path.join(work, 'dut', 'out'), args.runs)
        problems = []
        if not ref[0]:
            problems.append('the reference never halted (a generator problem)')
        if not dut[0]:
            problems.append('the DUT never halted')
        if ref[1] != dut[1]:
            n = next((k for k, (a, b) in enumerate(zip(ref[1], dut[1])) if a != b), min(len(ref[1]), len(dut[1])))
            problems.append(f'PCs differ at instruction {n}: reference {ref[1][n:n + 3]}, DUT {dut[1][n:n + 3]}')
        if ref[2] != dut[2]:
            problems.append('final SRAM differs')
        if problems:
            failures += 1
            print(f'FAIL {name}: ' + '; '.join(problems), flush=True)
        elif (i + 1) % 25 == 0:
            print(f'  {i + 1} programs agree', flush=True)
    print(f'{args.count - failures} of {args.count} programs agree'
          + (f'; programs and outputs are in {work}' if failures else ''))
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
