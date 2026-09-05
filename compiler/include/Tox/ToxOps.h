#ifndef TOX_TOXOPS_H
#define TOX_TOXOPS_H

#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinTypes.h"
#include "mlir/IR/Dialect.h"
#include "mlir/IR/OpDefinition.h"
#include "mlir/IR/OpImplementation.h"
#include "mlir/Interfaces/SideEffectInterfaces.h"

#include "Tox/ToxTypes.h"

#define GET_OP_CLASSES
#include "Tox/ToxOps.h.inc"

#endif // TOX_TOXOPS_H
