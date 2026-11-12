#!/usr/bin/env python3
"""Check the stack tests against broken cores: run every program in test_programs/submit/stack/
on the golden model (tools/thumb_iss.py), where each must pass, then on broken versions of it,
each with one stack instruction wrong, where at least one must fail.

    stack_mutants.py [--no-build] [-v]

The grader runs the stack tests on cores with broken stack instructions, which they should
catch; these mutants stand in for those cores. A program passes when it halts with 0x600D600D
at 0x20000000 (the stack tests' passWord.results.dmem); anything else fails it: another word,
an error (an UNPREDICTABLE case, a load from SRAM nobody wrote), or no halt. Each program must
also fit in Code (1 KiB) and halt within its budget of instructions (cycles can only be more:
10,000 is the website's limit, so this asks for well under it).

It builds each program first (`just compile` or `just assemble`, so the ARM toolchain must be
installed); --no-build runs the .imem files already there. -v prints every run's result.
Exits 1 if a program fails on the model, or a mutant passes every program.
"""

import argparse
import glob
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thumb_iss as iss   # noqa: E402
from thumb_iss import Machine, Unpredictable, u32   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STACK_DIR = os.path.join(ROOT, 'test_programs', 'submit', 'stack')
PASS_WORD = 0x600D_600D
CODE_SIZE = 1024
MAX_INSTRUCTIONS = 5000      # on the model; the website allows 10,000 cycles
MUTANT_LIMIT = 20000         # a mutant that runs this long has lost its way: a fail


# ---- the mutants: one stack instruction each, wrong -----------------------------------------

def push_like(m, regs, order=None, sp_moves=None, base_offset=0):
    n = len(regs)
    addr = u32(m.r[13] - 4 * n + base_offset)
    for i in (order or regs):
        m.store(addr, 4, m.r[i])
        addr += 4
    m.r[13] = u32(m.r[13] - (4 * n if sp_moves is None else sp_moves))


def pop_like(m, regs, order=None, sp_moves=None, branch=True):
    addr, target = m.r[13], None
    for i in (order or regs):
        value = m.load(addr, 4)
        if i == 15:
            target = value & ~1
        else:
            m.r[i] = value
        addr += 4
    m.r[13] = u32(m.r[13] + (4 * len(regs) if sp_moves is None else sp_moves))
    return target if branch else None


class SpResetLow(Machine):
    """SP resets to 0x200003FC, not 0x20000400"""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.r[13] = 0x2000_03FC


class SpResetZero(Machine):
    """SP resets to 0 (never set)"""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.r[13] = 0


class PushReversed(Machine):
    """PUSH stores the highest register at the lowest address"""
    def push(self, regs):
        push_like(self, regs, order=regs[::-1])


class PushNoLr(Machine):
    """PUSH leaves out LR"""
    def push(self, regs):
        regs = [i for i in regs if i != 14] or regs
        push_like(self, regs)


class PushSpKept(Machine):
    """PUSH stores, but leaves SP where it was"""
    def push(self, regs):
        push_like(self, regs, sp_moves=0)


class PushSpShort(Machine):
    """PUSH moves SP 4 too few"""
    def push(self, regs):
        push_like(self, regs, sp_moves=4 * len(regs) - 4)


class PushAtSp(Machine):
    """PUSH stores from SP up (post-decrement), not from SP - 4n"""
    def push(self, regs):
        if self.r[13] + 4 * len(regs) > iss.SRAM_BASE + self.sram_size:
            raise Unpredictable('store past the top of SRAM')
        push_like(self, regs, base_offset=4 * len(regs) - 4)


class PushFirstOnly(Machine):
    """PUSH stores only its lowest register, but moves SP for all"""
    def push(self, regs):
        n = len(regs)
        self.store(u32(self.r[13] - 4 * n), 4, self.r[regs[0]])
        self.r[13] = u32(self.r[13] - 4 * n)


class PopReversed(Machine):
    """POP loads the highest register from the lowest address"""
    def pop(self, regs):
        return pop_like(self, regs, order=regs[::-1])


class PopSpKept(Machine):
    """POP loads, but leaves SP where it was"""
    def pop(self, regs):
        return pop_like(self, regs, sp_moves=0)


class PopSpShort(Machine):
    """POP moves SP 4 too few"""
    def pop(self, regs):
        return pop_like(self, regs, sp_moves=4 * len(regs) - 4)


class PopNoBranch(Machine):
    """POP {pc} loads PC but doesn't branch"""
    def pop(self, regs):
        return pop_like(self, regs, branch=False)


