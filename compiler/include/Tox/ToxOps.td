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

#endif // TOX_OPS
