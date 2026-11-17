#!/usr/bin/env python3
"""Make the stress test programs in test_programs/stress/: directed programs that drive every
datapath of the core through its edge cases, each with expected .pcs, .after.dmem and .bus
from the golden model (tools/thumb_iss.py), so `just test-programs` checks them like the
course's own.

    gen_stress.py            (re)write test_programs/stress/
    gen_stress.py --check    exit 1 if what's there isn't what this makes

Every program saves its results (and the flags, captured with branches: see thumb_asm) to SRAM
from 0x2000_0000 up, so the final SRAM shows every value the core computed.
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thumb_asm   # noqa: E402
import thumb_iss   # noqa: E402
from thumb_asm import Asm   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'test_programs', 'stress')

EDGES = [0x0000_0000, 0x0000_0001, 0x7FFF_FFFF, 0x8000_0000, 0xFFFF_FFFF, 0x1234_5678]
# Flag states to start from: (C, V) both clear, both set (to see that what keeps them, does)
PRESETS = ['c0v0', 'c1v1']


def preset_flags(a, which, scratch=5):
    """Set the flags: c0v0 (0 - 1: N1 Z0 C0 V0) or c1v1 (0x80000000 - 1: N0 Z0 C1 V1)."""
    if which == 'c0v0':
        a.movs(scratch, 0)
        a.cmp_imm(scratch, 1)
    else:
        a.const(scratch, 0x8000_0000)
        a.cmp_imm(scratch, 1)


# ----------------------------------------------------------------------------------------------
# The programs. Each returns (title lines, Asm).

def p_alu(op):
    """One add/subtract-family instruction on every pair of edge values."""
    a = Asm()
    a.prologue()
    for x in EDGES:
        for y in EDGES:
            for preset in (PRESETS if op in ('adcs', 'sbcs') else ['c0v0']):
                a.const(0, x)
                a.const(1, y)
                preset_flags(a, preset)
                if op == 'adds':   a.adds_reg(2, 0, 1)
                elif op == 'subs': a.subs_reg(2, 0, 1)
                elif op in ('adcs', 'sbcs'):
                    a.mov_hi(2, 0)
                    a.dp(op, 2, 1)
                elif op in ('cmp', 'cmn'):
                    a.movs(2, 0x5A)           # (untouched: flags only)
                    preset_flags(a, preset)
                    a.dp(op, 0, 1)
                a.save_flags()
                a.save(2)
    a.halt()
    return [f'{op.upper()} on every pair of edge values, the result and NZCV of each'], a


def p_alu_imm():
    """The immediate forms: ADDS/SUBS #imm3 and #imm8, CMP #imm8, MOVS, RSBS."""
    a = Asm()
    a.prologue()
    for x in EDGES:
        for imm in (0, 1, 7, 255):
            for form in ('adds3', 'subs3', 'adds8', 'subs8', 'cmp8'):
                if form.endswith('3') and imm > 7:
                    continue
                a.const(0, x)
                preset_flags(a, 'c1v1')
                if form == 'adds3':   a.adds_imm3(2, 0, imm)
                elif form == 'subs3': a.subs_imm3(2, 0, imm)
                elif form == 'adds8': a.mov_hi(2, 0); a.adds_imm8(2, imm)
                elif form == 'subs8': a.mov_hi(2, 0); a.subs_imm8(2, imm)
                else:                 a.mov_hi(2, 0); a.cmp_imm(2, imm)
                a.save_flags()
                a.save(2)
        a.const(0, x)
        preset_flags(a, 'c1v1')
        a.dp('rsbs', 2, 0)
        a.save_flags()
        a.save(2)
    for imm in (0, 1, 128, 255):
        preset_flags(a, 'c1v1')
        a.movs(2, imm)
        a.save_flags()
        a.save(2)
    a.halt()
    return ['ADDS/SUBS #imm3 and #imm8, CMP #imm8, MOVS #imm8, RSBS on edge values, with NZCV'], a


def p_logic(op):
    a = Asm()
    a.prologue()
    if True:
        for x in EDGES:
            for y in EDGES[::-1]:
                for preset in PRESETS:
                    a.const(2, x)
                    a.const(1, y)
                    preset_flags(a, preset)
                    a.dp(op, 2, 1)
                    a.save_flags()
                    a.save(2)
    a.halt()
    return [f'{op.upper()} on edge values: N and Z set, C and V kept (both ways)'], a


def p_shift_reg(op):
    a = Asm()
    a.prologue()
    amounts = [0, 1, 2, 7, 8, 15, 16, 31, 32, 33, 63, 64, 127, 128, 255, 256, 257, 0xFFFF_FFFF]
    for x in (0x8000_0001, 0xFFFF_FFFF, 0x1234_5678, 0x0000_0001):
        for n in amounts:
            for preset in PRESETS:
                a.const(2, x)
                a.const(1, n)
                preset_flags(a, preset)
                a.dp(op, 2, 1)
                a.save_flags()
                a.save(2)
    a.halt()
    return [f'{op.upper()} Rdn, Rm by every amount edge (0, 32, past 32, Rm past 255), C kept or set'], a


def p_shift_imm(kind):
    a = Asm()
    a.prologue()
    for x in (0x8000_0001, 0xFFFF_FFFF, 0x1234_5678):
        if True:
            for n in range(32):
                for preset in (PRESETS if n == 0 else ['c1v1']):
                    a.const(1, x)
                    preset_flags(a, preset)
                    getattr(a, kind + '_imm')(2, 1, n)
                    a.save_flags()
                    a.save(2)
    a.halt()
    return [f'{kind.upper()} #imm5 by every amount (LSLS #0 is MOVS; LSRS/ASRS #0 mean 32)'], a


def p_extend():
    a = Asm()
    a.prologue()
    vals = EDGES + [0x0000_0080, 0x0000_7F80, 0x0000_8000, 0x8080_8080, 0xA5C3_0F96]
    for x in vals:
        for name in ('sxth', 'sxtb', 'uxth', 'uxtb'):
            a.const(1, x)
            a.ext(name, 2, 1)
            a.save(2)
        for name in ('rev', 'rev16', 'revsh'):
            a.const(1, x)
            a.rev(name, 2, 1)
            a.save(2)
    a.halt()
    return ['SXTH SXTB UXTH UXTB REV REV16 REVSH on edge and sign-boundary values'], a


