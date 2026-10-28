//! `config_types`：profile YAML 載入與 `RuntimeConfig` 讀取。
//!
//! 逐條承接 `docs/rust-refactor/inventory/09-events-transfer.md` §2（`sw_core/config.py`）。

use std::collections::BTreeMap;

use crate::pycompat::{
    decode_utf8_strict, py_float_parse, py_int_parse, py_strip, PyOsError, UnicodeDecodeError,
};
use crate::pyjson::PyInt;
use crate::pypath::PathFlavor;
use crate::yaml11::emit;
use crate::yaml11::{py_str_of, py_truthy_of, safe_load, YMap, YValue, YamlError};

/// `sw_core/config.py` `UartProfile`（09 §2.1）。
#[derive(Clone, Debug, PartialEq)]
pub struct UartProfile {
    pub baud: PyInt,
    pub data_bits: PyInt,
    pub parity: String,
    pub stop_bits: PyInt,
    pub flow_control: String,
    pub xonxoff: bool,
}

impl Default for UartProfile {
    fn default() -> Self {
        Self {
            baud: PyInt::Small(115200),
            data_bits: PyInt::Small(8),
            parity: "N".to_string(),
            stop_bits: PyInt::Small(1),
            flow_control: "none".to_string(),
            xonxoff: false,
        }
    }
}

/// `ProfileTemplate`（09 §2.1）。欄位順序與 Python dataclass 相同。
#[derive(Clone, Debug, PartialEq)]
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
    /// 開機完成訊號（#234，`config.py` 的 `boot_done_regex`）：`str(v or "").strip()`；空字串＝不設。
    pub boot_done_regex: String,
    pub max_console_line_chars: Option<PyInt>,
}

impl ProfileTemplate {
    /// Python `command_capable`：`ready_probe.strip()` 非空。
    pub fn command_capable(&self) -> bool {
        !py_strip(&self.ready_probe).is_empty()
    }
}

/// `SessionProfile`：`com`／`act_no`／`alias`／`device_by_id` 加上已套用 `_merge_session` 覆寫後的 template。
#[derive(Clone, Debug, PartialEq)]
pub struct SessionProfile {
    pub com: String,
    pub act_no: PyInt,
    pub alias: String,
    pub device_by_id: String,
    /// 已套用 `_merge_session`（config.py:243）逐 target 覆寫後的值，不是原 template。
    pub template: ProfileTemplate,
}

/// `load_profiles()` 的回傳值（`LoadResult`）。
#[derive(Clone, Debug, PartialEq)]
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

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConfigError::Yaml(e) => write!(f, "YamlError: {}", e.summary()),
            ConfigError::Value(msg) => write!(f, "ValueError: {msg}"),
            ConfigError::Os(e) => write!(f, "OSError: {e}"),
            ConfigError::Decode(e) => write!(f, "DecodeError: {e}"),
        }
    }
}

impl std::error::Error for ConfigError {}

impl From<YamlError> for ConfigError {
    fn from(e: YamlError) -> Self {
        ConfigError::Yaml(e)
    }
}

impl From<UnicodeDecodeError> for ConfigError {
    fn from(e: UnicodeDecodeError) -> Self {
        ConfigError::Decode(e)
    }
}

