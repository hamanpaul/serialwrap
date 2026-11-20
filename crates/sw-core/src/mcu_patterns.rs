//! `mcu.patterns` registry（Slice 2.1 Task 18 的骨架由 P8-T2 承接為完整 registry）。
//!
//! 行為逐字對照 Python `sw_core/mcu_patterns.py`（`McuPattern`／`McuPatternRegistry`／`_hex_to_bytes`）。
//! golden：`contracts/golden/mcu_patterns.ndjson`（由 `scripts/rust/capture_mcu_golden.py` 產生）。
//! RPC 列格式對照 Python：`{"family", "probe": p.probe.hex(" "), "expect": p.expect.hex(" "), "baud"}`，不含 `timeout_ms`。

use indexmap::IndexMap;
use sw_proto::pycompat::{py_int_parse, py_truthy};
use sw_proto::pydict;
use sw_proto::pyfmt::py_str;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

/// Python `_DEFAULTS` 的預設 pattern（TI CC26xx UART bootloader sync／ACK；已審核的非破壞性）。
const DEFAULT_FAMILY: &str = "ti-cc26xx";
const DEFAULT_PROBE: [u8; 2] = [0x55, 0x55];
const DEFAULT_EXPECT: [u8; 2] = [0x00, 0xcc];
const DEFAULT_BAUD: u32 = 115_200;
const DEFAULT_TIMEOUT_MS: u64 = 500;

/// MCU 韌體升級線路的探測樣式（Python `McuPattern`）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct McuPattern {
    pub family: String,
    pub probe: Vec<u8>,
    pub expect: Vec<u8>,
    pub baud: u32,
    pub timeout_ms: u64,
    pub non_destructive: bool,
}

/// 載入 pattern 失敗的原因；每個變體對應一個 Python 例外類別（見 [`McuPatternError::py_exc`]）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum McuPatternError {
    /// `pattern 'x' 未通過 non_destructive 審核，拒絕載入`（ValueError）。
    NotNonDestructive { family: String },
    /// 缺少必要鍵（KeyError）。
    MissingKey(&'static str),
    /// `bytes.fromhex` 解析失敗（ValueError）。
    Hex(String),
    /// `int()` 無法解析（ValueError）；亦含 Rust 端無法表示的範圍外數值。
    Int(String),
    /// `int()` 不支援的型別（TypeError）。
    IntType(String),
}

impl McuPatternError {
    /// 對應的 Python 例外類別名（golden 的 `{"error": ...}` 欄位）。
    pub fn py_exc(&self) -> &'static str {
        match self {
            McuPatternError::NotNonDestructive { .. }
            | McuPatternError::Hex(_)
            | McuPatternError::Int(_) => "ValueError",
            McuPatternError::MissingKey(_) => "KeyError",
            McuPatternError::IntType(_) => "TypeError",
        }
    }
}

impl McuPattern {
    /// 建構並套用 non_destructive 守衛（Python `__post_init__`）。
    pub fn new(
        family: String,
        probe: Vec<u8>,
        expect: Vec<u8>,
        baud: u32,
        timeout_ms: u64,
        non_destructive: bool,
    ) -> Result<Self, McuPatternError> {
        if !non_destructive {
            return Err(McuPatternError::NotNonDestructive { family });
        }
        Ok(McuPattern {
            family,
            probe,
            expect,
            baud,
            timeout_ms,
            non_destructive,
        })
    }

    /// Python `McuPattern.from_dict`：鍵缺失與型別轉換行為逐字對照。
    ///
    /// 求值順序與 Python 的關鍵字參數相同（family、probe、expect、baud、timeout_ms、non_destructive），
    /// 再進入守衛，故錯誤類別與 Python 一致。
    pub fn from_dict(d: &PyDict) -> Result<Self, McuPatternError> {
        let family = py_str(required(d, "family")?);
        let probe = hex_to_bytes(&py_str(required(d, "probe")?))?;
        let expect = hex_to_bytes(&py_str(required(d, "expect")?))?;
        let baud = match d.get("baud") {
            Some(v) => to_u32(py_int(v)?, "baud")?,
            None => DEFAULT_BAUD,
        };
        let timeout_ms = match d.get("timeout_ms") {
            Some(v) => to_u64(py_int(v)?, "timeout_ms")?,
            None => DEFAULT_TIMEOUT_MS,
        };
        let non_destructive = d.get("non_destructive").is_some_and(py_truthy);
        McuPattern::new(family, probe, expect, baud, timeout_ms, non_destructive)
    }
}

/// Python `_hex_to_bytes`：先去 ASCII 空白、再去小寫 `0x`，再依 `bytes.fromhex` 解析。
pub fn hex_to_bytes(s: &str) -> Result<Vec<u8>, McuPatternError> {
    let cleaned = s.replace(' ', "").replace("0x", "");
    let bytes = cleaned.as_bytes();
    let mut out = Vec::with_capacity(bytes.len() / 2);
    let mut i = 0;
    while i < bytes.len() {
        // bytes.fromhex 只在位元組對之間略過 ASCII 空白（Py_ISSPACE）。
        if matches!(bytes[i], b'\t' | b'\n' | 0x0b | 0x0c | b'\r') {
            i += 1;
            continue;
        }
        let hi = hex_digit(bytes[i])
            .ok_or_else(|| McuPatternError::Hex(format!("non-hexadecimal number at byte {i}")))?;
        let lo = bytes
            .get(i + 1)
            .and_then(|&b| hex_digit(b))
            .ok_or_else(|| {
                McuPatternError::Hex(format!("non-hexadecimal number at byte {}", i + 1))
            })?;
        out.push((hi << 4) | lo);
        i += 2;
    }
    Ok(out)
}