def p_memory():
    """Every store and load size, at every alignment, by both offset forms."""
    a = Asm()
    a.prologue()
    a.const(5, 0x2000_0200)               # a scratch block, apart from the results
    a.const(3, 0)
    # A known pattern, by words
    for k in range(8):
        a.const(0, 0x8070_6050 + 0x0101_0101 * k * 0x11 & 0xFFFF_FFFF)
        a.ldst_imm('str', 0, 5, 4 * k)
    # Bytes and halfwords over it, both forms
    for off in range(0, 8):
        a.const(0, 0xE0 + off)
        a.ldst_imm('strb', 0, 5, off)
    for off in range(8, 16, 2):
        a.const(0, 0xF000 | off)
        a.ldst_imm('strh', 0, 5, off)
    for off in (16, 17, 18, 19):
        a.const(0, 0x90 + off)
        a.const(1, off)
        a.ldst_reg('strb', 0, 5, 1)
    for off in (20, 22):
        a.const(0, 0x8000 | off)
        a.const(1, off)
        a.ldst_reg('strh', 0, 5, 1)
    a.const(0, 0xCAFE_F00D)
    a.const(1, 24)
    a.ldst_reg('str', 0, 5, 1)
    # Every load, every alignment
    for off in range(0, 32):
        for name in ('ldrb', 'ldrsb', 'ldrh', 'ldrsh', 'ldr'):
            size = {'ldrb': 1, 'ldrsb': 1, 'ldrh': 2, 'ldrsh': 2, 'ldr': 4}[name]
            if off % size:
                continue
            if name in ('ldrsb', 'ldrsh') or off % 3 == 0:
                a.const(1, off)
                a.ldst_reg(name, 2, 5, 1)
            else:
                a.ldst_imm(name, 2, 5, off)
            a.save(2)
    a.halt()
    return ['STR/STRH/STRB and every load (LDR LDRH LDRB LDRSH LDRSB), every alignment,',
            'register and immediate offsets: byte lanes in, lanes out, sign extension'], a


def p_literal():
    """LDR (literal) and ADR, from both halves of a word, and the literals' reach."""
    a = Asm()
    a.prologue()
    pool = a.fresh('pool')
    for pad in (0, 1):
        for k in range(4):
            if pad:
                a.nop()
            a.adr(2, k)
            a.save(2)
            a.ldr_lit(2, k)
            a.save(2)
    a.b(pool)
    a.align4()
    for k in range(8):
        a.word(0x1111_1111 * (k + 1))
    a.label(pool)
    a.halt()
    return ['ADR and LDR (literal), from either half of a word: PC read as Align(PC + 4, 4)'], a


def p_forward(kind):
    """Forwarding: a result used 1 to 4 instructions later, in every operand slot."""
    a = Asm()
    a.prologue()
    a.const(4, 0x2000_0300)                 # a block of known bytes, 0x20000300 up
    for k in range(4):
        a.const(5, 0x0D0C_0B0A + 0x0404_0404 * k)
        a.ldst_imm('str', 5, 4, 4 * k)      # bytes 10, 11, 12, ... 25
    a.const(5, 13)
    a.ldst_imm('str', 5, 4, 16)             # the word a load producer reads: 13
    for dist in (1, 2, 3, 4):
        for producer in ('alu', 'load', 'movs', 'shift', 'ext'):
            if (kind == 'load') != (producer == 'load'):
                continue
            for slot in ('a', 'b', 'store', 'shamt', 'shdata', 'offset', 'cmp', 'mov'):
                # r0's old value differs every case, and is written back before the
                # producer, so a forwarding path that's missed reads a wrong value
                a.n += 1
                a.movs(0, 0x40 + a.n % 0x40)
                for _ in range(4):
                    a.mov_hi(3, 1)
                a.movs(1, 3)
                # The producer writes r0 (a small value: 8, 13, 9, 12 or 3)
                if producer == 'alu':     a.adds_imm3(0, 1, 5)
                elif producer == 'load':  a.ldst_imm('ldr', 0, 4, 16)
                elif producer == 'movs':  a.movs(0, 9)
                elif producer == 'shift': a.lsls_imm(0, 1, 2)
                else:                     a.ext('uxtb', 0, 1)
                for _ in range(dist - 1):
                    a.mov_hi(3, 1)                                     # independent
                # The consumer reads r0
                if slot == 'a':        a.adds_reg(2, 0, 1)
                elif slot == 'b':      a.subs_reg(2, 1, 0)
                elif slot == 'store':  a.ldst_imm('str', 0, 4, 20); a.ldst_imm('ldr', 2, 4, 20)
                elif slot == 'shamt':  a.mov_hi(2, 1); a.dp('lsls', 2, 0)
                elif slot == 'shdata': a.lsls_imm(2, 0, 3)
                elif slot == 'offset': a.ldst_reg('ldrb', 2, 4, 0)    # address = block + r0
                elif slot == 'cmp':    a.cmp_imm(0, 9); a.capture_flags(2)
                else:                  a.mov_hi(7, 0); a.mov_hi(2, 7)
                a.save(2)
    a.halt()
    return [f'Forwarding ({kind} producers): each result used 1 to 4 instructions later, as',
            'operand A, B, store data, shift amount, shift data, address offset, compare, MOV'], a


def p_branches():
    """Every condition, against flag states that make it go each way, forward and back."""
    a = Asm()
    a.prologue()
    setups = [(0, 0), (1, 0), (0, 1), (0x8000_0000, 1), (0x7FFF_FFFF, 0xFFFF_FFFF),
              (0xFFFF_FFFF, 1), (5, 5), (0x8000_0000, 0x8000_0000)]
    for x, y in setups:
        for cond in thumb_asm.COND:
            a.const(0, x)
            a.const(1, y)
            a.movs(2, 1)
            a.dp('cmp', 0, 1)
            taken = a.fresh('t')
            done = a.fresh('d')
            a.bcond(cond, taken)
            a.mov_hi(2, thumb_asm.ZERO)        # not taken: 0 (flags kept)
            a.b(done)
            a.trap()
            a.label(taken)
            a.add_hi(2, thumb_asm.FLAG_N)      # taken: 1 + 8
            a.label(done)
            a.save(2)
    # Backward: a loop per condition that runs a known number of times
    for cond, start in (('ne', 3), ('gt', 3), ('ge', 3), ('pl', 3), ('hi', 3)):
        a.movs(0, start)
        a.movs(2, 0)
        top = a.fresh('loop')
        a.label(top)
        a.adds_imm8(2, 1)
        a.subs_imm8(0, 1)
        a.bcond(cond, top)
        a.save(2)
    a.halt()
    return ['B<cond>: all 14 conditions against 8 flag states (taken and not), forward,',
            'and backward loops ending each way'], a