impl From<PyOsError> for ConfigError {
    fn from(e: PyOsError) -> Self {
        ConfigError::Os(e)
    }
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

// ---------------------------------------------------------------------------
// 轉型與輔助函式（對齊 sw_core/config.py §2.2）
// ---------------------------------------------------------------------------

fn dec_str_sub_one(s: &str) -> String {
    let mut digits: Vec<u8> = s.bytes().map(|b| b.saturating_sub(b'0')).collect();
    if digits.is_empty() {
        return "0".to_string();
    }
    let mut i = digits.len() - 1;
    loop {
        if digits[i] > 0 {
            digits[i] -= 1;
            break;
        }
        digits[i] = 9;
        if i == 0 {
            break;
        }
        i -= 1;
    }
    let start = digits
        .iter()
        .position(|&d| d != 0)
        .unwrap_or(digits.len() - 1);
    digits[start..]
        .iter()
        .map(|&d| (b'0' + d) as char)
        .collect()
}

fn as_int_val(v: Option<&YValue>, default: PyInt) -> PyInt {
    match v {
        Some(YValue::Int(n)) => n.clone(),
        Some(YValue::Bool(b)) => {
            if *b {
                PyInt::Small(1)
            } else {
                PyInt::Small(0)
            }
        }
        Some(YValue::Float(f)) => {
            if f.is_nan() || f.is_infinite() {
                default
            } else if *f >= i128::MIN as f64 && *f <= i128::MAX as f64 {
                PyInt::Small(*f as i128)
            } else {
                PyInt::Big(format!("{:.0}", f.trunc()))
            }
        }
        Some(YValue::Str(s)) => py_int_parse(s).unwrap_or(default),
        Some(YValue::Binary(b)) => {
            if let Ok(s) = std::str::from_utf8(b) {
                py_int_parse(s).unwrap_or(default)
            } else {
                default
            }
        }
        _ => default,
    }
}

fn as_float_val(v: Option<&YValue>, default: f64) -> f64 {
    match v {
        Some(YValue::Float(f)) => *f,
        Some(YValue::Int(n)) => match n {
            PyInt::Small(i) => *i as f64,
            PyInt::Big(s) => s.parse::<f64>().unwrap_or(default),
        },
        Some(YValue::Bool(b)) => {
            if *b {
                1.0
            } else {
                0.0
            }
        }
        Some(YValue::Str(s)) => py_float_parse(s).unwrap_or(default),
        Some(YValue::Binary(b)) => {
            if let Ok(s) = std::str::from_utf8(b) {
                py_float_parse(s).unwrap_or(default)
            } else {
                default
            }
        }
        _ => default,
    }
}

fn as_opt_str(v: Option<&YValue>) -> Option<String> {
    match v {
        None | Some(YValue::Null) => None,
        Some(val) => {
            let s = py_str_of(val);
            let trimmed = py_strip(&s);
            if trimmed.is_empty() {
                None
            } else {
                Some(trimmed.to_string())
            }
        }
    }
}

fn as_str_keep_empty(v: Option<&YValue>, default: &str) -> String {
    match v {
        None | Some(YValue::Null) => default.to_string(),
        Some(val) => py_strip(&py_str_of(val)).to_string(),
    }
}

fn as_max_console_line_chars(
    v: Option<&YValue>,
    default: Option<PyInt>,
) -> Result<Option<PyInt>, ConfigError> {
    match v {
        None | Some(YValue::Null) => Ok(default),
        Some(YValue::Bool(_)) => Err(ConfigError::Value(
            "max_console_line_chars 必須是正整數或 null".to_string(),
        )),
        Some(YValue::Int(n)) => {
            let positive = match n {
                PyInt::Small(x) => *x > 0,
                PyInt::Big(s) => !s.starts_with('-') && s != "0",
            };
            if positive {
                Ok(Some(n.clone()))
            } else {
                Err(ConfigError::Value(
                    "max_console_line_chars 必須是正整數或 null".to_string(),
                ))
            }
        }
        _ => Err(ConfigError::Value(
            "max_console_line_chars 必須是正整數或 null".to_string(),
        )),
    }
}

fn as_unlock_idle(v: Option<&YValue>, default: f64) -> Result<f64, ConfigError> {
    match v {
        None | Some(YValue::Null) => Ok(default),
        Some(YValue::Bool(_)) => Err(ConfigError::Value(
            "console_unlock_idle_s 必須是有限正數".to_string(),
        )),
        Some(YValue::Float(f)) => {
            if f.is_finite() && *f > 0.0 {
                Ok(*f)
            } else {
                Err(ConfigError::Value(
                    "console_unlock_idle_s 必須是有限正數".to_string(),
                ))
            }
        }
        Some(YValue::Int(n)) => {
            let f = match n {
                PyInt::Small(i) => *i as f64,
                PyInt::Big(s) => s.parse::<f64>().unwrap_or(0.0),
            };
            if f.is_finite() && f > 0.0 {
                Ok(f)
            } else {
                Err(ConfigError::Value(
                    "console_unlock_idle_s 必須是有限正數".to_string(),
                ))
            }
        }
        Some(YValue::Str(s)) => {
            if let Some(f) = py_float_parse(s) {
                if f.is_finite() && f > 0.0 {
                    return Ok(f);
                }
            }
            Err(ConfigError::Value(
                "console_unlock_idle_s 必須是有限正數".to_string(),
            ))
        }
        Some(YValue::Binary(b)) => {
            if let Ok(s) = std::str::from_utf8(b) {
                if let Some(f) = py_float_parse(s) {
                    if f.is_finite() && f > 0.0 {
                        return Ok(f);
                    }
                }
            }
            Err(ConfigError::Value(
                "console_unlock_idle_s 必須是有限正數".to_string(),
            ))
        }
        _ => Err(ConfigError::Value(
            "console_unlock_idle_s 必須是有限正數".to_string(),
        )),
    }
}

fn resolve_opt_path(v: Option<&YValue>, base_dir: &str, fs: &dyn ProfileFs) -> Option<String> {
    let s = as_opt_str(v)?;
    let expanded = fs.expanduser(&s);
    let flavor = fs.flavor();
    if crate::pypath::isabs(flavor, &expanded) {
        Some(crate::pypath::normpath(flavor, &expanded))
    } else {
        let joined = crate::pypath::join(flavor, base_dir, &[&expanded]);
        Some(crate::pypath::normpath(flavor, &joined))
    }
}

fn load_uart(raw: Option<&YValue>, default: Option<&UartProfile>) -> UartProfile {
    let base_baud = default
        .map(|d| d.baud.clone())
        .unwrap_or(PyInt::Small(115200));
    let base_data_bits = default
        .map(|d| d.data_bits.clone())
        .unwrap_or(PyInt::Small(8));
    let base_parity = default
        .map(|d| d.parity.clone())
        .unwrap_or_else(|| "N".to_string());
    let base_stop_bits = default
        .map(|d| d.stop_bits.clone())
        .unwrap_or(PyInt::Small(1));
    let base_flow_control = default
        .map(|d| d.flow_control.clone())
        .unwrap_or_else(|| "none".to_string());
    let base_xonxoff = default.map(|d| d.xonxoff).unwrap_or(false);

    let obj = match raw {
        Some(YValue::Map(m)) => Some(m),
        _ => None,
    };

    let baud = as_int_val(obj.and_then(|m| m.get_str("baud")), base_baud);
    let data_bits = as_int_val(obj.and_then(|m| m.get_str("data_bits")), base_data_bits);

    let parity = match obj
        .and_then(|m| m.get_str("parity"))
        .filter(|v| py_truthy_of(v))
    {
        Some(v) => py_str_of(v).to_uppercase(),
        None => base_parity.to_uppercase(),
    };

    let stop_bits = as_int_val(obj.and_then(|m| m.get_str("stop_bits")), base_stop_bits);

    let flow_control = match obj
        .and_then(|m| m.get_str("flow_control"))
        .filter(|v| py_truthy_of(v))
    {
        Some(v) => py_str_of(v).to_lowercase(),
        None => base_flow_control.to_lowercase(),
    };

    let xonxoff = match obj.and_then(|m| m.get_str("xonxoff")) {
        Some(v) => py_truthy_of(v),
        None => base_xonxoff,
    };

    UartProfile {
        baud,
        data_bits,
        parity,
        stop_bits,
        flow_control,
        xonxoff,
    }
}

fn template_from_map(
    name: &str,
    raw: &YMap,
    base_dir: &str,
    fs: &dyn ProfileFs,
) -> Result<ProfileTemplate, ConfigError> {
    let platform = match raw.get_str("platform").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_lowercase(),
        None => "prpl".to_string(),
    };

