//! events.ndjson 事件日誌（P7 Task 13；`event_engine/event_log.py`，rotate 10 MiB×3）。
//!
//! 偏離 Task 0 骨架簽章：Python `write` 的 OSError（getsize／replace／open／fsync）穿出呼叫端，故回 `PalResult<()>`。

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use sw_pal::traits::{Clock, FsOps, PalErrorKind, PalResult};
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{self, PyDict, PyInt, PyValue};

use super::counter::read_text_utf8;
use super::pyops::{py_eq, py_lt, py_time_ms};
use super::schema::type_name;

const ROTATE_BYTES: u64 = 10 * 1024 * 1024;
const BACKUP_COUNT: u32 = 3;

/// 以換行分隔的 JSON 日誌，依大小輪替；每筆寫入 fsync。
pub struct EventLogger {
    path: PathBuf,
    fs: Arc<dyn FsOps>,
    clock: Arc<dyn Clock>,
    lock: Mutex<()>,
}

/// `json.dumps(event, ensure_ascii=False)`。
const LINE_OPTS: pyjson::DumpOpts = pyjson::DumpOpts {
    sep: pyjson::Sep::Default,
    ensure_ascii: false,
    sort_keys: false,
    allow_nan: true,
};

impl EventLogger {
    /// `os.makedirs(os.path.dirname(path) or ".", exist_ok=True)`。
    pub fn new(path: PathBuf, fs: Arc<dyn FsOps>, clock: Arc<dyn Clock>) -> PalResult<Self> {
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        fs.mkdirs(parent)?;
        Ok(Self {
            path,
            fs,
            clock,
            lock: Mutex::new(()),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// 缺 `ts` 時補在最後；整行先判定輪替再 append＋fsync。
    pub fn write(&self, mut ev: PyDict) -> PalResult<()> {
        if ev.get("ts").is_none() {
            ev.insert("ts", PyValue::from(py_time_ms(&*self.clock)));
        }
        let mut line = pyjson::dumps_dict(&ev, &LINE_OPTS);
        line.push('\n');
        let encoded = line.into_bytes();
        let _g = self.lock.lock().unwrap();
        self.rotate_if_needed(encoded.len() as u64)?;
        let mut f = self.fs.open_append(&self.path)?;
        f.write_all_flush(&encoded)?;
        f.sync()
    }

    fn backup(&self, i: u32) -> PathBuf {
        let mut p = self.path.clone().into_os_string();
        p.push(format!(".{i}"));
        PathBuf::from(p)
    }

    fn rotate_if_needed(&self, incoming: u64) -> PalResult<()> {
        let Some(size) = self.fs.file_size(&self.path)? else {
            return Ok(());
        };
        if size.saturating_add(incoming) <= ROTATE_BYTES {
            return Ok(());
        }
        for i in (1..BACKUP_COUNT).rev() {
            let src = self.backup(i);
            if self.fs.exists(&src) {
                self.fs.replace(&src, &self.backup(i + 1))?;
            }
        }
        if self.fs.exists(&self.path) {
            self.fs.replace(&self.path, &self.backup(1))?;
        }
        Ok(())
    }

    /// 只讀現行檔：逐行 `json.loads`（解析失敗略過），依序套 `rule_id`／`selector`（`obj.get(k) != v`）、
    /// `since_ts`（`obj.get("ts", 0) < since_ts`）過濾，最後 `out[-n:]`。
    pub fn tail(
        &self,
        rule_id: Option<&PyValue>,
        selector: Option<&PyValue>,
        since_ts: Option<&PyValue>,
        n: Option<&PyValue>,
    ) -> Result<Vec<PyValue>, PyErr> {
        let rule_id = rule_id.filter(|v| !v.is_null());
        let selector = selector.filter(|v| !v.is_null());
        let since_ts = since_ts.filter(|v| !v.is_null());
        let n = n.filter(|v| !v.is_null());
        let text = match read_text_utf8(&*self.fs, &self.path)? {
            Ok(t) => t,
            Err(e) if e.kind == PalErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => {
                return Err(PyErr {
                    message: format!(
                        "{}: {}",
                        e.py_message,
                        sw_proto::pyfmt::py_repr_str(&self.path.to_string_lossy())
                    ),
                })
            }
        };
        let mut out = Vec::new();
        for line in text.split_inclusive('\n') {
            let obj = match pyjson::loads(line) {
                Ok(v) => v,
                Err(pyjson::JsonError::Decode(_)) => continue,
                Err(e @ pyjson::JsonError::Recursion) => {
                    return Err(PyErr {
                        message: e.to_string(),
                    })
                }
            };
            let get = |k: &str| -> Result<Option<PyValue>, PyErr> {
                match &obj {
                    PyValue::Dict(d) => Ok(d.get(k).cloned()),
                    other => Err(PyErr {
                        message: format!("'{}' object has no attribute 'get'", type_name(other)),
                    }),
                }
            };
            if let Some(want) = rule_id {
                if !py_eq(&get("rule_id")?.unwrap_or(PyValue::Null), want) {
                    continue;
                }
            }
            if let Some(want) = selector {
                if !py_eq(&get("selector")?.unwrap_or(PyValue::Null), want) {
                    continue;
                }
            }
            if let Some(since) = since_ts {
                let ts = get("ts")?.unwrap_or(PyValue::from(0));
                if py_lt(&ts, since)? {
                    continue;
                }
            }
            out.push(obj);
        }
        match n {
            None => Ok(out),
            Some(n) => py_tail_slice(out, n),
        }
    }
}

/// `out[-n:]`：bool／int 依 Python 切片（`-0` 為全取、負數略過前段）；float → 切片 TypeError；其餘 → 一元負號 TypeError。
fn py_tail_slice(mut out: Vec<PyValue>, n: &PyValue) -> Result<Vec<PyValue>, PyErr> {
    let int = match n {
        PyValue::Bool(b) => PyInt::Small(i128::from(*b)),
        PyValue::Int(i) => i.clone(),
        PyValue::Float(_) => {
            return Err(PyErr {
                message: "slice indices must be integers or None or have an __index__ method"
                    .into(),
            })
        }
        other => {
            return Err(PyErr {
                message: format!("bad operand type for unary -: '{}'", type_name(other)),
            })
        }
    };
    let len = out.len() as i128;
    // start = -n；負的 start 加上 len 後夾到 [0, len]。
    let start = match int {
        PyInt::Small(k) => {
            let s = k.checked_neg().unwrap_or(i128::MAX);
            if s < 0 {
                (s + len).max(0)
            } else {
                s.min(len)
            }
        }
        // 超出 i128：正 n → start 極負 → 0；負 n → start 極大 → len。
        PyInt::Big(s) => {
            if s.starts_with('-') {
                len
            } else {
                0
            }
        }
    };
    Ok(out.split_off(start as usize))
}
