"""Branch and memory statistics for the test programs: for each program, how
many instructions run, loads/stores (and back-to-back ones), conditional
branches (taken, backward), B/BL/BX/PC writes, and how many conditional
branches each simple predictor gets wrong (always not taken, BTFN, a counter
reset on each new branch, a 16-entry 2-bit counter table).

    python3 tools/branch_stats.py test_programs/

Walks each program's .pcs (the instructions it runs, in order) against its
.imem.
"""
import glob, os, re, sys
root = sys.argv[1]

def load_imem(path):
    mem = {}; addr = 0
    for line in open(path):
        line = line.split('//')[0].strip()
        if not line: continue
        if line.startswith('@'): addr = int(line[1:], 16); continue
        for b in line.split():
            mem[addr] = int(b, 16); addr += 1
    return mem

def hw(mem, a): return mem.get(a, 0) | (mem.get(a+1, 0) << 8)

def kind(i):
    if (i >> 12) == 0xD and ((i >> 8) & 0xF) < 0xE:
        off = i & 0xFF
        off = off - 256 if off & 0x80 else off
        return 'bcond', off
    if (i >> 11) == 0x1C:
        off = i & 0x7FF
        return 'b', (off - 2048 if off & 0x400 else off)
    if (i >> 11) == 0x1E: return 'bl', None
    if (i >> 7) == 0x8E: return 'bx', None                      # 010001 11 0
    if (i >> 8) in (0x44, 0x46) and (((i >> 4) & 8) | (i & 7)) == 15:
        return 'pcwrite', None                                    # ADD/MOV Rd=PC
    if (i >> 12) in (5, 6, 7, 8, 9) or (i >> 11) == 9: return 'mem', None
    return 'other', None

class Tables:
    def __init__(s, n): s.n = n; s.t = {}
    def pred(s, pc): return s.t.get((pc >> 1) % s.n, 2) >= 2
    def upd(s, pc, tk):
        k = (pc >> 1) % s.n; c = s.t.get(k, 2)
        s.t[k] = min(3, c + 1) if tk else max(0, c - 1)

tot = {}
def add(k, v): tot[k] = tot.get(k, 0) + v
print(f"{'program':28} {'instr':>5} {'budget':>6} {'mem':>4} {'b2bmem':>6} {'bcond':>5} {'tkn':>4} {'bwd':>4} {'bwdT':>4} {'b':>3} {'bl':>3} {'bx':>3} {'pcw':>3} | mispredicts: NT  BTFN reset1 tbl16")
for d in sorted(glob.glob(os.path.join(root, 'level*/*/'))):
    imem = glob.glob(d + '*.imem'); pcs = glob.glob(d + '*.pcs')
    if not imem or not pcs: continue
    mem = load_imem(imem[0])
    seq = [int(m.group(1), 16) for m in re.finditer(r'pc=([0-9a-fA-F]+)', open(pcs[0]).read())]
    bud = open(glob.glob(d + '*.budget')[0]).read() if glob.glob(d + '*.budget') else ''
    bud = int(re.findall(r'^\s*(\d+)', bud, re.M)[0]) if re.findall(r'^\s*(\d+)', bud, re.M) else 0
    c = dict(mem=0, b2b=0, bcond=0, tkn=0, bwd=0, bwdT=0, b=0, bl=0, bx=0, pcw=0, mNT=0, mBTFN=0, mR=0, mT=0)
    last = None; lastk = None; ctr = 2; tbl = Tables(16); prevmem = False
    for idx, pc in enumerate(seq):
        k, off = kind(hw(mem, pc))
        ismem = k == 'mem'
        if ismem: c['mem'] += 1; c['b2b'] += prevmem
        prevmem = ismem
        if k == 'bcond':
            nxt = seq[idx + 1] if idx + 1 < len(seq) else pc + 2
            tk = nxt != pc + 2
            c['bcond'] += 1; c['tkn'] += tk
            if off < 0: c['bwd'] += 1; c['bwdT'] += tk
            c['mNT'] += tk
            c['mBTFN'] += tk != (off < 0)
            if last != pc: ctr = 2 if off < 0 else 1; last = pc
            c['mR'] += tk != (ctr >= 2); ctr = min(3, ctr+1) if tk else max(0, ctr-1)
            c['mT'] += tk != tbl.pred(pc); tbl.upd(pc, tk)
        elif k in ('b', 'bl', 'bx'): c[k] += 1
        elif k == 'pcwrite': c['pcw'] += 1
    name = d[len(root):].strip('/')
    print(f"{name:28} {len(seq):5} {bud:6} {c['mem']:4} {c['b2b']:6} {c['bcond']:5} {c['tkn']:4} {c['bwd']:4} {c['bwdT']:4} {c['b']:3} {c['bl']:3} {c['bx']:3} {c['pcw']:3} |              {c['mNT']:3} {c['mBTFN']:5} {c['mR']:6} {c['mT']:5}")
    add('instr', len(seq)); add('budget', bud)
    for k2, v in c.items(): add(k2, v)
print('TOTAL', tot)