    let prompt_regex = match raw.get_str("prompt_regex").filter(|v| py_truthy_of(v)) {
        Some(v) => py_str_of(v),
        None => r"(?m)^root@prplOS:.*# ".to_string(),
    };

    let login_regex = match raw.get_str("login_regex").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_string(),
        None => r"(?mi)^login:\\s*$".to_string(),
    };

    let password_regex = match raw.get_str("password_regex").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_string(),
        None => r"(?mi)^password:\\s*$".to_string(),
    };

    let post_login_cmd = match raw.get_str("post_login_cmd").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_string(),
        None => "".to_string(),
    };

    let ready_probe = as_str_keep_empty(raw.get_str("ready_probe"), "echo __READY__${nonce}");

    let username = as_opt_str(raw.get_str("username"));

    let user_env = {
        let val = raw
            .get_str("user_env")
            .filter(|v| py_truthy_of(v))
            .or_else(|| raw.get_str("username_env").filter(|v| py_truthy_of(v)))
            .or_else(|| raw.get_str("login_env"));
        as_opt_str(val)
    };

    let pass_env = {
        let val = raw
            .get_str("pass_env")
            .filter(|v| py_truthy_of(v))
            .or_else(|| raw.get_str("password_env").filter(|v| py_truthy_of(v)))
            .or_else(|| raw.get_str("pw_env"));
        as_opt_str(val)
    };

    let env_file = resolve_opt_path(raw.get_str("env_file"), base_dir, fs);

    let console_unlock_env = as_opt_str(raw.get_str("console_unlock_env"));

    let console_unlock_idle_s = as_unlock_idle(raw.get_str("console_unlock_idle_s"), 5.0)?;

    let timeout_s = as_float_val(raw.get_str("timeout_s"), 10.0);
    let quiet_window_s = as_float_val(raw.get_str("quiet_window_s"), 2.0);
    let hard_timeout_s = as_float_val(raw.get_str("hard_timeout_s"), 60.0);

    let log_dir = resolve_opt_path(raw.get_str("log_dir"), base_dir, fs);

    let max_console_line_chars =
        as_max_console_line_chars(raw.get_str("max_console_line_chars"), None)?;

    let uart = load_uart(raw.get_str("uart"), None);

    let bootloader_prompts = match raw.get_str("bootloader_prompts") {
        Some(YValue::Seq(list)) => list
            .iter()
            .filter_map(|item| {
                if let YValue::Str(s) = item {
                    Some(s.clone())
                } else {
                    None
                }
            })
            .collect(),
        _ => Vec::new(),
    };

    // config.py `_template_from_dict`：`str(raw.get("boot_done_regex") or "").strip()`
    let boot_done_regex = match raw.get_str("boot_done_regex").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_string(),
        None => String::new(),
    };

    Ok(ProfileTemplate {
        profile_name: name.to_string(),
        platform,
        prompt_regex,
        login_regex,
        password_regex,
        post_login_cmd,
        ready_probe,
        username,
        user_env,
        pass_env,
        env_file,
        console_unlock_env,
        console_unlock_idle_s,
        timeout_s,
        quiet_window_s,
        hard_timeout_s,
        log_dir,
        uart,
        bootloader_prompts,
        boot_done_regex,
        max_console_line_chars,
    })
}

