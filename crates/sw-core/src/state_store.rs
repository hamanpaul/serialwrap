//! P5-T15：`state.json` 單一寫入者（`StateStore`）、記憶體模型（`StateModel`）與位元組相容的載入／序列化。
//!
//! Python 對照：`sw_core/session_manager.py` 的 `_load_state`／`_save_state`。
//! - 載入：檔案不存在 → 預設狀態；讀取失敗 → `StateLoadError`（daemon 拒絕啟動，exit 3，原檔不動）；
//!   JSON 損毀（含非 UTF-8）→ 改名 `<p>.corrupt` 並以空狀態啟動（WARNING）；合法 JSON 但非 object → 空狀態、
//!   無備份（#95）；`aliases` 列無法以 `dict(v)` 轉換 → 與 Python 相同的未捕捉例外（exit 1，#12）。
//! - 序列化：`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"`；只寫 5 個 key，
//!   未知 key 於存檔時丟棄（WAL-D14）。
//! - 寫入：`FsOps::atomic_write(p, data, Some(0o600))`（同目錄 `state.json.tmp.` 前綴、fsync、replace、目錄 fsync；
//!   Windows 由 PAL 以 PermissionError 退避重試），目錄不存在時先 `mkdirs` 再寫（對應 `os.makedirs(exist_ok=True)`）。
//!   每次 commit 一律實際寫入，即使內容相同（#13，`Touch` 語意）。

use std::collections::HashSet;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use indexmap::IndexMap;
use sw_pal::traits::{FsOps, PalError, PalErrorKind};
use sw_proto::pycompat::{decode_utf8_strict, py_strip};
use sw_proto::pyfmt::py_repr_str;
use sw_proto::pyjson::{self, DISK_STABLE, JsonError, PyDict, PyValue};

/// released 列（`state.json` 的 released 區段）。
#[derive(Clone, Debug, PartialEq)]
pub struct ReleasedRow {
    pub by_id: PyValue,
    pub released_by: PyValue,
    pub released_at: PyValue,
    pub reason: PyValue,
}

impl ReleasedRow {
    fn to_dict(&self) -> PyDict {
        let mut d = PyDict::new();
        d.insert("by_id", self.by_id.clone());
        d.insert("released_by", self.released_by.clone());
        d.insert("released_at", self.released_at.clone());
        d.insert("reason", self.reason.clone());
        d
    }

    /// `by_id` 為非空字串時回傳（Python `isinstance(by_id, str) and by_id`）。
    pub fn by_id_str(&self) -> Option<&str> {
        match &self.by_id {
            PyValue::Str(s) if !s.is_empty() => Some(s.as_str()),
            _ => None,
        }
    }
}

/// 狀態變更（一次 commit 的增量）。
#[derive(Clone, Debug)]
pub enum StateDelta {
    Released { sid: String, row: ReleasedRow },
    Unreleased { sid: String },
    Binding { sid: String, by_id: Option<String> },
    Pin { device_key: String, profile: Option<String> },
    Detected { by_id: String, profile: String },
    AliasSet { alias: String, row: Option<PyDict> },
    Touch,
}

/// `state.json` 載入結果（Task 15 Produces）。
#[derive(Clone, Debug, Default, PartialEq)]
pub struct LoadedState {
    pub aliases: IndexMap<String, PyDict>,
    pub bindings: IndexMap<String, String>,
    pub released: IndexMap<String, ReleasedRow>,
    pub profile_pins: IndexMap<String, String>,
    pub profile_detected: IndexMap<String, String>,
    /// 本次載入遇到孤立 surrogate（以 `U+10F800..` 標記承載；啟動存檔前據此重現 Python 的 `UnicodeEncodeError`）。
    pub lone_surrogates: bool,
}

/// `StateLoadError` 的來源分類（決定 daemon 的 exit code 與 stderr 形狀）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StateLoadErrorKind {
    /// 既有 state.json 讀取失敗（Python `StateLoadError`）：stderr `serialwrapd: <message>`、exit 3。
    ReadFailed,
    /// Python 於 `_load_state` 拋出未捕捉例外（如 `aliases` 列無法 `dict(v)`，#12）：
    /// stderr 首行 `Traceback (most recent call last):`、末行 `<message>`（`TypeError: ...`）、exit 1。
    Uncaught,
}

