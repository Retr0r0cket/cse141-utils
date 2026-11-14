#!/usr/bin/env python3
"""A golden model: an instruction-set simulator for the ARMv6-M Thumb instructions in scope
(weeks 3 to 7: week 7 adds the stack: PUSH, POP, LDM, STM, the SP-relative forms, and
STR/STRH/STRB with a register offset), written from the architecture's rules, not from our
RTL, so a test checked against it doesn't share a core's mistakes. SP starts at 0x2000_0400,
as the core resets it.

    thumb_iss.py <program> [--out DIR]      run <program>.imem; write .pcs, .after.dmem, .bus

It runs the program as the test bench does (Code from <name>.imem, SRAM from <name>.before.dmem
if there is one, otherwise unknown) until the halt (UDF #0), and writes what `just
check-program` compares, in its formats:

  <name>.pcs         every instruction executed, in order (a BL once, at its first halfword)
  <name>.after.dmem  every byte of SRAM known at the halt (written, or loaded at the start)
  <name>.bus         every store, with its data, and every device load, in order (normal loads
                     are listed too; check-program ignores those)

A program that reads SRAM nobody wrote, or the cycle counter (its value depends on timing),
stops with an error: a test built on this model can't depend on what it can't predict.
"""

import argparse
import os
import sys

SRAM_BASE = 0x2000_0000
LED_ADDR = 0x4000_1000      # the whole 4 KB from here is the one LED register (led_apb)
COUNTER_BASE = 0x4000_2000  # COUNT at +0, CLEAR at +4; every other offset reads 0
SP_RESET = 0x2000_0400      # the top of SRAM: where the core puts SP at reset


class Unpredictable(Exception):
    pass


def u32(x):
    return x & 0xFFFF_FFFF


def s32(x):
    x &= 0xFFFF_FFFF
    return x - (1 << 32) if x & 0x8000_0000 else x


def sext(value, bits):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) & 1 else value


def read_bytes(path):
    """A $readmemh byte file (an .imem or .dmem): {address: byte}."""
    mem, addr = {}, 0
    for line in open(path):
        line = line.split('//', 1)[0].strip()
        for tok in line.split():
            if tok.startswith('@'):
                addr = int(tok[1:], 16)
            else:
                mem[addr] = int(tok, 16)
                addr += 1
    return mem


