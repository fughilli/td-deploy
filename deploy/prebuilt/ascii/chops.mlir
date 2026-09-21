module {
func.func private @chops(%t: f64, %dt: f64, %frame: f64, %src_midiin1_ch1ctrl5: f64, %src_midiin1_ch1ctrl1: f64, %src_midiin1_ch1ctrl2: f64, %src_midiin1_ch1ctrl3: f64, %st_speed1_0: f64, %st_spin1_0: f64, %st_spin1_1: f64, %st_spin1_2: f64) -> (f64, f64, f64, f64, f64, f64, f64, f64, f64, f64)
    attributes {llvm.linkage = #llvm.linkage<internal>} {
    %v0 = arith.constant 127.0 : f64
    %v1 = arith.divf %src_midiin1_ch1ctrl5, %v0 : f64
    %v2 = arith.constant 127.0 : f64
    %v3 = arith.divf %src_midiin1_ch1ctrl1, %v2 : f64
    %v4 = arith.constant 0.5 : f64
    %v5 = arith.subf %v3, %v4 : f64
    %v6 = arith.mulf %v5, %dt : f64
    %v7 = arith.addf %st_speed1_0, %v6 : f64
    %v8 = arith.constant 64.0 : f64
    %v9 = arith.subf %src_midiin1_ch1ctrl1, %v8 : f64
    %v10 = arith.constant 64.0 : f64
    %v11 = arith.divf %v9, %v10 : f64
    %v12 = arith.constant 180.0 : f64
    %v13 = arith.mulf %v11, %v12 : f64
    %v14 = arith.constant 64.0 : f64
    %v15 = arith.subf %src_midiin1_ch1ctrl2, %v14 : f64
    %v16 = arith.constant 64.0 : f64
    %v17 = arith.divf %v15, %v16 : f64
    %v18 = arith.constant 180.0 : f64
    %v19 = arith.mulf %v17, %v18 : f64
    %v20 = arith.constant 64.0 : f64
    %v21 = arith.subf %src_midiin1_ch1ctrl3, %v20 : f64
    %v22 = arith.constant 64.0 : f64
    %v23 = arith.divf %v21, %v22 : f64
    %v24 = arith.constant 180.0 : f64
    %v25 = arith.mulf %v23, %v24 : f64
    %v26 = arith.mulf %v13, %dt : f64
    %v27 = arith.addf %st_spin1_0, %v26 : f64
    %v28 = arith.mulf %v19, %dt : f64
    %v29 = arith.addf %st_spin1_1, %v28 : f64
    %v30 = arith.mulf %v25, %dt : f64
    %v31 = arith.addf %st_spin1_2, %v30 : f64
    return %v1, %v1, %v5, %v7, %v13, %v19, %v25, %v27, %v29, %v31 : f64, f64, f64, f64, f64, f64, f64, f64, f64, f64
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
  %pi5 = llvm.getelementptr %in[5] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai5 = llvm.load %pi5 : !llvm.ptr -> f64
  %pi6 = llvm.getelementptr %in[6] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai6 = llvm.load %pi6 : !llvm.ptr -> f64
  %pi7 = llvm.getelementptr %in[7] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai7 = llvm.load %pi7 : !llvm.ptr -> f64
  %pi8 = llvm.getelementptr %in[8] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai8 = llvm.load %pi8 : !llvm.ptr -> f64
  %pi9 = llvm.getelementptr %in[9] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai9 = llvm.load %pi9 : !llvm.ptr -> f64
  %pi10 = llvm.getelementptr %in[10] : (!llvm.ptr) -> !llvm.ptr, f64
  %ai10 = llvm.load %pi10 : !llvm.ptr -> f64
  %r:10 = func.call @chops(%ai0, %ai1, %ai2, %ai3, %ai4, %ai5, %ai6, %ai7, %ai8, %ai9, %ai10) : (f64, f64, f64, f64, f64, f64, f64, f64, f64, f64, f64) -> (f64, f64, f64, f64, f64, f64, f64, f64, f64, f64)
  %po0 = llvm.getelementptr %out[0] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#0, %po0 : f64, !llvm.ptr
  %po1 = llvm.getelementptr %out[1] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#1, %po1 : f64, !llvm.ptr
  %po2 = llvm.getelementptr %out[2] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#2, %po2 : f64, !llvm.ptr
  %po3 = llvm.getelementptr %out[3] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#3, %po3 : f64, !llvm.ptr
  %po4 = llvm.getelementptr %out[4] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#4, %po4 : f64, !llvm.ptr
  %po5 = llvm.getelementptr %out[5] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#5, %po5 : f64, !llvm.ptr
  %po6 = llvm.getelementptr %out[6] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#6, %po6 : f64, !llvm.ptr
  %po7 = llvm.getelementptr %out[7] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#7, %po7 : f64, !llvm.ptr
  %po8 = llvm.getelementptr %out[8] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#8, %po8 : f64, !llvm.ptr
  %po9 = llvm.getelementptr %out[9] : (!llvm.ptr) -> !llvm.ptr, f64
  llvm.store %r#9, %po9 : f64, !llvm.ptr
  llvm.return
}
}
