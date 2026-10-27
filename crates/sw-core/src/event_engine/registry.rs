//! 規則檔註冊表（P7 Task 13；`event_engine/registry.py`，DF-01、DF-09、DF-18、RPC-H-13）。
//!
//! 偏離 Task 0 骨架簽章：`os.listdir` 的非 FileNotFoundError、`json.load` 的 RecursionError 在 Python 穿出
//! `load_all`（只攔 `OSError`／`ValueError`），`os.unlink` 的非 FileNotFoundError 穿出 `delete`，故
//! `load_all`／`diff_against` 回 `Result<_, PyErr>`、`delete` 回 `PalResult<bool>`。Python 的 `get` 與其
//! 寫入快取只服務 `get`（engine 不呼叫），不承接。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use sw_pal::traits::{FsOps, PalError, PalErrorKind, PalResult};
use sw_proto::pycompat::PyErr;
use sw_proto::pyfmt::py_repr_str;
use sw_proto::pyjson::{self, PyValue};

use super::counter::{read_text_utf8, valid_rule_id};
use super::pyops::py_eq;
use super::schema::{validate_rule_dict, Rule, RuleError};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FailedLoad {
    pub path: String,
    pub reason: String,
}

pub struct LoadResult {
    pub rules: Vec<Arc<Rule>>,
    pub failed: Vec<FailedLoad>,
}

/// 升冪（RPC-H-13）。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DiffResult {
    pub added: Vec<String>,
    pub removed: Vec<String>,
    pub changed: Vec<String>,
}

/// 檔案式註冊表：`EVENTS_DIR/<rule_id>.json`。
pub struct RuleRegistry {
    root: PathBuf,
    fs: Arc<dyn FsOps>,
}

/// `Rule` → `INVALID_RULE_SCHEMA`（Schema 與 Value）；`Type`／`Io` → `EXCEPTION`。
#[derive(Debug)]
pub enum UpsertError {
    Rule(RuleError),
    Io(PalError),
}

/// `str(OSError)`（含 `filename`）：`<py_message>: <repr(path)>`。
fn os_error_with_path(e: &PalError, p: &Path) -> String {
    format!("{}: {}", e.py_message, py_repr_str(&p.to_string_lossy()))
}

fn rule_error_text(e: &RuleError) -> &str {
    match e {
        RuleError::Schema(m) | RuleError::Value(m) | RuleError::Type(m) => m,
    }
}

impl RuleRegistry {
    /// `os.makedirs(root, exist_ok=True)`。
    pub fn new(root: PathBuf, fs: Arc<dyn FsOps>) -> PalResult<Self> {
        fs.mkdirs(&root)?;
        Ok(Self { root, fs })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn path_for(&self, rule_id: &str) -> PathBuf {
        self.root.join(format!("{rule_id}.json"))
    }

    /// `sorted(os.listdir(root))` 中 `*.json`：讀取／解析失敗 → `read/parse failed: <exc>`，驗證失敗 → `str(exc)`（DF-09）。
    pub fn load_all(&self) -> Result<LoadResult, PyErr> {
        let mut rules = Vec::new();
        let mut failed = Vec::new();
        let mut entries = match self.fs.list_dir(&self.root) {
            Ok(e) => e,
            Err(e) if e.kind == PalErrorKind::NotFound => {
                return Ok(LoadResult { rules, failed });
            }
            Err(e) => {
                return Err(PyErr {
                    message: os_error_with_path(&e, &self.root),
                })
            }
        };
        entries.sort();
        for entry in entries {
            if !entry.ends_with(".json") {
                continue;
            }
            let full = self.root.join(&entry);
            let full_s = full.to_string_lossy().into_owned();
            let fail = |reason: String| FailedLoad {
                path: full_s.clone(),
                reason,
            };
            let text = match read_text_utf8(&*self.fs, &full) {
                Ok(Ok(t)) => t,
                Ok(Err(e)) => {
                    failed.push(fail(format!(
                        "read/parse failed: {}",
                        os_error_with_path(&e, &full)
                    )));
                    continue;
                }
                // UnicodeDecodeError 為 ValueError 子類。
                Err(e) => {
                    failed.push(fail(format!("read/parse failed: {}", e.message)));
                    continue;
                }
            };
            let data = match pyjson::loads(&text) {
                Ok(v) => v,
                Err(pyjson::JsonError::Decode(e)) => {
                    failed.push(fail(format!("read/parse failed: {e}")));
                    continue;
                }
                Err(e @ pyjson::JsonError::Recursion) => {
                    return Err(PyErr {
                        message: e.to_string(),
                    })
                }
            };
            match validate_rule_dict(&data) {
                Ok(rule) => rules.push(Arc::new(rule)),
                Err(e) => failed.push(fail(rule_error_text(&e).to_string())),
            }
        }
        Ok(LoadResult { rules, failed })
    }

    /// 驗證後寫 `<path>.tmp`（`json.dump(dict(rule.raw), ensure_ascii=False, indent=2, sort_keys=True)`）→ replace（DF-18）。
    pub fn upsert(&self, obj: &PyValue) -> Result<Arc<Rule>, UpsertError> {
        let rule = validate_rule_dict(obj).map_err(UpsertError::Rule)?;
        let path = self.path_for(&rule.rule_id);
        let mut tmp = path.clone().into_os_string();
        tmp.push(".tmp");
        let tmp = PathBuf::from(tmp);
        let body = pyjson::dumps_dict(
            &rule.raw,
            &pyjson::DumpOpts {
                sep: pyjson::Sep::Indent(2),
                ensure_ascii: false,
                sort_keys: true,
                allow_nan: true,
            },
        );
        self.fs
            .create_truncate(&tmp, body.as_bytes())
            .map_err(UpsertError::Io)?;
        self.fs.sync_file(&tmp).map_err(UpsertError::Io)?;
        self.fs.replace(&tmp, &path).map_err(UpsertError::Io)?;
        Ok(Arc::new(rule))
    }

    /// DF-01：不合法 rule_id 直接 `false`（不碰檔案系統）；不存在 → `false`。
    pub fn delete(&self, rule_id: &str) -> PalResult<bool> {
        if !valid_rule_id(rule_id) {
            return Ok(false);
        }
        self.fs.remove_file_if_exists(&self.path_for(rule_id))
    }

    /// 與磁碟現況比較：新增／移除／`raw` 不同（Python `!=`）的 rule_id，升冪（RPC-H-13）。
    pub fn diff_against(&self, prev: &[Arc<Rule>]) -> Result<DiffResult, PyErr> {
        let prev: BTreeMap<&str, &Arc<Rule>> =
            prev.iter().map(|r| (r.rule_id.as_str(), r)).collect();
        let current = self.load_all()?.rules;
        let current: BTreeMap<&str, &Arc<Rule>> =
            current.iter().map(|r| (r.rule_id.as_str(), r)).collect();
        let mut diff = DiffResult::default();
        for (id, rule) in &current {
            match prev.get(id) {
                None => diff.added.push((*id).to_string()),
                Some(p) => {
                    let (a, b) = (
                        PyValue::Dict(rule.raw.clone()),
                        PyValue::Dict(p.raw.clone()),
                    );
                    if !py_eq(&a, &b) {
                        diff.changed.push((*id).to_string());
                    }
                }
            }
        }
        diff.removed = prev
            .keys()
            .filter(|id| !current.contains_key(*id))
            .map(|id| (*id).to_string())
            .collect();
        Ok(diff)
    }
}
