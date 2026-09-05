#ifndef TOX_DIALECT
#define TOX_DIALECT

include "mlir/IR/OpBase.td"

def Tox_Dialect : Dialect {
  let name = "tox";
  let cppNamespace = "::tox";
  let summary = "toxc TouchDesigner operator dialect";
  let useDefaultTypePrinterParser = 1;
}

class Tox_Op<string mnemonic, list<Trait> traits = []> :
    Op<Tox_Dialect, mnemonic, traits>;

#endif // TOX_DIALECT
