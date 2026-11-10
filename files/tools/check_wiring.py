#!/usr/bin/env python3
"""Check that every wire in a block diagram's .dot is a real connection in the RTL.

    python3 check_wiring.py [--rtl rtl] [--top cpu_core] [--dot block_diagram.dot]
                            [--external shared_mem ...] [-D TARGET_FPGA ...]
                            [--list-unchecked]

check_diagram.py checks that names and widths exist. This checks that the
wires between them are real: a wire from signal A to signal B passes if, in
the RTL, A feeds B. That means A and B are the same net (a port and the wire on
it), or A is in B's combinational fan-in, or B is a register (flip-flop or
memory) that loads from A's fan-in cone. Only one register is crossed, so a
wire drawn into the wrong pipeline stage fails.

How a wire's ends are named, following the skill's .dot conventions:
- The port cell a wire attaches to (`node:port`) names a signal by its first
  `name [width]` (or `name[hi:lo] [width]`). A cell holding only `[8]` is an
  unnamed D input, and that end goes unchecked.
- A wire's own `label="name [width]"` names the net it carries. Then both
  halves are checked: tail feeds label, and label feeds head.
- A bare-name lookup matches any net with that leaf name in the flattened
  design (`rdata1` matches `decode.rdata1`). To pin a node's names to one
  instance, give the node an attribute rtl="decode" (or "execute.core_alu").
  A wire passes if any matching pair of nets connects.

Nodes outside the top module (memories, testbench) go in --external; their
ends go unchecked. Invisible edges are skipped. Needs Yosys with the slang
plugin (OSS CAD Suite has both) and Graphviz dot. Exits 1 on any problem.
"""
import argparse
import glob
import html
import json
import os
import re
import subprocess
import sys
import tempfile

LABEL = re.compile(r'([A-Za-z_]\w*(?:\[\d+(?::\d+)?\])?)\s*\[(\d+)\]')
SEQUENTIAL = ('$dff', '$adff', '$sdff', '$dffe', '$adffe', '$sdffe', '$sdffce',
              '$aldff', '$aldffe', '$dffsr', '$dffsre', '$dlatch', '$adlatch', '$memrd', '$memrd_v2')


def tool(name):
    found = os.environ.get(name.upper())
    if found:
        return found
    for d in os.environ.get('PATH', '').split(os.pathsep) + ['/opt/oss-cad-suite/latest/bin']:
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    sys.exit(f'check_wiring.py needs {name} (OSS CAD Suite has it in /opt/oss-cad-suite/latest/bin)')


def rtl_files(rtl):
    """Headers, then packages (a cpu_pkg first), then everything else."""
    files = sorted(glob.glob(os.path.join(rtl, '**', '*.vh'), recursive=True))
    pkgs = sorted(glob.glob(os.path.join(rtl, '**', 'pkg', '*.sv'), recursive=True))
    pkgs.sort(key=lambda f: 'cpu_pkg' not in f)
    rest = [f for f in sorted(glob.glob(os.path.join(rtl, '**', '*.sv'), recursive=True)) if f not in pkgs]
    rest += sorted(glob.glob(os.path.join(rtl, '**', '*.v'), recursive=True))
    return files + pkgs + rest


