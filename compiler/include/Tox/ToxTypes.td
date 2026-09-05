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

#endif // TOX_TYPES
