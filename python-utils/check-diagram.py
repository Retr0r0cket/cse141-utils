# This is Diego's brainchild FYI

#!/usr/bin/env python3
"""Check the block diagram's signal names and widths against the RTL.

    python3 tools/check_diagram.py

Every `name [width]` label in block_diagram.dot, and every signal with a width
in block_diagram.yaml (pipeline-register fields, block ports, internal signals,
cpu_core's outputs and bus), must be declared somewhere in rtl/ with that
width. A slice like `head_word[31:16] [16]` is checked against the slice
itself. Widths come from `logic [hi:lo]` declarations and ports, with each
file's localparams substituted, and from the enum and struct types below.

A name only has to match a declaration in some module, not in the one it's
drawn in, so this catches renamed and resized signals, not misplaced ones.
Needs PyYAML (pip install pyyaml); the course's tabbypy3 doesn't have it.
"""
import html
import math
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit('tools/check_diagram.py needs PyYAML: pip install pyyaml')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TYPES = {'alu_op_e': 5, 'mem_transaction_t': 2, 'apsr_t': 4}  # from cpu_pkg

DECL = re.compile(
    r'^\s*(?:input|output)?\s*(logic|' + '|'.join(TYPES) + r')\b\s*(\[[^\]]+\])?\s*(.*)$')
LOCALPARAM = re.compile(r'^\s*localparam\s+(?:int\s+)?(\w+)\s*=\s*([^;]+);')


def rtl_widths():
    """name -> the set of widths it's declared with, across rtl/."""
    widths = {}
    for dirpath, _, files in os.walk(os.path.join(ROOT, 'rtl')):
        for f in sorted(files):
            if not f.endswith('.sv'):
                continue
            params = {'clog2': lambda n: math.ceil(math.log2(n))}
            for raw in open(os.path.join(dirpath, f)):
                line = raw.split('//')[0]
                m = LOCALPARAM.match(line)
                if m:
                    try:
                        params[m[1]] = eval(m[2].replace('$clog2', 'clog2'), {}, params)
                    except (NameError, SyntaxError):
                        pass
                    continue
                m = DECL.match(line)
                if not m:
                    continue
                typ, rng, rest = m.groups()
                if typ in TYPES:
                    width = TYPES[typ]
                elif rng:
                    hi, lo = rng[1:-1].split(':')
                    try:
                        width = eval(hi, {}, params) - eval(lo, {}, params) + 1
                    except (NameError, SyntaxError):  # e.g. shared_mem's parameters
                        continue
                else:
                    width = 1
                rest = re.sub(r'\[[^\]]*\]', '', rest).split('=')[0]  # unpacked dims, initializers
                for name in re.split(r'[,;)\s]+', rest):
                    if re.fullmatch(r'[A-Za-z_]\w*', name):
                        widths.setdefault(name, set()).add(width)
    return widths


def width_of(value):
    """A YAML entry's width: `name: 32` or `name: {width: 32, ...}`."""
    if isinstance(value, int):
        return value
    if isinstance(value, dict) and isinstance(value.get('width'), int):
        return value['width']
    return None


def yaml_signals(diagram):
    """(where, name, width) for every signal with a width in block_diagram.yaml."""
    for section in ('outputs', 'bus'):
        for name, value in diagram['summary'][section].items():
            yield f'summary.{section}', name, width_of(value)
    for reg, r in diagram['pipeline_registers'].items():
        for name, value in r['fields'].items():
            yield reg, name, width_of(value)
    for stage, s in diagram['stages'].items():
        for block, b in s['blocks'].items():
            for part in ('in', 'out', 'internal'):
                for name, value in (b.get(part) or {}).items():
                    if width_of(value) is not None:
                        yield f'{stage}.{block}.{part}', name, width_of(value)
                    elif isinstance(value, dict):  # a group, like the ALU's units
                        for name2, value2 in value.items():
                            if width_of(value2) is not None:
                                yield f'{stage}.{block}.{part}.{name}', name2, width_of(value2)


def dot_signals(text):
    """(where, name, width) for every `name [width]` label in block_diagram.dot."""
    text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    for name, width in re.findall(r'([A-Za-z_][\w.]*(?:\[\d+(?::\d+)?\])?) \[(\d+)\]', text):
        yield 'dot', name.split('.')[-1], int(width)


def check(where, name, width, widths):
    """A problem with this label, or None."""
    m = re.fullmatch(r'([A-Za-z_]\w*)(?:\[(\d+)(?::(\d+))?\])?', str(name).strip())
    if not m:
        return None
    base, hi, lo = m.groups()
    if base not in widths:
        return f'{where}: {name} is not declared in rtl/'
    if hi is not None:
        bits = int(hi) - int(lo if lo is not None else hi) + 1
        if width != bits:
            return f'{where}: {name} is labeled [{width}], but the slice is {bits} bits'
    elif width not in widths[base]:
        return f'{where}: {name} is labeled [{width}], but rtl/ declares it {sorted(widths[base])}'
    return None


def main():
    widths = rtl_widths()
    with open(os.path.join(ROOT, 'block_diagram.yaml')) as f:
        diagram = yaml.safe_load(f)
    with open(os.path.join(ROOT, 'block_diagram.dot')) as f:
        dot = f.read()

    problems = []
    for source, signals in (('block_diagram.yaml', yaml_signals(diagram)),
                            ('block_diagram.dot', dot_signals(dot))):
        count = 0
        for where, name, width in signals:
            count += 1
            problem = check(where, name, width, widths)
            if problem:
                problems.append(problem)
        print(f'{source}: {count} signals checked')

    for problem in problems:
        print('  ' + problem)
    if problems:
        sys.exit(f'{len(problems)} labels don\'t match rtl/')
    print('Every name and width matches rtl/')


if __name__ == '__main__':
    main()
