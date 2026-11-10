#!/usr/bin/env python3
"""A small Thumb assembler for generated test programs (tools/gen_stress.py): labels, every
instruction in scope, and helpers to build constants and capture the flags.

    a = Asm()
    a.movs(0, 5); a.label('top'); a.subs_imm8(0, 1); a.bcond('ne', 'top'); a.halt()
    text = a.imem(['a comment for the header'])

Branch offsets are resolved at the end (B<cond> +-256 bytes, B +-2 KiB, BL +-4 MiB).
"""

COND = {'eq': 0, 'ne': 1, 'cs': 2, 'cc': 3, 'mi': 4, 'pl': 5, 'vs': 6, 'vc': 7,
        'hi': 8, 'ls': 9, 'ge': 10, 'lt': 11, 'gt': 12, 'le': 13}
DP = {'ands': 0, 'eors': 1, 'lsls': 2, 'lsrs': 3, 'asrs': 4, 'adcs': 5, 'sbcs': 6, 'rors': 7,
      'tst': 8, 'rsbs': 9, 'cmp': 10, 'cmn': 11, 'orrs': 12, 'bics': 14, 'mvns': 15}

# The registers flag capture uses: 8, 4, 2, 1, 0 (set by Asm.prologue)
FLAG_N, FLAG_Z, FLAG_C, FLAG_V, ZERO = 8, 9, 10, 11, 12