class Machine:
    def __init__(self, code, sram=None, sram_size=1024):
        self.code = code                       # {byte address: byte}
        self.sram = dict(sram or {})           # {offset: byte}: the bytes known
        self.sram_size = sram_size
        self.r = [0] * 16
        self.r[13] = SP_RESET
        self.n = self.z = self.c = self.v = 0
        self.led = 0
        # What a read of the counter's COUNT returns: counter(machine), or None to refuse it
        # (its value depends on timing). A test that reads COUNT gives a model, and checks
        # only what any core's count satisfies (see tools/gen_stress.py).
        self.counter = None
        self.cleared_at = 0                    # len(pcs) at the last CLEAR
        self.pcs = []
        self.bus = []
        self.halted = False

    # ---- memory --------------------------------------------------------------------------

    def fetch16(self, addr):
        lo, hi = self.code.get(addr), self.code.get(addr + 1)
        if lo is None or hi is None:
            raise Unpredictable(f'fetch from {addr:08x}, outside the program')
        return lo | hi << 8

    def load(self, addr, size):
        if addr % size:
            raise Unpredictable(f'unaligned {size}-byte load at {addr:08x}')
        if addr >= 0x4000_0000:
            if size != 4:
                raise Unpredictable(f'{size}-byte device load at {addr:08x}')
            self.bus.append(f'read  addr={addr:08x} (device) size={size}')
            if LED_ADDR <= addr < LED_ADDR + 0x1000:
                return self.led
            if COUNTER_BASE <= addr < COUNTER_BASE + 0x1000:
                if addr == COUNTER_BASE:
                    if self.counter is None:
                        raise Unpredictable('a read of the cycle counter (its value depends on timing)')
                    return self.counter(self) & 0xFFFF_FFFF
                return 0
            raise Unpredictable(f'device load from {addr:08x}, unmapped')
        self.bus.append(f'read  addr={addr:08x} (normal) size={size}')
        value = 0
        for i in range(size):
            a = addr + i
            if a < SRAM_BASE:
                b = self.code.get(a)
                if b is None:
                    raise Unpredictable(f'load from {a:08x}, outside the program')
            else:
                b = self.sram.get(a - SRAM_BASE)
                if b is None:
                    raise Unpredictable(f'load from {a:08x}, SRAM nobody wrote')
            value |= b << (8 * i)
        return value

    def store(self, addr, size, value):
        if addr % size:
            raise Unpredictable(f'unaligned {size}-byte store at {addr:08x}')
        value &= (1 << (8 * size)) - 1
        kind = 'device' if addr >= 0x4000_0000 else 'normal'
        self.bus.append(f'write addr={addr:08x} ({kind}) size={size} data={value:0{2 * size}x}')
        if addr >= 0x4000_0000:
            if size != 4:
                raise Unpredictable(f'{size}-byte device store at {addr:08x}')
            if LED_ADDR <= addr < LED_ADDR + 0x1000:
                self.led = value & 0x3F           # only bits 5:0 are kept (led_apb)
            elif addr == COUNTER_BASE + 4:
                self.cleared_at = len(self.pcs)   # CLEAR
            elif not COUNTER_BASE <= addr < COUNTER_BASE + 0x1000:
                raise Unpredictable(f'device store to {addr:08x}, unmapped')
            return
        if not SRAM_BASE <= addr < SRAM_BASE + self.sram_size:
            raise Unpredictable(f'store to {addr:08x}, outside SRAM (Code is read-only)')
        for i in range(size):
            self.sram[addr - SRAM_BASE + i] = value >> (8 * i) & 0xFF

    # ---- flags ---------------------------------------------------------------------------

    def nz(self, result):
        self.n, self.z = result >> 31 & 1, int(u32(result) == 0)

    def add_with_carry(self, a, b, carry):
        """ARM's AddWithCarry: (result, carry out, overflow)."""
        unsigned = u32(a) + u32(b) + carry
        signed = s32(a) + s32(b) + carry
        result = u32(unsigned)
        return result, int(unsigned != result), int(s32(result) != signed)

    # ---- shifts (ARM's Shift_C) ----------------------------------------------------------

    def shift_c(self, kind, value, amount, carry_in):
        value = u32(value)
        if amount == 0:
            return value, carry_in
        if kind == 'lsl':
            if amount <= 32:
                full = value << amount
                return u32(full), full >> 32 & 1
            return 0, 0
        if kind == 'lsr':
            if amount <= 32:
                return value >> amount, value >> (amount - 1) & 1
            return 0, 0
        if kind == 'asr':
            sv = s32(value)
            if amount < 32:
                return u32(sv >> amount), value >> (amount - 1) & 1
            return u32(sv >> 31), value >> 31 & 1
        if kind == 'ror':
            n = amount % 32
            result = value if n == 0 else u32(value >> n | value << (32 - n))
            return result, result >> 31 & 1
        raise ValueError(kind)

    # ---- one instruction -----------------------------------------------------------------

    def step(self):
        pc = self.r[15]
        instr = self.fetch16(pc)
        self.pcs.append(pc)
        next_pc = pc + 2
        r = self.r
        read_pc = pc + 4                          # what an instruction reads as PC

        def reg(i):
            return read_pc if i == 15 else r[i]

        top5 = instr >> 11
        if instr == 0xDE00:                       # UDF #0: the halt
            self.halted = True
            return
        if top5 in (0b00000, 0b00001, 0b00010):   # LSLS/LSRS/ASRS Rd, Rm, #imm5
            imm5, rm, rd = instr >> 6 & 31, instr >> 3 & 7, instr & 7
            kind = ('lsl', 'lsr', 'asr')[top5]
            amount = imm5 if kind == 'lsl' or imm5 else 32
            result, c = self.shift_c(kind, r[rm], amount, self.c)
            r[rd] = result
            self.nz(result)
            self.c = c
        elif top5 == 0b00011:                     # ADDS/SUBS Rd, Rn, Rm|#imm3
            imm, sub, rmi, rn, rd = instr >> 10 & 1, instr >> 9 & 1, instr >> 6 & 7, instr >> 3 & 7, instr & 7
            b = rmi if imm else r[rmi]
            if sub:
                result, c, v = self.add_with_carry(r[rn], ~b, 1)
            else:
                result, c, v = self.add_with_carry(r[rn], b, 0)
            r[rd] = result
            self.nz(result)
            self.c, self.v = c, v
        elif top5 >> 2 == 0b001:                  # MOVS/CMP/ADDS/SUBS Rdn, #imm8
            op, rdn, imm8 = instr >> 11 & 3, instr >> 8 & 7, instr & 0xFF
            if op == 0:
                r[rdn] = imm8
                self.nz(imm8)
            elif op == 1:
                result, self.c, self.v = self.add_with_carry(r[rdn], ~imm8, 1)
                self.nz(result)
            elif op == 2:
                result, self.c, self.v = self.add_with_carry(r[rdn], imm8, 0)
                r[rdn] = result
                self.nz(result)
            else:
                result, self.c, self.v = self.add_with_carry(r[rdn], ~imm8, 1)
                r[rdn] = result
                self.nz(result)
        elif instr >> 10 == 0b010000:             # data processing, Rdn, Rm
            op, rm, rdn = instr >> 6 & 15, instr >> 3 & 7, instr & 7
            a, b = r[rdn], r[rm]
            write = True
            if op == 0:   result = a & b
            elif op == 1: result = a ^ b
            elif op in (2, 3, 4, 7):
                kind = {2: 'lsl', 3: 'lsr', 4: 'asr', 7: 'ror'}[op]
                result, self.c = self.shift_c(kind, a, b & 0xFF, self.c)
            elif op == 5: result, self.c, self.v = self.add_with_carry(a, b, self.c)
            elif op == 6: result, self.c, self.v = self.add_with_carry(a, ~b, self.c)
            elif op == 8: result, write = a & b, False
            elif op == 9: result, self.c, self.v = self.add_with_carry(~b, 0, 1)        # RSBS Rd, Rn, #0 (Rn at 5:3)
            elif op == 10: (result, self.c, self.v), write = self.add_with_carry(a, ~b, 1), False
            elif op == 11: (result, self.c, self.v), write = self.add_with_carry(a, b, 0), False
            elif op == 12: result = a | b
            elif op == 13: raise Unpredictable('MULS is not in scope')
            elif op == 14: result = a & ~b
            else:          result = ~b
            result = u32(result)
            self.nz(result)
            if write:
                r[rdn] = result
        elif instr >> 10 == 0b010001:             # high registers: ADD, CMP, MOV, BX/BLX
            op, rm = instr >> 8 & 3, instr >> 3 & 15
            rdn = (instr >> 4 & 8) | (instr & 7)
            if op == 0:
                result = u32(reg(rdn) + reg(rm))
                if rdn == 15:
                    next_pc = result & ~1
                else:
                    r[rdn] = result
            elif op == 1:
                result, self.c, self.v = self.add_with_carry(reg(rdn), ~reg(rm), 1)
                self.nz(result)
            elif op == 2:
                if rdn == 15:
                    next_pc = reg(rm) & ~1
                else:
                    r[rdn] = reg(rm)
            else:
                target = reg(rm)
                if instr >> 7 & 1:                # BLX
                    r[14] = u32(pc + 2) | 1
                next_pc = target & ~1
        elif top5 == 0b01001:                     # LDR Rt, [PC, #imm8]
            rt, imm8 = instr >> 8 & 7, instr & 0xFF
            r[rt] = self.load((read_pc & ~3) + 4 * imm8, 4)
        elif instr >> 12 == 0b0101:               # loads and stores, [Rn, Rm]
            self.load_store_register(instr >> 9 & 7, instr & 7, instr >> 3 & 7, instr >> 6 & 7)
        elif top5 >> 2 == 0b011 or top5 >> 1 == 0b1000:  # loads and stores, [Rn, #imm5]
            imm5, rn, rt = instr >> 6 & 31, instr >> 3 & 7, instr & 7
            load = instr >> 11 & 1
            size = 2 if top5 >> 1 == 0b1000 else (1 if instr >> 12 & 1 else 4)
            addr = u32(r[rn] + imm5 * size)
            if load:
                r[rt] = self.load(addr, size)
            else:
                self.store(addr, size, r[rt])
        elif top5 == 0b10100:                     # ADR Rd, #imm8
            r[instr >> 8 & 7] = (read_pc & ~3) + 4 * (instr & 0xFF)
        elif top5 >> 1 == 0b1001:                 # LDR/STR Rt, [SP, #imm8 * 4]
            self.load_store_sp(instr >> 11 & 1, instr >> 8 & 7, 4 * (instr & 0xFF))
        elif top5 == 0b10101:                     # ADD Rd, SP, #imm8 * 4
            self.add_sp_imm(instr >> 8 & 7, 4 * (instr & 0xFF))
        elif instr >> 8 == 0b10110000:            # ADD/SUB SP, SP, #imm7 * 4
            self.adjust_sp(instr >> 7 & 1, 4 * (instr & 0x7F))
        elif instr >> 9 == 0b1011010:             # PUSH {list, LR?}
            self.push(self.reg_list(instr & 0xFF, 14 if instr >> 8 & 1 else None))
        elif instr >> 9 == 0b1011110:             # POP {list, PC?}
            target = self.pop(self.reg_list(instr & 0xFF, 15 if instr >> 8 & 1 else None))
            if target is not None:
                next_pc = target
        elif instr >> 12 == 0b1100:               # STM/LDM Rn!, {list}
            rn, regs = instr >> 8 & 7, self.reg_list(instr & 0xFF)
            if instr >> 11 & 1:
                self.ldm(rn, regs)
            else:
                self.stm(rn, regs)
        elif instr >> 8 == 0b10110010:            # SXTH, SXTB, UXTH, UXTB
            op, rm, rd = instr >> 6 & 3, instr >> 3 & 7, instr & 7
            v = r[rm]
            r[rd] = u32([sext(v, 16), sext(v, 8), v & 0xFFFF, v & 0xFF][op])
        elif instr >> 8 == 0b10111010 and (instr >> 6 & 3) != 2:   # REV, REV16, REVSH
            op, rm, rd = instr >> 6 & 3, instr >> 3 & 7, instr & 7
            v = r[rm]
            b = [v >> (8 * i) & 0xFF for i in range(4)]
            if op == 0:
                r[rd] = b[0] << 24 | b[1] << 16 | b[2] << 8 | b[3]
            elif op == 1:
                r[rd] = b[2] << 24 | b[3] << 16 | b[0] << 8 | b[1]
            else:
                r[rd] = u32(sext(b[0] << 8 | b[1], 16))
        elif instr == 0xBF00:                     # NOP
            pass
        elif instr >> 12 == 0b1101:               # B<cond>
            cond = instr >> 8 & 15
            if cond >= 14:
                raise Unpredictable(f'UDF/SVC {instr:04x} at {pc:08x}')
            if self.passes(cond):
                next_pc = u32(read_pc + 2 * sext(instr & 0xFF, 8))
        elif top5 == 0b11100:                     # B
            next_pc = u32(read_pc + 2 * sext(instr & 0x7FF, 11))
        elif top5 == 0b11110:                     # BL (two halfwords)
            second = self.fetch16(pc + 2)
            if second >> 14 != 0b11 or not second >> 12 & 1:
                raise Unpredictable(f'BL at {pc:08x} without its second halfword')
            s = instr >> 10 & 1
            j1, j2 = second >> 13 & 1, second >> 11 & 1
            i1, i2 = 1 - (j1 ^ s), 1 - (j2 ^ s)
            offset = sext(s << 24 | i1 << 23 | i2 << 22 | (instr & 0x3FF) << 12 | (second & 0x7FF) << 1, 25)
            r[14] = u32(pc + 4) | 1
            next_pc = u32(pc + 4 + offset)
        else:
            raise Unpredictable(f'{instr:04x} at {pc:08x} is not in scope')
        for i in range(15):
            r[i] = u32(r[i])
        r[15] = next_pc

    # ---- the stack and register-offset instructions (each a method, so a broken version can
    # replace just one of them: tools/stack_mutants.py) ------------------------------------

    @staticmethod
    def reg_list(low8, extra=None):
        regs = [i for i in range(8) if low8 >> i & 1] + ([extra] if extra is not None else [])
        if not regs:
            raise Unpredictable('an empty register list')
        return regs

    def load_store_register(self, op, rt, rn, rm):
        """STR/STRH/STRB/LDRSB/LDR/LDRH/LDRB/LDRSH Rt, [Rn, Rm]."""
        r = self.r
        addr = u32(r[rn] + r[rm])
        if op == 0:   self.store(addr, 4, r[rt])
        elif op == 1: self.store(addr, 2, r[rt])
        elif op == 2: self.store(addr, 1, r[rt])
        elif op == 3: r[rt] = u32(sext(self.load(addr, 1), 8))
        elif op == 4: r[rt] = self.load(addr, 4)
        elif op == 5: r[rt] = self.load(addr, 2)
        elif op == 6: r[rt] = self.load(addr, 1)
        else:         r[rt] = u32(sext(self.load(addr, 2), 16))

    def load_store_sp(self, load, rt, offset):
        """LDR/STR Rt, [SP, #offset]."""
        addr = u32(self.r[13] + offset)
        if load:
            self.r[rt] = self.load(addr, 4)
        else:
            self.store(addr, 4, self.r[rt])

    def add_sp_imm(self, rd, offset):
        """ADD Rd, SP, #offset."""
        self.r[rd] = u32(self.r[13] + offset)

    def adjust_sp(self, sub, offset):
        """ADD SP, SP, #offset, or SUB."""
        self.r[13] = u32(self.r[13] - offset if sub else self.r[13] + offset)

    def push(self, regs):
        """PUSH: the lowest register at the lowest address, SP down by 4 per register."""
        addr = u32(self.r[13] - 4 * len(regs))
        for i in regs:
            self.store(addr, 4, self.r[i])
            addr += 4
        self.r[13] = u32(self.r[13] - 4 * len(regs))

    def pop(self, regs):
        """POP: the lowest register from the lowest address, PC last; SP up by 4 per register.
        -> the new PC if PC was in the list, else None."""
        addr, target = self.r[13], None
        for i in regs:
            value = self.load(addr, 4)
            if i == 15:
                if not value & 1:
                    raise Unpredictable(f'POP {{pc}} of {value:08x}: bit 0 clear (a HardFault)')
                target = value & ~1
            else:
                self.r[i] = value
            addr += 4
        self.r[13] = u32(self.r[13] + 4 * len(regs))
        return target

    def stm(self, rn, regs):
        """STM Rn!, {regs}: always writes back. Rn in the list stores its old value, and must
        be the lowest register there (otherwise what's stored is UNKNOWN)."""
        if rn in regs and rn != regs[0]:
            raise Unpredictable(f'STM r{rn}! with r{rn} in its list, but not the lowest')
        addr = self.r[rn]
        for i in regs:
            self.store(addr, 4, self.r[i])
            addr += 4
        self.r[rn] = u32(self.r[rn] + 4 * len(regs))

    def ldm(self, rn, regs):
        """LDM Rn{!}, {regs}: writes back only if Rn isn't in the list (else Rn gets the word
        loaded for it)."""
        addr, values = self.r[rn], []
        for _ in regs:
            values.append(self.load(addr, 4))
            addr += 4
        if rn not in regs:
            self.r[rn] = u32(self.r[rn] + 4 * len(regs))
        for i, v in zip(regs, values):
            self.r[i] = v

    def passes(self, cond):
        n, z, c, v = self.n, self.z, self.c, self.v
        return [z, not z, c, not c, n, not n, v, not v, c and not z, not c or z,
                n == v, n != v, not z and n == v, z or n != v][cond]

    def run(self, max_instructions=100000):
        while not self.halted:
            if len(self.pcs) >= max_instructions:
                raise Unpredictable(f'no halt after {max_instructions} instructions')
            self.step()