def p_calls():
    """BL (both alignments, both directions), BLX, BX, MOV PC, ADD PC, returns."""
    a = Asm()
    a.prologue()
    a.movs(7, 0)
    a.b('main')
    a.trap()
    # Subroutines, before main (backward BLs)
    a.label('sub_lo')
    a.adds_imm8(7, 1)
    a.bx(14)
    a.trap()
    a.nop()
    a.label('sub_hi')                       # (an upper-half start)
    a.adds_imm8(7, 2)
    a.mov_hi(15, 14)                        # return with MOV PC, LR
    a.trap()
    a.label('main')
    for pad in (0, 1, 0, 1):
        if pad:
            a.nop()
        a.bl('sub_lo')
        a.save(7)
        a.bl('sub_hi')
        a.save(7)
        a.bl('sub_fwd')
        a.save(7)
    # BLX through a register (the address from a literal, Thumb bit set), to
    # subroutines starting in either half of a word, used right after loading it
    for k, target in enumerate(('sub_lo', 'sub_hi', 'sub_fwd')):
        pool, after = a.fresh('blxpool'), a.fresh('blxgo')
        a.ldr_label(3, pool)
        a.blx(3)
        a.save(7)
        a.b(after)
        a.align4()
        a.label(pool)
        a.word_label(target, 1)
        a.label(after)
    # ADD PC, Rm: a jump table of 4 entries, 4 bytes apart
    for k in range(4):
        a.movs(0, 4 * k)
        a.add_hi(15, 0)                      # PC = this + 4 + r0
        a.nop()
        for j in range(4):
            a.adds_imm8(7, 1 << j)
            a.b(f'jt_end{k}')
        a.label(f'jt_end{k}')
        a.save(7)
    # BX to a computed address (ADR + 1: Thumb bit), and to an upper half
    for k in range(2):
        a.adr(3, 2)
        a.adds_imm8(3, 1 + 2 * k)
        a.bx(3)
        a.trap()
        a.trap()
        a.align4()
        a.nop() if k else None
        a.adds_imm8(7, 10)
        a.save(7)
    a.halt()
    a.label('sub_fwd')
    a.adds_imm8(7, 4)
    a.mov_hi(0, 14)                         # LR to r0, return by BX r0
    a.bx(0)
    a.trap()
    return ['BL forward and back from both halves of a word, returns by BX LR and MOV PC, LR,',
            'ADD PC jump tables, BX to computed (and upper-half) addresses'], a


def p_peripherals():
    """The LEDs' wait cycle in the middle of dependent instructions."""
    a = Asm()
    a.prologue()
    a.const(4, 0x4000_1000)
    a.const(5, 0x4000_2004)                 # the counter's CLEAR (reads 0)
    for k in range(8):
        a.const(0, 0x15 + 7 * k)
        a.ldst_imm('str', 0, 4, 0)           # write the LEDs
        a.ldst_imm('ldr', 1, 4, 0)           # read them back: one wait cycle
        a.adds_reg(2, 1, 1)                  # used right away
        a.ldst_imm('str', 2, 6, 0)           # stored right after
        a.adds_imm8(6, 4)
        a.ldst_imm('ldr', 3, 5, 0)           # CLEAR reads 0
        a.ldst_imm('str', 3, 5, 0)           # clear the counter (a device store)
        a.cmp_imm(1, 0x15 + 7 * k & 0x3F)
        a.capture_flags(7)
        a.save(7)
        a.cmp_imm(1, 0x15 + 7 * k & 0x3F)
        a.ldst_imm('ldr', 0, 4, 0)           # (a wait cycle between the compare and the branch)
        ok = a.fresh('ok')
        a.bcond('eq', ok)                    # a branch on flags set before the device load
        a.trap()
        a.label(ok)
        a.save(0)
    a.const(0, 0)
    a.ldst_imm('str', 0, 4, 0)
    a.halt()
    return ['LED writes and reads (one APB wait cycle each) with their results used right',
            'away: by an ADD, a store, a compare; and the counter\'s CLEAR, read and written'], a


LEDS = 0x4000_1000
COUNT = 0x4000_2000
CLEAR = 0x4000_2004


def p_bus_back_to_back():
    """Bus transfers with nothing between them: a waited data phase's store data must hold
    while the next transfer's address phase waits behind it."""
    a = Asm()
    a.prologue()
    a.const(5, 0x2000_0400)                 # scratch words; they're in .after.dmem too
    a.const(4, LEDS)
    for r, v in enumerate((0x1111_1111, 0x2222_2222, 0x3333_3333, 0x4444_4444)):
        a.const(r, v)
    a.const(7, 0x5A5A_A5A5)
    # Four stores of different data, back to back
    for r in range(4):
        a.ldst_imm('str', r, 5, 4 * r)
    # Sizes mixed on the same words, back to back
    a.ldst_imm('strb', 0, 5, 16)
    a.ldst_imm('strb', 1, 5, 17)
    a.ldst_imm('strh', 2, 5, 18)
    a.ldst_imm('str', 3, 5, 20)
    a.ldst_imm('strh', 7, 5, 20)
    a.ldst_imm('strb', 1, 5, 23)
    # A store, then a load of the same word, alternating
    a.ldst_imm('str', 7, 5, 24)
    a.ldst_imm('ldr', 3, 5, 24)
    a.ldst_imm('str', 3, 5, 28)
    a.ldst_imm('ldr', 2, 5, 28)
    a.save(2)
    # Loads back to back, then stores of what they read
    a.ldst_imm('ldr', 0, 5, 0)
    a.ldst_imm('ldr', 1, 5, 4)
    a.ldst_imm('ldr', 2, 5, 16)
    a.ldst_imm('str', 0, 5, 32)
    a.ldst_imm('str', 1, 5, 36)
    a.ldst_imm('str', 2, 5, 40)
    # Peripherals and SRAM interleaved, back to back
    a.movs(0, 0x2B)
    a.movs(1, 0x14)
    a.ldst_imm('str', 0, 4, 0)              # LEDs
    a.ldst_imm('str', 7, 5, 44)             # SRAM, behind the LEDs' wait
    a.ldst_imm('str', 1, 4, 0)              # LEDs again
    a.ldst_imm('ldr', 2, 4, 0)              # read back: 0x14
    a.ldst_imm('str', 2, 5, 48)
    a.ldst_imm('ldr', 3, 5, 44)
    a.ldst_imm('str', 3, 4, 0)              # LEDs = 0x25 (0x5A5AA5A5's bits 5:0)
    a.ldst_imm('ldr', 0, 4, 0)
    a.save(0)
    # A loop of stores, each straight after a taken branch
    a.movs(0, 8)
    a.const(1, 0x2000_0440)
    loop = a.fresh('loop')
    a.label(loop)
    a.ldst_imm('str', 0, 1, 0)
    a.ldst_imm('strb', 0, 1, 4)
    a.adds_imm8(1, 8)
    a.subs_imm8(0, 1)
    a.bcond('ne', loop)
    a.ldst_imm('str', 0, 4, 0)              # LEDs off
    a.halt()
    return ['Loads and stores with nothing between them: stores of different data back to',
            'back, sizes mixed on one word, store then load, peripherals and SRAM interleaved,',
            'stores straight after taken branches'], a


