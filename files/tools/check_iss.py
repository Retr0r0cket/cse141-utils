#!/usr/bin/env python3
"""Check the golden model (tools/thumb_iss.py) against the course's own expected files: every
test program with a .pcs (and .after.dmem, .bus) must come out the same. The model's mistakes
would hide in tests built on it, so this keeps it honest.

    check_iss.py      exit 1 if any program differs

(The stress programs are left out: their expected files come from this model.)
"""

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thumb_iss as iss   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NORMAL = re.compile(r'^(fetch|read +addr=\S+ \(normal\))')


def body(path, drop_normal=False):
    if not os.path.exists(path):
        return None
    lines = [l.strip() for l in open(path) if l.strip() and not l.strip().startswith('//')]
    return [l for l in lines if not (drop_normal and NORMAL.match(l))]


def main():
    ok, bad = 0, []
    for d in sorted(glob.glob(os.path.join(ROOT, 'test_programs', '*', '[0-9]*_*/'))):
        if '/stress/' in d:
            continue
        name = re.sub(r'^\d+_', '', os.path.basename(d.rstrip('/')))
        base = d + name
        if not (os.path.exists(base + '.imem') and os.path.exists(base + '.pcs')):
            continue
        m = iss.load_program(base)
        try:
            m.run()
        except iss.Unpredictable as e:
            bad.append(f'{d}: {e}')
            continue
        diffs = []
        if body(base + '.pcs') != [f'pc={p:08x}' for p in m.pcs]:
            diffs.append('pcs')
        exp = body(base + '.after.dmem')
        if exp is not None and exp != [f'@{o:08x} {b:02x}' for o, b in sorted(m.sram.items())]:
            diffs.append('after.dmem')
        exp = body(base + '.bus', True)
        if exp is not None and exp != [l for l in m.bus if not NORMAL.match(l)]:
            diffs.append('bus')
        if diffs:
            bad.append(f'{os.path.relpath(d, ROOT)}: {", ".join(diffs)} differ')
        else:
            ok += 1
    for b in bad:
        print('FAIL', b)
    print(f'golden model: {ok} programs agree with their expected files, {len(bad)} differ')
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
