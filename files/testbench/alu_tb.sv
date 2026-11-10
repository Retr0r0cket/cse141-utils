`timescale 1ns / 1ps
`default_nettype none

// Checks the ALU (rtl/execute/arithmetic_logic_unit.sv) against a model written from ARM's own definitions
// (AddWithCarry, Shift_C), for every function and its variations: every pair of edge
// values, then random pairs, with the carry in both ways and, for shifts, every amount from
// 0 to 255. The shifts' factors come from decode's shift_factor_generator, as in the core.
//
//   just test alu_tb

module alu_tb;
  import cpu_pkg::*;

  // ---- the ALU, with its controls ------------------------------------------------------
  alu_fn_t        fn;
  shift_factors_t shift;
  logic [31:0]    a, b;
  logic           carry_flag;
  logic [31:0]    fast_result, shift_result, result;
  logic           carry, carry_we, overflow, overflow_we;

  arithmetic_logic_unit dut (
    .alu_fn_i      (fn), .shift_factors_i(shift), .operand_a_i(a), .shift_operand_a_i(a), .operand_b_i(b), .carry_flag_i(carry_flag),
    .fast_result_o (fast_result), .shift_result_o(shift_result),
    .carry_o       (carry), .carry_we_o(carry_we), .overflow_o(overflow), .overflow_we_o(overflow_we)
  );
  assign result = fast_result | shift_result;

  shift_factor_generator factors (
    .is_ror_i  (fn.sel_shr_lo), .is_arithmetic_i(fn.alu_signed), .amount_i(b[7:0]), .a_sign_i(a[31]),
    .factors_o (shift)
  );

  // ---- the model -----------------------------------------------------------------------
  typedef enum {ADD, SUB, ADC, SBC, AND, BIC, ORR, EOR, MOV, MVN,
                LSL, LSR, ASR, ROR, SXTH, SXTB, UXTH, UXTB, REV, REV16, REVSH} op_e;

  function automatic alu_fn_t fn_of(op_e op);
    case (op)
      ADD:   return fn_add(1'b0, CinZero);
      SUB:   return fn_sub();
      ADC:   return fn_add(1'b0, CinCarry);
      SBC:   return fn_add(1'b1, CinCarry);
      AND:   return fn_logic(LogicAnd, 1'b0);
      BIC:   return fn_logic(LogicAnd, 1'b1);
      ORR:   return fn_logic(LogicOrr, 1'b0);
      EOR:   return fn_logic(LogicEor, 1'b0);
      MOV:   return fn_mov();
      MVN:   return fn_mvn();
      LSL:   return fn_lsl();
      LSR:   return fn_shr(1'b0);
      ASR:   return fn_shr(1'b1);
      ROR:   return fn_ror();
      SXTH:  return fn_ext(1'b1, 1'b1);
      SXTB:  return fn_ext(1'b1, 1'b0);
      UXTH:  return fn_ext(1'b0, 1'b1);
      UXTB:  return fn_ext(1'b0, 1'b0);
      REV:   return fn_rev(1'b0, 1'b0);
      REV16: return fn_rev(1'b1, 1'b0);
      default: return fn_rev(1'b1, 1'b1);  // REVSH
    endcase
  endfunction

  // AddWithCarry: {carry, overflow, result}
  function automatic logic [33:0] add_with_carry(logic [31:0] x, logic [31:0] y, logic cin);
    logic [32:0] u;
    logic [31:0] r;
    logic        v;
    u = {1'b0, x} + {1'b0, y} + 33'(cin);
    r = u[31:0];
    v = (x[31] == y[31]) && (r[31] != x[31]);
    return {u[32], v, r};
  endfunction

  // Shift_C: {carry, result}; the amount is 0 to 255 (a register shift's)
  function automatic logic [32:0] shift_c(op_e op, logic [31:0] x, int n, logic cin);
    logic [31:0] r;
    logic        c;
    if (n == 0) return {cin, x};
    case (op)
      LSL: if (n <= 32) begin r = (n == 32) ? 0 : x << n; c = x[32 - n]; end else begin r = 0; c = 0; end
      LSR: if (n <= 32) begin r = (n == 32) ? 0 : x >> n; c = x[n - 1];  end else begin r = 0; c = 0; end
      ASR: if (n < 32)  begin r = $signed(x) >>> n; c = x[n - 1]; end else begin r = {32{x[31]}}; c = x[31]; end
      default: begin  // ROR
        r = (n % 32 == 0) ? x : (x >> (n % 32)) | (x << (32 - n % 32));
        c = r[31];
      end
    endcase
    return {c, r};
  endfunction

  int errors = 0, checks = 0;

  task automatic check(op_e op, logic [31:0] x, logic [31:0] y, logic cin);
    logic [31:0] want;
    logic        want_c, want_cwe, want_v, want_vwe;
    logic [33:0] awc;
    logic [32:0] sc;
    fn = fn_of(op); a = x; b = y; carry_flag = cin;
    #1;
    want_cwe = 0; want_vwe = 0; want_c = 'x; want_v = 'x;
    case (op)
      ADD: begin awc = add_with_carry(x, y, 0);    want = awc[31:0]; want_c = awc[33]; want_v = awc[32]; want_cwe = 1; want_vwe = 1; end
      SUB: begin awc = add_with_carry(x, ~y, 1);   want = awc[31:0]; want_c = awc[33]; want_v = awc[32]; want_cwe = 1; want_vwe = 1; end
      ADC: begin awc = add_with_carry(x, y, cin);  want = awc[31:0]; want_c = awc[33]; want_v = awc[32]; want_cwe = 1; want_vwe = 1; end
      SBC: begin awc = add_with_carry(x, ~y, cin); want = awc[31:0]; want_c = awc[33]; want_v = awc[32]; want_cwe = 1; want_vwe = 1; end
      AND: want = x & y;
      BIC: want = x & ~y;
      ORR: want = x | y;
      EOR: want = x ^ y;
      MOV: begin a = 0; #1; want = y; end            // (a move is ORR with A = 0)
      MVN: begin a = 0; #1; want = ~y; end
      LSL, LSR, ASR, ROR: begin
        sc = shift_c(op, x, int'(y[7:0]), cin);
        want = sc[31:0]; want_c = sc[32]; want_cwe = (y[7:0] != 0);
      end
      SXTH:  want = {{16{y[15]}}, y[15:0]};
      SXTB:  want = {{24{y[7]}}, y[7:0]};
      UXTH:  want = {16'd0, y[15:0]};
      UXTB:  want = {24'd0, y[7:0]};
      REV:   want = {y[7:0], y[15:8], y[23:16], y[31:24]};
      REV16: want = {y[23:16], y[31:24], y[7:0], y[15:8]};
      default: want = {{16{y[7]}}, y[7:0], y[15:8]};  // REVSH
    endcase
    checks++;
    if (result !== want || carry_we !== want_cwe || overflow_we !== want_vwe ||
        (want_cwe && carry !== want_c) || (want_vwe && overflow !== want_v)) begin
      if (errors < 20)
        $display("FAIL %s a=%h b=%h cin=%b: got %h c=%b(we %b) v=%b(we %b), want %h c=%b(we %b) v=%b(we %b)",
                 op.name(), a, y, cin, result, carry, carry_we, overflow, overflow_we,
                 want, want_c, want_cwe, want_v, want_vwe);
      errors++;
    end
  endtask

  logic [31:0] edges [8] = '{32'h0, 32'h1, 32'h7FFF_FFFF, 32'h8000_0000, 32'hFFFF_FFFF,
                              32'h1234_5678, 32'h0000_8000, 32'h0000_0080};

  op_e op;
  initial begin
    for (int o = 0; o <= REVSH; o++) begin
      op = op_e'(o);
      for (int cin = 0; cin < 2; cin++) begin
        if (op == LSL || op == LSR || op == ASR || op == ROR) begin
          // Every amount 0 to 255 (and a high byte that must be ignored), on edge values
          foreach (edges[i])
            for (int n = 0; n < 256; n++) begin
              check(op, edges[i], 32'(n), cin[0]);
              check(op, edges[i], 32'(n) | 32'hABCD_0000, cin[0]);
            end
        end else begin
          foreach (edges[i]) foreach (edges[j]) check(op, edges[i], edges[j], cin[0]);
          for (int k = 0; k < 2000; k++) check(op, $urandom, $urandom, cin[0]);
        end
      end
    end
    if (errors == 0) $display("PASSED: %0d checks", checks);
    else             $display("FAILED: %0d of %0d checks", errors, checks);
    $finish;
  end

endmodule