/// Python 未捕捉例外時 stderr 的首行。
pub const PY_TRACEBACK_HEAD: &str = "Traceback (most recent call last):";

/// `state.json` 載入錯誤（Display 即 Python `StateLoadError` 訊息；`Uncaught` 時為例外末行）。
#[derive(Debug)]
pub struct StateLoadError {
    pub message: String,
    pub kind: StateLoadErrorKind,
}

impl StateLoadError {
    /// daemon 啟動失敗的 exit code（Python 基線：`StateLoadError` → 3；未捕捉例外 → 1）。
    pub fn exit_code(&self) -> i32 {
        match self.kind {
            StateLoadErrorKind::ReadFailed => 3,
            StateLoadErrorKind::Uncaught => 1,
        }
    }

    /// daemon 啟動失敗時寫到 stderr 的文字（`Uncaught` 只保留 Python traceback 的首行與末行）。
    pub fn stderr_text(&self) -> String {
        match self.kind {
            StateLoadErrorKind::ReadFailed => format!("serialwrapd: {}\n", self.message),
            StateLoadErrorKind::Uncaught => format!("{PY_TRACEBACK_HEAD}\n{}\n", self.message),
        }
    }
}

impl std::fmt::Display for StateLoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for StateLoadError {}

/// `str(OSError)`：有 errno／winerror 時附上 `: '<filename>'`（Python 帶檔名的 OSError 格式）。
fn os_error_text(e: &PalError, path: &Path) -> String {
    if e.os_code.is_some() || e.winerror.is_some() {
        format!("{}: {}", e.py_message, py_repr_str(&path.to_string_lossy()))
    } else {
        e.py_message.clone()
    }
}

fn corrupt_path(path: &Path) -> PathBuf {
    let mut s: OsString = path.as_os_str().to_owned();
    s.push(".corrupt");
    PathBuf::from(s)
}

/// Python 型別名稱（`type(v).__name__`）。
fn py_type_name(v: &PyValue) -> &'static str {
    match v {
        PyValue::Null => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::Str(_) => "str",
        PyValue::List(_) => "list",
        PyValue::Dict(_) => "dict",
    }
}

/// `json.dumps` 對非字串 dict key 的轉換（`None`→`null`、`True`→`true`、int／float 依 repr）。
fn json_key(v: &PyValue) -> Result<String, String> {
    match v {
        PyValue::Str(s) => Ok(s.clone()),
        PyValue::List(_) | PyValue::Dict(_) => {
            Err(format!("TypeError: unhashable type: '{}'", py_type_name(v)))
        }
        other => Ok(pyjson::dumps(other, &DISK_STABLE)),
    }
}

/// Python `dict(v)`（`AliasRegistry.load` 的 `{k: dict(v)}`）；失敗回 Python 例外末行（`TypeError: ...`）。
fn py_dict_of(v: &PyValue) -> Result<PyDict, String> {
    match v {
        PyValue::Dict(d) => Ok(d.clone()),
        PyValue::Str(s) => match s.chars().count() {
            0 => Ok(PyDict::new()),
            _ => Err(
                "ValueError: dictionary update sequence element #0 has length 1; 2 is required"
                    .to_string(),
            ),
        },
        PyValue::List(items) => {
            let mut out = PyDict::new();
            for (i, item) in items.iter().enumerate() {
                let pair: Vec<PyValue> = match item {
                    PyValue::List(xs) => xs.clone(),
                    PyValue::Str(s) => s.chars().map(|c| PyValue::Str(c.to_string())).collect(),
                    PyValue::Dict(d) => d.0.keys().map(|k| PyValue::Str(k.clone())).collect(),
                    _ => {
                        return Err(format!(
                            "TypeError: cannot convert dictionary update sequence element #{i} to a sequence"
                        ));
                    }
                };
                if pair.len() != 2 {
                    return Err(format!(
                        "ValueError: dictionary update sequence element #{i} has length {}; 2 is required",
                        pair.len()
                    ));
                }
                let key = json_key(&pair[0])?;
                out.insert(&key, pair[1].clone());
            }
            Ok(out)
        }
        other => Err(format!(
            "TypeError: '{}' object is not iterable",
            py_type_name(other)
        )),
    }
}