fn load_templates(
    file_name: &str,
    obj: &YMap,
    base_dir: &str,
    fs: &dyn ProfileFs,
) -> Result<
    (
        BTreeMap<String, ProfileTemplate>,
        Vec<ProfileTemplate>,
        String,
    ),
    ConfigError,
> {
    let mut templates: BTreeMap<String, ProfileTemplate> = BTreeMap::new();
    let mut ordered: Vec<ProfileTemplate> = Vec::new();

    let stem = crate::pypath::splitext(fs.flavor(), file_name).0;
    let default_name = match obj.get_str("profile_name").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_string(),
        None => py_strip(&stem).to_string(),
    };

    let profiles_obj = obj.get_str("profiles");
    if let Some(YValue::Map(m)) = profiles_obj {
        for (k, v) in &m.0 {
            if let YValue::Map(sub_map) = v {
                let name = py_strip(&py_str_of(k)).to_string();
                if !name.is_empty() {
                    let tpl = template_from_map(&name, sub_map, base_dir, fs)?;
                    templates.insert(name, tpl.clone());
                    ordered.push(tpl);
                }
            }
        }
    } else if let Some(YValue::Seq(list)) = profiles_obj {
        for row in list {
            if let YValue::Map(row_map) = row {
                let name_val = row_map
                    .get_str("name")
                    .filter(|v| py_truthy_of(v))
                    .or_else(|| row_map.get_str("profile_name").filter(|v| py_truthy_of(v)));
                if let Some(v) = name_val {
                    let name = py_strip(&py_str_of(v)).to_string();
                    if !name.is_empty() {
                        let tpl = template_from_map(&name, row_map, base_dir, fs)?;
                        templates.insert(name, tpl.clone());
                        ordered.push(tpl);
                    }
                }
            }
        }
    }

    let mut actual_default_name = default_name;
    if templates.is_empty() {
        let tpl = template_from_map(&actual_default_name, obj, base_dir, fs)?;
        templates.insert(actual_default_name.clone(), tpl.clone());
        ordered.push(tpl);
    }

    if !templates.contains_key(&actual_default_name) {
        if let Some(first_key) = templates.keys().next() {
            actual_default_name = first_key.clone();
        }
    }

    // passthrough 排最後，其餘維持 YAML 定義順序（穩定排序）
    ordered.sort_by_key(|t| if t.platform == "passthrough" { 1 } else { 0 });

    Ok((templates, ordered, actual_default_name))
}

