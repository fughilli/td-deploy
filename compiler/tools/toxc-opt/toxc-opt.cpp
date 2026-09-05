// toxc-opt — an mlir-opt driver with the `tox` dialect registered, for
// round-tripping and (later) running the tox lowering pipeline.
#include "mlir/IR/DialectRegistry.h"
#include "mlir/InitAllDialects.h"
#include "mlir/InitAllPasses.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"

#include "Tox/ToxDialect.h"

int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  mlir::registerAllDialects(registry);
  mlir::registerAllPasses();
  registry.insert<tox::ToxDialect>();
  return mlir::asMainReturnCode(
      mlir::MlirOptMain(argc, argv, "toxc-opt (tox dialect)\n", registry));
}