/// Python 對 `bindings`／`profile_pins`／`profile_detected` 的過濾：鍵值皆為字串、strip 後非空，存 strip 後的值。
fn str_map_stripped(v: Option<&PyValue>) -> Option<IndexMap<String, String>> {
    let PyValue::Dict(d) = v? else {
        return None;
    };
    let mut out = IndexMap::new();
    for (k, v) in &d.0 {
        let PyValue::Str(v) = v else { continue };
        let (k, v) = (py_strip(k), py_strip(v));
        if !k.is_empty() && !v.is_empty() {
            out.insert(k.to_string(), v.to_string());
        }
    }
    Some(out)
}

/// 讀取 `state.json`；損毀時回 `(LoadedState::default(), Some(.corrupt 路徑))`；WARNING 寫到 stderr。
pub fn load_state(
    path: &Path,
    fs: &dyn FsOps,
) -> Result<(LoadedState, Option<PathBuf>), StateLoadError> {
    load_state_with_warn(path, fs, &|msg| eprintln!("{msg}"))
}

/// CPython `json.detect_encoding`：BOM 優先（utf-32 → utf-16 → utf-8-sig），否則依前 2／4 bytes 的 NUL 樣式判斷。
fn detect_json_encoding(b: &[u8]) -> &'static str {
    if b.starts_with(&[0x00, 0x00, 0xFE, 0xFF]) || b.starts_with(&[0xFF, 0xFE, 0x00, 0x00]) {
        return "utf-32";
    }
    if b.starts_with(&[0xFE, 0xFF]) || b.starts_with(&[0xFF, 0xFE]) {
        return "utf-16";
    }
    if b.starts_with(&[0xEF, 0xBB, 0xBF]) {
        return "utf-8-sig";
    }
    if b.len() >= 4 {
        if b[0] == 0 {
            return if b[1] != 0 { "utf-16-be" } else { "utf-32-be" };
        }
        if b[1] == 0 {
            return if b[2] != 0 || b[3] != 0 { "utf-16-le" } else { "utf-32-le" };
        }
    } else if b.len() == 2 {
        if b[0] == 0 {
            return "utf-16-be";
        }
        if b[1] == 0 {
            return "utf-16-le";
        }
    }
    "utf-8"
}

/// 孤立 surrogate（U+D800..=U+DFFF）的私有表示起點：`U+10F800 + (u - 0xD800)`（2048 個，落在 U+10F800..=U+10FFFF）。
///
/// Python `json.loads(bytes)` 以 `surrogatepass` 解碼、`\uXXXX` 跳脫也可產生孤立 surrogate；Rust `String` 無法承載，
/// 故載入時改以此區段標記，啟動存檔前再依標記重現 Python `_save_state` 的 `UnicodeEncodeError`（審查 47c7b25 #1）。
/// 只有在本次載入確實遇到孤立 surrogate 時才解讀此區段（`LoadedState::lone_surrogates`）；同檔同時含此區段的真實字元屬已知限制。
const SURROGATE_MARK_BASE: u32 = 0x10F800;

fn surrogate_mark(u: u32) -> char {
    char::from_u32(SURROGATE_MARK_BASE + (u - 0xD800)).expect("U+10F800..=U+10FFFF 為合法 char")
}

/// 若 `c` 是孤立 surrogate 標記，回原 surrogate 值。
fn marked_surrogate(c: char) -> Option<u32> {
    let v = c as u32;
    (SURROGATE_MARK_BASE..=0x10FFFF).contains(&v).then(|| v - SURROGATE_MARK_BASE + 0xD800)
}

/// Python `_save_state` 以 UTF-8 文字模式寫入含孤立 surrogate 的 payload 時的例外末行；無標記回 `None`。
///
/// 位置為 payload（`json.dumps(...)+"\n"`）中第一個 surrogate 的字元索引（code point 計）。
pub fn lone_surrogate_encode_error(payload: &str) -> Option<String> {
    payload.chars().enumerate().find_map(|(i, c)| {
        marked_surrogate(c).map(|u| {
            format!("UnicodeEncodeError: 'utf-8' codec can't encode character '\\u{u:04x}' in position {i}: surrogates not allowed")
        })
    })
}