fn merge_session(
    template: &ProfileTemplate,
    target: &YMap,
    act_no: PyInt,
    com: String,
    alias: String,
    device_by_id: String,
    base_dir: &str,
    defaults_log_dir: Option<&str>,
    fs: &dyn ProfileFs,
) -> Result<SessionProfile, ConfigError> {
    let target_log_dir = if target.contains_str("log_dir") {
        resolve_opt_path(target.get_str("log_dir"), base_dir, fs)
    } else {
        template.log_dir.clone()
    };
    let log_dir = target_log_dir.or_else(|| defaults_log_dir.map(str::to_string));

    let platform = match target.get_str("platform").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_lowercase(),
        None => template.platform.clone(),
    };

    let prompt_regex = match target.get_str("prompt_regex").filter(|v| py_truthy_of(v)) {
        Some(v) => py_str_of(v),
        None => template.prompt_regex.clone(),
    };

    let login_regex = match target.get_str("login_regex").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_string(),
        None => template.login_regex.clone(),
    };

    let password_regex = match target.get_str("password_regex").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_string(),
        None => template.password_regex.clone(),
    };

    let post_login_cmd = match target.get_str("post_login_cmd").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_string(),
        None => template.post_login_cmd.clone(),
    };

    let ready_probe = as_str_keep_empty(target.get_str("ready_probe"), &template.ready_probe);

    let username = match target.get_str("username") {
        Some(v) if !matches!(v, YValue::Null) => as_opt_str(Some(v)),
        _ => template.username.clone(),
    };

    let user_env = if target.contains_str("user_env")
        || target.contains_str("username_env")
        || target.contains_str("login_env")
    {
        let val = target
            .get_str("user_env")
            .filter(|v| py_truthy_of(v))
            .or_else(|| target.get_str("username_env").filter(|v| py_truthy_of(v)))
            .or_else(|| target.get_str("login_env"));
        as_opt_str(val)
    } else {
        template.user_env.clone()
    };

    let pass_env = if target.contains_str("pass_env")
        || target.contains_str("password_env")
        || target.contains_str("pw_env")
    {
        let val = target
            .get_str("pass_env")
            .filter(|v| py_truthy_of(v))
            .or_else(|| target.get_str("password_env").filter(|v| py_truthy_of(v)))
            .or_else(|| target.get_str("pw_env"));
        as_opt_str(val)
    } else {
        template.pass_env.clone()
    };

    let env_file = if target.contains_str("env_file") {
        resolve_opt_path(target.get_str("env_file"), base_dir, fs)
    } else {
        template.env_file.clone()
    };

    let console_unlock_env = if target.contains_str("console_unlock_env") {
        as_opt_str(target.get_str("console_unlock_env"))
    } else {
        template.console_unlock_env.clone()
    };

    let console_unlock_idle_s = as_unlock_idle(
        target.get_str("console_unlock_idle_s"),
        template.console_unlock_idle_s,
    )?;

    let timeout_s = as_float_val(target.get_str("timeout_s"), template.timeout_s);
    let quiet_window_s = as_float_val(target.get_str("quiet_window_s"), template.quiet_window_s);
    let hard_timeout_s = as_float_val(target.get_str("hard_timeout_s"), template.hard_timeout_s);

    let max_console_line_chars = if target.contains_str("max_console_line_chars") {
        as_max_console_line_chars(target.get_str("max_console_line_chars"), None)?
    } else {
        template.max_console_line_chars.clone()
    };

    let uart = load_uart(target.get_str("uart"), Some(&template.uart));
    let bootloader_prompts = template.bootloader_prompts.clone();
    // config.py `_merge_session`：`str(target.get("boot_done_regex") or template.boot_done_regex).strip()`
    let boot_done_regex = match target.get_str("boot_done_regex").filter(|v| py_truthy_of(v)) {
        Some(v) => py_strip(&py_str_of(v)).to_string(),
        None => py_strip(&template.boot_done_regex).to_string(),
    };

    let merged_template = ProfileTemplate {
        profile_name: template.profile_name.clone(),
        platform,
        prompt_regex,
        login_regex,
        password_regex,
        post_login_cmd,
        ready_probe,
        username,
        user_env,
        pass_env,
        env_file,
        console_unlock_env,
        console_unlock_idle_s,
        timeout_s,
        quiet_window_s,
        hard_timeout_s,
        log_dir,
        uart,
        bootloader_prompts,
        boot_done_regex,
        max_console_line_chars,
    };

    Ok(SessionProfile {
        com,
        act_no,
        alias,
        device_by_id,
        template: merged_template,
    })
}

