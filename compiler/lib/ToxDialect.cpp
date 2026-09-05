#include "Tox/ToxDialect.h"
#include "Tox/ToxOps.h"
#include "Tox/ToxTypes.h"

#include "mlir/IR/DialectImplementation.h"
#include "llvm/ADT/TypeSwitch.h"

using namespace mlir;
using namespace tox;

#include "Tox/ToxDialect.cpp.inc"

#define GET_TYPEDEF_CLASSES
#include "Tox/ToxTypes.cpp.inc"

#define GET_OP_CLASSES
#include "Tox/ToxOps.cpp.inc"

void ToxDialect::initialize() {
  addTypes<
#define GET_TYPEDEF_LIST
#include "Tox/ToxTypes.cpp.inc"
      >();
  addOperations<
#define GET_OP_LIST
#include "Tox/ToxOps.cpp.inc"
      >();
}