/// `bytes.decode(detect_encoding(b), "surrogatepass")`：孤立 surrogate 轉為標記字元，回 `(text, 是否遇到孤立 surrogate)`。
/// 錯誤訊息依 CPython 3.12 實測：位置以原始 `raw` 的 offset 計（含 BOM），UTF-32 多 byte 時為 `bytes in position S-E`。
fn json_bytes_to_text(raw: &[u8]) -> Result<(String, bool), String> {
    let codec = detect_json_encoding(raw);
    let marked = std::cell::Cell::new(false);
    let byte_err = |name: &str, s: usize, e: usize, reason: &str| -> String {
        if e == s {
            format!("'{name}' codec can't decode byte 0x{:02x} in position {s}: {reason}", raw[s])
        } else {
            format!("'{name}' codec can't decode bytes in position {s}-{e}: {reason}")
        }
    };
    let utf16 = |off: usize, be: bool, name: &str| -> Result<String, String> {
        let body = &raw[off..];
        let mut out = String::with_capacity(body.len() / 2);
        let unit = |i: usize| {
            let c = [body[i], body[i + 1]];
            u32::from(if be { u16::from_be_bytes(c) } else { u16::from_le_bytes(c) })
        };
        let mut i = 0;
        while i + 1 < body.len() {
            let u = unit(i);
            if (0xD800..=0xDBFF).contains(&u) && i + 3 < body.len() && (0xDC00..=0xDFFF).contains(&unit(i + 2)) {
                out.push(char::from_u32(0x10000 + ((u - 0xD800) << 10) + (unit(i + 2) - 0xDC00)).unwrap());
                i += 4;
            } else if (0xD800..=0xDFFF).contains(&u) {
                out.push(surrogate_mark(u));
                marked.set(true);
                i += 2;
            } else {
                out.push(char::from_u32(u).unwrap());
                i += 2;
            }
        }
        if i < body.len() {
            return Err(byte_err(name, off + i, off + body.len() - 1, "truncated data"));
        }
        Ok(out)
    };
    let utf32 = |off: usize, be: bool, name: &str| -> Result<String, String> {
        let body = &raw[off..];
        let mut out = String::with_capacity(body.len() / 4);
        let mut i = 0;
        while i + 3 < body.len() {
            let c = [body[i], body[i + 1], body[i + 2], body[i + 3]];
            let v = if be { u32::from_be_bytes(c) } else { u32::from_le_bytes(c) };
            if (0xD800..=0xDFFF).contains(&v) {
                out.push(surrogate_mark(v));
                marked.set(true);
            } else if let Some(ch) = char::from_u32(v) {
                out.push(ch);
            } else {
                return Err(byte_err(name, off + i, off + i + 3, "code point not in range(0x110000)"));
            }
            i += 4;
        }
        if i < body.len() {
            return Err(byte_err(name, off + i, off + body.len() - 1, "truncated data"));
        }
        Ok(out)
    };
    let text = match codec {
        "utf-32" => {
            let be = raw.starts_with(&[0x00, 0x00, 0xFE, 0xFF]);
            utf32(4, be, if be { "utf-32-be" } else { "utf-32-le" })
        }
        "utf-16" => {
            let be = raw.starts_with(&[0xFE, 0xFF]);
            utf16(2, be, if be { "utf-16-be" } else { "utf-16-le" })
        }
        "utf-16-be" => utf16(0, true, codec),
        "utf-16-le" => utf16(0, false, codec),
        "utf-32-be" => utf32(0, true, codec),
        "utf-32-le" => utf32(0, false, codec),
        "utf-8-sig" => utf8_surrogatepass(&raw[3..], &marked),
        _ => utf8_surrogatepass(raw, &marked),
    }?;
    Ok((text, marked.get()))
}