// ---------------------------------------------------------------------------
// 公開 API：load_profiles 與 RuntimeConfig
// ---------------------------------------------------------------------------

/// 載入 profile 目錄（09 §2.6 逐步）。
pub fn load_profiles(profile_dir: &str, fs: &dyn ProfileFs) -> Result<LoadResult, ConfigError> {
    let mut out: Vec<SessionProfile> = Vec::new();
    let mut all_templates: Vec<ProfileTemplate> = Vec::new();
    let mut max_sessions = PyInt::Small(16);

    if !fs.is_dir(profile_dir) {
        return Ok(LoadResult {
            profiles: out,
            templates: all_templates,
            max_sessions,
        });
    }

    let mut file_names = fs.list_dir(profile_dir).map_err(ConfigError::Os)?;
    file_names.sort();

    for file_name in file_names {
        if !(file_name.ends_with(".yaml") || file_name.ends_with(".yml")) {
            continue;
        }

        let path = crate::pypath::join(fs.flavor(), profile_dir, &[&file_name]);
        let bytes = fs.read_bytes(&path).map_err(ConfigError::Os)?;
        let text = decode_utf8_strict(&bytes).map_err(ConfigError::Decode)?;

        let loaded = safe_load(&text).map_err(ConfigError::Yaml)?;
        let obj_map = match loaded {
            YValue::Map(m) => Some(m),
            other if !py_truthy_of(&other) => Some(YMap(Vec::new())),
            _ => None,
        };
        let Some(obj_map) = obj_map else {
            continue;
        };

        let base_dir = crate::pypath::dirname(fs.flavor(), &path);
        let defaults_map = match obj_map.get_str("defaults") {
            Some(YValue::Map(m)) => Some(m),
            _ => None,
        };

        let defaults_log_dir = if let Some(dm) = defaults_map {
            if !dm.0.is_empty() {
                resolve_opt_path(dm.get_str("log_dir"), &base_dir, fs)
            } else {
                None
            }
        } else {
            None
        };

        if let Some(dm) = defaults_map {
            if !dm.0.is_empty() {
                max_sessions = as_int_val(dm.get_str("max_sessions"), max_sessions);
            }
        }

        let (templates_map, ordered_templates, default_profile_name) =
            load_templates(&file_name, &obj_map, &base_dir, fs)?;
        all_templates = ordered_templates;

        let targets_val = obj_map.get_str("targets");
        let Some(targets_val) = targets_val else {
            // targets 區段省略 → 全部走動態偵測
            continue;
        };
        let YValue::Seq(targets_list) = targets_val else {
            continue;
        };

        for (idx, target_item) in targets_list.iter().enumerate() {
            let idx = idx + 1;
            let YValue::Map(t) = target_item else {
                continue;
            };

            let profile_ref = {
                let raw_ref = t
                    .get_str("profile")
                    .filter(|v| py_truthy_of(v))
                    .or_else(|| t.get_str("profile_name").filter(|v| py_truthy_of(v)));
                match raw_ref {
                    Some(v) => py_strip(&py_str_of(v)).to_string(),
                    None => default_profile_name.clone(),
                }
            };

            let Some(tpl) = templates_map.get(&profile_ref) else {
                continue;
            };

            let act_no = match t.get_str("act_no") {
                Some(v) => as_int_val(Some(v), PyInt::Small(idx as i128)),
                None => PyInt::Small(idx as i128),
            };

            let com = match t.get_str("com").filter(|v| py_truthy_of(v)) {
                Some(v) => py_strip(&py_str_of(v)).to_string(),
                None => match &act_no {
                    PyInt::Small(n) => {
                        let com_idx = if *n <= 1 { 0 } else { *n - 1 };
                        format!("COM{com_idx}")
                    }
                    PyInt::Big(s) => {
                        if s.starts_with('-') || s == "0" || s == "1" {
                            "COM0".to_string()
                        } else {
                            format!("COM{}", dec_str_sub_one(s))
                        }
                    }
                },
            };

            let device_by_id = match t.get_str("device_by_id").filter(|v| py_truthy_of(v)) {
                Some(v) => py_strip(&py_str_of(v)).to_string(),
                None => String::new(),
            };
            if device_by_id.is_empty() {
                continue;
            }

            let alias = match t.get_str("alias").filter(|v| py_truthy_of(v)) {
                Some(v) => py_strip(&py_str_of(v)).to_string(),
                None => {
                    let act_str = match &act_no {
                        PyInt::Small(i) => i.to_string(),
                        PyInt::Big(s) => s.clone(),
                    };
                    format!("{}+{act_str}", tpl.profile_name)
                }
            };

            let session = merge_session(
                tpl,
                t,
                act_no,
                com,
                alias,
                device_by_id,
                &base_dir,
                defaults_log_dir.as_deref(),
                fs,
            )?;
            out.push(session);
        }
    }

    Ok(LoadResult {
        profiles: out,
        templates: all_templates,
        max_sessions,
    })
}

