`timescale 1ns / 1ps
`default_nettype none

// The system behind the core, on its own: the interconnect, memory, the APB
// bridge, the LEDs and our cycle counter, wired as top_level wires them, with
// a random AHB-Lite manager in the core's place. Every transfer is checked
// against a model, cycle by cycle:
//
//   - SRAM: bytes, halfwords and words at every alignment, with junk in the
//     byte lanes a write doesn't cover; a read returns what was written
//   - the LEDs: a read returns the last write's bits 5:0, at any offset in
//     their 4 KB, and the leds output follows every write
//   - the counter: COUNT is exactly the cycles since reset or the last CLEAR
//     (cleared at the end of CLEAR's access cycle); writes to COUNT, and
//     reads and writes elsewhere, do nothing; CLEAR and other offsets read 0
//   - unmapped addresses: the two-cycle ERROR response
//   - timing: an APB transfer's data phase is 2 cycles; memory's is 1 (unless
//     +MEM_WAITS adds waits); IDLE transfers do nothing
//   - APB: a setup cycle (psel, no penable) is always followed by its access
//     cycle with the same paddr, pwrite and pwdata; one psel at a time; paddr
//     is the transfer's low 12 bits, and pwdata its hwdata
//
// Transfers come back to back (an APB transfer's next address phase waits
// behind it, and its setup cycle follows the access cycle at once) or with
// IDLE cycles between, and resets come at random times, in the middle of
// transfers too. Run it with
//
//   just test peripherals_tb [+CYCLES=<n>] [+SEED=<n>] [+MEM_WAITS=<percent>]
//
// It prints PASSED or FAILED, with counts of what it saw.

module peripherals_tb
  import ahb_pkg::*;