/// UTF-8 `surrogatepass`：完整的 `ED A0..BF 80..BF` 視為孤立 surrogate，其餘依嚴格 UTF-8（錯誤訊息不變）。
///
/// 先以等長的 U+FFFD（3 bytes）佔位交給嚴格解碼，故其餘錯誤的位置與 Python 相同；成功後再把佔位換回標記。
fn utf8_surrogatepass(b: &[u8], marked: &std::cell::Cell<bool>) -> Result<String, String> {
    let mut spots: Vec<(usize, u32)> = Vec::new();
    let mut i = 0;
    while i + 2 < b.len() {
        if b[i] == 0xED && (0xA0..=0xBF).contains(&b[i + 1]) && (0x80..=0xBF).contains(&b[i + 2]) {
            spots.push((i, 0xD000 | (u32::from(b[i + 1] & 0x3F) << 6) | u32::from(b[i + 2] & 0x3F)));
            i += 3;
        } else {
            i += 1;
        }
    }
    if spots.is_empty() {
        return decode_utf8_strict(b).map_err(|e| e.to_string());
    }
    let mut patched = b.to_vec();
    for &(at, _) in &spots {
        patched[at..at + 3].copy_from_slice("\u{FFFD}".as_bytes());
    }
    let text = decode_utf8_strict(&patched).map_err(|e| e.to_string())?;
    marked.set(true);
    let mut out = String::with_capacity(text.len());
    let mut next = spots.iter().peekable();
    for (at, c) in text.char_indices() {
        match next.peek() {
            Some(&&(pos, u)) if pos == at => {
                out.push(surrogate_mark(u));
                next.next();
            }
            _ => out.push(c),
        }
    }
    Ok(out)
}

/// JSON 字串內的孤立 `\uXXXX` surrogate 跳脫改寫為標記字元（Python `py_scanstring` 的配對規則：high 後緊接 `\u`
/// 且為 low 才合併；其餘孤立）。沒有可改寫者回 `None`。只供載入使用：改寫後解析失敗時呼叫端改以原文取得錯誤訊息。
fn mark_lone_surrogate_escapes(text: &str) -> Option<String> {
    let b = text.as_bytes();
    let hex4 = |at: usize| -> Option<u32> {
        let h = b.get(at..at + 4)?;
        u32::from_str_radix(std::str::from_utf8(h).ok()?, 16).ok().filter(|_| h.iter().all(u8::is_ascii_hexdigit))
    };
    let mut out = String::with_capacity(text.len());
    let (mut i, mut last, mut in_str, mut changed) = (0, 0, false, false);
    while i < b.len() {
        match b[i] {
            b'"' => {
                in_str = !in_str;
                i += 1;
            }
            b'\\' if in_str => {
                if b.get(i + 1) == Some(&b'u') {
                    if let Some(u) = hex4(i + 2) {
                        let paired = (0xD800..=0xDBFF).contains(&u)
                            && b.get(i + 6..i + 8) == Some(b"\\u")
                            && hex4(i + 8).is_some_and(|l| (0xDC00..=0xDFFF).contains(&l));
                        if paired {
                            i += 12;
                            continue;
                        }
                        if (0xD800..=0xDFFF).contains(&u) {
                            out.push_str(&text[last..i]);
                            out.push(surrogate_mark(u));
                            i += 6;
                            last = i;
                            changed = true;
                            continue;
                        }
                    }
                }
                i += 2;
            }
            _ => i += 1,
        }
    }
    if !changed {
        return None;
    }
    out.push_str(&text[last..]);
    Some(out)
}

