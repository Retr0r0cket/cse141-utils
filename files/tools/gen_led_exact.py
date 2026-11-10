"""Assemble test_programs/submit/02_ledExact/ledExact.imem (Thumb, two passes, labels).

    python3 tools/gen_led_exact.py test_programs/submit/02_ledExact/ledExact.imem

This runs once, here: the .imem it writes is the program we submit, the same bytes for every
core. All the adapting to a core happens at run time, in the Thumb program itself.

The extra credit: turn LED 5 on with COUNT exactly 1000, on every core it's graded on (the
course's baseline and every group's correct core), none of which we can see. The spec lets LEDs
0-4 turn on and off first, and says a write reaches LED 5 as fast as any other LED. So the
program measures the core it's running on, and assumes as little as it can about it.

The timed code (at COPY) is the only code that's timed, and it's the same code every time:

    str  r0, [r7, #4]      CLEAR: COUNT is 0 from the next cycle
    loop: subs r1, #1; bne loop        N times (r1 = N)
    add  pc, r10           into the pad sled at entry m (r10 = 2m)
    M x str r5, [r7, #8]   harmless APB writes, to COUNT + 8 (nothing there)
    add  pc, r9            into the NOP sled at entry j (r9 = 2j)
    J x nop
    str  r5, [r6]          the timed store
    4 x nop; ldr r0, [r7]  COUNT at the end: the run's result
    bx   lr

Only registers change from run to run. The timed store writes 0 to the LEDs while measuring,
which turns nothing on, and 32 (LED 5) on the real run. So the core's predictors, fetch buffer
and so on see the same code each time.

The steps (the generated file's header says the same, briefly):

1. T. With the timed store aimed at CLEAR, COUNT at the end is V, the tail's length. So a run
   ends with COUNT = T = 1001 + V exactly when its timed store's COUNT was 1000. This is the
   one assumption: a store to CLEAR takes as long as a store to the LEDs (both are word writes
   to the same APB bridge).
2. S(N, j, m) is COUNT at the end, always the second of two identical runs (warm predictors).
   P = (S(8, 0, 0) - S(4, 0, 0)) / 4 is the loop's period. C8 = S(4, 0, 0) - S(4, 8, 0) is
   what 8 NOP entries take off: a NOP needn't be one cycle.
3. For each pad entry m, the NOP entries are scanned by arithmetic. Entry j takes about
   C8 * j / 8 off S(4, 0, m), and T - S must be a multiple of P. Only entries whose predicted
   residue is 0 get measured, and if one really fits, N = 4 + (T - S(4, j, m)) / P.
4. The real write. That run is repeated (at most 4 times) with the same code between runs.
   Between runs, r5 becomes 32 without a branch, only if the two runs before both ended at
   exactly T. So the run that writes LED 5 follows two that measured exactly right.
5. The pad sled exists for cores where every NOP-and-loop time has the same parity, so the
   NOP sled alone can't reach T. A core that fetches a word every 2 cycles is one example: an
   APB write's wait shifts it.

Registers: r0-r4 scratch; r5 the timed store's data; r6 its address; r7 COUNT (CLEAR at +4);
r8 N (C8 while scanning); r9 2j; r10 2m; r11 T; r12 the run before's result (C8 * j / 8 while
scanning). Values that outlive a call live in SRAM (VARS), since there are no free registers.

Tested on 21 cores (see docs/tools.md): all of our branches' cores, and slowed copies of ours
(decode or fetch waiting extra cycles). All hit 1000 within 4,300-7,900 cycles, except two
copies whose fetch waits after every fetch (one fetch every 3 cycles, CPI about 2.0; and one
every 7, CPI about 3.8). They run out of program_tb's default 10,000 cycles while searching:
every-3's mix of NOP and loop timings keeps the predicted entries from fitting. To try it on another core, build
that core's program_tb and run `vvp program_tb +TEST=<repo>/test_programs/submit/02_ledExact/ledExact`,
then look for `COUNT=1000` on the first LED 5 write in ledExact.leds.
"""
import sys
J = 32                      # NOP sled: entries 0..J-1 (covers a loop period up to about 32 NOPs)
M = 4                       # Pad sled: entries 0..M-1, each a harmless APB write
COPY = 0x200                # The timed code
# SRAM words (from 0x2000_0000): S(4, 0, 0), P, C8, 2m, and the scan's j, residue, run count,
# C8 * j + 4, and C8 * j / 8, saved across measurements
VARS = {"s40": 0, "p": 4, "c8": 8, "m": 12, "j": 16, "res": 20, "rep": 24, "acc": 28, "d": 32}

