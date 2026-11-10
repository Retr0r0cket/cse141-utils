`timescale 1ns / 1ps
`default_nettype none

// Checks fetch (rtl/fetch/fetch_stage.sv, with its instruction buffer) against
// a model of what it must hand decode: the instructions in program order from
// wherever it was last sent, each with its own address, PC + 4 and decoded
// controls, whatever the bus and decode do around it. For several buffer
// depths and reset addresses at once (one fetch_env each), each with:
//
//   - a random program in a memory model whose words come back the cycle after
//     their address (X while the bus waits, so nothing may take them then)
//   - random grants (the memory controller giving the bus to a load or store),
//     random bus waits, random decode stalls (advance low)
//   - random redirects, to anywhere, including a word's upper half, and in the
//     same cycle as anything else; random resets
//
// and then a stretch with nothing in the way, where fetch must hand decode an
// instruction every cycle.
//
//   just test fetch_tb

module fetch_tb;
  localparam int Envs = 6;
  logic [Envs-1:0] done;
  int errors [Envs];

  fetch_env #(.Depth(32), .ResetPc(32'h0)) e0  (.done(done[0]), .errors(errors[0]));
  fetch_env #(.Depth(4),  .ResetPc(32'h0)) e1  (.done(done[1]), .errors(errors[1]));
  fetch_env #(.Depth(8),  .ResetPc(32'h0)) e2  (.done(done[2]), .errors(errors[2]));
  fetch_env #(.Depth(16), .ResetPc(32'h0)) e3  (.done(done[3]), .errors(errors[3]));
  fetch_env #(.Depth(8),  .ResetPc(32'h102))  e4 (.done(done[4]), .errors(errors[4]));
  fetch_env #(.Depth(4),  .ResetPc(32'h1FFE)) e5 (.done(done[5]), .errors(errors[5]));

  initial begin
    int total;
    wait (&done);
    total = 0;
    for (int i = 0; i < Envs; i++) total += errors[i];
    if (total == 0) $display("PASSED: fetch, %0d configurations", Envs);
    else            $display("FAILED: %0d errors", total);
    $finish;
  end
endmodule


module fetch_env
  import cpu_pkg::*;