def p_leds():
    """The LED register: every value, the bits it drops, and every offset in its 4 KB."""
    a = Asm()
    a.prologue()
    a.const(4, LEDS)
    for v in range(64):
        junk = (v * 0x9E37_79B1) & 0xFFFF_FFC0  # bits above 5:0, which it doesn't keep
        a.const(0, v | junk)
        a.ldst_imm('str', 0, 4, 0)
        a.ldst_imm('ldr', 1, 4, 0)
        a.save(1)
    # The whole 4 KB is the one register: write and read it at other offsets
    for offset, v in ((0x004, 0x2A), (0x7C, 0x15), (0x800, 0x3F), (0xFFC, 0x01)):
        a.const(2, LEDS + offset)
        a.movs(0, v)
        a.ldst_imm('str', 0, 2, 0)
        a.ldst_imm('ldr', 1, 4, 0)          # read at 0
        a.ldst_imm('ldr', 3, 2, 0)          # and where it was written
        a.save(1)
        a.save(3)
    # A read's value used right away: as an address offset and in a compare
    a.movs(0, 8)
    a.ldst_imm('str', 0, 4, 0)
    a.const(5, 0x2000_0000)
    a.ldst_imm('ldr', 1, 4, 0)
    a.ldst_reg('ldr', 2, 5, 1)              # [0x2000_0008]: the third result above (0x02)
    a.save(2)
    a.ldst_imm('ldr', 1, 4, 0)
    a.cmp_imm(1, 8)
    ok = a.fresh('ok')
    a.bcond('eq', ok)
    a.trap()
    a.label(ok)
    a.movs(0, 0)
    a.ldst_imm('str', 0, 4, 0)
    a.halt()
    return ['The LED register: all 64 values (with junk above bit 5, which reads back as 0),',
            'written and read at other offsets in its 4 KB, a read used right away'], a


def counter_model(m):
    """The golden model's COUNT: two cycles per instruction since the last CLEAR, plus a few.
    p_counter only checks what any core's count satisfies, so any such model gives the same
    outputs."""
    return 2 * (len(m.pcs) - m.cleared_at) + 3


def p_counter():
    """The cycle counter, checked by ranges, so the outputs don't depend on timing."""
    a = Asm()
    a.prologue()
    a.const(4, COUNT)                       # r4: COUNT; CLEAR is [r4, #4]

    def check(rd, lo=None, hi=None, scratch=3):
        """lo <= rd < hi (unsigned), or run a trap; then save a marker."""
        for bound, cond in ((lo, 'cs'), (hi, 'cc')):
            if bound is None:
                continue
            a.const(scratch, bound)
            a.dp('cmp', rd, scratch)
            ok = a.fresh('ok')
            a.bcond(cond, ok)
            a.trap()
            a.label(ok)
        a.movs(scratch, 0xC0)
        a.save(scratch)

    # CLEAR, then COUNT straight away: small
    a.ldst_imm('str', 4, 4, 4)
    a.ldst_imm('ldr', 1, 4, 0)
    check(1, hi=64)
    # Two reads back to back: the second is later, by a little
    a.ldst_imm('ldr', 0, 4, 0)
    a.ldst_imm('ldr', 1, 4, 0)
    a.subs_reg(2, 1, 0)
    check(2, lo=1, hi=200)
    # A 100-iteration loop (200 instructions): at least 200 cycles
    a.ldst_imm('str', 4, 4, 4)
    a.movs(0, 100)
    loop = a.fresh('loop')
    a.label(loop)
    a.subs_imm8(0, 1)
    a.bcond('ne', loop)
    a.ldst_imm('ldr', 1, 4, 0)
    check(1, lo=200, hi=20000)
    # Writes to COUNT, and to offsets that aren't registers, change nothing
    a.movs(0, 0)
    a.ldst_imm('str', 0, 4, 0)
    a.const(5, COUNT + 0x008)
    a.ldst_imm('str', 0, 5, 0)
    a.ldst_imm('ldr', 1, 4, 0)
    check(1, lo=200)
    # Reads of CLEAR and of offsets that aren't registers are 0
    for offset in (0x004, 0x008, 0x00C, 0x800, 0xFFC):
        a.const(5, COUNT + offset)
        a.const(0, 0xFFFF_FFFF)
        if offset != 0x004:                 # (a write to CLEAR would clear it)
            a.ldst_imm('str', 0, 5, 0)
        a.ldst_imm('ldr', 1, 5, 0)
        a.save(1)
    # CLEAR with a nonzero value clears too
    a.const(0, 0xDEAD_BEEF)
    a.ldst_imm('str', 0, 4, 4)
    a.ldst_imm('ldr', 1, 4, 0)
    check(1, hi=64)
    # Across 8 SRAM stores, it goes up by at least 8
    a.ldst_imm('ldr', 0, 4, 0)
    for k in range(8):
        a.ldst_imm('str', 4, 6, 4 * k)
    a.ldst_imm('ldr', 1, 4, 0)
    a.subs_reg(2, 1, 0)
    check(2, lo=8, hi=2000)
    a.halt()
    return ['The cycle counter: CLEAR, reads back to back, a timed loop, writes to COUNT and to',
            'offsets that aren\'t registers ignored, those offsets and CLEAR read 0. COUNT is',
            'checked only by ranges, so any core\'s timing gives the same outputs.'], a