/// 同 [`load_state`]，WARNING 交給 `warn`（Python `logging.getLogger("serialwrap").warning`）。
pub fn load_state_with_warn(
    path: &Path,
    fs: &dyn FsOps,
    warn: &dyn Fn(&str),
) -> Result<(LoadedState, Option<PathBuf>), StateLoadError> {
    if !fs.exists(path) {
        return Ok((LoadedState::default(), None));
    }
    let raw = fs.read(path).map_err(|e| StateLoadError {
        message: format!(
            "無法讀取既有 state.json（{}）：{}；為避免以空狀態覆寫並遺失 RELEASED 交接（two-reader 風險），daemon 拒絕啟動。",
            path.display(),
            os_error_text(&e, path)
        ),
        kind: StateLoadErrorKind::ReadFailed,
    })?;
    // `json.loads(bytes)`：`json.detect_encoding` 判斷 UTF-8（含 utf-8-sig）／UTF-16／UTF-32 後解碼（審查 F54）。
    let mut lone_surrogates = false;
    let parsed = match json_bytes_to_text(&raw) {
        // bytes 路徑不做 str 的 BOM 檢查：解碼後仍以 U+FEFF 起頭（雙重 BOM）時，decoder 直接報
        // `Expecting value`（CPython `json.loads(bytes)` 實測；審查 F58）。
        Ok((text, _)) if text.starts_with('\u{feff}') => {
            Err(Ok("Expecting value: line 1 column 1 (char 0)".to_string()))
        }
        Ok((text, marked)) => {
            let escaped = mark_lone_surrogate_escapes(&text);
            let result = match &escaped {
                // 改寫會使字元位置位移：解析失敗時以原文重新解析取得與 Python 相同的錯誤訊息。
                Some(rewritten) => pyjson::loads(rewritten).or_else(|_| pyjson::loads(&text)),
                None => pyjson::loads(&text),
            };
            lone_surrogates = marked || escaped.is_some();
            result.map_err(|e| match e {
                JsonError::Recursion => Err(StateLoadError {
                    message: "RecursionError: maximum recursion depth exceeded".to_string(),
                    kind: StateLoadErrorKind::Uncaught,
                }),
                other => Ok(other.to_string()),
            })
        }
        Err(e) => Err(Ok(e)),
    };
    let obj = match parsed {
        Ok(v) => v,
        Err(Err(fatal)) => return Err(fatal),
        Err(Ok(msg)) => {
            let backup = corrupt_path(path);
            let backup = fs.replace(path, &backup).ok().map(|()| backup);
            let shown = backup
                .as_ref()
                .map(|b| b.display().to_string())
                .unwrap_or_else(|| "None".to_string());
            warn(&format!(
                "state.json 解析失敗（JSON 損毀），略過載入並備份至 {shown}：{msg}"
            ));
            return Ok((LoadedState::default(), backup));
        }
    };
    let PyValue::Dict(obj) = obj else {
        return Ok((LoadedState::default(), None));
    };

    let mut out = LoadedState::default();
    if let Some(PyValue::Dict(rows)) = obj.get("aliases") {
        let mut aliases = IndexMap::new();
        for (k, v) in &rows.0 {
            let row = py_dict_of(v).map_err(|message| StateLoadError {
                message,
                kind: StateLoadErrorKind::Uncaught,
            })?;
            aliases.insert(k.clone(), row);
        }
        out.aliases = aliases;
    }
    if let Some(PyValue::Dict(bindings)) = obj.get("bindings") {
        for (sid, by_id) in &bindings.0 {
            let PyValue::Str(by_id) = by_id else { continue };
            let (sid, by_id) = (py_strip(sid), py_strip(by_id));
            if !sid.is_empty() && !by_id.is_empty() {
                out.bindings.insert(sid.to_string(), by_id.to_string());
            }
        }
    }
    if let Some(PyValue::Dict(released)) = obj.get("released") {
        for (sid, meta) in &released.0 {
            let PyValue::Dict(meta) = meta else { continue };
            let field = |k: &str| meta.get(k).cloned().unwrap_or(PyValue::Null);
            out.released.insert(
                sid.clone(),
                ReleasedRow {
                    by_id: field("by_id"),
                    released_by: field("released_by"),
                    released_at: field("released_at"),
                    reason: field("reason"),
                },
            );
        }
    }
    if let Some(pins) = str_map_stripped(obj.get("profile_pins")) {
        out.profile_pins = pins;
    }
    if let Some(detected) = str_map_stripped(obj.get("profile_detected")) {
        out.profile_detected = detected;
    }
    out.lone_surrogates = lone_surrogates;
    Ok((out, None))
}

fn str_dict(m: &IndexMap<String, String>) -> PyValue {
    PyValue::Dict(PyDict(
        m.iter()
            .map(|(k, v)| (k.clone(), PyValue::Str(v.clone())))
            .collect(),
    ))
}

/// `state.json` 的序列化位元組（`DISK_STABLE` ＋ 文字模式換行；未知鍵於存檔時丟棄，WAL-D14）。
pub fn serialize_state(s: &LoadedState) -> Vec<u8> {
    serialize_state_with(s, sw_pal::fs_ops::TEXT_NEWLINE)
}