def load_program(path):
    """-> a Machine, ready to run <path>.imem (and <path>.before.dmem, .mem_sizes if there)."""
    sram_size = 1024
    sizes = path + '.mem_sizes'
    if os.path.exists(sizes):
        for line in open(sizes):
            if line.strip().startswith('SRAM_SIZE='):
                sram_size = int(line.split('=')[1])
    before = path + '.before.dmem'
    return Machine(read_bytes(path + '.imem'),
                   read_bytes(before) if os.path.exists(before) else None, sram_size)


def write_outputs(m, path, header=None, write=None):
    """.pcs, .after.dmem and .bus, as check-program reads them. `write(path, text)` writes
    a file (by default, always)."""
    def plain(p, text):
        with open(p, 'w') as f:
            f.write(text)
    write = write or plain
    head = [f'// {line}' for line in (header or [])]
    write(path + '.pcs', '\n'.join(head + ['// Expected PC of each instruction the core runs, in order, and the halt last.']
                                   + [f'pc={pc:08x}' for pc in m.pcs]) + '\n')
    write(path + '.after.dmem', '\n'.join(head + ['// Expected DMEM contents after the program halts: every byte written.']
                                          + [f'@{off:08x} {b:02x}' for off, b in sorted(m.sram.items())]) + '\n')
    write(path + '.bus', '\n'.join(head + ['// Expected loads and stores on the bus, in order (normal loads aren\'t checked).']
                                   + m.bus) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('program', help='path to the program, without .imem')
    ap.add_argument('--out', help='write the expected files here (default: next to the program)')
    args = ap.parse_args()
    m = load_program(args.program)
    try:
        m.run()
    except Unpredictable as e:
        sys.exit(f'{args.program}: {e}')
    out = os.path.join(args.out, os.path.basename(args.program)) if args.out else args.program
    write_outputs(m, out)
    print(f'{len(m.pcs)} instructions; wrote {out}.pcs, .after.dmem, .bus')


if __name__ == '__main__':
    main()