def p_fetch():
    """Fetch under pressure: long runs of loads and stores, and mixed 16/32-bit code."""
    a = Asm()
    a.prologue()
    a.const(5, 0x2000_0300)
    for k in range(16):
        a.const(0, k * 0x0101_0101)
        a.ldst_imm('str', 0, 5, 4 * (k % 32))
    for k in range(16):
        a.ldst_imm('ldr', k % 4, 5, 4 * k)
    for k in range(4):
        a.save(k)
    a.movs(7, 0)
    for k in range(12):
        if k % 3 == 0:
            a.nop()
        a.bl(f'leaf{k % 2}')
    a.save(7)
    a.halt()
    a.label('leaf0')
    a.adds_imm8(7, 1)
    a.bx(14)
    a.label('leaf1')
    a.nop()
    a.adds_imm8(7, 3)
    a.bx(14)
    return ['16 stores then 16 loads back to back (fetch starved of the bus), then BLs from',
            'both halves of words'], a


def p_halt(case):
    a = Asm()
    a.prologue()
    if case == 'afterStore':
        a.const(0, 0x1234_5678)
        a.ldst_imm('str', 0, 6, 0)
        a.halt()
        notes = ['A store right before the halt: its data phase ends no later than the halt']
    elif case == 'afterDeviceLoad':
        a.const(4, 0x4000_1000)
        a.movs(0, 0x2A)
        a.ldst_imm('str', 0, 4, 0)
        a.ldst_imm('ldr', 1, 4, 0)
        a.halt()
        notes = ['A device load (with its wait cycle) right before the halt']
    elif case == 'afterBranch':
        a.const(0, 0x55)
        a.ldst_imm('str', 0, 6, 0)
        a.b('end')
        a.trap()
        a.trap()
        a.label('end')
        a.halt()
        notes = ['The halt at a branch target, with halts on the wrong path']
    elif case == 'afterPush':
        a.const(0, 0x77)
        a.const(1, 0x1234)
        a.mov_hi(14, 1)
        a.push([0, 1], lr=True)
        a.halt()
        notes = ['A PUSH (three stores) right before the halt: every store happens']
    elif case == 'afterPopPc':
        a.const(0, 0x88)
        a.bl('leaf')
        a.halt()
        a.trap()
        a.label('leaf')
        a.push([0], lr=True)
        a.ldst_imm('str', 0, 6, 0)
        a.pop([0], pc=True)
        a.trap()
        a.trap()
        notes = ['The halt at the return target of a POP {r0, pc}, with traps after the POP']
    else:  # inCall
        a.const(0, 0x66)
        a.bl('leaf')
        a.trap()
        a.label('leaf')
        a.ldst_imm('str', 0, 6, 0)
        a.halt()
        notes = ['The halt inside a call, right after a store']
    return notes, a


# ----------------------------------------------------------------------------------------------
# The stack (week 7). SP starts at 0x2000_0400; these programs move it to STACK, so pushes go
# below it and [SP, #imm] above it, clear of the results (0x2000_0000 up) and of BUF.

STACK = 0x2000_0800
BUF = 0x2000_0C00           # STM/LDM's blocks, 0x40 bytes each
BUF2 = 0x2000_0A00          # and their edge cases'
LOW = (0, 1, 2, 3, 4, 5, 7)  # the low registers but r6, the results pointer


class Rng:
    """xorshift32: the same numbers on every Python, so `gen_stress.py --check` is stable."""
    def __init__(self, seed):
        self.x = seed or 1

    def bits(self, n):
        x = self.x
        x ^= x << 13 & 0xFFFF_FFFF
        x ^= x >> 17
        x ^= x << 5 & 0xFFFF_FFFF
        self.x = x
        return x & ((1 << n) - 1)


def fill(a, regs, tag):
    """Give each register a value of its own (tag and register number, with bit 23 set), so a
    word that lands in the wrong slot or register shows."""
    for r in regs:
        a.const(r, (0x80 | tag & 0x7F) << 16 | r)


def set_sp(a, value, scratch=0):
    a.const(scratch, value)
    a.mov_hi(13, scratch)


def p_stack_sp():
    """SP at reset, LDR/STR [SP, #imm], ADD Rd, SP, ADD/SUB SP, and SP used right after it's
    written."""
    a = Asm()
    a.prologue()
    a.mov_hi(0, 13)                         # SP at reset: 0x2000_0400
    a.save(0)
    set_sp(a, STACK)
    # STR [SP, #imm], every Rt (r6 stores the results pointer as it is), offsets over the whole
    # range; then LDR back in the other order
    offsets = (0, 4, 8, 12, 60, 124, 128, 252, 256, 508, 512, 1016, 1020)
    for k, off in enumerate(offsets):
        rt = k % 8
        if rt != 6:
            a.const(rt, 0x5100 + k)
        a.str_sp(rt, off)
    for k, off in enumerate(reversed(offsets)):
        rt = LOW[k % 7]
        a.ldr_sp(rt, off)
        a.save(rt)
    # ADD Rd, SP, #imm
    for k, off in enumerate((0, 4, 400, 1020)):
        a.add_rd_sp(LOW[k], off)
        a.save(LOW[k])
    # SUB SP, then a store at the new SP; ADD SP back; then load those words from below
    for k, off in enumerate((4, 128, 508)):
        a.sub_sp(off)
        a.mov_hi(0, 13)
        a.save(0)
        a.const(1, 0x6100 + k)
        a.str_sp(1, 0)
    for off in (508, 128, 4):
        a.add_sp(off)
    a.mov_hi(0, 13)
    a.save(0)
    a.sub_sp(508)
    a.sub_sp(132)                           # SP = STACK - 640: the last store above
    for off in (0, 508, 636):
        a.ldr_sp(2, off)
        a.save(2)
    a.add_sp(508)
    a.add_sp(132)
    # SP written, then used by the very next instruction
    a.const(0, 0x7100)
    a.sub_sp(8)
    a.str_sp(0, 4)                          # a store right after SUB SP
    a.ldr_sp(1, 4)
    a.adds_reg(2, 1, 1)                     # a load used right away
    a.save(2)
    set_sp(a, STACK - 0x100)
    a.str_sp(6, 0)                          # a store right after MOV SP
    a.ldr_sp(3, 0)
    a.save(3)
    a.add_sp(4)
    a.add_rd_sp(4, 0)                       # ADD Rd, SP right after ADD SP
    a.save(4)
    a.movs(0, 12)
    a.add_hi(13, 0)                         # ADD SP, r0
    a.mov_hi(5, 13)
    a.save(5)
    a.movs(7, 8)
    a.add_hi(7, 13)                         # ADD r7, SP
    a.save(7)
    # The results pointer through the stack: loaded back into r6 and used right away
    a.str_sp(6, 8)
    a.movs(6, 0)
    a.ldr_sp(6, 8)
    a.save(7)
    a.halt()
    return ['SP at reset; LDR/STR [SP, #imm] for every Rt over the whole offset range; ADD Rd, SP;',
            'ADD/SUB SP; ADD SP, Rm; SP (and r6, through the stack) used right after it\'s written'], a


