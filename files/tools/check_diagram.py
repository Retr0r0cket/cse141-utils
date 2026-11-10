#!/usr/bin/env python3
"""Check a block diagram's signal names and widths against SystemVerilog RTL.

    python3 check_diagram.py [--rtl rtl] [--yaml block_diagram.yaml]
                             [--dot block_diagram.dot] [--missing]
                             [--skip-file shared_mem.sv ...]

Stale labels: every `name [width]` label in the .dot, and every signal with a
width in the YAML spec, must be declared somewhere under --rtl with that width.
A slice like `head_word[31:16] [16]` is checked against the slice itself.
Widths come from `logic [hi:lo]` declarations and ports (each file's
localparams and packages' localparams substituted, and parameters taking the
values they're instantiated with, like `#(.Depth(8))`) and from typedefs (plain
vectors, enums and packed structs) found anywhere under --rtl (e.g. a cpu_pkg).

Missing signals (--missing): every name declared in the RTL that never appears
anywhere in the YAML text. Files named with --skip-file (e.g. provided memory
and top-level modules) are left out of this list.

A name only has to match a declaration in some module, not the one it's drawn
in, so this catches renamed, resized and deleted signals, not misplaced ones.
Either of --yaml / --dot may be missing on disk; it's then skipped. Needs
PyYAML for the YAML (pip install pyyaml); OSS CAD Suite's tabbypy3 lacks it.
Exits 1 if anything doesn't match.
"""
import argparse
import html
import math
import os
import re
import sys

BUILTIN = {'bit': 1, 'logic': 1, 'reg': 1, 'wire': 1}


def strip_comments(text):
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    return re.sub(r'//[^\n]*', '', text)


def sv_eval(expr, params):
    """A constant expression, with $clog2 and the given parameters."""
    return eval(expr.replace('$clog2', 'clog2'), {}, {'clog2': lambda n: math.ceil(math.log2(n)), **params})


def eval_width(rng, params):
    hi, lo = rng.split(':')
    return sv_eval(hi, params) - sv_eval(lo, params) + 1


def sv_files(rtl):
    for dirpath, _, files in os.walk(rtl):
        for f in sorted(files):
            if f.endswith(('.sv', '.v', '.svh', '.vh')):
                yield os.path.join(dirpath, f)


def typedef_widths(rtl):
    """Widths of typedefs: plain vectors, enums and packed structs, e.g. {'pc_t': 31, 'alu_op_e': 5}.
    Ranges may use package localparams (`typedef logic [PcTop:1] pc_t`)."""
    types, params = {}, package_params(rtl)

    def width(rng):
        return eval_width(rng, {'clog2': lambda n: math.ceil(math.log2(n)), **params})

    for path in sv_files(rtl):
        text = strip_comments(open(path).read())
        for m in re.finditer(r'typedef\s+(?:logic|bit)\s*(?:signed\s*)?\[([^\]]+)\]\s*(\w+)\s*;', text):
            try:
                types[m[2]] = width(m[1])
            except Exception:
                pass
        for m in re.finditer(r'typedef\s+enum\s+(?:logic|bit)\s*(?:\[([^\]]+)\])?\s*\{[^}]*\}\s*(\w+)\s*;', text):
            try:
                types[m[2]] = width(m[1]) if m[1] else 1
            except Exception:
                pass
        for m in re.finditer(r'typedef\s+struct\s+packed\s*\{([^}]*)\}\s*(\w+)\s*;', text):
            total = 0
            try:
                for field in re.finditer(r'(\w+)\s*(?:signed\s*)?(\[[^\]]+\])?\s*([\w\s,]+);', m[1]):
                    typ, rng, names = field.groups()
                    w = width(rng[1:-1]) if rng else types.get(typ, 1)
                    total += w * len([n for n in names.split(',') if n.strip()])
            except Exception:
                continue
            types[m[2]] = total
    return types


def package_params(rtl):
    """Integer localparams declared in packages, e.g. {'PcW': 32}: every importer sees them."""
    params = {}
    for path in sv_files(rtl):
        for body in re.findall(r'\bpackage\b.*?\bendpackage\b', strip_comments(open(path).read()), flags=re.S):
            for m in re.finditer(r'^\s*localparam\s+(?:int\s+|integer\s+)?(\w+)\s*=\s*(.+?)\s*;', body, flags=re.M):
                try:
                    params[m[1]] = eval(m[2], {}, params)
                except Exception:
                    pass
    return params


