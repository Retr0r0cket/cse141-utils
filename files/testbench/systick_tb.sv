`timescale 1ns / 1ps
`default_nettype none

// Checks SysTick (rtl/cpu/systick.sv) through the Private Peripheral Bus
// (rtl/cpu/private_peripheral_bus.sv), with the core's timing: the access in
// the cycle sel_i is high, a load's data on rdata_o the cycle after. Against
// ARMv6-M ARM B3.3, in two parts:
//
//   1. Directed checks, with the exact values: the registers after reset;
//      RVR's and CVR's top 8 bits RAZ/WI; a write to CVR clears it and
//      COUNTFLAG; counting down from RVR, a cycle at a time, reloading at 0;
//      COUNTFLAG set by the 1 -> 0 step and cleared by reading CSR (but not
//      lost when the two happen in the same cycle); disabling stops the
//      count; RVR 0 stops it at 0; a new RVR waits for the next reload;
//      other addresses in the region read 0 and ignore writes; rdata holds
//      between accesses; a reset partway through.
//   2. Random accesses, back to back or with gaps, random resets, against a
//      model of the registers, cycle by cycle.
//
//   just test systick_tb

module systick_tb;

  int errors = 0, checks = 0;

  task automatic fail(string what);
    if (errors < 20) $display("FAIL %s", what);
    errors++;
  endtask

  localparam logic [31:0] Csr = 32'hE000_E010, Rvr = 32'hE000_E014, Cvr = 32'hE000_E018,
                          Calib = 32'hE000_E01C;
  localparam logic [31:0] Enable = 32'h1, TickInt = 32'h2, ClkSource = 32'h4, CountFlag = 32'h1_0000;

  logic clk = 0, reset, sel, write;
  logic [31:0] addr, wdata, rdata;
  always #5 clk = !clk;

  private_peripheral_bus dut (
    .clk_i(clk), .reset_i(reset), .sel_i(sel), .write_i(write),
    .addr_i(addr), .wdata_i(wdata), .rdata_o(rdata)
  );

  // ========================================================================================
  // The model: SysTick's registers, a clock edge at a time
  // ========================================================================================

  logic [23:0] m_rvr, m_cvr;
  logic        m_enable, m_tickint, m_flag;
  logic [31:0] m_rdata;        // What rdata_o has, after the last load
  logic        m_check_rdata;  // The last cycle was a load: rdata_o must be m_rdata now
  logic [31:0] last_rdata;
  int          wraps, flag_reads, wrap_reads;  // Coverage

  function automatic logic is_systick(logic [31:0] a);
    return a[31:4] == 28'hE000_E01;
  endfunction

  function automatic logic [31:0] model_read(logic [31:0] a);
    if (!is_systick(a)) return 0;
    case (a[3:2])
      2'b00:   return {15'b0, m_flag, 13'b0, 1'b1, m_tickint, m_enable};  // CLKSOURCE reads 1
      2'b01:   return {8'b0, m_rvr};
      2'b10:   return {8'b0, m_cvr};
      default: return 32'h8000_0000;                                      // CALIB: NOREF, TENMS unknown
    endcase
  endfunction

  task automatic model_edge();
    logic [23:0] cvr;
    logic        flag, wrap;
    m_check_rdata = 0;
    if (reset) begin
      m_rvr = 0; m_cvr = 0; m_enable = 0; m_tickint = 0; m_flag = 0;
      return;
    end
    // Counting, with this cycle's ENABLE: reload at 0, else down, and the 1 -> 0 step sets
    // COUNTFLAG
    cvr = m_cvr; flag = m_flag; wrap = 0;
    if (m_enable) begin
      if (m_cvr == 0) cvr = m_rvr;
      else begin cvr = m_cvr - 1; wrap = (m_cvr == 1); end
    end
    if (sel && !write) begin
      m_rdata = model_read(addr);
      m_check_rdata = 1;
      // Reading CSR clears COUNTFLAG. A wrap in the same cycle comes after the read, so
      // it's "since the last read": the next read must still see it.
      if (is_systick(addr) && addr[3:2] == 2'b00) begin
        flag = 0;
        if (m_flag) flag_reads++;
        if (wrap) wrap_reads++;
      end
    end
    if (wrap) begin flag = 1; wraps++; end
    if (sel && write && is_systick(addr)) begin
      case (addr[3:2])
        2'b00: begin m_enable = wdata[0]; m_tickint = wdata[1]; end
        2'b01: m_rvr = wdata[23:0];
        2'b10: begin cvr = 0; flag = 0; end   // Any write clears CVR and COUNTFLAG
        default: ;                           // CALIB is read-only
      endcase
    end
    m_cvr = cvr; m_flag = flag;
  endtask

  // One clock edge: inputs are set; the model and the DUT both take the edge, then the
  // outputs are checked
  task automatic cycle();
    logic was_access;
    was_access = sel;
    last_rdata = rdata;
    model_edge();
    @(posedge clk); #1;
    if (m_check_rdata) begin
      checks++;
      if (rdata !== m_rdata)
        fail($sformatf("t=%0t: read %h: %h, want %h", $time, addr, rdata, m_rdata));
    end else if (!was_access && !reset) begin
      checks++;
      if (rdata !== last_rdata)
        fail($sformatf("t=%0t: rdata changed from %h to %h with no access", $time, last_rdata, rdata));
    end
  endtask

  task automatic idle(int n);
    sel = 0; write = 0;
    repeat (n) cycle();
  endtask

  task automatic wr(logic [31:0] a, logic [31:0] d);
    sel = 1; write = 1; addr = a; wdata = d;
    cycle();
    sel = 0; write = 0; wdata = 'x;
  endtask

  task automatic rd(logic [31:0] a, output logic [31:0] d);
    sel = 1; write = 0; addr = a;
    cycle();
    sel = 0;
    d = rdata;
  endtask

  // A read, checked against a literal value too (the model checks every read anyway)
  task automatic expect_rd(string what, logic [31:0] a, logic [31:0] want);
    logic [31:0] d;
    rd(a, d);
    checks++;
    if (d !== want) fail($sformatf("%s: read %h: %h, want %h", what, a, d, want));
  endtask

  task automatic do_reset();
    reset = 1; sel = 0; write = 0;
    cycle(); cycle();
    reset = 0;
  endtask

  // ========================================================================================
  // 1. Directed
  // ========================================================================================

  task automatic directed();
    logic [31:0] a, b;

    // After reset: everything 0 but CLKSOURCE (the processor clock, the only one) and
    // CALIB's NOREF (so no reference clock), with TENMS 0 (not known)
    do_reset();
    expect_rd("reset: CSR", Csr, ClkSource);
    expect_rd("reset: RVR", Rvr, 0);
    expect_rd("reset: CVR", Cvr, 0);
    expect_rd("reset: CALIB", Calib, 32'h8000_0000);
    idle(20);
    expect_rd("disabled after reset: CVR still", Cvr, 0);

    // RVR is 24 bits: the top 8 read as 0 and ignore writes
    wr(Rvr, 32'hFFFF_FFFF);
    expect_rd("RVR, top byte RAZ/WI", Rvr, 32'h00FF_FFFF);
    wr(Rvr, 32'hA5C3_0F96);
    expect_rd("RVR", Rvr, 32'h00C3_0F96);
    // CALIB is read-only; TICKINT reads back
    wr(Calib, 32'hFFFF_FFFF);
    expect_rd("CALIB, read-only", Calib, 32'h8000_0000);
    wr(Csr, TickInt);
    expect_rd("CSR, TICKINT", Csr, ClkSource | TickInt);

    // Counting down from RVR = 3, once a cycle, from 0 (written to CVR): the first
    // enabled cycle reloads it, then 3, 2, 1, 0, 3, ...
    wr(Csr, 0);
    wr(Rvr, 3);
    wr(Cvr, 32'h1234_5678);
    expect_rd("CVR, cleared by any write", Cvr, 0);
    wr(Csr, Enable);
    expect_rd("counting: CVR (reloads now)", Cvr, 0);
    expect_rd("counting: CVR", Cvr, 3);
    expect_rd("counting: CVR", Cvr, 2);
    expect_rd("counting: CVR", Cvr, 1);   // 1 -> 0 here: COUNTFLAG
    expect_rd("counting: CVR", Cvr, 0);
    expect_rd("counting: CVR (wrapped)", Cvr, 3);
    expect_rd("counting: CVR", Cvr, 2);
    // CVR is 1 in this cycle: COUNTFLAG is set from the last wrap, and this read clears
    // it, but CVR also steps 1 -> 0 here, which the next read must see
    expect_rd("COUNTFLAG, set", Csr, ClkSource | CountFlag | Enable);
    expect_rd("COUNTFLAG, a wrap during the last read isn't lost", Csr, ClkSource | CountFlag | Enable);
    expect_rd("COUNTFLAG, cleared by reading CSR", Csr, ClkSource | Enable);

    // A write to CVR clears COUNTFLAG
    idle(8);
    wr(Cvr, 0);
    expect_rd("COUNTFLAG, cleared by writing CVR", Csr, ClkSource | Enable);

    // Disabling stops the count where it is
    wr(Rvr, 1000);
    idle(10);
    wr(Csr, 0);
    rd(Cvr, a);
    idle(50);
    rd(Cvr, b);
    checks++;
    if (a !== b || a == 0 || a > 1000) fail($sformatf("disabled: CVR %0d then %0d, want it held", a, b));

    // A new RVR waits for the next reload: CVR counts on from where it was
    wr(Csr, Enable);
    wr(Rvr, 5);
    rd(Cvr, a);
    checks++;
    if (a <= 5) fail($sformatf("new RVR: CVR %0d, want it counting on from its old reload", a));
    wr(Cvr, 0);
    expect_rd("new RVR: CVR (reloads now)", Cvr, 0);
    expect_rd("new RVR: CVR", Cvr, 5);

    // RVR 0: the counter stops at 0 after its next wrap, and COUNTFLAG stays clear
    wr(Rvr, 0);
    idle(10);
    rd(Csr, a);   // Clear the flag from that wrap
    idle(20);
    expect_rd("RVR 0: CVR", Cvr, 0);
    expect_rd("RVR 0: COUNTFLAG stays clear", Csr, ClkSource | Enable);

    // Elsewhere in the system region: reads as 0, ignores writes
    wr(Rvr, 32'h77);
    wr(32'hE000_E000, 32'hFFFF_FFFF);
    wr(32'hE000_E020, 32'hFFFF_FFFF);
    wr(32'hE000_ED00, 32'hFFFF_FFFF);
    wr(32'hF000_E014, 32'hFFFF_FFFF);
    expect_rd("not SysTick: 0xE000E000", 32'hE000_E000, 0);
    expect_rd("not SysTick: 0xE000E024", 32'hE000_E024, 0);
    expect_rd("not SysTick: 0xF000E014", 32'hF000_E014, 0);
    expect_rd("RVR, after writes elsewhere", Rvr, 32'h77);

    // rdata holds between accesses (cycle checks it), even as CVR counts under it
    expect_rd("CSR before a gap", Csr, ClkSource | Enable | (m_flag ? CountFlag : 0));
    idle(30);

    // A reset partway through a count
    wr(Rvr, 32'h00FF_FFFF);
    wr(Csr, Enable | TickInt);
    idle(100);
    do_reset();
    expect_rd("reset while counting: CSR", Csr, ClkSource);
    expect_rd("reset while counting: RVR", Rvr, 0);
    expect_rd("reset while counting: CVR", Cvr, 0);
  endtask

  // ========================================================================================
  // 2. Random, against the model
  // ========================================================================================

  task automatic random_accesses(int n);
    int k;
    for (int i = 0; i < n; i++) begin
      reset = ($urandom % 20000) == 0;
      sel   = ($urandom % 100) < 45;
      write = ($urandom % 100) < 35;
      k = $urandom % 100;
      if (k < 30)      addr = Csr;
      else if (k < 50) addr = Rvr;
      else if (k < 80) addr = Cvr;
      else if (k < 88) addr = Calib;
      else             addr = {$urandom} | 32'hE000_0000;   // Anywhere in the region
      addr[1:0] = 0;
      wdata = $urandom;
      // Mostly enabled; mostly short reloads, so it wraps often (sometimes 0, sometimes long)
      if (addr == Csr) wdata[0] = ($urandom % 100) < 85;
      if (addr == Rvr) begin
        k = $urandom % 100;
        if (k < 70)      wdata[23:0] = 24'($urandom % 16);
        else if (k < 75) wdata[23:0] = 0;
      end
      cycle();
    end
    reset = 0;
  endtask

  initial begin
    reset = 0; sel = 0; write = 0; addr = 0; wdata = 0;
    m_rdata = 0; m_check_rdata = 0;
    wraps = 0; flag_reads = 0; wrap_reads = 0;
    directed();
    $display("directed: %0d checks (%0d errors so far)", checks, errors);
    random_accesses(300000);
    $display("random: %0d wraps, %0d reads of COUNTFLAG set, %0d reads of CSR as it wrapped",
             wraps, flag_reads, wrap_reads);
    if (wrap_reads == 0) fail("random: never read CSR in the cycle it wrapped");
    if (errors == 0) $display("PASSED: %0d checks", checks);
    else             $display("FAILED: %0d errors in %0d checks", errors, checks);
    $finish;
  end

endmodule