/// `RuntimeConfig`：`config.yaml` 的 mapping 原始資料。
pub struct RuntimeConfig {
    pub data: YMap,
}

impl RuntimeConfig {
    /// `None`＝檔案不存在 → 空；非 mapping → 空；YAML 錯 → `Err`。
    pub fn from_text(text: Option<&str>) -> Result<RuntimeConfig, ConfigError> {
        let Some(t) = text else {
            return Ok(RuntimeConfig {
                data: YMap(Vec::new()),
            });
        };
        let val = safe_load(t).map_err(ConfigError::Yaml)?;
        match val {
            YValue::Map(m) => Ok(RuntimeConfig { data: m }),
            _ => Ok(RuntimeConfig {
                data: YMap(Vec::new()),
            }),
        }
    }

    /// `data.get("supervision_mode")`。
    pub fn mode(&self) -> Option<&YValue> {
        self.data.get_str("supervision_mode")
    }

    /// `data.get("socket_path")`（不驗型別，Q-18）。
    pub fn socket_path(&self) -> Option<&YValue> {
        self.data.get_str("socket_path")
    }

    /// 07 §5.1：socket_path Some 才覆寫。
    ///
    /// 對應 `sw_core/runtime_config.py` `set_mode`：`supervision_mode` 必寫；`socket_path` 只在 `Some` 時覆寫，
    /// 其餘鍵（例如 `windows:`）原樣保留。
    pub fn set_mode(&mut self, mode: &str, socket_path: Option<&str>) {
        upsert_str_key(&mut self.data, "supervision_mode", mode);
        if let Some(endpoint) = socket_path {
            upsert_str_key(&mut self.data, "socket_path", endpoint);
        }
    }

