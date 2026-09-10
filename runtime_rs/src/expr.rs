// Interpreter for the INTERPRETED (non-lowered) TD parameter exprs the MLIR
// transpiler couldn't compile. fasteval does the arithmetic/functions/precedence
// (robust + pre-compiled for fast repeated eval); we only hand-roll the small
// TD-syntax preprocessor that rewrites `op('name')[chan]` and `absTime.*` into
// fasteval variables. A Program compiles once and is evaluated per-frame.

use fasteval::{Compiler, Evaler};

/// A compiled interpreted expression. Compile once, eval per-frame.
pub struct Program {
    slab: fasteval::Slab,
    instr: fasteval::Instruction,
    // fasteval var name -> (chop name, channel) for op('name')[chan] reads.
    vars: Vec<(String, String, String)>,
}

impl Program {
    pub fn compile(td: &str) -> Program {
        let (fe, vars) = preprocess(td);
        let parser = fasteval::Parser::new();
        let mut slab = fasteval::Slab::new();
        let src = if fe.trim().is_empty() { "0" } else { &fe };
        let instr = match parser.parse(src, &mut slab.ps) {
            Ok(e) => e.from(&slab.ps).compile(&slab.ps, &mut slab.cs),
            Err(_) => {
                // Unparseable -> compile a constant 0 so eval is a safe no-op.
                let mut s2 = fasteval::Slab::new();
                let z = fasteval::Parser::new().parse("0", &mut s2.ps).unwrap().from(&s2.ps).compile(&s2.ps, &mut s2.cs);
                slab = s2;
                z
            }
        };
        Program { slab, instr, vars }
    }

    /// Evaluate with the current time and CHOP store. `chop(name, chan)` reads a
    /// live CHOP channel (0.0 if unset).
    pub fn eval(&self, t: f64, frame: f64, chop: &dyn Fn(&str, &str) -> f64) -> f64 {
        let mut ns = |name: &str, _args: Vec<f64>| -> Option<f64> {
            match name {
                "t" => Some(t),
                "frame" => Some(frame),
                "pi" => Some(std::f64::consts::PI),
                "e" => Some(std::f64::consts::E),
                _ => self.vars.iter().find(|(v, _, _)| v == name).map(|(_, n, c)| chop(n, c)),
            }
        };
        match self.instr.eval(&self.slab, &mut ns) {
            Ok(x) if x.is_finite() => x,
            _ => 0.0,
        }
    }
}

// Rewrite TD-specific syntax into a fasteval-parseable string + a var table.
//   op('NAME')[CHAN]         -> cvK           (CHAN quoted or int; optional [S] sample dropped)
//   absTime.seconds          -> t
//   absTime.frame|step       -> frame
fn preprocess(src: &str) -> (String, Vec<(String, String, String)>) {
    let b: Vec<char> = src.trim().trim_matches('"').trim_matches('\'').chars().collect();
    let mut out = String::new();
    let mut vars: Vec<(String, String, String)> = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if let Some((name, chan, next)) = parse_op(&b, i) {
            let cv = format!("cv{}", vars.len());
            out.push_str(&cv);
            vars.push((cv, name, chan));
            i = next;
            continue;
        }
        if let Some(n) = starts_with(&b, i, "absTime.seconds") {
            out.push('t');
            i = n;
            continue;
        }
        if let Some(n) = starts_with(&b, i, "absTime.frame").or_else(|| starts_with(&b, i, "absTime.step")) {
            out.push_str("frame");
            i = n;
            continue;
        }
        out.push(b[i]);
        i += 1;
    }
    (out, vars)
}

fn starts_with(b: &[char], i: usize, pat: &str) -> Option<usize> {
    let p: Vec<char> = pat.chars().collect();
    if i + p.len() <= b.len() && b[i..i + p.len()] == p[..] {
        Some(i + p.len())
    } else {
        None
    }
}

