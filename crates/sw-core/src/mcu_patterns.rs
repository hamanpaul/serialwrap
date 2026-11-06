//! `mcu.patterns` registry（Slice 2.1 Task 18 的骨架由 P8-T2 承接為完整 registry）。
//!
//! 行為逐字對照 Python `sw_core/mcu_patterns.py`（`McuPattern`／`McuPatternRegistry`／`_hex_to_bytes`）。
//! golden：`contracts/golden/mcu_patterns.ndjson`（由 `scripts/rust/capture_mcu_golden.py` 產生）。
//! RPC 列格式對照 Python：`{"family", "probe": p.probe.hex(" "), "expect": p.expect.hex(" "), "baud"}`，不含 `timeout_ms`。

use indexmap::IndexMap;
use sw_proto::pyjson::{PyDict, PyValue};

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
        todo!("P8-T2")
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
        todo!("P8-T2")
    }

    /// Python `McuPattern.from_dict`：鍵缺失與型別轉換行為逐字對照。
    pub fn from_dict(d: &PyDict) -> Result<Self, McuPatternError> {
        todo!("P8-T2")
    }
}

/// Python `_hex_to_bytes`：先去 ASCII 空白、再去小寫 `0x`，再依 `bytes.fromhex` 解析。
pub fn hex_to_bytes(s: &str) -> Result<Vec<u8>, McuPatternError> {
    todo!("P8-T2")
}

/// Python `bytes.hex(" ")`：空白分隔的小寫十六進位。
pub fn bytes_hex_spaced(b: &[u8]) -> String {
    todo!("P8-T2")
}

/// Python `McuPatternRegistry`：以 family 為 key；同名後者覆蓋值、位置保留首次插入處。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct McuPatternRegistry {
    by_family: IndexMap<String, McuPattern>,
}

impl McuPatternRegistry {
    /// 僅含內建預設 pattern（Python `McuPatternRegistry.default()`）。
    pub fn default_registry() -> Self {
        let p = McuPattern {
            family: DEFAULT_FAMILY.to_string(),
            probe: DEFAULT_PROBE.to_vec(),
            expect: DEFAULT_EXPECT.to_vec(),
            baud: DEFAULT_BAUD,
            timeout_ms: DEFAULT_TIMEOUT_MS,
            non_destructive: true,
        };
        let mut by_family = IndexMap::new();
        by_family.insert(p.family.clone(), p);
        McuPatternRegistry { by_family }
    }

    /// 預設 + rows 依序載入（Python `load(rows)`；`None` 等同空列表）。
    pub fn load(rows: Option<&[PyDict]>) -> Result<Self, McuPatternError> {
        todo!("P8-T2")
    }

    /// 以 family 取得（Python `get` 的 KeyError 對應為 `None`）。
    pub fn get(&self, family: &str) -> Option<&McuPattern> {
        todo!("P8-T2")
    }

    /// 所有已登錄 pattern，依插入序。
    pub fn all(&self) -> Vec<&McuPattern> {
        todo!("P8-T2")
    }

    /// `mcu.patterns` 的 `patterns` 欄位：`[{"family","probe","expect","baud"}]`（不含 `timeout_ms`）。
    pub fn patterns_rpc(&self) -> PyValue {
        todo!("P8-T2")
    }
}