    /// 對應 `set_socket`：只更新 `socket_path`，不改動 `supervision_mode`（#84 PORT-4）。
    pub fn set_socket(&mut self, endpoint: &str) {
        upsert_str_key(&mut self.data, "socket_path", endpoint);
    }

    /// `yaml.safe_dump(data, allow_unicode=True)`（PyYAML 預設 `sort_keys=True`）的位元組等價輸出。
    ///
    /// 已知殘差（非契約，見 plan P4-T6）：PyYAML 對「同一物件被多處參照」會輸出 anchor／alias（`&id001`／`*id001`），
    /// 本函式以樹狀展開輸出，重新載入後語意相同、位元組不同。僅當 `config.yaml` 使用 alias 且經 `set_mode`／`set_socket` 寫回時才會出現差異；無 alias 的設定位元組相同。
    pub fn to_yaml(&self) -> String {
        emit::safe_dump(&YValue::Map(self.data.clone()), true, true)
    }
}

/// 以字串 key 覆寫值（既有 key 原位取代，否則附加於尾端；對應 Python `dict.__setitem__`）。
fn upsert_str_key(map: &mut YMap, key: &str, value: &str) {
    let value = YValue::Str(value.to_string());
    match map
        .0
        .iter_mut()
        .find(|(k, _)| matches!(k, YValue::Str(s) if s == key))
    {
        Some(slot) => slot.1 = value,
        None => map.0.push((YValue::Str(key.to_string()), value)),
    }
}

impl RuntimeConfig {
    /// `windows.exclude_coms` 讀取（Task 0c；Windows 裝置排除清單）。
    pub fn windows_exclude_coms(&self) -> Vec<String> {
        let Some(YValue::Map(windows)) = self.data.get_str("windows") else {
            return Vec::new();
        };
        let Some(YValue::Seq(items)) = windows.get_str("exclude_coms") else {
            return Vec::new();
        };
        let mut result = Vec::new();
        for item in items {
            let value = py_str_of(item);
            if !result.contains(&value) {
                result.push(value);
            }
        }
        result
    }
    /// `windows.test_device_allowlist` 讀取（Task 0c；OD-11 allowlist，`Err(())` 表示型別不合法）。
    pub fn windows_test_device_allowlist(&self) -> Result<Option<Vec<String>>, ()> {
        let Some(YValue::Map(windows)) = self.data.get_str("windows") else {
            return Ok(None);
        };
        let Some(value) = windows.get_str("test_device_allowlist") else {
            return Ok(None);
        };
        let YValue::Seq(items) = value else {
            return Err(());
        };
        let mut result = Vec::new();
        for item in items {
            let YValue::Str(item) = item else {
                return Err(());
            };
            let item = py_strip(item).to_string();
            if !item.is_empty() && !result.contains(&item) {
                result.push(item);
            }
        }
        Ok(Some(result))
    }
}
