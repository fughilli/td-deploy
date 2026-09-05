set(LLVM_TARGET_DEFINITIONS ToxDialect.td)
mlir_tablegen(ToxDialect.h.inc -gen-dialect-decls -dialect=tox)
mlir_tablegen(ToxDialect.cpp.inc -gen-dialect-defs -dialect=tox)

set(LLVM_TARGET_DEFINITIONS ToxTypes.td)
mlir_tablegen(ToxTypes.h.inc -gen-typedef-decls -typedefs-dialect=tox)
mlir_tablegen(ToxTypes.cpp.inc -gen-typedef-defs -typedefs-dialect=tox)

set(LLVM_TARGET_DEFINITIONS ToxOps.td)
mlir_tablegen(ToxOps.h.inc -gen-op-decls)
mlir_tablegen(ToxOps.cpp.inc -gen-op-defs)

add_public_tablegen_target(ToxIncGen)
