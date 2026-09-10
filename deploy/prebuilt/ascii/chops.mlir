module {
func.func @chops(%t: f64, %dt: f64, %frame: f64, %src_midiin1_0: f64, %st_speed1_0: f64) -> (f64, f64) {
    %v0 = arith.constant 127.0 : f64
    %v1 = arith.divf %src_midiin1_0, %v0 : f64
    %v2 = arith.constant 0.5 : f64
    %v3 = arith.subf %v1, %v2 : f64
    %v4 = arith.mulf %v3, %dt : f64
    %v5 = arith.addf %st_speed1_0, %v4 : f64
    return %v3, %v5 : f64, f64
}
llvm.func @chops_v(%in: !llvm.ptr, %out: !llvm.ptr) {
  %pi0 = llvm.getelementptr %in[0] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai0 = llvm.load %pi0 : !llvm.ptr -> f64
  %pi1 = llvm.getelementptr %in[1] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai1 = llvm.load %pi1 : !llvm.ptr -> f64
  %pi2 = llvm.getelementptr %in[2] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai2 = llvm.load %pi2 : !llvm.ptr -> f64
  %pi3 = llvm.getelementptr %in[3] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai3 = llvm.load %pi3 : !llvm.ptr -> f64
  %pi4 = llvm.getelementptr %in[4] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai4 = llvm.load %pi4 : !llvm.ptr -> f64
  %r:2 = func.call @chops(%ai0, %ai1, %ai2, %ai3, %ai4) : (f64, f64, f64, f64, f64) -> (f64, f64)
  %po0 = llvm.getelementptr %out[0] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#0, %po0 : f64, !llvm.ptr
  %po1 = llvm.getelementptr %out[1] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#1, %po1 : f64, !llvm.ptr
  llvm.return
}
}