;
  localparam int SramSize = 1024;

  logic clock = 0;
  always #5 clock = ~clock;
  logic reset_n;

  // ---- The system, as top_level wires it ----
  logic [31:0] haddr, hwdata, hrdata;
  logic  [1:0] htrans;
  logic        hwrite;
  logic  [2:0] hsize;
  logic        hready, hresp;
  logic        hsel_mem, hreadyout_mem, hresp_mem;
  logic [31:0] hrdata_mem;
  logic        hsel_apb, hreadyout_apb, hresp_apb;
  logic [31:0] hrdata_apb;
  logic [11:0] paddr;
  logic        penable, pwrite;
  logic [31:0] pwdata;
  logic        psel_led, pready_led, psel_counter, pready_counter;
  logic [31:0] prdata_led, prdata_counter;
  logic  [5:0] leds;

  ahb_interconnect bus (
      .clock, .reset_n, .haddr, .htrans, .hready, .hresp, .hrdata, .hsel_mem, .hsel_apb,
      .hreadyout_mem, .hresp_mem, .hrdata_mem, .hreadyout_apb, .hresp_apb, .hrdata_apb);
  ahb_mem #(.CODE_SIZE(1024), .SRAM_SIZE(SramSize)) memory (
      .clock, .reset_n, .hsel(hsel_mem), .haddr, .htrans, .hwrite, .hsize, .hwdata, .hready,
      .hreadyout(hreadyout_mem), .hresp(hresp_mem), .hrdata(hrdata_mem));
  ahb_to_apb bridge (
      .clock, .reset_n, .hsel(hsel_apb), .haddr, .htrans, .hwrite, .hwdata, .hready,
      .hreadyout(hreadyout_apb), .hresp(hresp_apb), .hrdata(hrdata_apb), .paddr, .penable,
      .pwrite, .pwdata, .psel_led, .prdata_led, .pready_led, .psel_counter, .prdata_counter,
      .pready_counter);
  led_apb led (
      .clock, .reset_n, .psel(psel_led), .penable, .pwrite, .pwdata, .prdata(prdata_led),
      .pready(pready_led), .leds);
  cycle_counter counter (
      .clock, .reset_n, .psel(psel_counter), .penable, .pwrite, .paddr, .pwdata,
      .prdata(prdata_counter), .pready(pready_counter));

  // ---- Checking and counting ----
  int unsigned cycle = 0, errors = 0;
  int unsigned n_idle = 0, n_sram = 0, n_led = 0, n_count = 0, n_clear = 0, n_other = 0,
               n_unmapped = 0, n_resets = 0, n_back_to_back_apb = 0, n_mid_reset = 0;

  task automatic fail(input string what);
    errors++;
    if (errors <= 20) $display("FAIL cycle %0d: %s", cycle, what);
  endtask

  // ---- The model ----
  typedef enum logic [2:0] {KSram, KLed, KCounter, KUnmapped} kind_t;
  logic  [7:0] m_sram [SramSize];
  logic        m_known [SramSize];
  logic  [5:0] m_led;
  logic [31:0] m_count;   // COUNT in the cycle now ending
  bit          mem_waits;

  function automatic kind_t kind_of(input logic [31:0] a);
    if (a[31:30] == 2'b00) return KSram;
    if (a[31:12] == 20'h4000_1) return KLed;
    if (a[31:12] == 20'h4000_2) return KCounter;
    return KUnmapped;
  endfunction

  function automatic logic is_apb(input kind_t k);
    return k == KLed || k == KCounter;
  endfunction

  // The data phase in progress
  logic        dp_valid, dp_write, dp_error_first;
  kind_t       dp_kind;
  logic [31:0] dp_addr, dp_wdata;
  logic  [2:0] dp_size;
  int unsigned dp_cycles;

  // Finish the data phase ending now: check it, and update the model
  task automatic finish(output logic clear_now);
    logic [31:0] want;
    clear_now = 0;
    case (dp_kind)
      KSram: begin
        if (hresp) fail($sformatf("SRAM %h: hresp ERROR", dp_addr));
        if (!mem_waits && dp_cycles != 1)
          fail($sformatf("SRAM %h: data phase of %0d cycles, not 1", dp_addr, dp_cycles));
        for (int i = 0; i < (1 << dp_size); i++) begin
          int unsigned off = dp_addr[9:0] + i;
          int unsigned lane = (dp_addr[1:0] + i) % 4;
          if (dp_write) begin
            m_sram[off] = dp_wdata[8 * lane +: 8];
            m_known[off] = 1;
          end else if (m_known[off] && hrdata[8 * lane +: 8] !== m_sram[off])
            fail($sformatf("SRAM read of %h (%0d bytes): lane %0d is %h, not %h",
                           dp_addr, 1 << dp_size, lane, hrdata[8 * lane +: 8], m_sram[off]));
        end
        n_sram++;
      end
      KLed: begin
        if (hresp) fail("LEDs: hresp ERROR");
        if (dp_cycles != 2) fail($sformatf("LEDs: data phase of %0d cycles, not 2", dp_cycles));
        if (dp_write) m_led = dp_wdata[5:0];
        else if (hrdata !== {26'b0, m_led})
          fail($sformatf("LEDs read at %h: %h, not %h", dp_addr, hrdata, {26'b0, m_led}));
        n_led++;
      end
      KCounter: begin
        if (hresp) fail("counter: hresp ERROR");
        if (dp_cycles != 2) fail($sformatf("counter: data phase of %0d cycles, not 2", dp_cycles));
        want = (dp_addr[11:0] == 12'h000) ? m_count : 32'h0;
        if (!dp_write && hrdata !== want)
          fail($sformatf("counter read at offset %h: %0d, not %0d", dp_addr[11:0], hrdata, want));
        if (dp_write && dp_addr[11:0] == 12'h004) clear_now = 1;
        if (dp_addr[11:0] == 12'h000 && !dp_write) n_count++;
        else if (dp_addr[11:0] == 12'h004 && dp_write) n_clear++;
        else n_other++;
      end
      KUnmapped: begin
        if (dp_cycles != 2) fail($sformatf("unmapped %h: data phase of %0d cycles, not 2", dp_addr, dp_cycles));
        if (!dp_error_first || hresp !== HrespError)
          fail($sformatf("unmapped %h: not a two-cycle ERROR response", dp_addr));
        n_unmapped++;
      end
      default: ;
    endcase
  endtask

  // A random transfer for the next address phase (or IDLE)
  task automatic next_address_phase();
    int r;
    logic [31:0] a;
    logic [2:0] s;
    logic w;
    r = $urandom % 100;
    s = HsizeWord;
    w = $urandom % 2;
    if (r < 30) begin
      // IDLE, with junk that must do nothing (it often looks like a write to the peripherals)
      htrans <= HtransIdle;
      haddr  <= ($urandom % 2) ? (32'h4000_1000 | ($urandom % 2) << 13 | ($urandom & 32'hFFC)) : $urandom;
      hwrite <= $urandom % 2;
      hsize  <= HsizeWord;
      n_idle++;
      return;
    end else if (r < 55) begin
      s = 3'($urandom % 3);
      a = 32'h2000_0000 | (($urandom % 64) & ~((32'd1 << s) - 1));
    end else if (r < 72) begin
      a = 32'h4000_1000 | (($urandom % 8 == 0) ? ($urandom & 32'hFFC) : 32'h0);
    end else if (r < 95) begin
      int c = $urandom % 10;
      if (c < 5) begin a = 32'h4000_2000; w = (c == 0); end         // COUNT (mostly read)
      else if (c < 8) begin a = 32'h4000_2004; w = (c != 7); end    // CLEAR (mostly write)
      else a = 32'h4000_2000 | (8 + ($urandom % 1022) * 4);         // nothing there
    end else begin
      // Nowhere: below, between and above the peripherals
      case ($urandom % 4)
        0: a = 32'h4000_0000 | ($urandom & 32'hFFC);
        1: a = 32'h4000_3000 | ($urandom & 32'hFFC);
        2: a = 32'h5000_0000;
        default: a = 32'hFFFF_FFFC;
      endcase
    end
    htrans <= HtransNonseq;
    haddr  <= a;
    hwrite <= w;
    hsize  <= s;
  endtask

  // The manager, and the model's clock
  logic [1:0] reset_hold;
  always @(posedge clock) begin
    logic clear_now;
    cycle++;
    clear_now = 0;
    if (!reset_n) begin
      // In reset: nothing on the bus, and COUNT is 0 in the cycle after
      htrans <= HtransIdle;
      dp_valid = 0;
      m_count = 0;
      if (reset_hold != 0) reset_hold--;
      else reset_n <= 1;
    end else begin
      // The leds output is the model's, every cycle
      if (leds !== m_led) fail($sformatf("leds %b, not %b", leds, m_led));
      if (dp_valid) begin
        dp_cycles++;
        if (dp_cycles == 1) dp_error_first = (hresp === HrespError) && !hready;
      end else if (hresp === HrespError)
        fail("hresp ERROR with no transfer");
      if (hready) begin
        if (dp_valid) finish(clear_now);
        // The address phase ending now starts its data phase
        if (htrans == HtransNonseq) begin
          if (dp_valid && is_apb(kind_of(haddr)) && is_apb(dp_kind))
            n_back_to_back_apb++;
          dp_valid  = 1;
          dp_kind   = kind_of(haddr);
          dp_addr   = haddr;
          dp_write  = hwrite;
          dp_size   = hsize;
          dp_cycles = 0;
          dp_wdata  = $urandom;
          hwdata   <= dp_wdata;
        end else begin
          dp_valid = 0;
          hwdata  <= $urandom;  // nobody's: must not be written anywhere
        end
        next_address_phase();
      end
      m_count = clear_now ? 32'h0 : m_count + 1;
      // Now and then, a reset (the peripherals' resets are asynchronous: right now)
      if ($urandom % 3000 == 0) begin
        if (dp_valid) n_mid_reset++;
        n_resets++;
        reset_n <= 0;
        reset_hold = 2'($urandom % 3);
        m_led = 0;
      end
    end
  end

  // ---- APB, every cycle ----
  logic        p_setup;
  logic [11:0] p_paddr;
  logic        p_pwrite, p_sel_led;
  always @(posedge clock) begin
    if (reset_n) begin
      if (psel_led && psel_counter) fail("psel_led and psel_counter together");
      if (penable && !(psel_led || psel_counter)) fail("penable without psel");
      if (p_setup) begin
        if (!penable || !(psel_led || psel_counter) || psel_led != p_sel_led ||
            paddr != p_paddr || pwrite != p_pwrite)
          fail("a setup cycle not followed by its access cycle");
      end
      if ((psel_led || psel_counter) && penable) begin
        if (pwrite !== dp_write || paddr !== dp_addr[11:0])
          fail($sformatf("APB access: paddr %h pwrite %b, for a transfer to %h (%s)", paddr, pwrite,
                         dp_addr, dp_write ? "write" : "read"));
        if (pwrite && pwdata !== dp_wdata) fail($sformatf("APB write: pwdata %h, not %h", pwdata, dp_wdata));
      end
      p_setup   = (psel_led || psel_counter) && !penable;
      p_paddr   = paddr;
      p_pwrite  = pwrite;
      p_sel_led = psel_led;
    end else
      p_setup = 0;
  end

  int unsigned cycles, seed, waits;
  initial begin
    cycles = 200000;
    seed = 1;
    waits = 0;
    void'($value$plusargs("CYCLES=%d", cycles));
    if ($value$plusargs("SEED=%d", seed)) void'($urandom(seed));
    mem_waits = $value$plusargs("MEM_WAITS=%d", waits) && waits != 0;
    for (int i = 0; i < SramSize; i++) m_known[i] = 0;
    m_led = 0;
    dp_valid = 0;
    p_setup = 0;
    reset_hold = 0;
    htrans = HtransIdle;
    haddr = 0;
    hwrite = 0;
    hsize = HsizeWord;
    hwdata = 0;
    reset_n = 0;
    repeat (cycles) @(posedge clock);
    #1;
    $display("%0d cycles: %0d SRAM transfers, %0d LED, %0d COUNT reads, %0d CLEARs, %0d elsewhere in the counter, %0d unmapped, %0d IDLE; %0d APB transfers back to back, %0d resets (%0d mid-transfer)",
             cycles, n_sram, n_led, n_count, n_clear, n_other, n_unmapped, n_idle,
             n_back_to_back_apb, n_resets, n_mid_reset);
    if (errors == 0 && n_count > 0 && n_back_to_back_apb > 0 && n_unmapped > 0)
      $display("PASSED");
    else
      $display("FAILED (%0d errors)", errors);
    $finish;
  end

endmodule : peripherals_tb