class PopPcOnly(Machine):
    """POP {.., pc} loads PC, but not the other registers in its list"""
    def pop(self, regs):
        if 15 not in regs or len(regs) == 1:
            return super().pop(regs)
        value = self.load(u32(self.r[13] + 4 * (len(regs) - 1)), 4)
        self.r[13] = u32(self.r[13] + 4 * len(regs))
        return value & ~1


class StmNoWriteback(Machine):
    """STM doesn't write back Rn"""
    def stm(self, rn, regs):
        old = self.r[rn]
        super().stm(rn, regs)
        self.r[rn] = old


class StmReversed(Machine):
    """STM stores the highest register at the lowest address"""
    def stm(self, rn, regs):
        addr = self.r[rn]
        for i in regs[::-1]:
            self.store(addr, 4, self.r[i])
            addr += 4
        self.r[rn] = u32(self.r[rn] + 4 * len(regs))


class StmNewBase(Machine):
    """STM with Rn in its list stores Rn's written-back value"""
    def stm(self, rn, regs):
        addr, new = self.r[rn], u32(self.r[rn] + 4 * len(regs))
        for i in regs:
            self.store(addr, 4, new if i == rn else self.r[i])
            addr += 4
        self.r[rn] = new


class StmFirstOnly(Machine):
    """STM stores only its lowest register, but writes back for all"""
    def stm(self, rn, regs):
        self.store(self.r[rn], 4, self.r[regs[0]])
        self.r[rn] = u32(self.r[rn] + 4 * len(regs))


class StmFromPlus4(Machine):
    """STM starts at Rn + 4 (pre-increment)"""
    def stm(self, rn, regs):
        addr = self.r[rn] + 4
        for i in regs:
            self.store(addr, 4, self.r[i])
            addr += 4
        self.r[rn] = u32(self.r[rn] + 4 * len(regs))


class LdmAlwaysWriteback(Machine):
    """LDM writes back even when Rn is in its list (over the loaded word)"""
    def ldm(self, rn, regs):
        new = u32(self.r[rn] + 4 * len(regs))
        super().ldm(rn, regs)
        self.r[rn] = new


class LdmNoWriteback(Machine):
    """LDM never writes back Rn"""
    def ldm(self, rn, regs):
        old = self.r[rn]
        super().ldm(rn, regs)
        if rn not in regs:
            self.r[rn] = old


class LdmReversed(Machine):
    """LDM loads the highest register from the lowest address"""
    def ldm(self, rn, regs):
        values = [self.load(u32(self.r[rn] + 4 * k), 4) for k in range(len(regs))]
        if rn not in regs:
            self.r[rn] = u32(self.r[rn] + 4 * len(regs))
        for i, v in zip(regs[::-1], values):
            self.r[i] = v


class LdmFirstOnly(Machine):
    """LDM loads only its lowest register, but writes back for all"""
    def ldm(self, rn, regs):
        value = self.load(self.r[rn], 4)
        if rn not in regs:
            self.r[rn] = u32(self.r[rn] + 4 * len(regs))
        self.r[regs[0]] = value


class LdmFromPlus4(Machine):
    """LDM starts at Rn + 4 (pre-increment)"""
    def ldm(self, rn, regs):
        self.r[rn] += 4
        try:
            super().ldm(rn, regs)
        finally:
            if rn not in regs:
                self.r[rn] = u32(self.r[rn] - 4)