PARAM = re.compile(r'^\s*(localparam|parameter)\s+(?:int\s+|integer\s+)?(\w+)\s*=\s*(.+?)\s*[;,]?\s*$')


def file_params(text, base, overrides):
    """A file's parameters and localparams, in order; a `parameter` takes its override, if any."""
    params = dict(base)
    for line in text.splitlines():
        m = PARAM.match(line)
        if m:
            try:
                params[m[2]] = overrides[m[2]] if m[1] == 'parameter' and m[2] in overrides \
                    else sv_eval(m[3], params)
            except Exception:
                pass
    return params


def balanced(text, i):
    """The index just past the parenthesis that closes the one at text[i]."""
    depth = 0
    for j in range(i, len(text)):
        depth += {'(': 1, ')': -1}.get(text[j], 0)
        if depth == 0:
            return j + 1
    return len(text)


def param_overrides(texts, base):
    """{module: {parameter: value}} from instantiations like `fetch_stage #(.BufferDepth(8)) fetch (`.
    Followed down the hierarchy, so an override passed on (`.Depth(BufferDepth)`) gets the
    instantiating module's own overridden value. A module instantiated with different values
    in different places keeps the last one."""
    modules = {path: m[1] for path, text in texts.items() if (m := re.search(r'\bmodule\s+(\w+)', text))}
    insts = []  # (parent file, child module, {parameter: expression})
    for path, text in texts.items():
        for m in re.finditer(r'\b(\w+)\s*#\s*\(', text):
            end = balanced(text, m.end() - 1)
            if not re.match(r'\s*\w+\s*\(', text[end:]):
                continue
            body = text[m.end():end - 1]
            assigns = {}
            for a in re.finditer(r'\.(\w+)\s*\(', body):
                assigns[a[1]] = body[a.end():balanced(body, a.end() - 1) - 1]
            insts.append((path, m[1], assigns))
    overrides = {}
    for _ in range(8):  # deep enough for any hierarchy we'll see
        for path, child, assigns in insts:
            params = file_params(texts[path], base, overrides.get(modules.get(path), {}))
            for name, expr in assigns.items():
                try:
                    overrides.setdefault(child, {})[name] = sv_eval(expr, params)
                except Exception:
                    pass
    return modules, overrides


def rtl_widths(rtl, skip=()):
    """(name -> set of declared widths across rtl, names declared outside skipped files).
    Parameters take the values the modules are instantiated with, where they can be worked out."""
    types = {**BUILTIN, **typedef_widths(rtl)}
    decl = re.compile(r'^\s*(?:input|output|inout)?\s*(?:var\s+)?(?:\w+::)?(' + '|'.join(map(re.escape, types))
                      + r')\b\s*(?:signed\s+)?(\[[^\]]+\])?\s*(.*)$')
    widths, own = {}, set()
    pkg_params = package_params(rtl)
    texts = {path: strip_comments(open(path).read()) for path in sv_files(rtl)}
    modules, overrides = param_overrides(texts, pkg_params)
    for path, text in texts.items():
        params = dict(pkg_params)
        given = overrides.get(modules.get(path), {})
        skipped = os.path.basename(path) in skip
        for line in text.splitlines():
            m = PARAM.match(line)
            if m:
                try:
                    params[m[2]] = given[m[2]] if m[1] == 'parameter' and m[2] in given \
                        else sv_eval(m[3], params)
                except Exception:
                    pass
                continue
            m = decl.match(line)
            if not m:
                continue
            typ, rng, rest = m.groups()
            if rng:
                try:
                    width = eval_width(rng[1:-1], params)
                except Exception:  # a parameter we can't evaluate
                    continue
            else:
                width = types[typ]
            rest = re.sub(r'\[[^\]]*\]', '', rest).split('=')[0]  # unpacked dims, initializers
            for name in re.split(r'[,;)\s]+', rest):
                if re.fullmatch(r'[A-Za-z_]\w*', name) and name not in types:
                    widths.setdefault(name, set()).add(width)
                    if not skipped:
                        own.add(name)
    return widths, own


