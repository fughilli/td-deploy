get_property(dialect_libs GLOBAL PROPERTY MLIR_DIALECT_LIBS)
get_property(conversion_libs GLOBAL PROPERTY MLIR_CONVERSION_LIBS)

add_llvm_executable(toxc-opt toxc-opt.cpp)
llvm_update_compile_flags(toxc-opt)
target_link_libraries(toxc-opt PRIVATE
  ToxDialect
  ${dialect_libs}
  ${conversion_libs}
  MLIROptLib
  MLIRIR
  MLIRSupport
)
mlir_check_all_link_libraries(toxc-opt)
