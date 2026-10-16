//! `config_types`：profile YAML 載入與 `RuntimeConfig` 讀取（P3-T6 紅燈 stub，行為尚未實作）。
//!
//! 逐條承接 `docs/rust-refactor/inventory/09-events-transfer.md` §2（`sw_core/config.py`）。
//! 本檔目前為紅燈階段的 stub：公開介面依 plan Task 6 固定，行為本體一律 `todo!`，
//! 實作（Step 4）時移除 `allow(unused_variables)` 與所有 `todo!`。
#![allow(unused_variables)]

use crate::pycompat::{PyOsError, UnicodeDecodeError};
use crate::pypath::PathFlavor;
use crate::pyjson::PyInt;
use crate::yaml11::{YMap, YValue, YamlError};

/// `sw_core/config.py` `UartProfile`（09 §2.1）。
pub struct UartProfile {
    pub baud: PyInt,
    pub data_bits: PyInt,
    pub parity: String,
    pub stop_bits: PyInt,
    pub flow_control: String,
    pub xonxoff: bool,
}

/// `ProfileTemplate`（09 §2.1）。欄位順序與 Python dataclass 相同。
pub struct ProfileTemplate {
    pub profile_name: String,
    pub platform: String,
    pub prompt_regex: String,
    pub login_regex: String,
    pub password_regex: String,
    pub post_login_cmd: String,
    pub ready_probe: String,
    pub username: Option<String>,
    pub user_env: Option<String>,
    pub pass_env: Option<String>,
    pub env_file: Option<String>,
    pub console_unlock_env: Option<String>,
    pub console_unlock_idle_s: f64,
    pub timeout_s: f64,
    pub quiet_window_s: f64,
    pub hard_timeout_s: f64,
    pub log_dir: Option<String>,
    pub uart: UartProfile,
    pub bootloader_prompts: Vec<String>,
    pub max_console_line_chars: Option<PyInt>,
}

impl ProfileTemplate {
    /// Python `command_capable`：`ready_probe.strip()` 非空。
    pub fn command_capable(&self) -> bool {
        todo!("P3-T6: ProfileTemplate::command_capable")
    }
}

/// `SessionProfile`：`com`／`act_no`／`alias`／`device_by_id` 加上已套用 `_merge_session` 覆寫後的 template。
pub struct SessionProfile {
    pub com: String,
    pub act_no: PyInt,
    pub alias: String,
    pub device_by_id: String,
    /// 已套用 `_merge_session`（config.py:243）逐 target 覆寫後的值，不是原 template。
    pub template: ProfileTemplate,
}

/// `load_profiles()` 的回傳值（`LoadResult`）。
pub struct LoadResult {
    pub profiles: Vec<SessionProfile>,
    pub templates: Vec<ProfileTemplate>,
    pub max_sessions: PyInt,
}

/// `load_profiles` 與 `RuntimeConfig::from_text` 的錯誤分類。
///
/// `Yaml`：PyYAML `YAMLError`；`Value`：Python `ValueError` 訊息；`Os`：`OSError`；`Decode`：UTF-8 strict 解碼錯誤。
#[derive(Debug)]
pub enum ConfigError {
    Yaml(YamlError),
    Value(String),
    Os(PyOsError),
    Decode(UnicodeDecodeError),
}

/// 檔案系統存取抽象（測試可注入 fake；生產實作於 sw-pal 之外的 client 側）。
pub trait ProfileFs {
    fn flavor(&self) -> PathFlavor;
    fn is_dir(&self, p: &str) -> bool;
    /// `os.listdir`（未排序）。
    fn list_dir(&self, p: &str) -> Result<Vec<String>, PyOsError>;
    fn read_bytes(&self, p: &str) -> Result<Vec<u8>, PyOsError>;
    fn expanduser(&self, p: &str) -> String;
}

/// 載入 profile 目錄（09 §2.6 逐步）。
pub fn load_profiles(profile_dir: &str, fs: &dyn ProfileFs) -> Result<LoadResult, ConfigError> {
    todo!("P3-T6: load_profiles")
}

/// `RuntimeConfig`：`config.yaml` 的 mapping 原始資料。
pub struct RuntimeConfig {
    pub data: YMap,
}

impl RuntimeConfig {
    /// `None`＝檔案不存在 → 空；非 mapping → 空；YAML 錯 → `Err`。
    pub fn from_text(text: Option<&str>) -> Result<RuntimeConfig, ConfigError> {
        todo!("P3-T6: RuntimeConfig::from_text")
    }

    /// `data.get("supervision_mode")`。
    pub fn mode(&self) -> Option<&YValue> {
        todo!("P3-T6: RuntimeConfig::mode")
    }

    /// `data.get("socket_path")`（不驗型別，Q-18）。
    pub fn socket_path(&self) -> Option<&YValue> {
        todo!("P3-T6: RuntimeConfig::socket_path")
    }
}