/// 同 [`serialize_state`]，換行由呼叫端指定（Python `_save_state` 文字模式：Windows `\r\n`；審查 F55）。
pub fn serialize_state_with(s: &LoadedState, newline: &[u8]) -> Vec<u8> {
    let mut d = PyDict::new();
    d.insert(
        "aliases",
        PyValue::Dict(PyDict(
            s.aliases
                .iter()
                .map(|(k, v)| (k.clone(), PyValue::Dict(v.clone())))
                .collect(),
        )),
    );
    d.insert("bindings", str_dict(&s.bindings));
    d.insert(
        "released",
        PyValue::Dict(PyDict(
            s.released
                .iter()
                .map(|(k, v)| (k.clone(), PyValue::Dict(v.to_dict())))
                .collect(),
        )),
    );
    d.insert("profile_pins", str_dict(&s.profile_pins));
    d.insert("profile_detected", str_dict(&s.profile_detected));
    let mut out = pyjson::dumps_dict(&d, &DISK_STABLE).into_bytes();
    out.extend_from_slice(newline);
    out
}

/// 記憶體狀態模型：`LoadedState` ＋ 已載入但未實體化的 released 列（DF-04）。
///
/// `state.released` 只放已實體化（session 處於 RELEASED）的列；`loaded_released` 為載入後尚未實體化者
/// （Python `_loaded_released`）。實體化失敗的列持續留在 `loaded_released`，跨 snapshot 保留並阻擋該 tty（#15）。
#[derive(Clone, Debug, Default)]
pub struct StateModel {
    state: LoadedState,
    loaded_released: IndexMap<String, ReleasedRow>,
}

impl StateModel {
    pub fn new(loaded: LoadedState) -> StateModel {
        let mut state = loaded;
        let loaded_released = std::mem::take(&mut state.released);
        StateModel {
            state,
            loaded_released,
        }
    }

    pub fn apply(&mut self, d: StateDelta) {
        match d {
            StateDelta::Released { sid, row } => {
                self.loaded_released.shift_remove(&sid);
                self.state.released.insert(sid, row);
            }
            StateDelta::Unreleased { sid } => {
                self.loaded_released.shift_remove(&sid);
                self.state.released.shift_remove(&sid);
            }
            StateDelta::Binding { sid, by_id } => match by_id {
                Some(b) => {
                    self.state.bindings.insert(sid, b);
                }
                None => {
                    self.state.bindings.shift_remove(&sid);
                }
            },
            StateDelta::Pin {
                device_key,
                profile,
            } => match profile {
                Some(p) => {
                    self.state.profile_pins.insert(device_key, p);
                }
                None => {
                    self.state.profile_pins.shift_remove(&device_key);
                }
            },
            StateDelta::Detected { by_id, profile } => {
                self.state.profile_detected.insert(by_id, profile);
            }
            StateDelta::AliasSet { alias, row } => match row {
                Some(r) => {
                    self.state.aliases.insert(alias, r);
                }
                None => {
                    self.state.aliases.shift_remove(&alias);
                }
            },
            StateDelta::Touch => {}
        }
    }

    /// 存檔快照：released ＝ 已實體化者 ∪ 未實體化的已載入列（同 sid 以實體化者為準）。
    pub fn snapshot(&self) -> LoadedState {
        let mut s = self.state.clone();
        for (sid, row) in &self.loaded_released {
            s.released.entry(sid.clone()).or_insert_with(|| row.clone());
        }
        s
    }

    /// 快照中 released 列的非空字串 `by_id`（阻擋 attach 的 tty 集合）。
    pub fn released_by_ids(&self) -> HashSet<String> {
        self.state
            .released
            .values()
            .chain(self.loaded_released.values())
            .filter_map(|r| r.by_id_str().map(str::to_string))
            .collect()
    }

    /// 已載入但尚未實體化的 released 列（Python `_loaded_released`；SessionManager 啟動時逐列嘗試實體化）。
    pub fn unmaterialized_released(&self) -> &IndexMap<String, ReleasedRow> {
        &self.loaded_released
    }
}

type Hook = Box<dyn Fn() + Send + Sync>;

struct CommitJob {
    snap: LoadedState,
    done: crossbeam_channel::Sender<Result<(), PalError>>,
}

