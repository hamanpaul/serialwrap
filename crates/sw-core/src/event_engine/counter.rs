//! fire 計數持久化（P7 Task 13；`event_engine/counter.py`，EVT-D-E11 write-through 快取，DF-01、DF-18）。
//!
//! 偏離 Task 0 骨架簽章：Python `load` 只攔 `FileNotFoundError`／`JSONDecodeError`／`OSError`，壞檔的
//! AttributeError／TypeError／ValueError／UnicodeDecodeError 會穿出（golden `counter_file:007／012／013`），
//! 故 `from_json`／`load` 回 `Result<_, PyErr>`；`clear`、`known_rule_ids` 的非 FileNotFoundError 同樣穿出。

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use sw_pal::traits::{FsOps, PalErrorKind, PalResult};
use sw_proto::pycompat::{decode_utf8_strict, py_truthy, universal_newlines, PyErr};
use sw_proto::pyjson::{self, PyDict, PyInt, PyValue};

use super::schema::{py_int, type_name, RuleError};

/// counter 檔可被手改成超出 i128 的值，Python 照收；遞增經 `PyInt::add_small(1)`。
#[derive(Clone, Debug, PartialEq)]
pub struct Counter {
    pub fires: PyInt,
    pub last_fire_ts: Option<PyInt>,
    pub exhausted: bool,
}

impl Default for Counter {
    fn default() -> Self {
        Self {
            fires: PyInt::Small(0),
            last_fire_ts: None,
            exhausted: false,
        }
    }
}

fn rule_err(e: RuleError) -> PyErr {
    match e {
        RuleError::Schema(m) | RuleError::Value(m) | RuleError::Type(m) => PyErr { message: m },
    }
}

impl Counter {
    /// `{"fires": …, "last_fire_ts": …, "exhausted": …}`（插入序即 Python dict 序）。
    pub fn to_json(&self) -> PyDict {
        let mut d = PyDict::new();
        d.insert("fires", PyValue::Int(self.fires.clone()));
        d.insert(
            "last_fire_ts",
            self.last_fire_ts
                .clone()
                .map_or(PyValue::Null, PyValue::Int),
        );
        d.insert("exhausted", PyValue::Bool(self.exhausted));
        d
    }

    /// `Counter.from_json`：`int(obj.get("fires", 0))`、`int(obj["last_fire_ts"]) if obj.get(...) is not None`、
    /// `bool(obj.get("exhausted", False))`，依序求值；非 dict → `AttributeError`。
    pub fn from_json(v: &PyValue) -> Result<Counter, PyErr> {
        let PyValue::Dict(d) = v else {
            return Err(PyErr {
                message: format!("'{}' object has no attribute 'get'", type_name(v)),
            });
        };
        let fires = match d.get("fires") {
            Some(f) => py_int(f).map_err(rule_err)?,
            None => PyInt::Small(0),
        };
        let last_fire_ts = match d.get("last_fire_ts") {
            Some(t) if !t.is_null() => Some(py_int(t).map_err(rule_err)?),
            _ => None,
        };
        let exhausted = d.get("exhausted").is_some_and(py_truthy);
        Ok(Counter {
            fires,
            last_fire_ts,
            exhausted,
        })
    }
}

/// `open(path, "r", encoding="utf-8")` 後 `json.load`：strict UTF-8 解碼、universal newlines。
pub(crate) fn read_text_utf8(fs: &dyn FsOps, path: &Path) -> Result<PalResult<String>, PyErr> {
    match fs.read(path) {
        Ok(bytes) => match decode_utf8_strict(&bytes) {
            Ok(s) => Ok(Ok(universal_newlines(&s))),
            Err(e) => Err(PyErr {
                message: e.to_string(),
            }),
        },
        Err(e) => Ok(Err(e)),
    }
}

/// Per-rule counter，存放於 `EVENTS_RUNTIME_DIR`（tmpfs）。缺檔＝歸零；寫入經 tmp＋replace。
pub struct CounterStore {
    root: PathBuf,
    fs: Arc<dyn FsOps>,
    /// EVT-D-E11：write-through 快取（daemon 是唯一寫入者）；載入成功與 save 寫入、clear 失效。
    cache: Mutex<HashMap<String, Counter>>,
}