// Parse `op('NAME')[CHAN]([S])?` starting at b[i] (must be an identifier
// boundary). Returns (name, chan_string, index_past_the_end).
fn parse_op(b: &[char], i: usize) -> Option<(String, String, usize)> {
    if i + 2 > b.len() || b[i] != 'o' || b[i + 1] != 'p' {
        return None;
    }
    if i > 0 && (b[i - 1].is_alphanumeric() || b[i - 1] == '_') {
        return None; // part of a longer identifier
    }
    let mut j = i + 2;
    while j < b.len() && b[j].is_whitespace() {
        j += 1;
    }
    if j >= b.len() || b[j] != '(' {
        return None;
    }
    j += 1;
    while j < b.len() && b[j].is_whitespace() {
        j += 1;
    }
    let quote = if j < b.len() && (b[j] == '\'' || b[j] == '"') {
        let q = b[j];
        j += 1;
        q
    } else {
        return None;
    };
    let ns = j;
    while j < b.len() && b[j] != quote {
        j += 1;
    }
    let name: String = b[ns..j].iter().collect();
    if j < b.len() {
        j += 1; // closing quote
    }
    while j < b.len() && b[j].is_whitespace() {
        j += 1;
    }
    if j >= b.len() || b[j] != ')' {
        return None;
    }
    j += 1;
    // channel: [ 'chan' ] or [ int ]
    let mut chan = String::from("0");
    if j < b.len() && b[j] == '[' {
        j += 1;
        while j < b.len() && b[j].is_whitespace() {
            j += 1;
        }
        let (c, nj) = read_index(b, j);
        chan = c;
        j = nj;
        while j < b.len() && b[j] != ']' {
            j += 1;
        }
        if j < b.len() {
            j += 1; // ]
        }
        // optional second [sample] -> ignore whatever's inside
        if j < b.len() && b[j] == '[' {
            j += 1;
            while j < b.len() && b[j] != ']' {
                j += 1;
            }
            if j < b.len() {
                j += 1;
            }
        }
    }
    Some((name, chan, j))
}

fn read_index(b: &[char], j: usize) -> (String, usize) {
    if j < b.len() && (b[j] == '\'' || b[j] == '"') {
        let q = b[j];
        let mut k = j + 1;
        while k < b.len() && b[k] != q {
            k += 1;
        }
        let s: String = b[j + 1..k].iter().collect();
        (s, if k < b.len() { k + 1 } else { k })
    } else {
        let mut k = j;
        while k < b.len() && (b[k].is_ascii_digit() || b[k] == '-') {
            k += 1;
        }
        let s: String = b[j..k].iter().collect();
        (if s.is_empty() { "0".into() } else { s }, k)
    }
}

#[cfg(test)]
mod tests {
    use super::Program;

    fn noc(_: &str, _: &str) -> f64 {
        0.0
    }

    #[test]
    fn arithmetic_and_funcs() {
        assert_eq!(Program::compile("1 + 2 * 3").eval(0.0, 0.0, &noc), 7.0);
        assert_eq!(Program::compile("(1 + 2) * 3").eval(0.0, 0.0, &noc), 9.0);
        assert_eq!(Program::compile("absTime.seconds * 10").eval(2.0, 0.0, &noc), 20.0);
        assert!((Program::compile("sin(0) + cos(0)").eval(0.0, 0.0, &noc) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn chop_reads_single_and_double_index() {
        let ch = |name: &str, chan: &str| if name == "midiin1" && chan == "0" { 127.0 } else { 0.0 };
        // op('midiin1')[0][0]/127 - 0.5  == 0.5   (double index: sample dropped)
        assert!((Program::compile("op('midiin1')[0][0]/127 - 0.5").eval(0.0, 0.0, &ch) - 0.5).abs() < 1e-9);
        // op('speed1')[0] * 1000, speed1 unset -> 0
        assert_eq!(Program::compile("op('speed1')[0] * 1000").eval(0.0, 0.0, &ch), 0.0);
    }
}