def read_netlist(rtl, top, defines):
    files = rtl_files(rtl)
    if any(' ' in f for f in files):
        sys.exit('Yosys scripts can\'t take a path with a space in it: ' + rtl)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, 'flat.json')
        script = ('plugin -i slang; read_slang --single-unit ' + ' '.join(f'-D {d}' for d in defines)
                  + f' -top {top} ' + ' '.join(files)
                  + f'; hierarchy -top {top}; proc; flatten; write_json {out}')
        r = subprocess.run([tool('yosys'), '-q', '-p', script], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit('yosys failed:\n' + r.stdout + r.stderr)
        return json.load(open(out))['modules'][top]


class Netlist:
    def __init__(self, module):
        self.nets = {}
        for name, v in module['netnames'].items():
            if not v.get('hide_name'):
                self.nets[name.replace('\\', '')] = (v['bits'], v.get('offset', 0))
        self.driver = {}      # bit -> cell driving it
        self.mem_writes = {}  # MEMID -> input bits of its write ports
        for cell in module['cells'].values():
            dirs = cell.get('port_directions', {})
            for port, bits in cell['connections'].items():
                if dirs.get(port) == 'output':
                    for b in bits:
                        self.driver[b] = cell
            if cell['type'].startswith('$memwr'):
                mem = cell['parameters'].get('MEMID')
                self.mem_writes.setdefault(mem, []).extend(
                    b for p, bits in cell['connections'].items() if dirs.get(p) == 'input' for b in bits)

    def candidates(self, ref, scope=None):
        """Bit lists of every net a name like `rdata1` or `head_word[31:16]` could mean."""
        m = re.fullmatch(r'([A-Za-z_]\w*)(?:\[(\d+)(?::(\d+))?\])?', ref)
        if not m:
            return []
        name, hi, lo = m[1], m[2], m[3]
        if scope and f'{scope}.{name}' in self.nets:
            names = [f'{scope}.{name}']
        else:
            names = [n for n in self.nets if n == name or n.endswith('.' + name)]
        out = []
        for n in names:
            bits, offset = self.nets[n]
            if hi is not None:
                h = int(hi); l = int(lo) if lo is not None else h
                bits = bits[l - offset:h - offset + 1]
            out.append([b for b in bits if not isinstance(b, str)])  # drop constant bits
        return [b for b in out if b]

    def cone(self, bits):
        """Every bit that can reach `bits`: through combinational cells, plus one
        register (flip-flop or memory) when `bits` is that register's output."""
        def inputs(cell):
            dirs = cell.get('port_directions', {})
            return [b for p, pb in cell['connections'].items() if dirs.get(p) == 'input' for b in pb
                    if not isinstance(b, str)]

        def through_bufs(b):
            """The bit behind any $buf cells (slang puts one between a port and its register)."""
            for _ in range(64):
                cell = self.driver.get(b)
                if cell is None or cell['type'] != '$buf':
                    return b
                ins, outs = cell['connections']['A'], cell['connections']['Y']
                src = ins[outs.index(b)]
                if isinstance(src, str):
                    return b
                seen.add(b)
                b = src
            return b

        seen, stack = set(), []
        for b in bits:  # the head may be a register: cross it once
            seen.add(b)
            b = through_bufs(b)
            seen.add(b)
            cell = self.driver.get(b)
            if cell is not None and cell['type'] in SEQUENTIAL:
                stack.extend(inputs(cell))
                if cell['type'].startswith('$memrd'):
                    stack.extend(self.mem_writes.get(cell['parameters'].get('MEMID'), []))
            else:
                stack.append(b)
                seen.discard(b)
        while stack:
            b = stack.pop()
            if b in seen:
                continue
            seen.add(b)
            cell = self.driver.get(b)
            if cell is None or cell['type'] in SEQUENTIAL:
                continue
            stack.extend(inputs(cell))
        return seen

    def feeds(self, a_refs, b_refs):
        return any(set(a) & self.cone(b) for a in a_refs for b in b_refs)


def read_dot(path):
    r = subprocess.run([tool('dot'), '-Tjson0', path], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit('dot failed:\n' + r.stderr)
    d = json.loads(r.stdout)
    nodes = {o['_gvid']: o for o in d['objects'] if 'nodes' not in o and 'subgraphs' not in o}
    return nodes, d.get('edges', [])


def port_names(label):
    """PORT id -> the signal its cell names (None for an unnamed `[8]` cell)."""
    cells = {}
    for m in re.finditer(r'<TD[^>]*\bPORT="([^"]+)"[^>]*>(.*?)</TD>', label or '', re.S | re.I):
        text = html.unescape(re.sub(r'<[^>]+>', ' ', m[2]))
        found = LABEL.search(text)
        cells[m[1]] = found[1] if found else None
    return cells


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--rtl', default='rtl')
    ap.add_argument('--top', default='cpu_core')
    ap.add_argument('--dot', default='block_diagram.dot')
    ap.add_argument('--external', action='append', default=[], help='node outside the top module')
    ap.add_argument('--list-unchecked', action='store_true', help='name the wires that went unchecked')
    ap.add_argument('-D', dest='defines', action='append', default=None,
                    help='preprocessor define (default: TARGET_FPGA, as the course synthesis uses)')
    args = ap.parse_args()

    net = Netlist(read_netlist(args.rtl, args.top, args.defines if args.defines is not None else ['TARGET_FPGA']))
    nodes, edges = read_dot(args.dot)
    cells = {gv: port_names(n.get('label')) for gv, n in nodes.items()}

    problems, full, half, unchecked, skipped = [], 0, 0, 0, []
    for e in edges:
        if 'invis' in e.get('style', ''):
            continue
        t, h = nodes[e['tail']], nodes[e['head']]

        def end(node, gv, port):
            if node['name'] in args.external:
                return None, None
            port = (port or '').split(':')[0]
            name = cells[gv].get(port) if port else None
            return name, node.get('rtl')

        tn, ts = end(t, e['tail'], e.get('tailport'))
        hn, hs = end(h, e['head'], e.get('headport'))
        found = LABEL.fullmatch(html.unescape(e.get('label', '')).strip())
        ln = found[1] if found else None
        what = f"{t['name']}{':' + tn if tn else ''} -> {h['name']}{':' + hn if hn else ''}"

        refs = {}
        for ref, scope in ((tn, ts), (hn, hs), (ln, None)):
            if ref and ref not in refs:
                refs[ref] = net.candidates(ref, scope)
                if not refs[ref]:
                    problems.append(f'{what}: no net named {ref} in {args.top}')
        if any(r and not refs[r] for r in (tn, hn, ln)):
            continue

        pairs = [(tn, ln), (ln, hn)] if ln else [(tn, hn)]
        checkable = [(a, b) for a, b in pairs if a and b]
        for a, b in checkable:
            if not net.feeds(refs[a], refs[b]):
                problems.append(f'{what}: {a} doesn\'t feed {b} in the RTL'
                                + (f' (wire labeled {ln})' if ln else ''))
        if not checkable:
            unchecked += 1
            skipped.append(what + (f' [{ln}]' if ln else ''))
        elif len(checkable) == len(pairs) and tn and hn:
            full += 1
        else:
            half += 1

    print(f'{args.dot}: {full} wires checked end to end, {half} at one end '
          f'(the other is unnamed or external), {unchecked} unchecked')
    if args.list_unchecked:
        for w in skipped:
            print('  unchecked: ' + w)
    for p in problems:
        print('  ' + p)
    if problems:
        sys.exit(f'{len(problems)} wires don\'t match the RTL')
    print('Every checked wire is a real connection in the RTL')


if __name__ == '__main__':
    main()