/// `state.json` 的 `StateStore`（thread `serialwrap-state-store`，bounded(64)；Task 15 Produces）。
///
/// 所有寫入經同一 thread 依序 `atomic_write`（Python `_state_io_lock` 的 last-writer-wins 序列化）。
/// 失敗以 `Err` 回報給呼叫端，呼叫端可再次 commit 重試（#23 的重試入口；磁碟舊檔不變）。
pub struct StateStore {
    tx: Mutex<Option<crossbeam_channel::Sender<CommitJob>>>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

fn write_state(fs: &dyn FsOps, path: &Path, data: &[u8]) -> Result<(), PalError> {
    match fs.atomic_write(path, data, Some(0o600)) {
        Err(e) if e.kind == PalErrorKind::NotFound => {
            // `os.makedirs(state_dir, exist_ok=True)`：目錄不存在時建立後重寫一次。
            let parent = path
                .parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or(Path::new("."));
            fs.mkdirs(parent)?;
            fs.atomic_write(path, data, Some(0o600))
        }
        other => other,
    }
}

impl StateStore {
    pub fn spawn(path: PathBuf, fs: Arc<dyn FsOps>) -> StateStore {
        Self::spawn_inner(path, fs, None, None)
    }

    /// 測試掛鉤（Review Focus 1）：`before_commit`／`after_commit` 於 store thread 中、`atomic_write` 前後呼叫。
    #[cfg(feature = "test-fakes")]
    pub fn spawn_with_hooks(
        path: PathBuf,
        fs: Arc<dyn FsOps>,
        before_commit: Option<Box<dyn Fn() + Send + Sync>>,
        after_commit: Option<Box<dyn Fn() + Send + Sync>>,
    ) -> StateStore {
        Self::spawn_inner(path, fs, before_commit, after_commit)
    }

    fn spawn_inner(
        path: PathBuf,
        fs: Arc<dyn FsOps>,
        before: Option<Hook>,
        after: Option<Hook>,
    ) -> StateStore {
        let (tx, rx) = crossbeam_channel::bounded::<CommitJob>(64);
        let handle = std::thread::Builder::new()
            .name("serialwrap-state-store".to_string())
            .spawn(move || {
                for job in rx.iter() {
                    if let Some(h) = &before {
                        h();
                    }
                    let data = serialize_state(&job.snap);
                    let result = write_state(&*fs, &path, &data);
                    if let Some(h) = &after {
                        h();
                    }
                    let _ = job.done.send(result);
                }
            })
            .expect("無法建立 serialwrap-state-store thread");
        StateStore {
            tx: Mutex::new(Some(tx)),
            thread: Mutex::new(Some(handle)),
        }
    }

    /// 非同步提交：結果經 `done` 回傳（actor 以 `ControlEvent::StateCommitted` 收斂）。
    pub fn commit(&self, snap: LoadedState, done: crossbeam_channel::Sender<Result<(), PalError>>) {
        let tx = self
            .tx
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone();
        let job = CommitJob { snap, done };
        let failed = match tx {
            Some(tx) => tx.send(job).err().map(|e| e.into_inner()),
            None => Some(job),
        };
        if let Some(job) = failed {
            let _ = job.done.send(Err(PalError::plain(
                PalErrorKind::BrokenPipe,
                "state store 已停止",
            )));
        }
    }

    /// 同步提交；失敗回 `Err`（管理操作以此轉 `Err(PyErr)`）。
    pub fn commit_sync(&self, snap: LoadedState) -> Result<(), PalError> {
        let (tx, rx) = crossbeam_channel::bounded(1);
        self.commit(snap, tx);
        rx.recv().unwrap_or_else(|_| {
            Err(PalError::plain(
                PalErrorKind::BrokenPipe,
                "state store 已停止",
            ))
        })
    }
}

impl Drop for StateStore {
    fn drop(&mut self) {
        self.tx.lock().unwrap_or_else(|e| e.into_inner()).take();
        if let Some(h) = self.thread.lock().unwrap_or_else(|e| e.into_inner()).take() {
            if h.thread().id() != std::thread::current().id() {
                let _ = h.join();
            }
        }
    }
}
