#ifndef TOX_TYPES
#define TOX_TYPES

include "mlir/IR/AttrTypeBase.td"
include "Tox/ToxDialect.td"

class Tox_Type<string name, string typeMnemonic, list<Trait> traits = []>
    : TypeDef<Tox_Dialect, name, traits> {
  let mnemonic = typeMnemonic;
}

def Tox_TopType : Tox_Type<"Top", "top"> {
  let summary = "A TOP (2D texture) value";
}

def Tox_ChopType : Tox_Type<"Chop", "chop"> {
  let summary = "A CHOP (control-rate channel bundle) value";
  let description = [{
    A CHOP carries `numChannels` control-rate scalar channels (e.g. a Constant
    CHOP's values, a MIDI In's controls, a Speed CHOP's integrated output).
    Printed as `!tox.chop<N>`.
  }];
  let parameters = (ins "unsigned":$numChannels);
  let assemblyFormat = "`<` $numChannels `>`";
}

#endif // TOX_TYPES