#(
  parameter int          Depth   = 8,
  parameter logic [31:0] ResetPc = 32'h0
) (
  output logic done,
  output int   errors
);
  localparam int Cycles    = 100000;   // Random
  localparam int Straight  = 2000;     // Nothing in the way
  localparam int MemWords  = 4096;     // 16 KiB of program; addresses wrap
  localparam int AddrBits  = $clog2(MemWords) + 2;

  logic clk = 0, reset;
  always #5 clk = !clk;

  // ---- the fetch stage --------------------------------------------------------------------
  logic        bus_stall, redirect, advance;
  pc_t         redirect_pc;
  logic        fetch_req, fetch_grant, bus_free;  // The memory controller grants a request when the bus is free
  logic [31:0] fetch_addr, hrdata;
  logic        if_id_valid;
  logic [15:0] if_id_instr;
  pc_t         if_id_pc, if_id_pc_plus4, if_id_target;
  ctrl_t       if_id_ctrl;
  logic [ImmW-1:0] if_id_imm;

  fetch_stage #(.ResetPc(ResetPc), .BufferDepth(Depth)) dut (
    .clk_i            (clk), .reset_i(reset), .bus_stall_i(bus_stall),
    .redirect_i       (redirect), .redirect_pc_i(redirect_pc),
    .fetch_req_o      (fetch_req), .fetch_addr_o(fetch_addr), .fetch_grant_i(fetch_grant), .hrdata_i(hrdata),
    .advance_i        (advance), .if_id_valid_o(if_id_valid), .if_id_instr_o(if_id_instr), .if_id_pc_o(if_id_pc),
    .if_id_pc_plus4_o (if_id_pc_plus4), .if_id_ctrl_o(if_id_ctrl), .if_id_imm_o(if_id_imm),
    .if_id_target_o   (if_id_target)
  );

  // What IF/ID's controls must be: the decoders' on IF/ID's own instruction and PC
  ctrl_t       want_ctrl;
  imm_sel_e    want_imm_sel;
  logic [ImmW-1:0] want_imm;
  pc_t         want_target, want_pc_plus4;
  control_decoder   ref_controls (.instr_i(if_id_instr), .ctrl_o(want_ctrl), .imm_sel_o(want_imm_sel));
  immediate_decoder ref_immediates (.instr_i(if_id_instr), .pc_i(if_id_pc), .imm_sel_i(want_imm_sel),
                                    .imm_o(want_imm), .branch_target_o(want_target), .pc_plus4_o(want_pc_plus4));

  // ---- memory: a word the cycle after its address is granted -----------------------------
  logic [31:0] mem [MemWords];
  logic [31:0] data_addr;
  logic        data_phase;

  function automatic logic [15:0] halfword(pc_t pc);
    logic [31:0] w;
    w = mem[pc[AddrBits-1:2]];
    return pc[1] ? w[31:16] : w[15:0];
  endfunction

  always_ff @(posedge clk) begin
    if (reset) data_phase <= 1'b0;
    else if (!bus_stall) begin
      data_phase <= fetch_grant;
      if (fetch_grant) data_addr <= fetch_addr;
    end
  end
  assign fetch_grant = fetch_req && bus_free;
  assign hrdata = (bus_stall || !data_phase) ? 'x : mem[data_addr[AddrBits-1:2]];

  // ---- stimulus and checking --------------------------------------------------------------
  int  grant_pct, stall_pct, advance_pct, redirect_pct;
  pc_t want_pc;        // The next instruction decode must get
  int  taken, straight_taken;

  task automatic fail(string what);
    if (errors < 10)
      $display("FAIL fetch_env Depth=%0d ResetPc=%h t=%0t: %s", Depth, ResetPc, $time, what);
    errors++;
  endtask

  // Checks one edge's outcome, just before the edge (everything settled)
  task automatic check_edge();
    if (reset) begin
      want_pc = to_pc(ResetPc);
    end else if (!bus_stall) begin
      if (redirect) begin
        want_pc = redirect_pc;
      end else if (advance && if_id_valid) begin
        if (if_id_pc !== want_pc)
          fail($sformatf("decode got PC %h, want %h", pc_addr(if_id_pc), pc_addr(want_pc)));
        else if (if_id_instr !== halfword(want_pc))
          fail($sformatf("at %h decode got %h, want %h", pc_addr(want_pc), if_id_instr,
                         halfword(want_pc)));
        else if (if_id_pc_plus4 !== want_pc_plus4)
          fail($sformatf("at %h PC + 4 is %h", pc_addr(want_pc), pc_addr(if_id_pc_plus4)));
        else if (if_id_ctrl !== want_ctrl)
          fail($sformatf("at %h (%h) the controls aren't the decoder's", pc_addr(want_pc), if_id_instr));
        else if (if_id_ctrl.b_imm && if_id_imm !== want_imm)
          fail($sformatf("at %h (%h) imm %h, want %h", pc_addr(want_pc), if_id_instr, if_id_imm, want_imm));
        else if (if_id_ctrl.branch && if_id_target !== want_target)
          fail($sformatf("at %h (%h) target %h, want %h", pc_addr(want_pc), if_id_instr,
                         pc_addr(if_id_target), pc_addr(want_target)));
        want_pc = want_pc + pc_t'(1);
        taken++;
      end
    end
  endtask

  initial begin
    errors = 0; done = 0; taken = 0;
    for (int i = 0; i < MemWords; i++) mem[i] = $urandom;
    // A few halts, BLs, branches in there, so the decoders see each kind
    for (int i = 0; i < MemWords; i += 7) mem[i][15:0] = 16'hDE00;
    for (int i = 3; i < MemWords; i += 11) mem[i][31:16] = 16'hF000 | 16'($urandom % 2048);

    reset = 1; bus_stall = 0; redirect = 0; redirect_pc = '0; advance = 0; bus_free = 0;
    want_pc = to_pc(ResetPc);
    repeat (2) @(posedge clk);
    #1 reset = 0;

    // Random: each phase leans a different way (how busy the bus is, how much decode
    // stalls, how often it's sent elsewhere)
    for (int c = 0; c < Cycles; c++) begin
      if (c % 5000 == 0) begin
        grant_pct    = 20 + $urandom % 81;
        stall_pct    = $urandom % 30;
        advance_pct  = 20 + $urandom % 81;
        redirect_pct = $urandom % 15;
      end
      // Stall's cycle ends: anything decode and execute drive holds while the bus waits
      if (!bus_stall) begin
        advance  = ($urandom % 100) < advance_pct;
        redirect = ($urandom % 100) < redirect_pct;
        // Anywhere in the program, or (a quarter of the time) near the top of Code, so
        // fetch runs off its end (wrapping, like the memory model)
        if (redirect) redirect_pc = pc_t'($urandom % (MemWords * 2)) +
                                    (($urandom % 4 == 0) ? pc_t'(28'hFFF_FFF8) : pc_t'(0));
      end
      bus_stall   = ($urandom % 100) < stall_pct;
      bus_free    = ($urandom % 100) < grant_pct;
      reset       = ($urandom % 20000) == 0;
      #4;
      check_edge();
      @(posedge clk); #1;
    end

    // Nothing in the way: after the buffer fills, decode gets one every cycle
    redirect = 1; redirect_pc = to_pc(32'h0000_0100); bus_stall = 0; reset = 0; advance = 1;
    bus_free = 1;
    #4 check_edge(); @(posedge clk); #1;
    redirect = 0;
    repeat (20) begin #4 check_edge(); @(posedge clk); #1; end
    straight_taken = taken;
    repeat (Straight) begin #4 check_edge(); @(posedge clk); #1; end
    straight_taken = taken - straight_taken;
    if (straight_taken != Straight)
      fail($sformatf("with nothing in the way decode got %0d instructions in %0d cycles",
                     straight_taken, Straight));
    $display("fetch_env Depth=%0d ResetPc=%h: %0d instructions checked, %0d in %0d straight cycles",
             Depth, ResetPc, taken, straight_taken, Straight);
    done = 1;
  end

endmodule