def width_of(value):
    """A YAML entry's width: `name: 32` or `name: {width: 32, ...}`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict) and isinstance(value.get('width'), int):
        return value['width']
    return None


def yaml_signals(d):
    """(where, name, width) for every signal with a width in the YAML spec."""
    summary = d.get('summary') or {}
    for section in ('outputs', 'bus'):
        for name, value in (summary.get(section) or {}).items():
            yield f'summary.{section}', name, width_of(value)
    for reg, r in (d.get('pipeline_registers') or {}).items():
        for name, value in ((r or {}).get('fields') or {}).items():
            yield reg, name, width_of(value)
    stages = d.get('stages') or {}
    blocks = [(f'{st}.{b}', blk) for st, s in stages.items() for b, blk in ((s or {}).get('blocks') or {}).items()]
    blocks += [(b, blk) for b, blk in (d.get('blocks') or {}).items()]  # unpipelined designs
    for where, b in blocks:
        for part in ('in', 'out', 'internal'):
            part_map = (b or {}).get(part)
            if not isinstance(part_map, dict):
                continue
            for name, value in part_map.items():
                if width_of(value) is not None:
                    yield f'{where}.{part}', name, width_of(value)
                elif isinstance(value, dict):  # a group, like an ALU's units
                    for name2, value2 in value.items():
                        if width_of(value2) is not None:
                            yield f'{where}.{part}.{name}', name2, width_of(value2)


def dot_signals(text):
    """(where, name, width) for every `name [width]` label in a .dot (HTML labels included)."""
    text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    for name, width in re.findall(r'([A-Za-z_][\w.]*(?:\[\d+(?::\d+)?\])?) \[(\d+)\]', text):
        yield 'dot', name.split('.')[-1], int(width)


def check(where, name, width, widths):
    m = re.fullmatch(r'([A-Za-z_]\w*)(?:\[(\d+)(?::(\d+))?\])?', str(name).strip())
    if not m:
        return None
    base, hi, lo = m.groups()
    if base not in widths:
        return f'{where}: {name} is not declared in the RTL'
    if width is None:
        return f'{where}: {name} has no width'
    if hi is not None:
        bits = int(hi) - int(lo if lo is not None else hi) + 1
        if width != bits:
            return f'{where}: {name} is labeled [{width}], but the slice is {bits} bits'
    elif width not in widths[base]:
        return f'{where}: {name} is labeled [{width}], but the RTL declares it {sorted(widths[base])}'
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--rtl', default='rtl')
    ap.add_argument('--yaml', default='block_diagram.yaml')
    ap.add_argument('--dot', default='block_diagram.dot')
    ap.add_argument('--missing', action='store_true', help='also list RTL names the YAML never mentions')
    ap.add_argument('--skip-file', action='append', default=[], help='RTL file to leave out of --missing')
    args = ap.parse_args()

    widths, own = rtl_widths(args.rtl, set(args.skip_file))
    problems, yaml_text = [], None
    sources = []
    if os.path.exists(args.yaml):
        try:
            import yaml
        except ImportError:
            sys.exit('check_diagram.py needs PyYAML for the YAML spec: pip install pyyaml')
        yaml_text = open(args.yaml).read()
        sources.append((args.yaml, yaml_signals(yaml.safe_load(yaml_text) or {})))
    if os.path.exists(args.dot):
        sources.append((args.dot, dot_signals(open(args.dot).read())))
    if not sources:
        sys.exit(f'neither {args.yaml} nor {args.dot} exists')

    for source, signals in sources:
        count = 0
        for where, name, width in signals:
            count += 1
            problem = check(where, name, width, widths)
            if problem:
                problems.append(problem)
        print(f'{source}: {count} signals checked')
    for p in problems:
        print('  ' + p)

    missing = []
    if args.missing and yaml_text is not None:
        mentioned = set(re.findall(r'[A-Za-z_]\w*', yaml_text))
        missing = sorted(n for n in own if n not in mentioned)
        print(f'RTL names the YAML never mentions ({len(missing)}): ' + (', '.join(missing) or 'none'))

    if problems:
        sys.exit(f"{len(problems)} labels don't match the RTL")
    print('Every name and width matches the RTL')


if __name__ == '__main__':
    main()
