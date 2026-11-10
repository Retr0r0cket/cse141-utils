#!/usr/bin/env python3
"""Draw block_diagram.dot from block_diagram.yaml.

    python3 tools/diagram_from_yaml.py      (needs PyYAML)

Each block is a table, its inputs on the left and outputs on the right, each
`name [width]`; each pipeline register is a column of its fields. An edge
joins a signal's producer to every block or field that takes a signal of the
same name (a pipeline register field `x` also feeds inputs named `x`), so the
picture shows exactly what the YAML says, and `just diagram-check` checks its
labels like the YAML's. Edit the YAML, then run this and `just diagram`.
"""
import html
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit('tools/diagram_from_yaml.py needs PyYAML: pip install pyyaml')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLORS = {  # stage: (cluster fill, cluster border, block header)
    'IF':        ('#eef4fb', '#9db8d9', '#dde7f3'),
    'ID':        ('#eefaf0', '#9ccfa6', '#dcf0e0'),
    'EX':        ('#fdf3e7', '#e0b37a', '#f6e2c6'),
    'redirects': ('#f7eefa', '#c49bd6', '#ead8f2'),
    'MEM':       ('#fbeeee', '#d99d9d', '#f3dddd'),
    'WB':        ('#f3f3f3', '#aaaaaa', '#e3e3e3'),
}
# Where each pipeline register sits, between which stages
REG_AFTER = {'IF/ID': 'IF', 'ID/EX': 'ID', 'EX/MEM': 'EX', 'MEM/WB': 'MEM'}


def esc(text):
    return html.escape(str(text), quote=True)


def width_of(value):
    if isinstance(value, int):
        return value
    if isinstance(value, dict) and isinstance(value.get('width'), int):
        return value['width']
    return None


def node_id(*parts):
    return '_'.join(p.replace('/', '').replace('.', '_') for p in parts)


def block_table(name, b, header):
    ins = [(n, width_of(v)) for n, v in (b.get('in') or {}).items() if width_of(v) is not None]
    outs = [(n, width_of(v)) for n, v in (b.get('out') or {}).items() if width_of(v) is not None]
    internal = []
    for n, v in (b.get('internal') or {}).items():
        if width_of(v) is not None:
            internal.append(f'{n} [{width_of(v)}]')
        elif isinstance(v, dict):
            internal.append(f'{n}: ' + ', '.join(f'{n2} [{width_of(v2)}]' for n2, v2 in v.items()))
    title = f'<B>{esc(name)}</B>'
    if b.get('module'):
        title += f' <FONT POINT-SIZE="8">({esc(b["module"])})</FONT>'
    rows = [f'<TR><TD COLSPAN="2" BGCOLOR="{header}">{title}</TD></TR>']
    for i in range(max(len(ins), len(outs))):
        left = (f'<TD PORT="i_{ins[i][0]}" ALIGN="LEFT">{esc(ins[i][0])} [{ins[i][1]}]</TD>'
                if i < len(ins) else '<TD></TD>')
        right = (f'<TD PORT="o_{outs[i][0]}" ALIGN="RIGHT">{esc(outs[i][0])} [{outs[i][1]}]</TD>'
                 if i < len(outs) else '<TD></TD>')
        rows.append(f'<TR>{left}{right}</TR>')
    if internal:
        rows.append('<TR><TD COLSPAN="2" ALIGN="LEFT"><FONT POINT-SIZE="7" COLOR="#555555">'
                    + '<BR ALIGN="LEFT"/>'.join(esc(t) for t in internal)
                    + '<BR ALIGN="LEFT"/></FONT></TD></TR>')
    return ('<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="2" BGCOLOR="white">'
            + ''.join(rows) + '</TABLE>>'), ins, outs


def main():
    with open(os.path.join(ROOT, 'block_diagram.yaml')) as f:
        d = yaml.safe_load(f)

    lines = [
        '// Block diagram of cpu_core, the five-stage pipeline in rtl/ (week 5).',
        '// Generated from block_diagram.yaml by tools/diagram_from_yaml.py: edit the',
        '// YAML, not this file. Render with `just diagram`; check every name and width',
        '// against rtl/ with `just diagram-check`.',
        '//',
        '// Every label is `name [width in bits]`, with the name used in the RTL.',
        '',
        'digraph cpu_core {',
        '  graph [rankdir=LR, newrank=true, compound=true, splines=true, fontname="Helvetica", fontsize=12,',
        '         nodesep=0.1, ranksep=0.6, pad=0.3, labelloc=t,',
        '         label=<<B>cpu_core: five-stage pipeline (week 5)</B><BR/><FONT POINT-SIZE="10">'
        'every signal is labeled name [width in bits]; pipeline registers are the dark columns</FONT>>];',
        '  node  [shape=plain, fontname="Helvetica", fontsize=8];',
        '  edge  [fontname="Helvetica", fontsize=7, arrowsize=0.4, penwidth=0.6, color="#555555"];',
        '',
    ]

    producers = {}   # signal -> list of (node, port)
    consumers = []   # (node, port, signal)

    for stage, s in d['stages'].items():
        fill, border, header = COLORS.get(stage, ('#ffffff', '#999999', '#eeeeee'))
        lines.append(f'  subgraph cluster_{node_id(stage)} {{')
        lines.append(f'    label=<<B>{esc(stage)}</B>: {esc(s.get("module", ""))}>; labeljust=l; '
                     f'style="rounded,filled"; fillcolor="{fill}"; color="{border}";')
        for name, b in s['blocks'].items():
            nid = node_id(stage, name)
            label, ins, outs = block_table(name, b, header)
            lines.append(f'    {nid} [label={label}];')
            for n, _ in outs:
                producers.setdefault(n, []).append((nid, f'o_{n}'))
            for n, _ in ins:
                consumers.append((nid, f'i_{n}', n))
        lines.append('  }')
        lines.append('')

    for reg, r in d['pipeline_registers'].items():
        nid = node_id('reg', reg)
        rows = [f'<TR><TD BGCOLOR="#444444"><FONT COLOR="white"><B>{esc(reg)} &#9650;</B></FONT></TD></TR>']
        for n, v in r['fields'].items():
            rows.append(f'<TR><TD PORT="f_{n}">{esc(n)} [{width_of(v)}]</TD></TR>')
            producers.setdefault(n, []).append((nid, f'f_{n}'))
            dsrc = v.get('d') if isinstance(v, dict) else None
            if isinstance(dsrc, str) and dsrc in producers:
                consumers.append((nid, f'f_{n}', dsrc))
        lines.append(f'  {nid} [label=<<TABLE BORDER="1" CELLBORDER="1" CELLSPACING="0" CELLPADDING="2" '
                     f'BGCOLOR="#f2f2f2" COLOR="#555555">{"".join(rows)}</TABLE>>];')
    lines.append('')

    # Order the columns: each stage, then its pipeline register
    order = []
    for stage in d['stages']:
        order.append(f'cluster_{node_id(stage)}')
        for reg, after in REG_AFTER.items():
            if after == stage:
                order.append(node_id('reg', reg))

    seen = set()
    for nid, port, sig in consumers:
        for src, sport in producers.get(sig, []):
            if src == nid or (src, sport, nid, port) in seen:
                continue
            seen.add((src, sport, nid, port))
            lines.append(f'  {src}:{sport}:e -> {nid}:{port}:w;')
    lines.append('}')

    with open(os.path.join(ROOT, 'block_diagram.dot'), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print('Wrote block_diagram.dot')


if __name__ == '__main__':
    main()
