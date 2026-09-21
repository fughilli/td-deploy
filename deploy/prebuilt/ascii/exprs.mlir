func.func @expr0() -> f64 {
    %v0 = arith.constant 1.0 : f64
    return %v0 : f64
}

func.func @expr1() -> f64 {
    %v0 = arith.constant 0.5 : f64
    return %v0 : f64
}

func.func @expr2() -> f64 {
    %v0 = arith.constant 0.0 : f64
    return %v0 : f64
}

func.func @expr3(%arg_chop_midiin1_ch1ctrl2: f64) -> f64 {
    %v0 = arith.constant 64.0 : f64
    %v1 = arith.divf %arg_chop_midiin1_ch1ctrl2, %v0 : f64
    return %v1 : f64
}

func.func @expr4() -> f64 {
    %v0 = arith.constant 0.95 : f64
    return %v0 : f64
}

