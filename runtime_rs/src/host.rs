// The Python host co-process (pyhost/tdhost): runs the project's Python —
// Execute DAT callbacks, Script TOP/CHOP/SOP cooks, Python parameter
// expressions — against the TouchDesigner API emulation, and hands the renderer
// the values it is bound to.
//
// It runs as a child process rather than an in-process interpreter: the runtime
// stays a pure Rust binary (no libpython to link or cross-compile against), the
// project can use whatever Python environment the device provides (the
// project's own venv when shipped), and a Python crash can't take the renderer
// down with it.
//
// Wire protocol over the child's stdin/stdout (its print()s go to stderr):
//   message = u32 LE header length | header JSON | blob bytes...
//   header["blobs"] = [len0, len1, ...] sizes of the raw blobs that follow.
use serde_json::{json, Value};
use std::io::{BufReader, BufWriter, Read, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

pub struct Host {
    child: Child,
    tx: BufWriter<ChildStdin>,
    rx: BufReader<ChildStdout>,
}

/// One reply: the JSON header and the raw blobs it indexes.
pub struct Msg {
    pub head: Value,
    pub blobs: Vec<Vec<u8>>,
}

impl Msg {
    pub fn blob(&self, v: &Value) -> Option<&[u8]> {
        v.as_u64()
            .and_then(|i| self.blobs.get(i as usize))
            .map(|b| b.as_slice())
    }
    pub fn f64s(&self, v: &Value) -> Vec<f64> {
        self.blob(v)
            .map(|b| {
                b.chunks_exact(8)
                    .map(|c| f64::from_le_bytes(c.try_into().unwrap()))
                    .collect()
            })
            .unwrap_or_default()
    }
    pub fn f32s(&self, v: &Value) -> Vec<f32> {
        self.blob(v)
            .map(|b| {
                b.chunks_exact(4)
                    .map(|c| f32::from_le_bytes(c.try_into().unwrap()))
                    .collect()
            })
            .unwrap_or_default()
    }
}

/// Pick the interpreter: $TOXC_PYTHON, then the project's shipped venv, then
/// the system python3.
fn interpreter(artifact: &str) -> String {
    if let Ok(p) = std::env::var("TOXC_PYTHON") {
        return p;
    }
    for cand in [
        format!("{artifact}/project/.venv/bin/python"),
        format!("{artifact}/.venv/bin/python"),
    ] {
        if std::path::Path::new(&cand).exists() {
            return cand;
        }
    }
    "python3".to_string()
}

impl Host {
    pub fn spawn(artifact: &str, init: Value) -> std::io::Result<Host> {
        let py = interpreter(artifact);
        let pkg = format!("{artifact}/host");
        let pypath = match std::env::var("PYTHONPATH") {
            Ok(p) if !p.is_empty() => format!("{pkg}:{p}"),
            _ => pkg.clone(),
        };
        eprintln!("[host] starting {py} -m tdhost.serve (PYTHONPATH={pkg})");
        let mut child = Command::new(&py)
            .args(["-u", "-m", "tdhost.serve", artifact])
            .env("PYTHONPATH", pypath)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?;
        let tx = BufWriter::new(child.stdin.take().unwrap());
        let rx = BufReader::new(child.stdout.take().unwrap());
        let mut h = Host { child, tx, rx };
        let r = h.call(json!({"cmd": "init", "init": init}), &[])?;
        if r.head.get("ok").and_then(|v| v.as_bool()) != Some(true) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                format!("host init failed: {}", r.head),
            ));
        }
        Ok(h)
    }

    pub fn call(&mut self, mut head: Value, blobs: &[&[u8]]) -> std::io::Result<Msg> {
        head["blobs"] = json!(blobs.iter().map(|b| b.len()).collect::<Vec<_>>());
        let hb = serde_json::to_vec(&head).unwrap();
        self.tx.write_all(&(hb.len() as u32).to_le_bytes())?;
        self.tx.write_all(&hb)?;
        for b in blobs {
            self.tx.write_all(b)?;
        }
        self.tx.flush()?;
        self.read()
    }

    fn read(&mut self) -> std::io::Result<Msg> {
        let mut n = [0u8; 4];
        self.rx.read_exact(&mut n)?;
        let mut hb = vec![0u8; u32::from_le_bytes(n) as usize];
        self.rx.read_exact(&mut hb)?;
        let head: Value = serde_json::from_slice(&hb)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        let mut blobs = Vec::new();
        if let Some(lens) = head.get("blobs").and_then(|v| v.as_array()) {
            for l in lens {
                let mut b = vec![0u8; l.as_u64().unwrap_or(0) as usize];
                self.rx.read_exact(&mut b)?;
                blobs.push(b);
            }
        }
        Ok(Msg { head, blobs })
    }

    pub fn exit(&mut self) {
        let _ = self.call(json!({"cmd": "exit"}), &[]);
        let _ = self.child.wait();
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn msg_decodes_typed_blobs() {
        let f64b: Vec<u8> = [1.5f64, -2.0]
            .iter()
            .flat_map(|v| v.to_le_bytes())
            .collect();
        let f32b: Vec<u8> = [0.25f32, 4.0]
            .iter()
            .flat_map(|v| v.to_le_bytes())
            .collect();
        let m = Msg {
            head: json!({"a": 0, "b": 1}),
            blobs: vec![f64b, f32b],
        };
        assert_eq!(m.f64s(&m.head["a"]), vec![1.5, -2.0]);
        assert_eq!(m.f32s(&m.head["b"]), vec![0.25, 4.0]);
        assert!(m.f64s(&json!(7)).is_empty());
        assert!(m.blob(&json!("x")).is_none());
    }
}