class Asm:
    def __init__(self):
        self.items = []       # ('h', value, text) | ('label', name) | ('b', cond, label, text) | ('bl', label) | ('align',) | ('word', value)
        self.n = 0

    # ---- plumbing -------------------------------------------------------------------------

    def fresh(self, stem='L'):
        self.n += 1
        return f'{stem}{self.n}'

    def label(self, name):
        self.items.append(('label', name))

    def h(self, value, text):
        assert 0 <= value < 0x10000, (hex(value), text)
        self.items.append(('h', value, text))

    def align4(self):
        self.items.append(('align',))

    def word(self, value):
        self.items.append(('word', value & 0xFFFFFFFF))

    def word_label(self, label, add=0):
        """A word holding a label's address (+ add: 1 for the Thumb bit)."""
        self.items.append(('wordl', label, add))

    def ldr_label(self, rt, pool_label):
        """LDR (literal) of the word at pool_label (forward, within 1 KiB)."""
        self.items.append(('ldrl', rt, pool_label))

    def _layout(self):
        addr, labels = 0, {}
        for it in self.items:
            if it[0] == 'label':
                labels[it[1]] = addr
            elif it[0] == 'align':
                addr += addr % 4
            elif it[0] in ('word', 'wordl'):
                assert addr % 4 == 0, 'a word must be aligned: align4() first'
                addr += 4
            elif it[0] == 'bl':
                addr += 4
            else:
                addr += 2
        return labels

    def resolve(self):
        """-> [(address, halfword, text)] (address None for a label line)."""
        labels = self._layout()
        out, addr = [], 0
        for it in self.items:
            kind = it[0]
            if kind == 'label':
                out.append((None, None, f'{it[1]}:'))
            elif kind == 'align':
                if addr % 4:
                    out.append((addr, 0xBF00, 'nop    (align)'))
                    addr += 2
            elif kind == 'word':
                out.append((addr, it[1] & 0xFFFF, f'.word  0x{it[1]:08x}'))
                out.append((addr + 2, it[1] >> 16, ''))
                addr += 4
            elif kind == 'wordl':
                value = labels[it[1]] + it[2]
                out.append((addr, value & 0xFFFF, f'.word  {it[1]} + {it[2]}'))
                out.append((addr + 2, value >> 16, ''))
                addr += 4
            elif kind == 'ldrl':
                off = labels[it[2]] - ((addr + 4) & ~3)
                assert off % 4 == 0 and 0 <= off // 4 < 256, (it, off)
                out.append((addr, 0x4800 | it[1] << 8 | off // 4, f'ldr    r{it[1]}, ={it[2]}'))
                addr += 2
            elif kind == 'h':
                out.append((addr, it[1], it[2]))
                addr += 2
            elif kind == 'b':
                cond, target, text = it[1], labels[it[2]], it[3]
                off = (target - (addr + 4)) // 2
                if cond is None:
                    assert -1024 <= off < 1024, (text, off)
                    out.append((addr, 0xE000 | (off & 0x7FF), text))
                else:
                    assert -128 <= off < 128, (text, off)
                    out.append((addr, 0xD000 | COND[cond] << 8 | (off & 0xFF), text))
                addr += 2
            else:  # bl
                off = labels[it[1]] - (addr + 4)
                assert -(1 << 24) <= off < (1 << 24)
                s = off >> 24 & 1
                i1, i2 = off >> 23 & 1, off >> 22 & 1
                j1, j2 = (1 - i1) ^ s, (1 - i2) ^ s
                out.append((addr, 0xF000 | s << 10 | (off >> 12 & 0x3FF), f'bl     {it[1]}'))
                out.append((addr + 2, 0xD000 | j1 << 13 | j2 << 11 | (off >> 1 & 0x7FF), ''))
                addr += 4
        return out

    def size(self):
        return sum(1 for a, _, _ in self.resolve() if a is not None) * 2

    def imem(self, header):
        lines = [f'// {line}' for line in header] + ['@00000000']
        for addr, value, text in self.resolve():
            if addr is None:
                lines.append(f'// {text}')
            else:
                lines.append(f'{value & 0xFF:02X} {value >> 8:02X}        // {addr:04x}: {text}')
        return '\n'.join(lines) + '\n'

    # ---- instructions ---------------------------------------------------------------------

    def movs(self, rd, imm):       self.h(0x2000 | rd << 8 | imm, f'movs   r{rd}, #{imm}')
    def cmp_imm(self, rn, imm):    self.h(0x2800 | rn << 8 | imm, f'cmp    r{rn}, #{imm}')
    def adds_imm8(self, rd, imm):  self.h(0x3000 | rd << 8 | imm, f'adds   r{rd}, #{imm}')
    def subs_imm8(self, rd, imm):  self.h(0x3800 | rd << 8 | imm, f'subs   r{rd}, #{imm}')
    def adds_reg(self, rd, rn, rm): self.h(0x1800 | rm << 6 | rn << 3 | rd, f'adds   r{rd}, r{rn}, r{rm}')
    def subs_reg(self, rd, rn, rm): self.h(0x1A00 | rm << 6 | rn << 3 | rd, f'subs   r{rd}, r{rn}, r{rm}')
    def adds_imm3(self, rd, rn, imm): self.h(0x1C00 | imm << 6 | rn << 3 | rd, f'adds   r{rd}, r{rn}, #{imm}')
    def subs_imm3(self, rd, rn, imm): self.h(0x1E00 | imm << 6 | rn << 3 | rd, f'subs   r{rd}, r{rn}, #{imm}')
    def lsls_imm(self, rd, rm, n): self.h(0x0000 | n << 6 | rm << 3 | rd, f'lsls   r{rd}, r{rm}, #{n}')
    def lsrs_imm(self, rd, rm, n): self.h(0x0800 | n << 6 | rm << 3 | rd, f'lsrs   r{rd}, r{rm}, #{n}')
    def asrs_imm(self, rd, rm, n): self.h(0x1000 | n << 6 | rm << 3 | rd, f'asrs   r{rd}, r{rm}, #{n}')

    def dp(self, name, rdn, rm):
        self.h(0x4000 | DP[name] << 6 | rm << 3 | rdn, f'{name:6} r{rdn}, r{rm}')

    def add_hi(self, rdn, rm):  self.h(0x4400 | (rdn >> 3) << 7 | rm << 3 | (rdn & 7), f'add    r{rdn}, r{rm}')
    def cmp_hi(self, rn, rm):   self.h(0x4500 | (rn >> 3) << 7 | rm << 3 | (rn & 7), f'cmp    r{rn}, r{rm}')
    def mov_hi(self, rd, rm):   self.h(0x4600 | (rd >> 3) << 7 | rm << 3 | (rd & 7), f'mov    r{rd}, r{rm}')
    def bx(self, rm):           self.h(0x4700 | rm << 3, f'bx     r{rm}')
    def blx(self, rm):          self.h(0x4780 | rm << 3, f'blx    r{rm}')

    def ldr_lit(self, rt, imm8): self.h(0x4800 | rt << 8 | imm8, f'ldr    r{rt}, [pc, #{4 * imm8}]')
    def adr(self, rd, imm8):     self.h(0xA000 | rd << 8 | imm8, f'adr    r{rd}, #{4 * imm8}')

    def ldst_reg(self, name, rt, rn, rm):
        op = ['str', 'strh', 'strb', 'ldrsb', 'ldr', 'ldrh', 'ldrb', 'ldrsh'].index(name)
        self.h(0x5000 | op << 9 | rm << 6 | rn << 3 | rt, f'{name:6} r{rt}, [r{rn}, r{rm}]')

    def ldst_imm(self, name, rt, rn, offset):
        size = {'str': 4, 'ldr': 4, 'strb': 1, 'ldrb': 1, 'strh': 2, 'ldrh': 2}[name]
        assert offset % size == 0 and 0 <= offset // size < 32, (name, offset)
        base = {'str': 0x6000, 'ldr': 0x6800, 'strb': 0x7000, 'ldrb': 0x7800, 'strh': 0x8000, 'ldrh': 0x8800}[name]
        self.h(base | (offset // size) << 6 | rn << 3 | rt, f'{name:6} r{rt}, [r{rn}, #{offset}]')

    def ext(self, name, rd, rm):
        op = ['sxth', 'sxtb', 'uxth', 'uxtb'].index(name)
        self.h(0xB200 | op << 6 | rm << 3 | rd, f'{name:6} r{rd}, r{rm}')

    def rev(self, name, rd, rm):
        op = {'rev': 0, 'rev16': 1, 'revsh': 3}[name]
        self.h(0xBA00 | op << 6 | rm << 3 | rd, f'{name:6} r{rd}, r{rm}')

    def nop(self):  self.h(0xBF00, 'nop')
    def halt(self): self.h(0xDE00, 'udf    #0          @ halt')
    def trap(self):
        # UDF #1, not #0 (the halt): the core doesn't halt on it, and the golden model
        # refuses it, so a test that runs one fails rather than halting early
        self.h(0xDE01, 'udf    #1          @ must never run')

    def b(self, label):          self.items.append(('b', None, label, f'b      {label}'))
    def bcond(self, cond, label): self.items.append(('b', cond, label, f'b{cond:5} {label}'))
    def bl(self, label):         self.items.append(('bl', label))

    # ---- helpers --------------------------------------------------------------------------

    def const(self, rd, value):
        """rd = value (32 bits), with MOVS/LSLS/ADDS (flags change)."""
        value &= 0xFFFFFFFF
        if value < 256:
            self.movs(rd, value)
            return
        if value == 0xFFFFFFFF:
            self.movs(rd, 0)
            self.dp('mvns', rd, rd)
            return
        bs = [value >> s & 0xFF for s in (24, 16, 8, 0)]
        while bs[0] == 0:
            bs.pop(0)
        self.movs(rd, bs[0])
        for b in bs[1:]:
            self.lsls_imm(rd, rd, 8)
            if b:
                self.adds_imm8(rd, b)

    def prologue(self, base=0x2000_0000):
        """r6 = base (results go there, 4 bytes at a time); r8-r12 = 8, 4, 2, 1, 0."""
        for reg, v in ((FLAG_N, 8), (FLAG_Z, 4), (FLAG_C, 2), (FLAG_V, 1), (ZERO, 0)):
            self.movs(0, v)
            self.mov_hi(reg, 0)
        self.const(6, base)

    def capture_flags(self, rd):
        """rd = N*8 + Z*4 + C*2 + V, without changing the flags (high-register ADD and MOV)."""
        self.mov_hi(rd, ZERO)
        for cond, reg in (('pl', FLAG_N), ('ne', FLAG_Z), ('cc', FLAG_C), ('vc', FLAG_V)):
            skip = self.fresh('f')
            self.bcond(cond, skip)
            self.add_hi(rd, reg)
            self.label(skip)

    def save(self, rt):
        """Store rt at [r6], and move r6 on (ADDS: the flags change)."""
        self.ldst_imm('str', rt, 6, 0)
        self.adds_imm8(6, 4)

    def save_flags(self, scratch=7):
        self.capture_flags(scratch)
        self.save(scratch)