def p_push_pop():
    """PUSH and POP: every register alone, random lists up to all nine, and the hazards around
    them."""
    a = Asm()
    a.prologue()
    set_sp(a, STACK)
    rng = Rng(141)

    def check_pushed(n):
        """Save the n words just pushed (from SP up) and SP, then drop them."""
        for k in range(n):
            a.ldr_sp(0, 4 * k)
            a.save(0)
        a.mov_hi(0, 13)
        a.save(0)
        a.add_sp(4 * n)

    def stack_words(n, tag):
        """n known words at a new SP, for a POP."""
        a.sub_sp(4 * n)
        for k in range(n):
            a.const(0, (0xC0 | tag & 0x3F) << 16 | k)
            a.str_sp(0, 4 * k)

    # PUSH: every register alone, LR, random lists, and all nine
    for r in range(8):
        fill(a, [r] if r != 6 else [], r)
        a.push([r])
        check_pushed(1)
    a.const(0, 0x1E1E)
    a.mov_hi(14, 0)
    a.push([], lr=True)
    check_pushed(1)
    for tag in range(10, 22):
        regs = [r for r in range(8) if rng.bits(1)]
        lr = bool(rng.bits(1)) or not regs
        if lr:
            a.const(0, 0x1E00 | tag)
            a.mov_hi(14, 0)
        fill(a, [r for r in regs if r != 6], tag)
        a.push(regs, lr=lr)
        check_pushed(len(regs) + lr)
    fill(a, LOW, 30)
    a.const(0, 0x1E1E_0000)
    a.mov_hi(14, 0)
    a.movs(0, 0x30)
    a.push(range(8), lr=True)
    check_pushed(9)
    # POP: random lists of the low registers (but r6), each register then saved
    for tag in range(40, 50):
        regs = [r for r in LOW if rng.bits(1)] or [LOW[tag % 7]]
        stack_words(len(regs), tag)
        a.pop(regs)
        for r in regs:
            a.save(r)
        a.mov_hi(0, 13)
        a.save(0)
    # POP into r6, used right away as the results pointer
    a.push([6])
    a.movs(6, 0)
    a.pop([6])
    a.save(0)
    # A value made right before PUSH, last in its list (the last store), 1 to 3 instructions back
    for dist in (1, 2, 3):
        fill(a, [0, 1, 2, 3], 50 + dist)
        a.adds_imm8(3, 7)
        for _ in range(dist - 1):
            a.mov_hi(5, 1)
        a.push([0, 1, 2, 3])
        check_pushed(4)
    a.movs(0, 5)
    a.adds_imm8(0, 1)
    a.push([0])                             # an ALU result, the PUSH's only store
    check_pushed(1)
    # A load right before PUSH
    a.const(5, BUF)
    a.const(0, 0x99)
    a.ldst_imm('str', 0, 5, 0)
    a.movs(0, 0x11)
    a.ldst_imm('ldr', 1, 5, 0)
    a.push([0, 1])
    check_pushed(2)
    a.ldst_imm('ldr', 0, 5, 0)
    a.push([0])
    check_pushed(1)
    # MOV SP right before PUSH
    fill(a, [1, 2], 60)
    set_sp(a, STACK - 0x40)
    a.push([1, 2])
    check_pushed(2)
    set_sp(a, STACK)
    # PUSH then POP straight away, into other registers
    fill(a, [0, 1], 61)
    a.push([0, 1])
    a.pop([2, 3])
    a.save(2)
    a.save(3)
    # POP, then its last register (and its first) used right away
    stack_words(1, 62)
    a.pop([0])
    a.adds_reg(1, 0, 0)
    a.save(1)
    stack_words(4, 63)
    a.pop([0, 1, 2, 3])
    a.adds_reg(4, 3, 0)
    a.save(4)
    # POP then PUSH of the same registers; POP then a store to [SP]
    stack_words(2, 0)
    a.pop([1, 2])
    a.push([1, 2])
    check_pushed(2)
    stack_words(2, 1)
    a.pop([0])
    a.str_sp(0, 4)
    a.ldr_sp(1, 4)
    a.save(1)
    a.add_sp(4)
    a.mov_hi(0, 13)
    a.save(0)
    a.halt()
    return ['PUSH of every register alone, LR, random lists, all nine; POP of random lists;',
            'values made (by ALU, load, MOV SP) right before a PUSH; POP used right away;',
            'PUSH then POP, POP then PUSH, back to back'], a


