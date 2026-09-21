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

func.func @expr3(%arg_chop_spin1_rx: f64) -> f64 {
    
    return %arg_chop_spin1_rx : f64
}

func.func @expr4(%arg_chop_spin1_ry: f64) -> f64 {
    
    return %arg_chop_spin1_ry : f64
}

func.func @expr5(%arg_chop_spin1_rz: f64) -> f64 {
    
    return %arg_chop_spin1_rz : f64
}

func.func @expr6(%arg_chop_midiin1_ch1ctrl4: f64) -> f64 {
    %v0 = arith.constant 0.25 : f64
    %v1 = arith.constant 64.0 : f64
    %v2 = arith.divf %arg_chop_midiin1_ch1ctrl4, %v1 : f64
    %v3 = arith.addf %v0, %v2 : f64
    return %v3 : f64
}

func.func @expr7(%arg_chop_midiin1_ch1ctrl6: f64) -> f64 {
    %v0 = arith.constant 64.0 : f64
    %v1 = arith.divf %arg_chop_midiin1_ch1ctrl6, %v0 : f64
    return %v1 : f64
}

func.func @expr8(%arg_chop_in_sat_sat: f64) -> f64 {
    
    return %arg_chop_in_sat_sat : f64
}

func.func @expr9(%arg_t: f64) -> f64 {
    
    return %arg_t : f64
}

