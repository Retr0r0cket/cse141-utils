`timescale 1ns / 1ps
`default_nettype none

// An AHB-Lite protocol checker for the core as a manager. It runs a test
// program on the whole system exactly as program_tb does (program_tb is
// inside, unchanged, so it writes the same .trace, .after.dmem and .bus for
// `just check-program`), and watches the bus between the core and the
// interconnect every cycle for what AMBA AHB (IHI 0033C) requires of a
// manager:
//
//   - in reset, htrans is IDLE (§7.1.2)
//   - htrans is never X, and an address phase's control signals are never X
//   - every transfer is a single: htrans IDLE or NONSEQ (never BUSY or SEQ),
//     hburst SINGLE, hmastlock 0 (§3.2, §3.5)
//   - hsize is a byte, a halfword or a word, and haddr is aligned to it (§3.4)
//   - hprot says what it is: 4'b0010 for a fetch, 4'b0011 for a load or store,
//     and a fetch is a word read (the course's rule; see manager_tb.sv)
//   - an address phase waited on (htrans NONSEQ, hready low) holds every
//     address and control signal until hready is high (§3.6.1)
//   - a write's hwdata is valid in its byte lanes, and holds through a
//     waited data phase (§3.1)
//   - nothing goes where there's nothing: no address outside Code, SRAM and
//     the peripherals (that gets the ERROR response), no write to Code, and
//     only words to the peripherals (the bridge's rule); hresp never ERROR
//   - in the cycle halt_o is high, no load or store starts, and any still
//     in its data phase ends (program_tb stops at the end of that cycle)
//
// It doesn't check timing or results; check-program does that. Run it with
// waits in memory (ahb_mem's +MEM_WAITS=<percent>, +MEM_WAIT_SEED=<n>) to
// exercise the waited cases; tools/ahb_suite.sh runs every program that way.
//
//   vvp ahb_lite_tb +TEST=<dir>/<name> [+MEM_WAITS=<percent>] [program_tb's plusargs]
//
// Each problem prints a line starting `AHB-LITE VIOLATION` (the first 20),
// and the end of the run prints `AHB-LITE PASSED` or `AHB-LITE FAILED` with
// counts of what was seen, so a run that never waited is easy to spot.

module ahb_lite_tb
  import ahb_pkg::*;
