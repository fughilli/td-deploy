#ifndef TOX_OPS
#define TOX_OPS

include "mlir/Interfaces/SideEffectInterfaces.td"
include "Tox/ToxDialect.td"
include "Tox/ToxTypes.td"

def Tox_SourceOp : Tox_Op<"source", [Pure]> {
  let summary = "A TOP source (e.g. image/movie in)";
  let results = (outs Tox_TopType:$result);
  let assemblyFormat = "attr-dict `:` type($result)";
}

def Tox_LevelOp : Tox_Op<"level", [Pure]> {
  let summary = "Level TOP (brightness/contrast); identity placeholder";
  let arguments = (ins Tox_TopType:$input);
  let results = (outs Tox_TopType:$result);
  let assemblyFormat = "$input attr-dict `:` type($result)";
}

//===----------------------------------------------------------------------===//
// CHOP (control-rate) ops — the CHOP DAG in the same graph as the TOPs, so the
// compiler can optimize across the CHOP<->TOP (CPU<->GPU) boundary instead of
// leaving CHOPs to the runtime fasteval interpreter. See
// docs/design/fused-chop-top-mlir.md (P0: represent the DAG; no fusion yet).
//===----------------------------------------------------------------------===//

def Tox_ChopSourceOp : Tox_Op<"chop_source"> {
  let summary = "External CHOP source (MIDI/OSC In) — a live control input";
  let description = [{
    A named live control source (e.g. `midiin1`). Not `Pure`: it reads an
    external device each cook, so it must not be CSE'd/DCE'd away.
  }];
  let arguments = (ins StrAttr:$name);
  let results = (outs Tox_ChopType:$result);
  let assemblyFormat = "$name attr-dict `:` type($result)";
}

def Tox_ChopConstantOp : Tox_Op<"chop_constant", [Pure]> {
  let summary = "Constant CHOP: fixed (literal) channel values";
  let arguments = (ins F64ArrayAttr:$values);
  let results = (outs Tox_ChopType:$result);
  let assemblyFormat = "$values attr-dict `:` type($result)";
}

def Tox_ChopExprOp : Tox_Op<"chop_expr", [Pure]> {
  let summary = "CHOP whose channels are TD expressions over its input CHOPs";
  let description = [{
    The general control-math node: each result channel is a TD expression string
    (`channels`) that may reference the operand CHOPs (e.g. `op('midiin1')[0][0]
    /127 - 0.5`). Covers Constant CHOPs with expression parameters and Math CHOPs.
    P0 carries the exprs verbatim (parity with the runtime fasteval path); P1
    lowers them to `arith`/`math` and fuses the DAG into one compiled kernel.
  }];
  let arguments = (ins Variadic<Tox_ChopType>:$inputs, StrArrayAttr:$channels);
  let results = (outs Tox_ChopType:$result);
  let assemblyFormat = "`(` $inputs `)` $channels attr-dict `:` functional-type($inputs, $result)";
}

def Tox_ChopSpeedOp : Tox_Op<"chop_speed"> {
  let summary = "Speed CHOP: time-integrate the input channels";
  let description = [{
    Loop-carried integrator (`state' = state + input * dt`) — the CPU-side
    instance of the `tox.feedback` pattern. Not `Pure` (depends on time + prior
    state).
  }];
  let arguments = (ins Tox_ChopType:$input);
  let results = (outs Tox_ChopType:$result);
  let assemblyFormat = "$input attr-dict `:` functional-type($input, $result)";
}

def Tox_ChopMathOp : Tox_Op<"chop_math", [Pure]> {
  let summary = "Elementwise affine math on a CHOP (channel*scale + offset)";
  let arguments = (ins Tox_ChopType:$input,
                       DefaultValuedAttr<F64Attr, "1.0">:$scale,
                       DefaultValuedAttr<F64Attr, "0.0">:$offset);
  let results = (outs Tox_ChopType:$result);
  let assemblyFormat = "$input attr-dict `:` functional-type($input, $result)";
}

def Tox_ChopSelectOp : Tox_Op<"chop_select", [Pure]> {
  let summary = "Select/Null CHOP: passthrough (optionally a channel subset)";
  let arguments = (ins Tox_ChopType:$input);
  let results = (outs Tox_ChopType:$result);
  let assemblyFormat = "$input attr-dict `:` functional-type($input, $result)";
}

def Tox_ChopSampleOp : Tox_Op<"chop_sample", [Pure]> {
  let summary = "Read channel `index` of a CHOP as a scalar (CHOP->uniform edge)";
  let arguments = (ins Tox_ChopType:$input, I64Attr:$index);
  let results = (outs F64:$result);
  let assemblyFormat = "$input `[` $index `]` attr-dict `:` functional-type($input, $result)";
}

#endif // TOX_OPS
