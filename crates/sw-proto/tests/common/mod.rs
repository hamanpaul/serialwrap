//! 測試通用輔助函式。
use std::path::PathBuf;

/// 取得 repo 根目錄路徑。
///
/// 優先讀取執行期環境變數 `SW_TEST_DATA_DIR`；若未設定則以編譯期 `CARGO_MANIFEST_DIR/../..` 推導。
pub fn data_root() -> PathBuf {
    if let Ok(dir) = std::env::var("SW_TEST_DATA_DIR") {
        return PathBuf::from(dir);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// `ConfigError` 的 golden 記錄格式（與 `gen_profiles.py::classify` 一致）：
/// `YAMLError`／`ValueError:<msg>`（含 UTF-8 解碼錯誤）／`OSError`。
#[allow(dead_code)]
pub fn config_error_kind(e: &sw_proto::config_types::ConfigError) -> String {
    use sw_proto::config_types::ConfigError;
    match e {
        ConfigError::Yaml(_) => "YAMLError".into(),
        ConfigError::Value(msg) => format!("ValueError:{msg}"),
        ConfigError::Decode(d) => format!("ValueError:{d}"),
        ConfigError::Os(_) => "OSError".into(),
    }
}

/// `LoadResult` 的 golden 編碼：與 `gen_profiles.py` 的 `json.dumps(asdict(result), sort_keys=True,
/// ensure_ascii=False, separators=(",",":"))` 同形。`SessionProfile` 依 Python dataclass 欄位攤平
/// （`com`、`act_no`、`alias`、`device_by_id` 加上 template 全部欄位），`uart` 為巢狀 mapping。
#[allow(dead_code)]
pub fn load_result_json(res: &sw_proto::config_types::LoadResult) -> String {
    use sw_proto::pyjson::{self, CLI_STDOUT, PyDict, PyValue};

    fn opt_str(v: &Option<String>) -> PyValue {
        match v {
            Some(s) => PyValue::Str(s.clone()),
            None => PyValue::Null,
        }
    }
    fn template_fields(t: &sw_proto::config_types::ProfileTemplate) -> Vec<(String, PyValue)> {
        let u = &t.uart;
        let uart = PyDict::from_pairs(vec![
            ("baud".into(), PyValue::Int(u.baud.clone())),
            ("data_bits".into(), PyValue::Int(u.data_bits.clone())),
            ("parity".into(), PyValue::Str(u.parity.clone())),
            ("stop_bits".into(), PyValue::Int(u.stop_bits.clone())),
            ("flow_control".into(), PyValue::Str(u.flow_control.clone())),
            ("xonxoff".into(), PyValue::Bool(u.xonxoff)),
        ]);
        vec![
            ("profile_name".into(), PyValue::Str(t.profile_name.clone())),
            ("platform".into(), PyValue::Str(t.platform.clone())),
            ("prompt_regex".into(), PyValue::Str(t.prompt_regex.clone())),
            ("login_regex".into(), PyValue::Str(t.login_regex.clone())),
            ("password_regex".into(), PyValue::Str(t.password_regex.clone())),
            ("post_login_cmd".into(), PyValue::Str(t.post_login_cmd.clone())),
            ("ready_probe".into(), PyValue::Str(t.ready_probe.clone())),
            ("username".into(), opt_str(&t.username)),
            ("user_env".into(), opt_str(&t.user_env)),
            ("pass_env".into(), opt_str(&t.pass_env)),
            ("env_file".into(), opt_str(&t.env_file)),
            ("console_unlock_env".into(), opt_str(&t.console_unlock_env)),
            ("console_unlock_idle_s".into(), PyValue::Float(t.console_unlock_idle_s)),
            ("timeout_s".into(), PyValue::Float(t.timeout_s)),
            ("quiet_window_s".into(), PyValue::Float(t.quiet_window_s)),
            ("hard_timeout_s".into(), PyValue::Float(t.hard_timeout_s)),
            ("log_dir".into(), opt_str(&t.log_dir)),
            ("uart".into(), PyValue::Dict(uart)),
            (
                "bootloader_prompts".into(),
                PyValue::List(t.bootloader_prompts.iter().map(|s| PyValue::Str(s.clone())).collect()),
            ),
            ("boot_done_regex".into(), PyValue::Str(t.boot_done_regex.clone())),
            (
                "max_console_line_chars".into(),
                match &t.max_console_line_chars {
                    Some(n) => PyValue::Int(n.clone()),
                    None => PyValue::Null,
                },
            ),
        ]
    }

    let profiles: Vec<PyValue> = res
        .profiles
        .iter()
        .map(|s| {
            let mut fields = vec![
                ("com".into(), PyValue::Str(s.com.clone())),
                ("act_no".into(), PyValue::Int(s.act_no.clone())),
                ("alias".into(), PyValue::Str(s.alias.clone())),
                ("device_by_id".into(), PyValue::Str(s.device_by_id.clone())),
            ];
            fields.extend(template_fields(&s.template));
            PyValue::Dict(PyDict::from_pairs(fields))
        })
        .collect();
    let templates: Vec<PyValue> = res
        .templates
        .iter()
        .map(|t| PyValue::Dict(PyDict::from_pairs(template_fields(t))))
        .collect();
    let top = PyDict::from_pairs(vec![
        ("profiles".into(), PyValue::List(profiles)),
        ("templates".into(), PyValue::List(templates)),
        ("max_sessions".into(), PyValue::Int(res.max_sessions.clone())),
    ]);
    pyjson::dumps(&PyValue::Dict(top), &CLI_STDOUT)
}

/// golden TSV 的一列（格式見 `scripts/rust/goldens/_tsv.py`）。
pub struct Row {
    pub id: String,
    pub op: String,
    pub input: String,
    pub expected: String,
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

/// 公開 unhex 函式（P3-T4 decode 測試使用）。
pub fn unhex_pub(s: &str) -> Vec<u8> {
    unhex(s)
}

/// 讀取 `contracts/golden/<name>`；欄位以 hex 編碼的 UTF-8 還原（不依賴受測的 pyjson）。
pub fn golden(name: &str) -> Vec<Row> {
    let p = data_root()
        .join("contracts/golden")
        .join(name)
        .to_string_lossy()
        .into_owned();
    std::fs::read_to_string(&p)
        .unwrap_or_else(|e| panic!("{p}: {e}"))
        .lines()
        .map(|l| {
            let f: Vec<&str> = l.split('\t').collect();
            Row {
                id: f[0].into(),
                op: f[1].into(),
                input: String::from_utf8(unhex(f[2])).unwrap(),
                expected: String::from_utf8(unhex(f[3])).unwrap(),
            }
        })
        .collect()
}

/// 從 case 字串解析 `PyOsError`（五欄以 unit separator `\x1f` 分隔：`errno|winerror|strerror|filename|plain`）。
pub fn oserror_from_case(s: &str) -> sw_proto::pycompat::PyOsError {
    let parts: Vec<&str> = s.split('\x1f').collect();
    assert_eq!(parts.len(), 5, "oserror case 必須由 5 欄組成");
    let errno = if parts[0].is_empty() {
        None
    } else {
        Some(parts[0].parse().expect("valid errno"))
    };
    let winerror = if parts[1].is_empty() {
        None
    } else {
        Some(parts[1].parse().expect("valid winerror"))
    };
    let strerror = if parts[2].is_empty() {
        None
    } else {
        Some(parts[2].to_string())
    };
    let filename = if parts[3].is_empty() {
        None
    } else {
        Some(parts[3].to_string())
    };
    let plain = if parts[4].is_empty() {
        None
    } else {
        Some(parts[4].to_string())
    };
    sw_proto::pycompat::PyOsError {
        errno,
        winerror,
        strerror,
        filename,
        plain,
    }
}

/// 分派路徑操作並回傳字串結果。
pub fn pypath_dispatch(
    flavor: sw_proto::pypath::PathFlavor,
    op: &str,
    s: &str,
    env: &sw_proto::runtime_paths::EnvSnapshot,
    home: &dyn sw_proto::pypath::HomeLookup,
) -> String {
    let op = op.strip_prefix("pypath.").unwrap_or(op);
    match op {
        "join" => {
            let parts: Vec<&str> = s.split('\x1f').collect();
            sw_proto::pypath::join(flavor, parts.first().copied().unwrap_or(""), &parts[1..])
        }
        "normpath" => sw_proto::pypath::normpath(flavor, s),
        "isabs" => {
            if sw_proto::pypath::isabs(flavor, s) {
                "True".into()
            } else {
                "False".into()
            }
        }
        "dirname" => sw_proto::pypath::dirname(flavor, s),
        "basename" => sw_proto::pypath::basename(flavor, s),
        "splitext" => {
            let (root, ext) = sw_proto::pypath::splitext(flavor, s);
            format!("{root}\x1f{ext}")
        }
        "expanduser" => sw_proto::pypath::expanduser(flavor, s, env, home),
        other => panic!("unknown pypath op {other}"),
    }
}

/// 殘餘差異檔（`contracts/golden/pyre.residuals.txt`）的一列：`<pattern hex>\t<構造名>\t<核可者>\t<核可紀錄>`（P3-T5）。
#[allow(dead_code)]
pub struct Residual {
    pub pattern_hex: String,
    /// 解碼後的 pattern 文字。
    pub pattern: String,
    pub construct: String,
    pub approver: String,
    pub record: String,
}

/// 讀取殘餘檔；檔案缺失即 panic（不得以 SKIP 代替）。
#[allow(dead_code)]
pub fn residual_rows() -> Vec<Residual> {
    let p = data_root().join("contracts/golden/pyre.residuals.txt");
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    text.lines()
        .filter(|l| !l.is_empty())
        .map(|l| {
            let f: Vec<&str> = l.split('\t').collect();
            assert_eq!(f.len(), 4, "殘餘檔每列必須為 4 欄：{l:?}");
            Residual {
                pattern_hex: f[0].into(),
                pattern: String::from_utf8(unhex(f[0])).expect("殘餘檔 pattern 必須為 UTF-8"),
                construct: f[1].into(),
                approver: f[2].into(),
                record: f[3].into(),
            }
        })
        .collect()
}

/// 殘餘差異豁免：僅當殘餘檔登記了此 pattern，且 Rust `compile` 實際回 `Untranslatable(同一構造名)` 時為真。
/// 不以 case id 豁免；其他任何 Rust 結果一律不豁免。
#[allow(dead_code)]
pub fn residual_accepts(pat: &str) -> bool {
    let hex: String = pat.as_bytes().iter().map(|b| format!("{b:02x}")).collect();
    let Some(row) = residual_rows().into_iter().find(|r| r.pattern_hex == hex) else {
        return false;
    };
    matches!(
        sw_proto::pyre::compile(pat, sw_proto::pyre::PyFlags::default()),
        Err(sw_proto::pyre::CompileError::Untranslatable(name)) if name == row.construct
    )
}

/// 出貨 profile regex 與 `UBOOT_FALLBACK_PROMPTS` 的字串清單（殘餘檔 hygiene 用）。
///
/// 以行為單位粗略抽取：`sw_core/assets/profiles/*.yaml` 中 `*_regex:` 與 `bootloader_prompts` 項目的值
/// （同時保留去引號的原文與 YAML 雙引號跳脫後的值），加上 `sw_core/constants.py` 中 `UBOOT_FALLBACK_PROMPTS` 的雙引號字串。
#[allow(dead_code)]
pub fn shipped_regexes() -> Vec<String> {
    let mut out = Vec::new();
    let dir = data_root().join("sw_core/assets/profiles");
    let entries = std::fs::read_dir(&dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display()));
    for entry in entries {
        let path = entry.expect("profiles 目錄項目").path();
        if path.extension().and_then(|e| e.to_str()) != Some("yaml") {
            continue;
        }
        let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        for line in text.lines() {
            let t = line.trim();
            let value = match t.split_once(':') {
                Some((key, val)) if key.trim().ends_with("_regex") => Some(val.trim()),
                _ => t.strip_prefix("- ").map(str::trim),
            };
            if let Some(v) = value {
                out.push(v.to_string());
                let inner = v.trim_matches(|c| c == '"' || c == '\'');
                out.push(inner.to_string());
                out.push(inner.replace("\\\\", "\\").replace("\\\"", "\""));
            }
        }
    }
    let consts = data_root().join("sw_core/constants.py");
    let text = std::fs::read_to_string(&consts).unwrap_or_else(|e| panic!("{}: {e}", consts.display()));
    for line in text.lines().filter(|l| l.starts_with("UBOOT_FALLBACK_PROMPTS")) {
        out.extend(line.split('"').skip(1).step_by(2).map(str::to_string));
    }
    out
}

/// 將 `RuntimePaths` 序列化為與 Python `json.dumps(..., sort_keys=True, separators=(",", ":"))` 相同的 JSON 字串。
#[allow(dead_code)]
pub fn paths_json(p: &sw_proto::runtime_paths::RuntimePaths) -> String {
    use sw_proto::pyjson::PyInt;

    fn esc(s: &str) -> String {
        s.replace('\\', "\\\\").replace('"', "\\\"")
    }

    let port_str = match &p.default_tcp_port {
        PyInt::Small(n) => n.to_string(),
        PyInt::Big(s) => s.clone(),
    };

    format!(
        "{{\"config_dir\":\"{}\",\"config_yaml\":\"{}\",\"data_dir\":\"{}\",\"default_endpoint\":\"{}\",\"default_tcp_port\":{},\"lock_path\":\"{}\",\"log_dir\":\"{}\",\"profile_dir\":\"{}\",\"run_dir\":\"{}\",\"socket_path\":\"{}\",\"state_dir\":\"{}\",\"wal_dir\":\"{}\"}}",
        esc(&p.config_dir),
        esc(&p.config_yaml),
        esc(&p.data_dir),
        esc(&p.default_endpoint),
        port_str,
        esc(&p.lock_path),
        esc(&p.log_dir),
        esc(&p.profile_dir),
        esc(&p.run_dir),
        esc(&p.socket_path),
        esc(&p.state_dir),
        esc(&p.wal_dir),
    )
}

/// 解析以 `\x1f` 分隔的 `KEY=VALUE` 環境變數序列。
#[allow(dead_code)]
pub fn env_pairs(s: &str) -> Vec<(String, String)> {
    if s.is_empty() {
        return Vec::new();
    }
    s.split('\x1f')
        .map(|pair| {
            if let Some((k, v)) = pair.split_once('=') {
                (k.to_string(), v.to_string())
            } else {
                (pair.to_string(), String::new())
            }
        })
        .collect()
}

/// 讀取 `contracts/rpc_schema.json` 取得 55 個方法清單。
#[allow(dead_code)]
pub fn rpc_schema_methods() -> Vec<String> {
    let p = data_root().join("contracts/rpc_schema.json");
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("P0 contracts 缺少: {}: {e}", p.display()));
    let mut methods = std::collections::BTreeSet::new();

    if let Some(idx) = text.find("\"x-methods\"") {
        let end_idx = text[idx..].find("\"x-pending\"").map(|i| idx + i).unwrap_or(text.len());
        let section = &text[idx..end_idx];
        for line in section.lines() {
            let trimmed = line.trim();
            if trimmed.starts_with('"') && trimmed.ends_with('{') {
                if let Some(colon_pos) = trimmed.find(':') {
                    let key = trimmed[1..colon_pos].trim_matches('"');
                    if key.contains('.') {
                        methods.insert(key.to_string());
                    }
                }
            }
        }
    }

    for line in text.lines() {
        if line.contains("\"adds_method\"") {
            if let Some(pos) = line.find(':') {
                let val = line[pos + 1..].trim().trim_matches(|c| c == '"' || c == ',' || c == ' ');
                if !val.is_empty() {
                    methods.insert(val.to_string());
                }
            }
        }
    }

    methods.into_iter().collect()
}