#(
  parameter int CODE_SIZE = 1024,
  parameter int SRAM_SIZE = 1024
);

  program_tb #(.CODE_SIZE(CODE_SIZE), .SRAM_SIZE(SRAM_SIZE)) tb ();

  // The bus, as top_level wires it
  wire        clock     = tb.clock;
  wire        reset_n   = tb.reset_n;
  wire        halt      = tb.halt;
  wire [31:0] haddr     = tb.dut.haddr;
  wire  [1:0] htrans    = tb.dut.htrans;
  wire        hwrite    = tb.dut.hwrite;
  wire  [2:0] hsize     = tb.dut.hsize;
  wire  [2:0] hburst    = tb.dut.hburst;
  wire  [3:0] hprot     = tb.dut.hprot;
  wire        hmastlock = tb.dut.hmastlock;
  wire [31:0] hwdata    = tb.dut.hwdata;
  wire        hready    = tb.dut.hready;
  wire        hresp     = tb.dut.hresp;

  localparam logic [3:0] HprotFetch = 4'b0010;
  localparam logic [3:0] HprotLoadStore = 4'b0011;

  int unsigned cycle = 0;
  int unsigned violations = 0;

  // What was seen, for the summary
  int unsigned fetches = 0, loads = 0, stores = 0, device = 0;
  int unsigned waited_data = 0;    // data phase cycles with hready low
  int unsigned held_address = 0;   // cycles an address phase was held through a wait
  int unsigned waited_writes = 0;  // cycles a store's data was held through a wait

  task automatic violation(input string what);
    violations++;
    if (violations <= 20)
      $display("AHB-LITE VIOLATION cycle %0d: %s", cycle, what);
    if (violations == 20)
      $display("AHB-LITE (no more violations shown)");
  endtask

  function automatic logic [31:0] lane_mask(input logic [1:0] addr, input logic [2:0] size);
    case (size)
      HsizeByte:     return 32'h0000_00FF << (8 * addr);
      HsizeHalfword: return addr[1] ? 32'hFFFF_0000 : 32'h0000_FFFF;
      default:       return 32'hFFFF_FFFF;
    endcase
  endfunction

  function automatic logic is_peripheral(input logic [31:0] addr);
    return addr[31:12] == 20'h4000_1 || addr[31:12] == 20'h4000_2;
  endfunction

  // The last address phase, for the hold check
  logic        held;          // last cycle was NONSEQ with hready low
  logic [31:0] p_haddr;
  logic        p_hwrite;
  logic  [2:0] p_hsize, p_hburst;
  logic  [3:0] p_hprot;
  logic        p_hmastlock;

  // The data phase in progress
  logic        dp_active, dp_write, dp_data, dp_first;
  logic [31:0] dp_addr, dp_wdata, dp_mask;
  logic  [2:0] dp_size;

  always @(posedge clock) begin
    cycle++;
    if (!reset_n) begin
      if (htrans !== HtransIdle)
        violation($sformatf("htrans is %b in reset, not IDLE", htrans));
      held = 0;
      dp_active = 0;
    end else begin
      // ---- The address phase ----
      if ($isunknown(htrans))
        violation($sformatf("htrans is %b", htrans));
      if ($isunknown(hready))
        violation("hready is X (a subordinate's fault, or a transfer nobody answers)");
      if (htrans == HtransBusy || htrans == HtransSeq)
        violation($sformatf("htrans %s: every transfer here is a single (NONSEQ)",
                            htrans == HtransBusy ? "BUSY" : "SEQ"));
      if (htrans == HtransNonseq) begin
        if ($isunknown({haddr, hwrite, hsize, hburst, hprot, hmastlock}))
          violation($sformatf("X in an address phase: haddr %h hwrite %b hsize %b hburst %b hprot %b hmastlock %b",
                              haddr, hwrite, hsize, hburst, hprot, hmastlock));
        else begin
          if (hburst != HburstSingle)
            violation($sformatf("hburst %b, not SINGLE", hburst));
          if (hmastlock)
            violation("hmastlock high");
          if (hsize > HsizeWord)
            violation($sformatf("hsize %b, wider than a word", hsize));
          else if ((haddr & ((32'd1 << hsize) - 1)) != 0)
            violation($sformatf("%0d-byte transfer at %h isn't aligned", 1 << hsize, haddr));
          if (hprot != HprotFetch && hprot != HprotLoadStore)
            violation($sformatf("hprot %b: a fetch is 0010, a load or store 0011", hprot));
          if (hprot == HprotFetch && (hwrite || hsize != HsizeWord))
            violation($sformatf("a fetch (hprot 0010) that isn't a word read: hwrite %b hsize %b", hwrite, hsize));
          if (!(haddr[31:30] == 2'b00 || is_peripheral(haddr)))
            violation($sformatf("transfer to %h, where nothing is (it gets ERROR)", haddr));
          if (hwrite && haddr[31:29] == 3'b000)
            violation($sformatf("write to Code at %h", haddr));
          if (is_peripheral(haddr) && hsize != HsizeWord)
            violation($sformatf("%0d-byte transfer to the peripherals at %h: words only", 1 << hsize, haddr));
          if (is_peripheral(haddr) && hprot != HprotLoadStore)
            violation($sformatf("a fetch from the peripherals at %h", haddr));
        end
      end
      if (held) begin
        held_address++;
        if (htrans !== HtransNonseq || haddr !== p_haddr || hwrite !== p_hwrite || hsize !== p_hsize ||
            hburst !== p_hburst || hprot !== p_hprot || hmastlock !== p_hmastlock)
          violation($sformatf("address phase changed while waited: was %h %s size %0d hprot %b, now htrans %b %h %s size %0d hprot %b",
                              p_haddr, p_hwrite ? "write" : "read", p_hsize, p_hprot,
                              htrans, haddr, hwrite ? "write" : "read", hsize, hprot));
      end

      // ---- The data phase ----
      if (dp_active && dp_write) begin
        if ($isunknown(hwdata & dp_mask))
          violation($sformatf("store to %h: hwdata %h has X in its byte lanes", dp_addr, hwdata));
        else if (!dp_first && ((hwdata ^ dp_wdata) & dp_mask) != 0)
          violation($sformatf("store to %h: hwdata changed from %h to %h during its waited data phase",
                              dp_addr, dp_wdata, hwdata));
        if (dp_first)
          dp_wdata = hwdata;
        if (!hready)
          waited_writes++;
      end
      if (dp_active && !hready)
        waited_data++;
      if (hresp === HrespError)
        violation("hresp ERROR");

      // ---- The halt ----
      if (halt) begin
        if (htrans == HtransNonseq && hprot == HprotLoadStore && hready)
          violation($sformatf("a %s of %h starts in the halt cycle", hwrite ? "store" : "load", haddr));
        if (dp_active && dp_data && !hready)
          violation($sformatf("the %s of %h is still in its data phase at the halt",
                              dp_write ? "store" : "load", dp_addr));
      end

      // ---- On to the next cycle ----
      held = (htrans == HtransNonseq) && !hready;
      {p_haddr, p_hwrite, p_hsize, p_hburst, p_hprot, p_hmastlock} =
          {haddr, hwrite, hsize, hburst, hprot, hmastlock};
      dp_first = 0;
      if (hready) begin
        dp_active = (htrans == HtransNonseq);
        dp_first = 1;
        if (dp_active) begin
          dp_write = hwrite;
          dp_data  = hprot[0];
          dp_addr  = haddr;
          dp_size  = hsize;
          dp_mask  = lane_mask(haddr[1:0], hsize);
          if (!hprot[0]) fetches++;
          else if (hwrite) stores++;
          else loads++;
          if (is_peripheral(haddr)) device++;
        end
      end
    end
  end

  final begin
    $display("AHB-LITE %s: %0d fetches, %0d loads, %0d stores (%0d to the peripherals); %0d waited data phase cycles, %0d held address phases, %0d held store data cycles; %0d violation%s",
             violations ? "FAILED" : "PASSED", fetches, loads, stores, device, waited_data,
             held_address, waited_writes, violations, violations == 1 ? "" : "s");
  end

endmodule : ahb_lite_tb