def p_pop_pc():
    """Calls that save registers with PUSH {..., lr} and return with POP {..., pc}."""
    a = Asm()
    a.prologue()
    set_sp(a, STACK)
    a.b('main')
    a.trap()
    # leaf: POP {pc} alone
    a.label('leaf')
    a.push([], lr=True)
    a.adds_imm8(7, 1)
    a.pop([], pc=True)
    a.trap()
    a.trap()
    # mid: saves r4-r7 and LR, clobbers them (r6, the results pointer, too), calls leaf, and
    # returns a value with the PC: POP {r0, r4-r7, pc}
    a.label('mid')
    a.push([4, 5, 6, 7], lr=True)
    for r in (4, 5, 6, 7):
        a.movs(r, 0x40 + r)
    a.bl('leaf')
    a.mov_hi(0, 7)
    a.adds_imm8(0, 0x20)
    a.push([0])
    a.pop([0, 4, 5, 6, 7], pc=True)
    a.trap()
    a.trap()
    # sum: r0 = 0 + 1 + ... + r0, recursively, keeping r4 and LR on the stack
    a.label('sum')
    a.push([4], lr=True)
    a.lsls_imm(4, 0, 0)                     # movs r4, r0
    a.cmp_imm(0, 0)
    a.bcond('eq', 'sum_done')
    a.subs_imm8(0, 1)
    a.bl('sum')
    a.adds_reg(0, 0, 4)
    a.label('sum_done')
    a.pop([4], pc=True)
    a.trap()
    a.label('main')
    a.movs(7, 0)
    # From both halves of a word (the return addresses too)
    for pad in (0, 1):
        if pad:
            a.nop()
        a.movs(4, 0x14 + pad)
        a.movs(5, 0x15 + pad)
        a.bl('leaf')
        a.save(7)                           # (r6 used right after a POP {pc} return)
        a.bl('mid')
        for r in (0, 4, 5, 7):              # r0 returned; r4, r5, r7 kept
            a.save(r)
        a.movs(0, 10 + pad)
        a.bl('sum')
        a.save(0)
        a.save(4)
        a.mov_hi(1, 13)                     # SP back where it was
        a.save(1)
    # BLX to mid through a register
    pool, after = a.fresh('pool'), a.fresh('go')
    a.ldr_label(3, pool)
    a.blx(3)
    a.save(0)
    a.b(after)
    a.align4()
    a.label(pool)
    a.word_label('mid', 1)
    a.label(after)
    # POP {pc} of an address pushed from a register (Thumb bit set), traps on the way
    pool, target = a.fresh('pool'), a.fresh('target')
    a.ldr_label(0, pool)
    a.push([0])
    a.pop([], pc=True)
    a.trap()
    a.trap()
    a.align4()
    a.label(pool)
    a.word_label(target, 1)
    a.label(target)
    a.movs(0, 0x5C)
    a.save(0)
    a.mov_hi(1, 13)
    a.save(1)
    a.halt()
    return ['PUSH {..., lr} / POP {..., pc}: POP {pc} alone, POP {r0, r4-r7, pc} restoring r6 (used',
            'right after), recursion 11 and 12 deep, calls from both halves of a word, BLX, POP',
            '{pc} of a pushed address'], a


def p_ldm_stm():
    """STM and LDM: every base, random lists, the base in the list, and the hazards around
    them."""
    a = Asm()
    a.prologue()
    rng = Rng(7)
    # Every base (but r6), two random lists each: STM writes back; LDM loads the words into
    # other registers, and writes back
    block = BUF
    for base in LOW:
        for tag in range(2):
            others = [r for r in LOW if r != base]
            regs = [r for r in others if rng.bits(1)] or [others[tag]]
            dest = others[-len(regs):] if regs != others[-len(regs):] else others[:len(regs)]
            fill(a, regs, 8 * base + tag)
            a.const(base, block)
            a.stm(base, regs)
            a.save(base)
            a.const(base, block)
            a.ldm(base, dest)
            for r in dest:
                a.save(r)
            a.save(base)
            block += 0x40
    # STM with its base the lowest in the list: stores the base's old value
    fill(a, [1, 2], 20)
    a.const(0, BUF2)
    a.stm(0, [0, 1, 2])
    a.save(0)
    a.const(7, BUF2)
    a.ldm(7, [3, 4, 5])
    for r in (3, 4, 5, 7):
        a.save(r)
    # LDM with its base in the list, lowest, middle and highest: no write-back, the base gets
    # its word
    fill(a, [0, 1, 2], 21)
    a.const(3, BUF2 + 0x20)
    a.stm(3, [0, 1, 2])
    for base in (2, 3, 4):
        a.const(base, BUF2 + 0x20)
        a.ldm(base, [2, 3, 4])
        for r in (2, 3, 4):
            a.save(r)
    # All eight: STM r0!, {r0-r7}, and LDM r7, {r0-r5, r7}
    fill(a, [1, 2, 3, 4, 5, 7], 22)
    a.const(0, BUF2 + 0x40)
    a.stm(0, range(8))
    a.save(0)
    a.const(7, BUF2 + 0x40)
    a.ldm(7, [0, 1, 2, 3, 4, 5, 7])
    for r in LOW:
        a.save(r)
    # The base made right before (ALU, then a load), and the last store's data
    fill(a, [2, 3], 23)
    a.const(1, BUF2 + 0x80 - 4)
    a.adds_imm8(1, 4)
    a.stm(1, [2, 3])
    a.save(1)
    a.const(5, BUF2 + 0x90)
    a.const(0, BUF2 + 0x80)
    a.ldst_imm('str', 0, 5, 0)
    a.ldst_imm('ldr', 1, 5, 0)
    a.ldm(1, [2, 3])
    a.save(2)
    a.save(3)
    a.save(1)
    fill(a, [2, 3], 24)
    a.const(1, BUF2 + 0xA0)
    a.adds_imm8(3, 1)
    a.stm(1, [2, 3])
    a.const(1, BUF2 + 0xA0)
    a.ldm(1, [4, 5])
    a.save(4)
    a.save(5)
    # LDM, then its last register and its written-back base used right away
    a.const(1, BUF2 + 0x80)
    a.ldm(1, [2, 3])
    a.adds_reg(4, 3, 1)
    a.save(4)
    # STM then LDM of the same words, back to back; then LDM's registers stored by STM
    fill(a, [2, 3], 25)
    a.const(1, BUF2 + 0xC0)
    a.const(7, BUF2 + 0xC0)
    a.stm(1, [2, 3])
    a.ldm(7, [4, 5])
    a.stm(1, [4, 5])
    a.const(7, BUF2 + 0xC8)
    a.ldm(7, [2, 3])
    a.save(2)
    a.save(3)
    # Over the LED register (the whole 4 KB is the one register): device stores and loads, with
    # their wait cycles
    a.const(4, LEDS)
    a.movs(0, 0x15)
    a.movs(1, 0x6A)                         # (bits above 5 are dropped)
    a.stm(4, [0, 1])
    a.save(4)
    a.const(4, LEDS)
    a.ldm(4, [2, 3])
    for r in (2, 3, 4):
        a.save(r)
    a.halt()
    return ['STM/LDM with every base and random lists (write-back, word order); STM with its',
            'base lowest; LDM with its base in the list (no write-back); all eight; the base and',
            'data made right before; LDM used right away; STM/LDM back to back; over the LEDs'], a


