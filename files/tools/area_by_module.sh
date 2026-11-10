#!/bin/bash
# area_by_module.sh <repo> <outdir>: synthesize without flattening and print
# the area (LUT1-4, ALU, DFF*) per module
set -e
R=$(cd "$1" && pwd); O=$2; mkdir -p "$O"
# The justfile's order: defines, ahb_pkg, cpu_pkg, the other packages, then
# rtl/*.sv and rtl/*/*.sv (rtl/cpu/pkg/ is a folder inside one, so not again)
PKGS=$(ls $R/rtl/cpu/pkg/*.sv | grep -v '/cpu_pkg.sv$' | tr '\n' ' ')
FILES="$R/rtl/config_defines.vh $R/rtl/ahb/pkg/ahb_pkg.sv $R/rtl/cpu/pkg/cpu_pkg.sv $PKGS $(ls $R/rtl/*.sv $R/rtl/*/*.sv | tr '\n' ' ')"
cd "$O"
yosys -q -p "plugin -i slang; read_slang --keep-hierarchy --single-unit -D TARGET_FPGA -top cpu_core $FILES; synth_gowin -noiopads -noflatten -family gw2a; tee -q -o stat.json stat -json" > yosys.log 2>&1 || { tail -20 yosys.log; exit 1; }
python3 - <<'PY'
import json, fnmatch
mods = json.load(open("stat.json"))["modules"]
def area(c): return sum(v for k, v in c.items() if isinstance(v, int) and (k in ("LUT1","LUT2","LUT3","LUT4","ALU") or k.startswith("DFF")))
tot = 0
for name, m in sorted(mods.items(), key=lambda kv: -area(kv[1].get("num_cells_by_type", {}))):
    c = m.get("num_cells_by_type", {})
    a = area(c); tot += a
    if a == 0: continue
    luts = sum(c.get(k, 0) for k in ("LUT1","LUT2","LUT3","LUT4")); ffs = sum(v for k, v in c.items() if k.startswith("DFF")); alu = c.get("ALU", 0)
    print(f"{name.strip(chr(92)).split('$')[0]:28} {a:5}   LUT {luts:4}  FF {ffs:4}  ALU {alu:3}")
print(f"{'total':28} {tot:5}")
PY