prog = []                   # (addr or None, kind, args, comment)
def at(a): prog.append(("org", a))
def L(name): prog.append(("label", name))
def I(text, *enc): prog.append(("ins", text, enc))

# Data-processing register ops: rdn = rdn op rm
DP = {"ands": 0, "eors": 1, "adcs": 5, "rsbs": 9, "cmp": 10, "orrs": 12}
def dp(op, rdn, rm, note=""):
    I(f"{op} r{rdn}, r{rm}{'  -> ' + note if note else ''}", "dp", DP[op], rdn, rm)

def sram(reg):
    I("movs r3, #0x20", "movs", 3, 0x20)
    I(f"lsls r3, r3, #24  -> SRAM", "lsls", 3, 3, 24)
def load(reg, var):
    sram(3); I(f"ldr r{reg}, [r3, #{VARS[var]}]  -> {var}", "ldr", reg, 3, VARS[var] // 4)
def store(reg, var):
    sram(3); I(f"str r{reg}, [r3, #{VARS[var]}]  -> {var}", "str", reg, 3, VARS[var] // 4)

def measure(what):
    # Two runs with N = r8, NOP entry r9, pad entry r10; r0 = the second's COUNT at the end
    I(f"mov r1, r8       -> {what}: once to warm up", "movhi", 1, 8)
    I("bl copy", "bl", "copy")
    I("mov r1, r8       -> and again, measured", "movhi", 1, 8)
    I("bl copy", "bl", "copy")

def divide(tag):
    # r3 / r2, for r3 < r2 * 1024: quotient in r1, remainder in r3 (r0 and r4 used)
    I("lsls r0, r2, #9", "lsls", 0, 2, 9)
    I("movs r4, #1", "movs", 4, 1)
    I("lsls r4, r4, #9", "lsls", 4, 4, 9)
    I("movs r1, #0", "movs", 1, 0)
    L(f"divide_{tag}")
    I("cmp r3, r0", "cmpr", 3, 0)
    I(f"bcc smaller_{tag}", "bcond", 3, f"smaller_{tag}")
    I("subs r3, r3, r0", "subsr", 3, 3, 0)
    I("adds r1, r1, r4", "addsr", 1, 1, 4)
    L(f"smaller_{tag}")
    I("lsrs r0, r0, #1", "lsrs", 0, 0, 1)
    I("lsrs r4, r4, #1", "lsrs", 4, 4, 1)
    I(f"bne divide_{tag}", "bcond", 1, f"divide_{tag}")

def set_hi(hi, value, note):
    I(f"movs r0, #{value}", "movs", 0, value)
    I(f"mov r{hi}, r0       -> {note}", "movhi", hi, 0)

def build():
    # ---- setup
    I("movs r7, #0x40", "movs", 7, 0x40); I("lsls r7, r7, #12", "lsls", 7, 7, 12)
    I("adds r7, #2", "addsi", 7, 2);      I("lsls r7, r7, #12  -> r7 = 0x40002000 (COUNT; CLEAR at +4)", "lsls", 7, 7, 12)
    I("movs r5, #0       -> the timed store's data", "movs", 5, 0)
    set_hi(9, 0, "r9: NOP sled entry (2j)")
    set_hi(10, 0, "r10: pad sled entry (2m)")
    set_hi(8, 4, "r8: N = 4")
    # ---- V: the timed store aimed at CLEAR; COUNT at the end is then V
    I("adds r6, r7, #4   -> r6 = CLEAR", "addsi3", 6, 7, 4)
    measure("V")
    I("movs r3, #250", "movs", 3, 250); I("lsls r3, r3, #2  -> 1000", "lsls", 3, 3, 2)
    I("adds r3, #1", "addsi", 3, 1)
    I("adds r3, r3, r0   -> T = 1001 + V: COUNT at the end when the timed store's is 1000", "addsr", 3, 3, 0)
    I("mov r11, r3       -> r11: T", "movhi", 11, 3)
    # ---- from now on the timed store writes r5 (0 until the real run) to the LEDs
    I("movs r6, #0x40", "movs", 6, 0x40); I("lsls r6, r6, #12", "lsls", 6, 6, 12)
    I("adds r6, #1", "addsi", 6, 1);      I("lsls r6, r6, #12  -> r6 = 0x40001000 (LEDs)", "lsls", 6, 6, 12)
    # ---- P, the loop's period: (S(8) - S(4)) / 4; C8, what 8 NOP entries cost
    measure("S(4, 0, 0)")
    store(0, "s40")
    set_hi(8, 8, "N = 8")
    measure("S(8, 0, 0)")
    load(2, "s40")
    I("subs r0, r0, r2   -> 4 P", "subsr", 0, 0, 2)
    I("lsrs r0, r0, #2   -> P", "lsrs", 0, 0, 2)
    I("bne have_p", "bcond", 1, "have_p")
    I("udf #0           -> P = 0: nothing to measure with", "raw", 0xDE00)
    L("have_p")
    store(0, "p")
    set_hi(8, 4, "N = 4")
    set_hi(9, 16, "entry 8")
    measure("S(4, 8, 0)")
    load(2, "s40")
    I("subs r0, r2, r0   -> C8: entry 8 skips 8 NOPs", "subsr", 0, 2, 0)
    I("cmp r0, #4", "cmpi", 0, 4)
    I("bge have_c8", "bcond", 10, "have_c8")
    I("movs r0, #8       -> (not a sensible cost: take a cycle a NOP)", "movs", 0, 8)
    L("have_c8")
    store(0, "c8")
    I("movs r0, #0", "movs", 0, 0)
    store(0, "m")
    # ---- for each pad entry m, scan the NOP entries j by arithmetic: S(4, j, m) is about
    # S(4, 0, m) - C8 * j / 8, so T - S(4, j, m) is a multiple of P when the residue
    # (T - S(4, 0, m) + C8 * j / 8) mod P is 0. Only those entries get measured.
    L("try_m")
    load(0, "m")
    I("mov r10, r0", "movhi", 10, 0)
    I("movs r5, #0", "movs", 5, 0)
    set_hi(8, 4, "N = 4")
    set_hi(9, 0, "entry 0")
    measure("S(4, 0, m)")
    I("mov r4, r0       -> S(4, 0, m)", "movhi", 4, 0)
    load(0, "c8")
    I("mov r8, r0       -> r8: C8, while scanning", "movhi", 8, 0)
    load(2, "p")
    I("mov r3, r11", "movhi", 3, 11)
    I("subs r3, r3, r4   -> T - S(4, 0, m)", "subsr", 3, 3, 4)
    I("bpl t_above", "bcond", 5, "t_above")
    I("movs r3, #0", "movs", 3, 0)
    L("t_above")
    divide("r")
    I("movs r4, #4       -> r4: C8 * j + 4 (rounding), r12: C8 * j / 8, r1: j, r3: residue", "movs", 4, 4)
    I("movs r1, #0", "movs", 1, 0)
    I("mov r12, r1", "movhi", 12, 1)
    L("scan")
    I("cmp r3, #0", "cmpi", 3, 0)
    I("beq candidate", "bcond", 0, "candidate")
    L("advance")
    I("adds r1, #1       -> j + 1", "addsi", 1, 1)
    I(f"cmp r1, #{J}", "cmpi", 1, J)
    I("bcs next_m", "bcond", 2, "next_m")
    I("mov r0, r8", "movhi", 0, 8)
    I("adds r4, r4, r0", "addsr", 4, 4, 0)
    I("lsrs r0, r4, #3   -> C8 * j / 8", "lsrs", 0, 4, 3)
    I("mov r5, r12", "movhi", 5, 12)
    I("subs r5, r0, r5   -> what this entry takes off", "subsr", 5, 0, 5)
    I("mov r12, r0", "movhi", 12, 0)
    I("adds r3, r3, r5", "addsr", 3, 3, 5)
    L("modp")
    I("cmp r3, r2", "cmpr", 3, 2)
    I("bcc scan", "bcond", 3, "scan")
    I("subs r3, r3, r2", "subsr", 3, 3, 2)
    I("b modp", "b", "modp")
    # ---- an entry that should fit: measure it
    L("candidate")
    I("adds r0, r3, #0", "addsi3", 0, 3, 0)
    store(0, "res")
    store(1, "j")
    store(4, "acc")
    I("mov r0, r12", "movhi", 0, 12)
    store(0, "d")
    I("adds r1, r1, r1   -> 2j", "addsr", 1, 1, 1)
    I("mov r9, r1", "movhi", 9, 1)
    I("movs r5, #0", "movs", 5, 0)
    set_hi(8, 4, "N = 4")
    measure("S(4, j, m)")
    load(2, "p")
    I("mov r3, r11", "movhi", 3, 11)
    I("subs r3, r3, r0   -> T - S(4, j, m)", "subsr", 3, 3, 0)
    I("bmi resume", "bcond", 4, "resume")
    divide("n")
    I("cmp r3, #0", "cmpi", 3, 0)
    I("bne resume        -> not a whole number of loop periods after all", "bcond", 1, "resume")
    I("adds r1, #4       -> N = 4 + (T - S(4, j, m)) / P", "addsi", 1, 4)
    I("mov r8, r1", "movhi", 8, 1)
    # ---- the real thing: the same run, over and over with the same code between runs,
    # writing LED 5 once the two runs before have both ended at exactly T
    I("movs r0, #4       -> at most 4 runs", "movs", 0, 4)
    store(0, "rep")
    I("movs r5, #0", "movs", 5, 0)
    I("mov r12, r5       -> r12: the run before's COUNT at the end", "movhi", 12, 5)
    L("rep")
    I("mov r1, r8", "movhi", 1, 8)
    I("bl copy", "bl", "copy")
    I("cmp r5, #32", "cmpi", 5, 32)
    I("beq done         -> that run wrote LED 5", "bcond", 0, "done")
    I("mov r3, r11", "movhi", 3, 11)
    I("subs r4, r0, r3   -> this run - T", "subsr", 4, 0, 3)
    I("mov r2, r12", "movhi", 2, 12)
    I("subs r2, r2, r3   -> the run before - T", "subsr", 2, 2, 3)
    dp("orrs", 4, 2, "0 only if both were exactly T")
    I("rsbs r4, r4, #0   -> C set only if so", "dp", DP["rsbs"], 4, 4)
    I("movs r5, #0", "movs", 5, 0)
    dp("adcs", 5, 5, "C")
    I("lsls r5, r5, #5   -> 32 (LED 5) or 0: no branch, so the time between runs is always the same", "lsls", 5, 5, 5)
    I("mov r12, r0", "movhi", 12, 0)
    load(2, "rep")
    I("subs r2, #1", "subsi", 2, 1)
    I("str r2, [r3, #24]", "str", 2, 3, VARS["rep"] // 4)
    I("bne rep", "bcond", 1, "rep")
    I("movs r5, #0       -> never steady at T: keep looking", "movs", 5, 0)
    I("b resume", "b", "resume")
    L("resume")
    load(0, "c8")
    I("mov r8, r0", "movhi", 8, 0)
    load(0, "d")
    I("mov r12, r0", "movhi", 12, 0)
    load(1, "j")
    load(4, "acc")
    load(2, "p")
    load(0, "res")
    I("adds r3, r0, #0", "addsi3", 3, 0, 0)
    I("b advance", "b", "advance")
    L("next_m")
    load(0, "m")
    I("adds r0, #2       -> next pad entry", "addsi", 0, 2)
    I("str r0, [r3, #12]", "str", 0, 3, VARS["m"] // 4)
    I(f"cmp r0, #{2*M}", "cmpi", 0, 2*M)
    I("bcs give_up", "bcond", 2, "give_up")
    I("b try_m", "b", "try_m")
    L("give_up")
    L("done")
    I("udf #0           -> halt", "raw", 0xDE00)
    # ---- the timed code
    at(COPY)
    L("copy")
    I("str r0, [r7, #4]  -> CLEAR: COUNT is 0 from the next cycle", "str", 0, 7, 1)
    L("loop")
    I("subs r1, #1", "subsi", 1, 1)
    I("bne loop         -> N times", "bcond", 1, "loop")
    I("add pc, r10      -> into the pad sled, at entry m", "addpc", 10)
    I("nop              (never runs)", "raw", 0xBF00)
    for k in range(M): I(f"str r5, [r7, #8]  pad {k}: an APB write to nothing (COUNT + 8)", "str", 5, 7, 2)
    I("add pc, r9       -> into the NOP sled, at entry j", "addpc", 9)
    I("nop              (never runs)", "raw", 0xBF00)
    for k in range(J): I(f"nop              sled {k}", "raw", 0xBF00)
    I("str r5, [r6, #0]  -> the timed store: r5 to the LEDs (or CLEAR)", "str", 5, 6, 0)
    for _ in range(4): I("nop", "raw", 0xBF00)
    I("ldr r0, [r7, #0]  -> COUNT", "ldr", 0, 7, 0)
    I("bx lr", "raw", 0x4770)

def encode(kind, args, pc, labels):
    def off(t): return labels[t] - (pc + 4)
    if kind == "raw":   return [args[0]]
    if kind == "dp":    return [0x4000 | args[0] << 6 | args[2] << 3 | args[1]]
    if kind == "movs":  return [0x2000 | args[0] << 8 | args[1]]
    if kind == "addsi": return [0x3000 | args[0] << 8 | args[1]]
    if kind == "addsi3": return [0x1C00 | args[2] << 6 | args[1] << 3 | args[0]]
    if kind == "subsi": return [0x3800 | args[0] << 8 | args[1]]
    if kind == "cmpi":  return [0x2800 | args[0] << 8 | args[1]]
    if kind == "lsls":  return [0x0000 | args[2] << 6 | args[1] << 3 | args[0]]
    if kind == "lsrs":  return [0x0800 | args[2] << 6 | args[1] << 3 | args[0]]
    if kind == "addsr": return [0x1800 | args[2] << 6 | args[1] << 3 | args[0]]
    if kind == "subsr": return [0x1A00 | args[2] << 6 | args[1] << 3 | args[0]]
    if kind == "cmpr":  return [0x4280 | args[1] << 3 | args[0]]
    if kind == "movhi": rd, rm = args; return [0x4600 | (rd >> 3) << 7 | rm << 3 | (rd & 7)]
    if kind == "addpc": return [0x4487 | args[0] << 3]
    if kind == "str":   return [0x6000 | args[2] << 6 | args[1] << 3 | args[0]]
    if kind == "ldr":   return [0x6800 | args[2] << 6 | args[1] << 3 | args[0]]
    if kind == "b":
        o = off(args[0]); assert -2048 <= o >> 1 < 1024
        return [0xE000 | (o >> 1) & 0x7FF]
    if kind == "bcond":
        o = off(args[1]); assert -128 <= o >> 1 < 128
        return [0xD000 | args[0] << 8 | (o >> 1) & 0xFF]
    if kind == "bl":
        o = off(args[0]) >> 1
        s = (o >> 23) & 1; i1 = (o >> 22) & 1; i2 = (o >> 21) & 1
        j1 = 1 - (i1 ^ s); j2 = 1 - (i2 ^ s)
        return [0xF000 | s << 10 | (o >> 11) & 0x3FF, 0xD000 | j1 << 13 | j2 << 11 | o & 0x7FF]
    raise ValueError(kind)

def size(kind): return 4 if kind == "bl" else 2

build()
labels, pc = {}, 0
for item in prog:
    if item[0] == "org": assert item[1] >= pc, hex(pc); pc = item[1]
    elif item[0] == "label": labels[item[1]] = pc
    else: pc += size(item[2][0])
lines, pc = [], 0
header = """// Turn LED 5 on with COUNT exactly 1000, on any core
//
// Approach: measure this core, then do it, always with the same code. A run of
// the timed code clears the counter (COUNT is 0 from the cycle after), goes N
// times round a loop, jumps into a sled of harmless APB writes at entry m and a
// sled of NOPs at entry j, and ends with the timed store, then a load of COUNT.
// Only registers change between runs: the timed store writes 0 to the LEDs
// while measuring, and 32 (LED 5) for real.
//
// T: with the timed store aimed at CLEAR, COUNT at the end is V; so a run that
// ends with COUNT = T = 1001 + V had its timed store at COUNT 1000. S(N, j, m)
// is COUNT at the end; each measurement is the second of two identical runs.
// P = (S(8, 0, 0) - S(4, 0, 0)) / 4 is the loop's period, and C8 what 8 NOP
// entries cost. For each m, the NOP entries are scanned by arithmetic (entry j
// takes about C8 * j / 8 off S(4, 0, m)), and those that should leave T - S(4, j, m)
// a multiple of P are measured: if it is, N = 4 + that / P. Then the
// run is repeated with the same code between runs, and the run after two that
// both ended at exactly T writes LED 5. The pad writes give a step other than
// a NOP's, for a core where every NOP-and-loop time has the same parity.
//
// Assumes only that a store to CLEAR takes as long as one to the LEDs (both APB
// writes behind the same bridge), and a loop period of at most about 32 NOPs.
"""
lines.append(header + "@00000000")
for item in prog:
    if item[0] == "org":
        lines.append(f"@{item[1]:08x}"); pc = item[1]
    elif item[0] == "label":
        lines.append(f"// {item[1]}:")
    else:
        text, enc = item[1], item[2]
        words = encode(enc[0], enc[1:], pc, labels)
        b = " ".join(f"{w & 0xFF:02X} {w >> 8:02X}" for w in words)
        lines.append(f"{b:<12} // {text}")
        pc += 2 * len(words)
open(sys.argv[1], "w").write("\n".join(lines) + "\n")
print(f"{pc} bytes; labels:", {k: hex(v) for k, v in labels.items() if not k.startswith('loop')})