impl CounterStore {
    /// `os.makedirs(root, exist_ok=True)`。
    pub fn new(root: PathBuf, fs: Arc<dyn FsOps>) -> PalResult<Self> {
        fs.mkdirs(&root)?;
        Ok(Self {
            root,
            fs,
            cache: Mutex::new(HashMap::new()),
        })
    }

    pub fn path_for(&self, rule_id: &str) -> PathBuf {
        self.root.join(format!("{rule_id}.counter.json"))
    }

    pub fn load(&self, rule_id: &str) -> Result<Counter, PyErr> {
        if let Some(c) = self.cache.lock().unwrap().get(rule_id) {
            return Ok(c.clone());
        }
        let text = match read_text_utf8(&*self.fs, &self.path_for(rule_id))? {
            Ok(t) => t,
            // FileNotFoundError 與其他 OSError 一律歸零（不快取：之後的檔案仍可被讀到）。
            Err(_) => return Ok(Counter::default()),
        };
        let value = match pyjson::loads(&text) {
            Ok(v) => v,
            Err(pyjson::JsonError::Decode(_)) => return Ok(Counter::default()),
            Err(e @ pyjson::JsonError::Recursion) => {
                return Err(PyErr {
                    message: e.to_string(),
                })
            }
        };
        let c = Counter::from_json(&value)?;
        self.cache
            .lock()
            .unwrap()
            .insert(rule_id.to_string(), c.clone());
        Ok(c)
    }

    /// `FsOps::create_truncate(<path>.tmp)` → `FsOps::replace(tmp, path)`（DF-18）。
    /// 內容為 `json.dump(counter.to_json(), f)`（預設分隔符、ensure_ascii=True）。
    pub fn save(&self, rule_id: &str, c: &Counter) -> PalResult<()> {
        let path = self.path_for(rule_id);
        let mut tmp = path.clone().into_os_string();
        tmp.push(".tmp");
        let tmp = PathBuf::from(tmp);
        let body = pyjson::dumps_dict(
            &c.to_json(),
            &pyjson::DumpOpts {
                sep: pyjson::Sep::Default,
                ensure_ascii: true,
                sort_keys: false,
                allow_nan: true,
            },
        );
        self.fs.create_truncate(&tmp, body.as_bytes())?;
        self.fs.sync_file(&tmp)?;
        self.fs.replace(&tmp, &path)?;
        self.cache
            .lock()
            .unwrap()
            .insert(rule_id.to_string(), c.clone());
        Ok(())
    }

    /// `os.unlink`；FileNotFoundError 略過、其他 OSError 穿出。
    pub fn clear(&self, rule_id: &str) -> PalResult<()> {
        self.cache.lock().unwrap().remove(rule_id);
        self.fs.remove_file_if_exists(&self.path_for(rule_id))?;
        Ok(())
    }

    /// `os.listdir(root)` 中 `*.counter.json` 的 rule_id（listdir 序）；root 不存在 → 空。
    pub fn known_rule_ids(&self) -> PalResult<Vec<String>> {
        let entries = match self.fs.list_dir(&self.root) {
            Ok(e) => e,
            Err(e) if e.kind == PalErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => return Err(e),
        };
        Ok(entries
            .into_iter()
            .filter_map(|e| e.strip_suffix(".counter.json").map(str::to_string))
            .collect())
    }
}

/// DF-01 正規式：`^[a-z0-9][a-z0-9-]{0,63}\.[a-z0-9][a-z0-9-]{0,63}\Z`。
pub fn valid_rule_id(rule_id: &str) -> bool {
    let part = |s: &str| {
        let b = s.as_bytes();
        (1..=64).contains(&b.len())
            && matches!(b[0], b'a'..=b'z' | b'0'..=b'9')
            && b[1..]
                .iter()
                .all(|c| matches!(c, b'a'..=b'z' | b'0'..=b'9' | b'-'))
    };
    match rule_id.split_once('.') {
        Some((owner, name)) => part(owner) && part(name),
        None => false,
    }
}