def p_store_register():
    """STR/STRH/STRB [Rn, Rm]: three registers read, each made 1 to 3 instructions before."""
    a = Asm()
    a.prologue()
    a.const(4, 0x2000_0300)                 # the block the stores go to
    a.const(7, 0x2000_0380)                 # what load producers read: block, offset, data
    a.ldst_imm('str', 4, 7, 0)
    for k in range(16):
        a.const(0, 0x0101_0101 * (k + 1))
        a.ldst_imm('str', 0, 4, 4 * k)
    n = 0
    for dist in (1, 2, 3):
        for producer in ('alu', 'load'):
            for slot in ('data', 'base', 'offset', 'all'):
                for name in ('str', 'strh', 'strb'):
                    n += 1
                    word = 4 * (n % 16)
                    off = word + {'str': 0, 'strh': 2, 'strb': 3}[name]
                    data = (0x8000_0000 | n * 0x0003_0507) & 0xFFFF_FFFF
                    a.const(5, data)
                    a.const(0, off)
                    a.ldst_imm('str', 0, 7, 4)
                    a.ldst_imm('str', 5, 7, 8)
                    a.mov_hi(0, 5)
                    a.mov_hi(2, 4)
                    a.movs(3, off)
                    # The ones this case makes: wrong first, so a missed forward shows
                    made = {'data': [0], 'base': [2], 'offset': [3], 'all': [3, 2, 0]}[slot]
                    for r in made:
                        a.movs(r, 0x7C)
                    for r in made:
                        if producer == 'load':
                            a.ldst_imm('ldr', r, 7, {0: 8, 2: 0, 3: 4}[r])
                        elif r == 0:
                            a.mov_hi(0, 5)
                        elif r == 2:
                            a.adds_imm3(2, 4, 0)
                        else:
                            a.movs(3, off)
                    for _ in range(dist - 1):
                        a.mov_hi(1, 1)
                    a.ldst_reg(name, 0, 2, 3)
                    a.ldst_imm('ldr', 1, 4, word)
                    a.save(1)
    # Offsets that need all 32 bits of Rm: negative (base above the block), and past 16 bits
    # (base below it)
    for k, (rm, name) in enumerate(((-0x100, 'str'), (-0x0F_FFFE, 'strh'), (0x1_0003, 'strb'),
                                    (0x7F_FFF0, 'str'))):
        a.const(0, 0xA5C3_0F96 + k)
        a.const(3, rm)
        a.const(2, 0x2000_0300 + 4 * k - rm)
        a.ldst_reg(name, 0, 2, 3)
        a.ldst_imm('ldr', 1, 4, 4 * k)
        a.save(1)
    a.halt()
    return ['STR/STRH/STRB [Rn, Rm] with its data, base or offset (or all three) made by an ALU',
            'op or a load 1 to 3 instructions before: a third register read, forwarded; and',
            'offsets that need all 32 bits of Rm (negative, and past 16 bits)'], a


PROGRAMS = (
    [(f'alu_{op}', lambda op=op: p_alu(op)) for op in ('adds', 'subs', 'adcs', 'sbcs', 'cmp', 'cmn')]
    + [('alu_imm', p_alu_imm)]
    + [(f'logic_{op}', lambda op=op: p_logic(op)) for op in ('ands', 'orrs', 'eors', 'bics', 'mvns', 'tst')]
    + [(f'shift_{op}', lambda op=op: p_shift_reg(op)) for op in ('lsls', 'lsrs', 'asrs', 'rors')]
    + [(f'shift_{k}Imm', lambda k=k: p_shift_imm(k)) for k in ('lsls', 'lsrs', 'asrs')]
    + [('extend', p_extend), ('memory', p_memory), ('literal', p_literal)]
    + [(f'forward_{k}', lambda k=k: p_forward(k)) for k in ('alu', 'load')]
    + [('branches', p_branches), ('calls', p_calls), ('peripherals', p_peripherals), ('fetch', p_fetch)]
    + [(f'halt_{c}', lambda c=c: p_halt(c)) for c in ('afterStore', 'afterDeviceLoad', 'afterBranch', 'inCall')]
    + [('bus_backToBack', p_bus_back_to_back), ('leds', p_leds), ('counter', p_counter)]
    + [('stack_spRelative', p_stack_sp), ('stack_pushPop', p_push_pop), ('stack_popPc', p_pop_pc),
       ('stack_ldmStm', p_ldm_stm), ('memory_storeRegister', p_store_register)]
    + [(f'halt_{c}', lambda c=c: p_halt(c)) for c in ('afterPush', 'afterPopPc')]
)


def write_if_changed(path, text):
    """Write a file only if its contents change: deleting and remaking the folder makes
    iCloud's Desktop & Documents sync leave empty copies behind ('01_aluAdds 2')."""
    if os.path.exists(path) and open(path).read() == text:
        return
    with open(path, 'w') as f:
        f.write(text)


def build_all(out):
    os.makedirs(out, exist_ok=True)
    wanted = set()
    for i, (name, make) in enumerate(PROGRAMS, 1):
        title, a = make()
        camel = name.split('_')[0] + ''.join(w[:1].upper() + w[1:] for w in name.split('_')[1:])
        d = os.path.join(out, f'{i:02d}_{camel}')
        wanted.add(os.path.basename(d))
        os.makedirs(d, exist_ok=True)
        base = os.path.join(d, camel)
        header = title + ['', 'Made by tools/gen_stress.py (just gen-stress); expected outputs from the',
                          'golden model, tools/thumb_iss.py. Results go to SRAM from 0x20000000.']
        write_if_changed(base + '.imem', a.imem(header))
        code = a.size()
        code_size = max(1024, 1 << (code - 1).bit_length())
        write_if_changed(base + '.mem_sizes', f'// Code for {code} bytes of program; SRAM for the results\n'
                                              f'CODE_SIZE={code_size}\nSRAM_SIZE=4096\n')
        m = thumb_iss.load_program(base)
        m.counter = counter_model
        m.run()
        thumb_iss.write_outputs(m, base, write=write_if_changed)
        print(f'{os.path.relpath(d, ROOT)}: {code} bytes, {len(m.pcs)} instructions')
    # Programs it no longer makes
    for d in os.listdir(out):
        if d not in wanted and os.path.isdir(os.path.join(out, d)):
            shutil.rmtree(os.path.join(out, d))


def main():
    if '--check' in sys.argv[1:]:
        import filecmp
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            build_all(os.path.join(tmp, 'stress'))
            cmp = filecmp.dircmp(os.path.join(tmp, 'stress'), OUT)
            def differs(c):
                return c.left_only or c.right_only or c.diff_files or any(differs(s) for s in c.subdirs.values())
            if differs(cmp):
                sys.exit(f'{OUT} is out of date: run `just gen-stress`')
        print(f'{OUT} is up to date')
        return
    build_all(OUT)


if __name__ == '__main__':
    main()
