#!/bin/bash
# area_mac.sh <repo> <outdir>: `just area` where yosys has no Tcl (this Mac):
# scripts/synth.tcl's commands via yosys -p, then scripts/area.py
set -e
R=$(cd "$1" && pwd); O=$2; mkdir -p "$O"
# The justfile's order: defines, ahb_pkg, cpu_pkg, the other packages, then
# rtl/*.sv and rtl/*/*.sv (rtl/cpu/pkg/ is a folder inside one, so not again)
PKGS=$(ls $R/rtl/cpu/pkg/*.sv | grep -v '/cpu_pkg.sv$' | tr '\n' ' ')
FILES="$R/rtl/config_defines.vh $R/rtl/ahb/pkg/ahb_pkg.sv $R/rtl/cpu/pkg/cpu_pkg.sv $PKGS $(ls $R/rtl/*.sv $R/rtl/*/*.sv | tr '\n' ' ')"
cd "$O"
yosys -q -p "plugin -i slang; read_slang --single-unit -D TARGET_FPGA -top cpu_core $FILES; synth_gowin -noiopads -family gw2a -json 1_synth_netlist.json; write_verilog 1_synth_netlist.v; tee -q -o 1_synth_stat.json stat -top cpu_core -json" > yosys.log 2>&1 || { tail -20 yosys.log; exit 1; }
python3 "$R/scripts/area.py" 1_synth_stat.json