class SpImmUnscaled(Machine):
    """ADD/SUB SP, #imm7 doesn't multiply imm7 by 4"""
    def adjust_sp(self, sub, offset):
        super().adjust_sp(sub, offset // 4)


class SubSpAdds(Machine):
    """SUB SP, #imm adds"""
    def adjust_sp(self, sub, offset):
        super().adjust_sp(0, offset)


class SpImm6Bits(Machine):
    """ADD/SUB SP, #imm7 loses imm7's top bit"""
    def adjust_sp(self, sub, offset):
        super().adjust_sp(sub, offset & 0xFC)


class AddRdSpUnscaled(Machine):
    """ADD Rd, SP, #imm8 doesn't multiply imm8 by 4"""
    def add_sp_imm(self, rd, offset):
        super().add_sp_imm(rd, offset // 4)


class AddRdSpImm7Bits(Machine):
    """ADD Rd, SP, #imm8 loses imm8's top bit"""
    def add_sp_imm(self, rd, offset):
        super().add_sp_imm(rd, offset & 0x1FC)


class SpRelUnscaled(Machine):
    """LDR/STR [SP, #imm8] doesn't multiply imm8 by 4"""
    def load_store_sp(self, load, rt, offset):
        super().load_store_sp(load, rt, offset // 4)


class SpRelImm7Bits(Machine):
    """LDR/STR [SP, #imm8] loses imm8's top bit"""
    def load_store_sp(self, load, rt, offset):
        super().load_store_sp(load, rt, offset & 0x1FC)


class SpRelBaseR7(Machine):
    """LDR/STR [SP, #imm8] uses r7 (the decoder's Rn field) as the base, not SP"""
    def load_store_sp(self, load, rt, offset):
        addr = u32(self.r[7] + offset)
        if load:
            self.r[rt] = self.load(addr, 4)
        else:
            self.store(addr, 4, self.r[rt])


class SpRelLoadOnly(Machine):
    """STR [SP, #imm8] doesn't store"""
    def load_store_sp(self, load, rt, offset):
        if load:
            super().load_store_sp(load, rt, offset)


class StrRegIgnoresRm(Machine):
    """STR/STRH/STRB [Rn, Rm] store at Rn, without Rm"""
    def load_store_register(self, op, rt, rn, rm):
        if op <= 2:
            size = (4, 2, 1)[op]
            self.store(self.r[rn], size, self.r[rt])
        else:
            super().load_store_register(op, rt, rn, rm)


class StrRegRm16(Machine):
    """STR/STRH/STRB [Rn, Rm] use only Rm's low 16 bits (drops a negative Rm's sign)"""
    def load_store_register(self, op, rt, rn, rm):
        if op <= 2:
            size = (4, 2, 1)[op]
            self.store(u32(self.r[rn] + (self.r[rm] & 0xFFFF)), size, self.r[rt])
        else:
            super().load_store_register(op, rt, rn, rm)


class StrbWord(Machine):
    """STRB [Rn, Rm] stores the whole word (at the aligned address)"""
    def load_store_register(self, op, rt, rn, rm):
        if op == 2:
            self.store(u32(self.r[rn] + self.r[rm]) & ~3, 4, self.r[rt])
        else:
            super().load_store_register(op, rt, rn, rm)


class StrbLane0(Machine):
    """STRB [Rn, Rm] always writes byte 0 of the word"""
    def load_store_register(self, op, rt, rn, rm):
        if op == 2:
            self.store(u32(self.r[rn] + self.r[rm]) & ~3, 1, self.r[rt])
        else:
            super().load_store_register(op, rt, rn, rm)


class StrhByte(Machine):
    """STRH [Rn, Rm] stores only a byte"""
    def load_store_register(self, op, rt, rn, rm):
        if op == 1:
            self.store(u32(self.r[rn] + self.r[rm]), 1, self.r[rt])
        else:
            super().load_store_register(op, rt, rn, rm)


class StrhLowHalf(Machine):
    """STRH [Rn, Rm] always writes the word's low half"""
    def load_store_register(self, op, rt, rn, rm):
        if op == 1:
            self.store(u32(self.r[rn] + self.r[rm]) & ~3, 2, self.r[rt])
        else:
            super().load_store_register(op, rt, rn, rm)


class StrRegAsStrh(Machine):
    """STR [Rn, Rm] stores only a halfword"""
    def load_store_register(self, op, rt, rn, rm):
        super().load_store_register(1 if op == 0 else op, rt, rn, rm)


class HighAddMutant(Machine):
    """The high-register ADD (its SP forms), broken by `fix`"""
    def step(self):
        pc = self.r[15]
        instr = self.fetch16(pc)
        if instr >> 8 == 0b01000100:          # ADD Rdn, Rm (high registers)
            rm, rdn = instr >> 3 & 15, (instr >> 4 & 8) | (instr & 7)
            if 13 in (rm, rdn) and rdn != 15:
                self.pcs.append(pc)
                self.r[rdn] = self.fix(rdn, rm)
                self.r[15] = pc + 2
                return
        super().step()


class AddSpRmIgnored(HighAddMutant):
    """ADD SP, Rm (T2) leaves SP alone"""
    def fix(self, rdn, rm):
        return self.r[rdn] if rdn == 13 else u32(self.r[rdn] + self.r[rm])


class AddRdSpRdIgnoresSp(HighAddMutant):
    """ADD Rd, SP, Rd (T1) leaves Rd alone (adds nothing for SP)"""
    def fix(self, rdn, rm):
        return self.r[rdn] if rm == 13 else u32(self.r[rdn] + self.r[rm])


MUTANTS = [SpResetLow, SpResetZero,
           PushReversed, PushNoLr, PushSpKept, PushSpShort, PushAtSp, PushFirstOnly,
           PopReversed, PopSpKept, PopSpShort, PopNoBranch, PopPcOnly,
           StmNoWriteback, StmReversed, StmNewBase, StmFirstOnly, StmFromPlus4,
           LdmAlwaysWriteback, LdmNoWriteback, LdmReversed, LdmFirstOnly, LdmFromPlus4,
           SpImmUnscaled, SubSpAdds, SpImm6Bits, AddRdSpUnscaled, AddRdSpImm7Bits,
           SpRelUnscaled, SpRelImm7Bits, SpRelBaseR7, SpRelLoadOnly,
           AddSpRmIgnored, AddRdSpRdIgnoresSp,
           StrRegIgnoresRm, StrRegRm16, StrbWord, StrbLane0, StrhByte, StrhLowHalf, StrRegAsStrh]


# ---- running them ---------------------------------------------------------------------------

def programs():
    """-> [(dir name, path without extension, source extension)]"""
    out = []
    for d in sorted(glob.glob(os.path.join(STACK_DIR, '[0-9]*_*/'))):
        d = d.rstrip('/')
        name = re.sub(r'^\d+_', '', os.path.basename(d))
        for ext in ('c', 's'):
            if os.path.exists(os.path.join(d, f'{name}.{ext}')):
                out.append((os.path.basename(d), os.path.join(d, name), ext))
    return out


def build(dirname, ext):
    recipe = 'compile' if ext == 'c' else 'assemble'
    r = subprocess.run(['just', recipe, f'submit/stack/{dirname}'], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f'just {recipe} submit/stack/{dirname} failed:\n{r.stdout}{r.stderr}')


def run(cls, path, limit):
    """-> (passed, what happened, instructions run)"""
    base = iss.load_program(path)
    m = cls(base.code, base.sram, base.sram_size)
    try:
        m.run(limit)
    except Unpredictable as e:
        return False, f'error: {e}', len(m.pcs)
    except Exception as e:                    # a mutant can wander anywhere
        return False, f'error: {type(e).__name__}: {e}', len(m.pcs)
    if all(i in m.sram for i in range(4)):
        word = sum(m.sram[i] << (8 * i) for i in range(4))
        return word == PASS_WORD, f'{word:08x}', len(m.pcs)
    return False, 'no word stored', len(m.pcs)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--no-build', action='store_true', help="run the .imem files already there")
    ap.add_argument('-v', action='store_true', help="print every run's result")
    args = ap.parse_args()

    progs = programs()
    if not progs:
        sys.exit(f'no programs in {STACK_DIR}')
    bad = []
    print('On the golden model:')
    for dirname, path, ext in progs:
        if not args.no_build:
            build(dirname, ext)
        size = len(iss.read_bytes(path + '.imem'))
        ok, what, n = run(Machine, path, MAX_INSTRUCTIONS)
        problems = ([] if ok else [f'got {what}']) + \
                   ([f'{size} bytes of Code, over {CODE_SIZE}'] if size > CODE_SIZE else [])
        print(f'  {"PASS" if not problems else "FAIL"}  {dirname:24} {n:5} instructions, '
              f'{size:4} bytes{"  " + "; ".join(problems) if problems else ""}')
        bad += [f'{dirname}: {p}' for p in problems]

    width = max(len(c.__name__) for c in MUTANTS)
    cols = '  '.join(f'{d[:2]:>2}' for d, _, _ in progs)
    print(f'\nOn {len(MUTANTS)} broken models (x: that program fails, so it catches the mutant):')
    print(f'  {"":{width}}  {cols}')
    missed = []
    for cls in MUTANTS:
        results = [run(cls, path, MUTANT_LIMIT) for _, path, _ in progs]
        marks = '  '.join(' .' if ok else ' x' for ok, _, _ in results)
        caught = not all(ok for ok, _, _ in results)
        print(f'  {cls.__name__:{width}}  {marks}  {"" if caught else "MISSED: "}{cls.__doc__}')
        if args.v:
            for (d, _, _), (ok, what, n) in zip(progs, results):
                print(f'      {d:24} {"pass" if ok else "fail"}  {what} ({n} instructions)')
        if not caught:
            missed.append(cls.__name__)

    print(f'\n{len(MUTANTS) - len(missed)} of {len(MUTANTS)} mutants caught'
          + (f'; missed: {", ".join(missed)}' if missed else ''))
    for b in bad:
        print('FAIL', b)
    sys.exit(1 if bad or missed else 0)


if __name__ == '__main__':
    main()
