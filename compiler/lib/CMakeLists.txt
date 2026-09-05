add_mlir_dialect_library(ToxDialect
  ToxDialect.cpp

  DEPENDS
  ToxIncGen

  LINK_LIBS PUBLIC
  MLIRIR
  MLIRSupport
)