fn hex_digit(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

/// Python `bytes.hex(" ")`：空白分隔的小寫十六進位。
pub fn bytes_hex_spaced(b: &[u8]) -> String {
    b.iter()
        .map(|x| format!("{x:02x}"))
        .collect::<Vec<_>>()
        .join(" ")
}

/// Python `_DEFAULTS` 的單一預設 pattern。
fn default_pattern() -> McuPattern {
    McuPattern {
        family: DEFAULT_FAMILY.to_string(),
        probe: DEFAULT_PROBE.to_vec(),
        expect: DEFAULT_EXPECT.to_vec(),
        baud: DEFAULT_BAUD,
        timeout_ms: DEFAULT_TIMEOUT_MS,
        non_destructive: true,
    }
}

/// 取得必要鍵（Python `d["k"]`）。
fn required<'a>(d: &'a PyDict, key: &'static str) -> Result<&'a PyValue, McuPatternError> {
    d.get(key).ok_or(McuPatternError::MissingKey(key))
}

/// Python `int(v)`：bool→0/1、float 截斷、str 依 `int()` 語法、其餘型別 TypeError。
fn py_int(v: &PyValue) -> Result<i128, McuPatternError> {
    match v {
        PyValue::Bool(b) => Ok(i128::from(*b)),
        PyValue::Int(PyInt::Small(n)) => Ok(*n),
        PyValue::Int(PyInt::Big(s)) => Err(McuPatternError::Int(format!("{s} 超出可表示範圍"))),
        PyValue::Float(f) if f.is_finite() => Ok(f.trunc() as i128),
        PyValue::Float(f) => Err(McuPatternError::Int(format!(
            "cannot convert float {f} to integer"
        ))),
        PyValue::Str(s) => match py_int_parse(s) {
            Some(PyInt::Small(n)) => Ok(n),
            Some(PyInt::Big(b)) => Err(McuPatternError::Int(format!("{b} 超出可表示範圍"))),
            None => Err(McuPatternError::Int(format!(
                "invalid literal for int(): {s:?}"
            ))),
        },
        PyValue::Null | PyValue::List(_) | PyValue::Dict(_) => {
            Err(McuPatternError::IntType(format!(
                "int() argument must be a string, a bytes-like object or a real number, not {}",
                type_name(v)
            )))
        }
    }
}

fn type_name(v: &PyValue) -> &'static str {
    match v {
        PyValue::Null => "NoneType",
        PyValue::List(_) => "list",
        PyValue::Dict(_) => "dict",
        _ => "value",
    }
}

// Rust 端以固定寬度整數保存 baud／timeout；Python 接受的範圍外值在此拒絕（見回報的 SPEC-GAP）。
fn to_u32(n: i128, key: &'static str) -> Result<u32, McuPatternError> {
    u32::try_from(n).map_err(|_| McuPatternError::Int(format!("{key}={n} 超出 u32 範圍")))
}

fn to_u64(n: i128, key: &'static str) -> Result<u64, McuPatternError> {
    u64::try_from(n).map_err(|_| McuPatternError::Int(format!("{key}={n} 超出 u64 範圍")))
}

/// Python `McuPatternRegistry`：以 family 為 key；同名後者覆蓋值、位置保留首次插入處。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct McuPatternRegistry {
    by_family: IndexMap<String, McuPattern>,
}

impl McuPatternRegistry {
    /// 僅含內建預設 pattern（Python `McuPatternRegistry.default()`）。
    pub fn default_registry() -> Self {
        let p = default_pattern();
        let mut by_family = IndexMap::new();
        by_family.insert(p.family.clone(), p);
        McuPatternRegistry { by_family }
    }

    /// 預設 + rows 依序載入（Python `load(rows)`；`None` 等同空列表）。
    pub fn load(rows: Option<&[PyDict]>) -> Result<Self, McuPatternError> {
        let mut registry = Self::default_registry();
        for d in rows.unwrap_or(&[]) {
            let p = McuPattern::from_dict(d)?;
            registry.by_family.insert(p.family.clone(), p);
        }
        Ok(registry)
    }

    /// 以 family 取得（Python `get` 的 KeyError 對應為 `None`）。
    pub fn get(&self, family: &str) -> Option<&McuPattern> {
        self.by_family.get(family)
    }

    /// 所有已登錄 pattern，依插入序。
    pub fn all(&self) -> Vec<&McuPattern> {
        self.by_family.values().collect()
    }

    /// `mcu.patterns` 的 `patterns` 欄位：`[{"family","probe","expect","baud"}]`（不含 `timeout_ms`）。
    pub fn patterns_rpc(&self) -> PyValue {
        PyValue::List(
            self.by_family
                .values()
                .map(|p| {
                    PyValue::Dict(pydict! {
                        "family" => p.family.as_str(),
                        "probe" => bytes_hex_spaced(&p.probe),
                        "expect" => bytes_hex_spaced(&p.expect),
                        "baud" => i64::from(p.baud),
                    })
                })
                .collect(),
        )
    }
}
