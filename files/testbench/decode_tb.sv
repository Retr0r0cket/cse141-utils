`timescale 1ns / 1ps
`default_nettype none

// Checks decoding, in three parts:
//
//   1. The control and immediate decoders (rtl/fetch/), on every one of the 65536
//      halfwords, against a reference decode written from the ARMv6-M encoding tables:
//      which registers each instruction reads and writes, what the ALU does, the flags,
//      memory, branches, the immediate and a B's target. Anything out of scope must do
//      nothing at all.
//   2. The BL decoder (rtl/decode/branch_link_decoder.sv), on random BLs from random addresses,
//      against the ARM formula for the offset (I1 = NOT(J1 EOR S), ...) and the link.
//   3. The decode stage (rtl/decode/decode_stage.sv) as a whole, against a model of
//      ID/EX: random instructions (BL halves among them), random register values,
//      forwarding selects, hazard stalls, flushes, holds and bus waits. Every operand:
//      PC reads as + 4, Align(PC, 4) for ADR and LDR literal, BLX's and BL's links, a
//      store's data; every target: BX, MOV PC, ADD PC, BL; BL's halves joined, with a
//      bubble for the first.
//
//   just test decode_tb

module decode_tb;
  import cpu_pkg::*;

  int errors = 0, checks = 0;

  task automatic fail(string what);
    if (errors < 20) $display("FAIL %s", what);
    errors++;
  endtask

  // ========================================================================================
  // 1. The decoders, against ARM's encoding tables
  // ========================================================================================

  logic [15:0]     instr;
  pc_t             pc;
  ctrl_t           ctrl;
  imm_sel_e        imm_sel;
  logic [ImmW-1:0] imm;
  pc_t             target, pc_plus4;

  control_decoder   controls   (.instr_i(instr), .ctrl_o(ctrl), .imm_sel_o(imm_sel));
  immediate_decoder immediates (.instr_i(instr), .pc_i(pc), .imm_sel_i(imm_sel),
                                .imm_o(imm), .branch_target_o(target), .pc_plus4_o(pc_plus4));

  // What an instruction must decode to. `kind` names it, for messages.
  task automatic reference(input logic [15:0] i, output ctrl_t c, output logic [31:0] value,
                           output string kind);
    logic [3:0] lo0, lo3, lo6, lo8, hdn, hm;
    lo0 = {1'b0, i[2:0]};
    lo3 = {1'b0, i[5:3]};
    lo6 = {1'b0, i[8:6]};
    lo8 = {1'b0, i[10:8]};
    hdn = {i[7], i[2:0]};
    hm  = i[6:3];
    c = '0;
    c.a_sel = ASelReg;
    c.cond = CondAl;
    c.mem_size = SizeWord;
    value = 0;
    kind = "nothing";

    if (i == 16'hDE00) begin
      kind = "halt (UDF #0)";
      c.halt = 1;

    // Shift (immediate), add, subtract, move, compare: 00xxxx
    end else if (i[15:14] == 2'b00) begin
      if (i[13] == 0 && i[12:11] != 2'b11) begin
        kind = "shift #imm5";
        case (i[12:11])
          2'b00:   c.alu = fn_lsl();
          2'b01:   c.alu = fn_shr(0);
          default: c.alu = fn_shr(1);
        endcase
        c.rs1 = lo3; c.rs1_read = 1;
        c.b_imm = 1;
        value = (i[12:11] != 2'b00 && i[10:6] == 0) ? 32 : 32'(i[10:6]);  // LSR/ASR #0 is 32
        c.set_flags = 1; c.reg_write = 1; c.rd = lo0;
      end else if (i[13] == 0) begin
        kind = i[10] ? "ADDS/SUBS #imm3" : "ADDS/SUBS reg";
        c.alu = i[9] ? fn_sub() : fn_add(0, CinZero);
        c.rs1 = lo3; c.rs1_read = 1;
        if (i[10]) begin c.b_imm = 1; value = 32'(i[8:6]); end
        else       begin c.rs2 = lo6; c.rs2_read = 1; end
        c.set_flags = 1; c.reg_write = 1; c.rd = lo0;
      end else begin
        kind = "imm8 op";
        c.b_imm = 1; value = 32'(i[7:0]); c.set_flags = 1;
        case (i[12:11])
          2'b00: begin c.alu = fn_mov(); c.a_sel = ASelZero; c.reg_write = 1; c.rd = lo8; end
          2'b01: begin c.alu = fn_sub(); c.rs1 = lo8; c.rs1_read = 1; end
          2'b10: begin c.alu = fn_add(0, CinZero); c.rs1 = lo8; c.rs1_read = 1; c.reg_write = 1; c.rd = lo8; end
          default: begin c.alu = fn_sub(); c.rs1 = lo8; c.rs1_read = 1; c.reg_write = 1; c.rd = lo8; end
        endcase
      end

    // Data processing: 010000
    end else if (i[15:10] == 6'b010000) begin
      kind = "data processing";
      if (i[9:6] != 4'b1101) begin  // (1101 is MUL: out of scope)
        c.set_flags = 1;
        c.rs1 = lo0; c.rs1_read = 1;
        c.rs2 = lo3; c.rs2_read = 1;
        c.reg_write = 1; c.rd = lo0;
      end
      case (i[9:6])
        4'b0000: c.alu = fn_logic(LogicAnd, 0);
        4'b0001: c.alu = fn_logic(LogicEor, 0);
        4'b0010: begin c.alu = fn_lsl();  c.rs2_in_decode = 1; end
        4'b0011: begin c.alu = fn_shr(0); c.rs2_in_decode = 1; end
        4'b0100: begin c.alu = fn_shr(1); c.rs2_in_decode = 1; end
        4'b0101: c.alu = fn_add(0, CinCarry);
        4'b0110: c.alu = fn_add(1, CinCarry);
        4'b0111: begin c.alu = fn_ror();  c.rs2_in_decode = 1; end
        4'b1000: begin c.alu = fn_logic(LogicAnd, 0); c.reg_write = 0; end           // TST
        4'b1001: begin c.alu = fn_sub(); c.a_sel = ASelZero; c.rs1_read = 0; end      // RSBS #0
        4'b1010: begin c.alu = fn_sub(); c.reg_write = 0; end                        // CMP
        4'b1011: begin c.alu = fn_add(0, CinZero); c.reg_write = 0; end              // CMN
        4'b1100: c.alu = fn_logic(LogicOrr, 0);
        4'b1101: kind = "MUL (out of scope)";
        4'b1110: c.alu = fn_logic(LogicAnd, 1);                                      // BIC
        default: begin c.alu = fn_mvn(); c.a_sel = ASelZero; c.rs1_read = 0; end     // MVN
      endcase

    // Special data processing, branch and exchange: 010001
    end else if (i[15:10] == 6'b010001) begin
      c.rs2 = hm; c.rs2_read = 1;
      case (i[9:8])
        2'b00: if (hdn == 15) begin
                 kind = "ADD PC, Rm";
                 c.branch = 1; c.branch_add = 1; c.rs2_in_decode = 1;
               end else begin
                 kind = "ADD Rdn, Rm";
                 c.alu = fn_add(0, CinZero); c.rs1 = hdn; c.rs1_read = 1; c.reg_write = 1; c.rd = hdn;
               end
        2'b01: begin
                 kind = "CMP Rn, Rm (high)";
                 c.alu = fn_sub(); c.rs1 = hdn; c.rs1_read = 1; c.set_flags = 1;
               end
        2'b10: if (hdn == 15) begin
                 kind = "MOV PC, Rm";
                 c.branch = 1; c.branch_reg = 1; c.rs2_in_decode = 1;
               end else begin
                 kind = "MOV Rd, Rm";
                 c.alu = fn_mov(); c.a_sel = ASelZero; c.reg_write = 1; c.rd = hdn;
               end
        default: begin
                 kind = i[7] ? "BLX Rm" : "BX Rm";
                 c.branch = 1; c.branch_reg = 1; c.rs2_in_decode = 1;
                 if (i[7]) begin
                   c.alu = fn_mov(); c.a_sel = ASelZero; c.b_link = 1; c.reg_write = 1; c.rd = 14;
                 end
               end
      endcase

    // LDR Rt, [PC, #imm8]: 01001
    end else if (i[15:11] == 5'b01001) begin
      kind = "LDR literal";
      c.alu = fn_add(0, CinZero); c.a_sel = ASelPc; c.b_imm = 1; value = {i[7:0], 2'b00};
      c.mem_read = 1; c.reg_write = 1; c.rd = lo8;

    // Load/store, register offset: 0101
    end else if (i[15:12] == 4'b0101) begin
      kind = "load/store [Rn, Rm]";
      c.alu = fn_add(0, CinZero);
      c.rs1 = lo3; c.rs1_read = 1;
      c.rs2 = lo6; c.rs2_read = 1;
      c.rd = lo0;
      case (i[11:9])
        3'b000: begin c.mem_write = 1; c.mem_size = SizeWord; end
        3'b001: begin c.mem_write = 1; c.mem_size = SizeHalf; end
        3'b010: begin c.mem_write = 1; c.mem_size = SizeByte; end
        3'b011: begin c.mem_read = 1;  c.mem_size = SizeByte; c.mem_signed = 1; end
        3'b100: begin c.mem_read = 1;  c.mem_size = SizeWord; end
        3'b101: begin c.mem_read = 1;  c.mem_size = SizeHalf; end
        3'b110: begin c.mem_read = 1;  c.mem_size = SizeByte; end
        default: begin c.mem_read = 1; c.mem_size = SizeHalf; c.mem_signed = 1; end
      endcase
      c.rd_read = c.mem_write; c.reg_write = c.mem_read;

    // Load/store, immediate offset: 011 (word, byte), 1000 (halfword)
    end else if (i[15:13] == 3'b011 || i[15:12] == 4'b1000) begin
      kind = "load/store [Rn, #imm5]";
      c.alu = fn_add(0, CinZero);
      c.rs1 = lo3; c.rs1_read = 1;
      c.b_imm = 1;
      if (i[15:12] == 4'b1000)  begin c.mem_size = SizeHalf; value = 32'(i[10:6]) * 2; end
      else if (i[12])           begin c.mem_size = SizeByte; value = 32'(i[10:6]); end
      else                      begin c.mem_size = SizeWord; value = 32'(i[10:6]) * 4; end
      c.rd = lo0;
      if (i[11]) begin c.mem_read = 1; c.reg_write = 1; end
      else       begin c.mem_write = 1; c.rd_read = 1; end

    // ADR: 10100
    end else if (i[15:11] == 5'b10100) begin
      kind = "ADR";
      c.alu = fn_add(0, CinZero); c.a_sel = ASelPc; c.b_imm = 1; value = {i[7:0], 2'b00};
      c.reg_write = 1; c.rd = lo8;

    // Extend: 1011 0010 xx
    end else if (i[15:8] == 8'b10110010) begin
      kind = "extend";
      c.alu = fn_ext(i[7:6] == 2'b00 || i[7:6] == 2'b01, i[7:6] == 2'b00 || i[7:6] == 2'b10);
      c.rs2 = lo3; c.rs2_read = 1; c.reg_write = 1; c.rd = lo0;

    // Reverse: 1011 1010 xx (10 is undefined)
    end else if (i[15:8] == 8'b10111010 && i[7:6] != 2'b10) begin
      kind = "reverse";
      c.alu = fn_rev(i[7:6] != 2'b00, i[7:6] == 2'b11);
      c.rs2 = lo3; c.rs2_read = 1; c.reg_write = 1; c.rd = lo0;

    // B<cond>: 1101 (cond 1110 is UDF, 1111 SVC)
    end else if (i[15:12] == 4'b1101 && i[11:8] < 4'b1110) begin
      kind = "B<cond>";
      c.branch = 1; c.cond = cond_e'(i[11:8]);

    // B: 11100
    end else if (i[15:11] == 5'b11100) begin
      kind = "B";
      c.branch = 1;
    end
  endtask

  // A B or B<cond>'s target, from ARM's formula: PC + 4 + SignExtend(imm:'0')
  function automatic logic [31:0] b_target(logic [15:0] i, logic [31:0] address);
    if (i[15:11] == 5'b11100) return address + 4 + {{20{i[10]}}, i[10:0], 1'b0};
    else                      return address + 4 + {{23{i[7]}}, i[7:0], 1'b0};
  endfunction

  // Whether the ALU's controls matter for it: it writes a register, sets the flags, or
  // makes an address
  function automatic logic uses_alu(ctrl_t c);
    return c.reg_write || c.set_flags || c.mem_read || c.mem_write;
  endfunction

  task automatic check_decoders();
    ctrl_t       want;
    logic [31:0] want_imm;
    string       kind, where;
    logic        bad;
    for (int n = 0; n < 65536; n++) begin
      instr = 16'(n);
      pc    = pc_t'({$urandom} % (1 << PcTop));
      #1;
      reference(instr, want, want_imm, kind);
      where = $sformatf("decoder: %h (%s) at %h", instr, kind, pc_addr(pc));
      bad = 0;
      // What it reads and writes, exactly: an extra read stalls for nothing
      if (ctrl.rs1_read !== want.rs1_read || ctrl.rs2_read !== want.rs2_read ||
          ctrl.rd_read !== want.rd_read || ctrl.rs2_in_decode !== want.rs2_in_decode ||
          (want.rs1_read && ctrl.rs1 !== want.rs1) || (want.rs2_read && ctrl.rs2 !== want.rs2)) begin
        fail($sformatf("%s: reads rs1 %0d(%b) rs2 %0d(%b) rd %0d(%b) in-decode %b, want %0d(%b) %0d(%b) %0d(%b) %b",
                       where, ctrl.rs1, ctrl.rs1_read, ctrl.rs2, ctrl.rs2_read, ctrl.rd, ctrl.rd_read,
                       ctrl.rs2_in_decode, want.rs1, want.rs1_read, want.rs2, want.rs2_read, want.rd,
                       want.rd_read, want.rs2_in_decode));
        bad = 1;
      end
      if (ctrl.reg_write !== want.reg_write || ctrl.set_flags !== want.set_flags ||
          ((want.reg_write || want.rd_read) && ctrl.rd !== want.rd)) begin
        fail($sformatf("%s: writes r%0d(%b) flags %b, want r%0d(%b) flags %b", where,
                       ctrl.rd, ctrl.reg_write, ctrl.set_flags, want.rd, want.reg_write, want.set_flags));
        bad = 1;
      end
      if (uses_alu(want) && (ctrl.alu !== want.alu || ctrl.a_sel !== want.a_sel ||
                             ctrl.b_imm !== want.b_imm || ctrl.b_link !== want.b_link)) begin
        fail($sformatf("%s: ALU %h a_sel %0d b_imm %b b_link %b, want %h %0d %b %b", where,
                       ctrl.alu, ctrl.a_sel, ctrl.b_imm, ctrl.b_link,
                       want.alu, want.a_sel, want.b_imm, want.b_link));
        bad = 1;
      end
      if (uses_alu(want) && want.b_imm && 32'(imm) !== want_imm) begin
        fail($sformatf("%s: immediate %0d, want %0d", where, imm, want_imm));
        bad = 1;
      end
      if (ctrl.mem_read !== want.mem_read || ctrl.mem_write !== want.mem_write ||
          ((want.mem_read || want.mem_write) &&
           (ctrl.mem_size !== want.mem_size || ctrl.mem_signed !== want.mem_signed))) begin
        fail($sformatf("%s: memory read %b write %b size %0d signed %b, want %b %b %0d %b", where,
                       ctrl.mem_read, ctrl.mem_write, ctrl.mem_size, ctrl.mem_signed,
                       want.mem_read, want.mem_write, want.mem_size, want.mem_signed));
        bad = 1;
      end
      if (ctrl.branch !== want.branch || ctrl.branch_reg !== want.branch_reg ||
          ctrl.branch_add !== want.branch_add || (want.branch && ctrl.cond !== want.cond)) begin
        fail($sformatf("%s: branch %b reg %b add %b cond %h, want %b %b %b %h", where,
                       ctrl.branch, ctrl.branch_reg, ctrl.branch_add, ctrl.cond,
                       want.branch, want.branch_reg, want.branch_add, want.cond));
        bad = 1;
      end
      if (want.branch && !want.branch_reg && !want.branch_add &&
          pc_addr(target) !== (b_target(instr, pc_addr(pc)) & 32'h1FFF_FFFF)) begin
        fail($sformatf("%s: target %h, want %h", where, pc_addr(target), b_target(instr, pc_addr(pc))));
        bad = 1;
      end
      if (pc_addr(pc_plus4) !== ((pc_addr(pc) + 4) & 32'h1FFF_FFFF)) begin
        fail($sformatf("%s: PC + 4 is %h", where, pc_addr(pc_plus4)));
        bad = 1;
      end
      if (ctrl.halt !== want.halt) begin
        fail($sformatf("%s: halt %b, want %b", where, ctrl.halt, want.halt));
        bad = 1;
      end
      checks++;
    end
  endtask

  // ========================================================================================
  // 2. The BL decoder, against ARM's formula
  // ========================================================================================

  logic [15:0] bl_second;
  logic [10:0] bl_first;
  pc_t         bl_at;
  ctrl_t       bl_ctrl;
  logic [31:0] bl_link;
  pc_t         bl_target;

  branch_link_decoder bl (.second_half_i(bl_second), .first_half_i(bl_first), .bl_pc_i(bl_at),
                 .ctrl_o(bl_ctrl), .link_addr_o(bl_link), .target_o(bl_target));

  function automatic logic [31:0] bl_offset(logic [10:0] first, logic [15:0] second);
    logic s, j1, j2, i1, i2;
    s = first[10]; j1 = second[13]; j2 = second[11];
    i1 = !(j1 ^ s); i2 = !(j2 ^ s);
    return {{7{s}}, s, i1, i2, first[9:0], second[10:0], 1'b0};  // SignExtend(S:I1:I2:imm10:imm11:'0')
  endfunction

  task automatic check_bl();
    ctrl_t       want;
    logic [31:0] address;
    want = '0;
    want.branch = 1; want.cond = CondAl; want.alu = fn_mov(); want.a_sel = ASelZero;
    want.b_link = 1; want.mem_size = SizeWord; want.reg_write = 1; want.rd = 14;
    for (int n = 0; n < 200000; n++) begin
      bl_first  = 11'($urandom);
      bl_second = {2'b11, 1'($urandom), 1'b1, 1'($urandom), 11'($urandom)};
      if (n < 4) begin  // The extremes: furthest forward and back, from the ends of Code
        bl_first = (n % 2) ? 11'h400 : 11'h3FF;
        bl_second = (n % 2) ? 16'hD000 : 16'hFFFF;  // S=1, J=0 (most negative); S=0, J=1 (most positive)
      end
      address = (n < 4) ? ((n / 2) ? 32'h1FFF_FFFC : 32'h0) : {3'b000, 28'($urandom), 1'b0};
      bl_at = to_pc(address);
      #1;
      checks++;
      if (bl_target !== to_pc(address + 4 + bl_offset(bl_first, bl_second)))
        fail($sformatf("BL %h %h at %h: target %h, want %h", {5'b11110, bl_first}, bl_second, address,
                       pc_addr(bl_target), (address + 4 + bl_offset(bl_first, bl_second)) & 32'h1FFF_FFFE));
      if (bl_link !== (((address + 4) & 32'h1FFF_FFFF) | 1))
        fail($sformatf("BL at %h: link %h, want %h", address, bl_link, (address + 4) | 1));
      if (bl_ctrl !== want)
        fail($sformatf("BL: controls %h, want %h", bl_ctrl, want));
    end
  endtask

  // ========================================================================================
  // 3. The decode stage, against a model of ID/EX
  // ========================================================================================

  logic clk = 0, reset, bus_stall;
  always #5 clk = !clk;

  logic        if_id_valid;
  logic [15:0] if_id_instr;
  pc_t         if_id_pc;
  ctrl_t       if_id_ctrl;
  imm_sel_e    if_id_imm_sel;
  logic [ImmW-1:0] if_id_imm;
  pc_t         if_id_target, if_id_pc_plus4;
  logic        advance, flush, hold, hazard_stall;
  logic        id_valid;
  ctrl_t       id_ctrl;
  logic        id_fwd_a, id_fwd_b, id_fwd_store, ex_fwd_a, ex_fwd_b, ex_fwd_store;
  logic [31:0] ex_mem_result;
  logic [3:0]  raddr1, raddr2, raddr3;
  logic [31:0] rdata1, rdata2, rdata3;
  logic        id_ex_valid, id_ex_fwd_a, id_ex_fwd_b, id_ex_fwd_store;
  pc_t         id_ex_pc, id_ex_target;
  ctrl_t       id_ex_ctrl;
  shift_factors_t id_ex_shift;
  logic [31:0] id_ex_a, id_ex_b, id_ex_store_data;

  // IF/ID, as fetch makes it: decoded by the same decoders
  control_decoder   stage_controls   (.instr_i(if_id_instr), .ctrl_o(if_id_ctrl), .imm_sel_o(if_id_imm_sel));
  immediate_decoder stage_immediates (.instr_i(if_id_instr), .pc_i(if_id_pc), .imm_sel_i(if_id_imm_sel),
                                      .imm_o(if_id_imm), .branch_target_o(if_id_target), .pc_plus4_o(if_id_pc_plus4));

  // The register file: r0-r14 hold random values (r15 is never read from it)
  logic [31:0] regs [16];
  assign rdata1 = regs[raddr1];
  assign rdata2 = regs[raddr2];
  assign rdata3 = regs[raddr3];

  decode_stage dut (
    .clk_i              (clk), .reset_i(reset), .bus_stall_i(bus_stall),
    .if_id_valid_i      (if_id_valid), .if_id_instr_i(if_id_instr), .if_id_pc_i(if_id_pc),
    .if_id_pc_plus4_i   (if_id_pc_plus4), .if_id_ctrl_i(if_id_ctrl), .if_id_imm_i(if_id_imm),
    .if_id_target_i     (if_id_target), .advance_o(advance), .flush_i(flush), .hold_i(hold),
    .id_valid_o         (id_valid), .id_ctrl_o(id_ctrl), .hazard_stall_i(hazard_stall),
    .id_fwd_a_i         (id_fwd_a), .id_fwd_b_i(id_fwd_b), .id_fwd_store_i(id_fwd_store),
    .ex_fwd_a_i         (ex_fwd_a), .ex_fwd_b_i(ex_fwd_b), .ex_fwd_store_i(ex_fwd_store),
    .ex_mem_result_i    (ex_mem_result),
    .read_addr1_o       (raddr1), .read_addr2_o(raddr2), .read_addr3_o(raddr3),
    .read_data1_i       (rdata1), .read_data2_i(rdata2), .read_data3_i(rdata3),
    .id_ex_valid_o      (id_ex_valid), .id_ex_pc_o(id_ex_pc), .id_ex_ctrl_o(id_ex_ctrl),
    .id_ex_shift_o      (id_ex_shift), .id_ex_a_o(id_ex_a), .id_ex_b_o(id_ex_b),
    .id_ex_store_data_o (id_ex_store_data), .id_ex_fwd_a_o(id_ex_fwd_a), .id_ex_fwd_b_o(id_ex_fwd_b),
    .id_ex_fwd_store_o  (id_ex_fwd_store), .id_ex_target_o(id_ex_target)
  );

  // A random instruction, leaning towards the ones decode does something special for
  function automatic logic [15:0] random_instr();
    int k;
    k = $urandom % 100;
    if (k < 12) return {5'b11110, 11'($urandom)};                                  // BL, first half
    if (k < 24) return {2'b11, 1'($urandom), 1'b1, 1'($urandom), 11'($urandom)};   // BL, second half
    if (k < 30) return {9'b010001110, 4'($urandom), 3'b000};                       // BX, BLX
    if (k < 34) return {8'b01000100, 1'b1, 4'($urandom), 3'b111};                  // ADD PC, Rm
    if (k < 38) return {8'b01000110, 1'b1, 4'($urandom), 3'b111};                  // MOV PC, Rm
    if (k < 44) return {5'b10100, 11'($urandom)};                                  // ADR
    if (k < 50) return {5'b01001, 11'($urandom)};                                  // LDR literal
    if (k < 58) return {3'b011, 1'($urandom), 1'b0, 11'($urandom)};                // STR, STRB #imm
    if (k < 62) return {7'b0101000, 9'($urandom)};                                 // STR [Rn, Rm]
    if (k < 66) return {8'b01000100, 8'($urandom)};                                // ADD hi (PC reads)
    return 16'($urandom);
  endfunction

  // ID/EX, as the model has it
  logic        m_valid, m_fwd_a, m_fwd_b, m_fwd_store;
  pc_t         m_pc, m_target;
  ctrl_t       m_ctrl;
  logic [31:0] m_a, m_b, m_store;
  logic        m_branch_target;   // Whether m_target counts (a branch)
  logic        m_pending;         // A BL's first half is waiting
  logic [10:0] m_first;
  logic [31:0] m_first_at;

  task automatic model_edge();
    logic        go, first, second;
    logic [31:0] address, pc4, v1, v2;
    ctrl_t       c;
    if (reset) begin
      m_valid = 0; m_ctrl = '0; m_fwd_a = 0; m_fwd_b = 0; m_fwd_store = 0; m_pending = 0;
      m_branch_target = 0;
      return;
    end
    if (bus_stall) return;
    go      = if_id_valid && !flush && !hazard_stall && !hold;
    first   = !m_pending && if_id_instr[15:11] == 5'b11110;
    second  = m_pending && if_id_instr[15:14] == 2'b11 && if_id_instr[12];
    address = pc_addr(if_id_pc);
    pc4     = (address + 4) & 32'h1FFF_FFFF;
    v1 = id_fwd_a ? ex_mem_result : (if_id_ctrl.rs1 == 15) ? pc4 : regs[if_id_ctrl.rs1];
    v2 = id_fwd_b ? ex_mem_result : (if_id_ctrl.rs2 == 15) ? pc4 : regs[if_id_ctrl.rs2];

    if (flush || (!hold && (!go || first))) begin
      m_valid = 0; m_ctrl = '0; m_fwd_a = 0; m_fwd_b = 0; m_fwd_store = 0; m_branch_target = 0;
    end else if (!hold) begin
      if (second) begin
        c = '0;
        c.branch = 1; c.cond = CondAl; c.alu = fn_mov(); c.a_sel = ASelZero; c.b_link = 1;
        c.mem_size = SizeWord; c.reg_write = 1; c.rd = 14;
      end else c = if_id_ctrl;
      m_valid = 1;
      m_ctrl  = c;
      m_pc    = second ? to_pc(m_first_at) : if_id_pc;
      case (c.a_sel)
        ASelPc:   m_a = pc4 & ~32'h3;   // Align(PC, 4)
        ASelZero: m_a = 0;
        default:  m_a = v1;
      endcase
      if (second)      m_b = ((m_first_at + 4) & 32'h1FFF_FFFF) | 1;   // BL's link
      else if (c.b_link) m_b = ((address + 2) & 32'h1FFF_FFFF) | 1;    // BLX's
      else if (c.b_imm)  m_b = 32'(if_id_imm);
      else               m_b = v2;
      m_store = id_fwd_store ? ex_mem_result : regs[if_id_ctrl.rd];
      m_fwd_a = ex_fwd_a; m_fwd_b = ex_fwd_b; m_fwd_store = ex_fwd_store;
      m_branch_target = c.branch;
      if (second)            m_target = to_pc(m_first_at + 4 + bl_offset(m_first, if_id_instr));
      else if (c.branch_reg) m_target = to_pc(v2);
      else if (c.branch_add) m_target = to_pc(pc4 + v2);
      else                   m_target = to_pc(b_target(if_id_instr, address));
    end

    if (flush) m_pending = 0;
    else if (go && first) begin m_pending = 1; m_first = if_id_instr[10:0]; m_first_at = address; end
    else if (go) m_pending = 0;
  endtask

  task automatic check_stage();
    string where;
    if (id_ex_valid !== m_valid) begin
      fail($sformatf("decode t=%0t: ID/EX valid %b, want %b", $time, id_ex_valid, m_valid));
      return;
    end
    if (id_ex_ctrl !== m_ctrl || id_ex_fwd_a !== m_fwd_a || id_ex_fwd_b !== m_fwd_b ||
        id_ex_fwd_store !== m_fwd_store)
      fail($sformatf("decode t=%0t: ID/EX controls %h fwd %b%b%b, want %h %b%b%b", $time, id_ex_ctrl,
                     id_ex_fwd_a, id_ex_fwd_b, id_ex_fwd_store, m_ctrl, m_fwd_a, m_fwd_b, m_fwd_store));
    if (!m_valid) return;
    where = $sformatf("decode t=%0t at %h", $time, pc_addr(m_pc));
    checks++;
    if (id_ex_pc !== m_pc)       fail($sformatf("%s: PC %h", where, pc_addr(id_ex_pc)));
    if (id_ex_a !== m_a)         fail($sformatf("%s: A %h, want %h", where, id_ex_a, m_a));
    if (id_ex_b !== m_b)         fail($sformatf("%s: B %h, want %h", where, id_ex_b, m_b));
    if (id_ex_store_data !== m_store)
      fail($sformatf("%s: store data %h, want %h", where, id_ex_store_data, m_store));
    if (m_branch_target && id_ex_target !== m_target)
      fail($sformatf("%s: target %h, want %h", where, pc_addr(id_ex_target), pc_addr(m_target)));
  endtask

  task automatic check_decode_stage();
    logic took;
    reset = 1; bus_stall = 0; if_id_valid = 0; if_id_instr = 0; if_id_pc = '0;
    flush = 0; hold = 0; hazard_stall = 0;
    {id_fwd_a, id_fwd_b, id_fwd_store, ex_fwd_a, ex_fwd_b, ex_fwd_store} = '0;
    ex_mem_result = 0;
    for (int r = 0; r < 16; r++) regs[r] = $urandom;
    @(negedge clk);
    model_edge();
    @(posedge clk); #1;
    reset = 0;
    took = 1;
    for (int n = 0; n < 300000; n++) begin
      // While the bus waits, everything around decode holds
      if (!bus_stall) begin
        // Fetch moves IF/ID on when decode took it (or it was empty, or flushed)
        if (took || !if_id_valid || flush) begin
          if_id_valid = ($urandom % 100) < 85;
          if_id_instr = random_instr();
          // The next halfword (usually), or anywhere, near the top of Code at times
          if (($urandom % 4) == 0) if_id_pc = pc_t'({$urandom} % (1 << PcTop));
          else                     if_id_pc = if_id_pc + pc_t'(1);
        end
        flush        = ($urandom % 100) < 8;
        hold         = ($urandom % 100) < 5;
        hazard_stall = ($urandom % 100) < 15;
        {id_fwd_a, id_fwd_b, id_fwd_store} = 3'($urandom);
        {ex_fwd_a, ex_fwd_b, ex_fwd_store} = 3'($urandom);
        ex_mem_result = $urandom;
        if (($urandom % 50) == 0) for (int r = 0; r < 16; r++) regs[r] = $urandom;
      end
      bus_stall = ($urandom % 100) < 10;
      reset     = ($urandom % 50000) == 0;
      #3;
      took = !bus_stall && advance;
      if (advance !== (if_id_valid && !flush && !hazard_stall && !hold))
        fail($sformatf("decode t=%0t: advance %b", $time, advance));
      model_edge();
      @(posedge clk); #1;
      check_stage();
    end
  endtask

  initial begin
    check_decoders();
    $display("decoders: every halfword checked (%0d errors so far)", errors);
    check_bl();
    $display("BL decoder: checked (%0d errors so far)", errors);
    check_decode_stage();
    if (errors == 0) $display("PASSED: %0d checks", checks);
    else             $display("FAILED: %0d errors in %0d checks", errors, checks);
    $finish;
  end

endmodule
