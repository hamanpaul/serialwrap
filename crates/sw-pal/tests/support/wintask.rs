//! P8 Task 13：`windows-task` 監管 Rust daemon 實跑（`tests/wintask_runtime.rs`）的支援與三道 COM 防線。
//!
//! 計畫：`docs/rust-refactor/plans/09-slice24-transfer-flash.md` Task 13（`crates/sw-pal/tests/support/wintask.rs`）；
//! 擁有者裁決（`origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/s2.md`「T13 的 Windows 排程工作實跑」）：
//! 測試 daemon 不得開啟任何真 COM port，不得碰正式 `\serialwrap` 工作、48700 埠與使用者的 state／config，跑完或中途
//! 失敗都要刪除測試工作、結束殘留的 staging `serialwrapd.exe`。
//!
//! 三道 COM 防線：
//! 1. staging 批次檔設 `SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM250`（不存在的 port）。daemon 只在隔離條件成立時採用
//!    allowlist（`sw-daemon` `isolation_reason`：STATE／RUN／CONFIG 皆設定且不等於預設），隔離不成立時以
//!    `TEST_DEVICE_ALLOWLIST_REJECTED` exit 2、不開任何裝置；批次檔因此先清掉所有繼承的 `SERIALWRAP_*`，再把全部目錄
//!    指向 staging。staging config 另寫 `windows.test_device_allowlist: [COM250]`。
//! 2. staging config 的 `windows.exclude_coms` 列出測試開始時 registry `SERIALCOMM` 的全部 COM port。
//! 3. daemon 起來後、做任何其他動作前，以 RPC `session.list` 確認 session 數為 0、`health.status` 的
//!    `test_device_allowlist` 為 `{enabled:true, coms:["COM250"], source:"env"}`；任一不符即有序停止並刪除測試工作。
//!
//! 本檔不含平台 cfg：批次檔／config 的內容在 POSIX 以替身驗證（`tests/wintask_staging.rs`、
//! `crates/sw-daemon/tests/wintask_staging_isolation.rs`），實跑只在 Windows（`wintask_runtime.rs` 以 `cfg(windows)`）。
//! 排程工作一律以具名 API（`*_named`）操作 `sw-p8-test-<pid>`；不得呼叫固定名稱 `serialwrap` 的 `ServiceCtl` 方法。

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use sw_pal::device_source::windows_model::RegistryView;
use sw_pal::service_ctl::{ServiceCtl, SupervisionMode, TaskQuery};
use sw_pal::traits::SysEnv;

/// 測試排程工作名稱前綴（擁有者裁決：`sw-p8-test-<pid>`）。
pub const TASK_PREFIX: &str = "sw-p8-test-";
/// 正式排程工作名稱：本測試絕不操作。
pub const PRODUCTION_TASK: &str = "serialwrap";
/// 正式 daemon 的 TCP 埠：本測試絕不連線或監聽。
pub const PRODUCTION_PORT: u16 = 48700;
/// 不存在的 COM port（防線 1）。
pub const ALLOWLIST_COM: &str = "COM250";
/// test-fakes daemon 專用：寫入一筆可辨認 WAL 紀錄，不會開啟 UART。
pub const TEST_WAL_RECORD_ENV: &str = "SERIALWRAP_TEST_T13_WAL_RECORD";
pub const TEST_WAL_PAYLOAD: &[u8] = b"serialwrap T13 isolated staging WAL evidence\n";
/// `reg query` 讀取的 registry 鍵（防線 2）。
pub const SERIALCOMM_KEY: &str = r"HKLM\HARDWARE\DEVICEMAP\SERIALCOMM";
/// 批次檔第一步：清掉所有繼承的 `SERIALWRAP_*`（使用者全域 env 不得穿透隔離）。
pub const BATCH_CLEAR_LINE: &str = r#"for /f "delims==" %%V in ('set SERIALWRAP_ 2^>nul') do set "%%V=""#;
/// staging 內 daemon 執行檔名稱（行程比對以完整路徑為準，不以映像名稱比對，避免碰到正式 daemon）。
pub const DAEMON_EXE: &str = "serialwrapd.exe";
/// 正式停止路徑 harness 執行檔（sw-cli `wintask_stop_harness`，`--features test-fakes`；由 `SW_WIN_EXTRA_FILES`
/// 帶到實跑測試執行檔旁）。sw-pal 不得依賴 sw-cli（arch §1.3），故以獨立行程執行正式 `serialwrap service stop`。
pub const HARNESS_EXE: &str = "wintask_stop_harness.exe";
/// harness 的 CLI config：只讓正式 `service stop` 走 windows-task 監管分支（daemon 的 staging config 不變）。
pub const CLI_CONFIG: &str = "supervision_mode: windows-task\n";

const RPC_TIMEOUT: Duration = Duration::from_secs(2);
const CMD_TIMEOUT: Duration = Duration::from_secs(30);
const TASK_CLEANUP_TIMEOUT: Duration = Duration::from_secs(10);

/// 擁有者授權旗標（`SW_TEST_ALLOW_SCHTASKS=1`）。
pub fn allowed() -> bool {
    std::env::var("SW_TEST_ALLOW_SCHTASKS").as_deref() == Ok("1")
}

/// 本行程的測試工作名稱。
pub fn test_task_name(pid: u32) -> String {
    format!("{TASK_PREFIX}{pid}")
}

/// 名稱守門：只允許 `sw-p8-test-<十進位數字>`；正式 `serialwrap` 或其他名稱一律 panic。
pub fn assert_test_task_name(name: &str) {
    assert!(
        !name.eq_ignore_ascii_case(PRODUCTION_TASK),
        "拒絕操作正式排程工作 {PRODUCTION_TASK}"
    );
    let digits = name
        .strip_prefix(TASK_PREFIX)
        .unwrap_or_else(|| panic!("拒絕操作非測試排程工作：{name:?}（只允許 {TASK_PREFIX}<pid>）"));
    assert!(
        !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()),
        "拒絕操作非測試排程工作：{name:?}（只允許 {TASK_PREFIX}<pid>）"
    );
}

fn argv(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

/// Windows 路徑串接（`\` 分隔；不依執行平台）。
pub fn win_join(root: &str, parts: &[&str]) -> String {
    let mut out = root.trim_end_matches('\\').to_string();
    for part in parts {
        out.push('\\');
        out.push_str(part);
    }
    out
}

// ---------------------------------------------------------------------------
// 防線 2：registry SERIALCOMM 的現有 COM port
// ---------------------------------------------------------------------------

/// `reg query HKLM\HARDWARE\DEVICEMAP\SERIALCOMM`。
pub fn reg_query_argv() -> Vec<String> {
    argv(&["reg", "query", SERIALCOMM_KEY])
}

/// 解析 `reg query` 輸出：每個 `REG_SZ` 值的資料（port 名稱，原樣保留），保序去重。
pub fn parse_serialcomm(stdout: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for line in stdout.lines() {
        let tokens: Vec<&str> = line.split_whitespace().collect();
        let Some(pos) = tokens.iter().position(|t| t.eq_ignore_ascii_case("REG_SZ")) else {
            continue;
        };
        let value = tokens[pos + 1..].join(" ");
        if !value.is_empty() && !out.contains(&value) {
            out.push(value);
        }
    }
    out
}

/// 以 `reg query` 讀取 registry 的全部 COM port；指令失敗（rc≠0 或無法執行）→ `Err`（fail-closed：不在未知 COM 狀態下啟動 daemon）。
pub fn registry_com_ports(env: &dyn SysEnv) -> Result<Vec<String>, String> {
    let argv = reg_query_argv();
    let out = env
        .run(&argv, Some(CMD_TIMEOUT))
        .map_err(|e| format!("{argv:?} 無法執行：{}", e.py_message))?;
    if out.rc != 0 {
        return Err(format!("{argv:?} rc={} stderr={}", out.rc, out.stderr.trim()));
    }
    Ok(parse_serialcomm(&out.stdout))
}

/// `reg query` 與 daemon 自己使用的 `RegistryView`（SERIALCOMM 值）取聯集；任一來源讀取失敗即 `Err`。
pub fn all_registry_com_ports(env: &dyn SysEnv, registry: &dyn RegistryView) -> Result<Vec<String>, String> {
    let mut coms = registry_com_ports(env)?;
    let values = registry
        .enum_values(sw_pal::device_source::windows_model::SERIALCOMM_KEY)
        .map_err(|e| format!("RegistryView SERIALCOMM 讀取失敗：{}", e.py_message))?;
    for (_, port) in values {
        if !port.is_empty() && !coms.contains(&port) {
            coms.push(port);
        }
    }
    Ok(coms)
}

// ---------------------------------------------------------------------------
// staging：批次檔、config、目錄
// ---------------------------------------------------------------------------

/// 一次實跑的 staging（全部路徑以 Windows 形式表示）。
#[derive(Debug, Clone)]
pub struct StagingSpec {
    /// staging 根目錄（絕對路徑）。
    pub root: String,
    /// 測試 daemon 的 TCP 埠（≠ 48700）。
    pub port: u16,
    /// 防線 2：測試開始時 registry 的全部 COM port。
    pub exclude_coms: Vec<String>,
}

impl StagingSpec {
    pub fn new(root: &str, port: u16, exclude_coms: Vec<String>) -> StagingSpec {
        assert_ne!(port, PRODUCTION_PORT, "測試 daemon 不得使用正式埠 {PRODUCTION_PORT}");
        assert!(port != 0, "埠必須已選定");
        for ch in ['%', '!', '^', '"', '&', '|', '<', '>'] {
            assert!(!root.contains(ch), "staging 路徑含批次檔特殊字元 {ch:?}：{root}");
        }
        StagingSpec {
            root: root.to_string(),
            port,
            exclude_coms,
        }
    }

    /// plan API：`staging(name)`＝staging 內檔案的完整路徑。
    pub fn path(&self, name: &str) -> String {
        win_join(&self.root, &[name])
    }
    /// plan API：`staging_dir()`。
    pub fn dir(&self) -> String {
        self.root.clone()
    }
    pub fn batch_path(&self) -> String {
        self.path("run-daemon.cmd")
    }
    pub fn task_xml_path(&self) -> String {
        self.path("task.xml")
    }
    pub fn log_path(&self) -> String {
        self.path("daemon.log")
    }
    pub fn daemon_exe(&self) -> String {
        win_join(&self.root, &["bin", DAEMON_EXE])
    }
    pub fn state_dir(&self) -> String {
        win_join(&self.root, &["state"])
    }
    pub fn run_dir(&self) -> String {
        win_join(&self.root, &["run"])
    }
    pub fn config_dir(&self) -> String {
        win_join(&self.root, &["config"])
    }
    pub fn config_yaml(&self) -> String {
        win_join(&self.root, &["config", "config.yaml"])
    }
    pub fn profile_dir(&self) -> String {
        win_join(&self.root, &["config", "profiles"])
    }
    pub fn wal_dir(&self) -> String {
        win_join(&self.root, &["wal"])
    }
    pub fn wal_test_record_path(&self) -> String {
        win_join(&self.root, &["wal", "t13-record.seed"])
    }
    pub fn lock_path(&self) -> String {
        win_join(&self.root, &["run", "serialwrapd.lock"])
    }
    pub fn endpoint(&self) -> String {
        format!("tcp://127.0.0.1:{}", self.port)
    }
    /// harness（正式 `serialwrap service stop`）讀取的 CLI config（`SERIALWRAP_CONFIG`）。
    pub fn cli_config_path(&self) -> String {
        win_join(&self.root, &["config", "cli-config.yaml"])
    }
    /// CLI config 內容（[`CLI_CONFIG`]）。
    pub fn render_cli_config(&self) -> String {
        CLI_CONFIG.to_string()
    }
    /// harness 的 `--var`：daemon 的 staging 目錄（不含 daemon 專用的 allowlist）＋ `SERIALWRAP_CONFIG`；
    /// 埠由 harness 依 `--port` 自行設定，不在此列。
    pub fn harness_vars(&self) -> Vec<(String, String)> {
        [
            ("SERIALWRAP_CONFIG", self.cli_config_path()),
            ("SERIALWRAP_CONFIG_DIR", self.config_dir()),
            ("SERIALWRAP_STATE_DIR", self.state_dir()),
            ("SERIALWRAP_RUN_DIR", self.run_dir()),
            ("SERIALWRAP_WAL_DIR", self.wal_dir()),
            ("SERIALWRAP_LOG_DIR", win_join(&self.root, &["blog"])),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .collect()
    }
    /// harness 命令列：`<harness> --task <task> --port <port> --var K=V …`（只允許測試工作名稱）。
    pub fn harness_argv(&self, harness: &str, task: &str) -> Vec<String> {
        assert_test_task_name(task);
        let mut args = argv(&[harness, "--task", task, "--port", &self.port.to_string()]);
        for (key, value) in self.harness_vars() {
            args.push("--var".into());
            args.push(format!("{key}={value}"));
        }
        args
    }

    /// 批次檔設定的完整 env（清掉繼承的 `SERIALWRAP_*` 之後依序設定）。
    pub fn isolation_env(&self) -> Vec<(String, String)> {
        let r = &self.root;
        [
            ("SERIALWRAP_STATE_DIR", self.state_dir()),
            ("SERIALWRAP_RUN_DIR", self.run_dir()),
            ("SERIALWRAP_CONFIG_DIR", self.config_dir()),
            ("SERIALWRAP_DATA_DIR", win_join(r, &["data"])),
            ("SERIALWRAP_WAL_DIR", self.wal_dir()),
            ("SERIALWRAP_LOG_DIR", win_join(r, &["blog"])),
            ("SERIALWRAP_EVENTS_DIR", win_join(r, &["events.d"])),
            ("SERIALWRAP_EVENTS_RUNTIME_DIR", win_join(r, &["events-rt"])),
            ("SERIALWRAP_EVENTS_LOG_PATH", win_join(r, &["events-rt", "events.ndjson"])),
            ("SERIALWRAP_BY_ID_DIR", win_join(r, &["by-id"])),
            ("SERIALWRAP_BY_PATH_DIR", win_join(r, &["by-path"])),
            ("SERIALWRAP_INSTALL_ROOT", win_join(r, &["install"])),
            ("SERIALWRAP_PROFILE_DIR", self.profile_dir()),
            ("SERIALWRAP_TTYMCU_PATH", win_join(r, &["run", "dev", "ttyMCU"])),
            ("SERIALWRAP_BENCHES_FILE", win_join(r, &["config", "benches.yaml"])),
            (
                "SERIALWRAP_TEST_SYSTEM_CONFIG_PATH",
                win_join(r, &["system-config", "config.yaml"]),
            ),
            ("SERIALWRAP_TEST_DEVICE_ALLOWLIST", ALLOWLIST_COM.to_string()),
            (TEST_WAL_RECORD_ENV, self.wal_test_record_path()),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect()
    }

    /// 需事先建立的目錄（`system-config` 刻意不放檔案：daemon 不得讀到系統 config）。
    pub fn dirs(&self) -> Vec<String> {
        let r = &self.root;
        vec![
            win_join(r, &["bin"]),
            self.state_dir(),
            self.run_dir(),
            self.config_dir(),
            self.profile_dir(),
            win_join(r, &["data"]),
            self.wal_dir(),
            win_join(r, &["blog"]),
            win_join(r, &["events.d"]),
            win_join(r, &["events-rt"]),
            win_join(r, &["by-id"]),
            win_join(r, &["by-path"]),
            win_join(r, &["install"]),
            win_join(r, &["system-config"]),
        ]
    }

    /// `run-daemon.cmd`（CRLF）：清 env → 設隔離 env → 執行 staging daemon（stdout／stderr 附加到 `daemon.log`）→ 以 daemon 的 exit code 結束。
    pub fn render_batch(&self) -> String {
        let mut lines: Vec<String> = vec![
            "@echo off".into(),
            "setlocal EnableExtensions DisableDelayedExpansion".into(),
            BATCH_CLEAR_LINE.into(),
        ];
        for (k, v) in self.isolation_env() {
            lines.push(format!("set \"{k}={v}\""));
        }
        lines.push(format!(
            "\"{}\" --socket {} --lock \"{}\" --profile-dir \"{}\" >> \"{}\" 2>&1",
            self.daemon_exe(),
            self.endpoint(),
            self.lock_path(),
            self.profile_dir(),
            self.log_path()
        ));
        lines.push("exit /b %ERRORLEVEL%".into());
        let mut text = lines.join("\r\n");
        text.push_str("\r\n");
        text
    }

    /// staging `config.yaml`：`windows.exclude_coms`（防線 2）與 `windows.test_device_allowlist`（防線 1 的第二層）。
    pub fn render_config(&self) -> String {
        let mut text = String::from("windows:\n");
        if self.exclude_coms.is_empty() {
            text.push_str("  exclude_coms: []\n");
        } else {
            text.push_str("  exclude_coms:\n");
            for com in &self.exclude_coms {
                text.push_str(&format!("  - '{}'\n", com.replace('\'', "''")));
            }
        }
        text.push_str(&format!("  test_device_allowlist:\n  - '{ALLOWLIST_COM}'\n"));
        text
    }

    /// 建立目錄、批次檔、config，並把 daemon 複製到 `bin\serialwrapd.exe`（只在實跑主機呼叫）。
    pub fn write(&self, daemon_src: &Path) -> Result<(), String> {
        for dir in self.dirs() {
            std::fs::create_dir_all(&dir).map_err(|e| format!("mkdir {dir}: {e}"))?;
        }
        std::fs::write(self.batch_path(), self.render_batch())
            .map_err(|e| format!("write {}: {e}", self.batch_path()))?;
        std::fs::write(self.config_yaml(), self.render_config())
            .map_err(|e| format!("write {}: {e}", self.config_yaml()))?;
        std::fs::write(self.cli_config_path(), self.render_cli_config())
            .map_err(|e| format!("write {}: {e}", self.cli_config_path()))?;
        std::fs::write(self.wal_test_record_path(), TEST_WAL_PAYLOAD)
            .map_err(|e| format!("write {}: {e}", self.wal_test_record_path()))?;
        std::fs::copy(daemon_src, self.daemon_exe())
            .map_err(|e| format!("copy {} → {}: {e}", daemon_src.display(), self.daemon_exe()))?;
        Ok(())
    }

    /// `daemon.log` 尾端（失敗訊息用）。
    pub fn log_tail(&self, max: usize) -> String {
        let text = std::fs::read(self.log_path())
            .map(|b| String::from_utf8_lossy(&b).into_owned())
            .unwrap_or_else(|e| format!("<無法讀取 daemon.log：{e}>"));
        let start = text.len().saturating_sub(max);
        let start = (start..text.len()).find(|i| text.is_char_boundary(*i)).unwrap_or(text.len());
        text[start..].to_string()
    }
}

/// 以批次檔文字模擬 cmd 對 env 的效果（只解讀 [`BATCH_CLEAR_LINE`] 與 `set "K=V"` 兩種行；其餘行不影響 env）。
/// 鍵比對不分大小寫（Windows env 語意），輸出鍵一律大寫。
pub fn simulate_batch_env(batch: &str, inherited: &[(String, String)]) -> Vec<(String, String)> {
    let mut env: Vec<(String, String)> = inherited
        .iter()
        .map(|(k, v)| (k.to_uppercase(), v.clone()))
        .collect();
    for line in batch.lines() {
        let line = line.trim_end_matches('\r');
        if line == BATCH_CLEAR_LINE {
            env.retain(|(k, _)| !k.starts_with("SERIALWRAP_"));
            continue;
        }
        let Some(body) = line.strip_prefix("set \"").and_then(|s| s.strip_suffix('"')) else {
            continue;
        };
        let Some((k, v)) = body.split_once('=') else {
            continue;
        };
        let k = k.to_uppercase();
        env.retain(|(key, _)| *key != k);
        if !v.is_empty() {
            env.push((k, v.to_string()));
        }
    }
    env
}

/// 未被使用的本機 TCP 埠（≠ 48700）。
pub fn pick_free_port() -> u16 {
    for _ in 0..16 {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind 127.0.0.1:0");
        let port = listener.local_addr().expect("local_addr").port();
        if port != PRODUCTION_PORT {
            return port;
        }
    }
    panic!("取不到 48700 以外的空閒埠");
}

// ---------------------------------------------------------------------------
// 最小 JSON（daemon RPC 回應；不新增依賴）
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(items) => items.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }
    pub fn as_u32(&self) -> Option<u32> {
        match self {
            Json::Num(n) if *n >= 0.0 && n.fract() == 0.0 && *n <= f64::from(u32::MAX) => Some(*n as u32),
            _ => None,
        }
    }
    pub fn as_arr(&self) -> Option<&[Json]> {
        match self {
            Json::Arr(items) => Some(items),
            _ => None,
        }
    }
}

pub fn parse_json(text: &str) -> Result<Json, String> {
    let chars: Vec<char> = text.chars().collect();
    let mut pos = 0usize;
    let value = json_value(&chars, &mut pos)?;
    json_ws(&chars, &mut pos);
    if pos != chars.len() {
        return Err(format!("JSON 尾端多餘字元（位置 {pos}）"));
    }
    Ok(value)
}

fn json_ws(c: &[char], pos: &mut usize) {
    while *pos < c.len() && matches!(c[*pos], ' ' | '\t' | '\n' | '\r') {
        *pos += 1;
    }
}

fn json_value(c: &[char], pos: &mut usize) -> Result<Json, String> {
    json_ws(c, pos);
    match c.get(*pos) {
        None => Err("JSON 提前結束".into()),
        Some('{') => {
            *pos += 1;
            let mut items = Vec::new();
            json_ws(c, pos);
            if c.get(*pos) == Some(&'}') {
                *pos += 1;
                return Ok(Json::Obj(items));
            }
            loop {
                json_ws(c, pos);
                let Json::Str(key) = json_value(c, pos)? else {
                    return Err(format!("JSON 物件鍵不是字串（位置 {pos}）"));
                };
                json_ws(c, pos);
                if c.get(*pos) != Some(&':') {
                    return Err(format!("JSON 缺少 ':'（位置 {pos}）"));
                }
                *pos += 1;
                let value = json_value(c, pos)?;
                items.push((key, value));
                json_ws(c, pos);
                match c.get(*pos) {
                    Some(',') => *pos += 1,
                    Some('}') => {
                        *pos += 1;
                        return Ok(Json::Obj(items));
                    }
                    _ => return Err(format!("JSON 物件缺少 ',' 或 '}}'（位置 {pos}）")),
                }
            }
        }
        Some('[') => {
            *pos += 1;
            let mut items = Vec::new();
            json_ws(c, pos);
            if c.get(*pos) == Some(&']') {
                *pos += 1;
                return Ok(Json::Arr(items));
            }
            loop {
                items.push(json_value(c, pos)?);
                json_ws(c, pos);
                match c.get(*pos) {
                    Some(',') => *pos += 1,
                    Some(']') => {
                        *pos += 1;
                        return Ok(Json::Arr(items));
                    }
                    _ => return Err(format!("JSON 陣列缺少 ',' 或 ']'（位置 {pos}）")),
                }
            }
        }
        Some('"') => {
            *pos += 1;
            let mut s = String::new();
            loop {
                let ch = *c.get(*pos).ok_or("JSON 字串未結束")?;
                *pos += 1;
                match ch {
                    '"' => return Ok(Json::Str(s)),
                    '\\' => {
                        let esc = *c.get(*pos).ok_or("JSON 跳脫未結束")?;
                        *pos += 1;
                        match esc {
                            '"' => s.push('"'),
                            '\\' => s.push('\\'),
                            '/' => s.push('/'),
                            'b' => s.push('\u{8}'),
                            'f' => s.push('\u{c}'),
                            'n' => s.push('\n'),
                            'r' => s.push('\r'),
                            't' => s.push('\t'),
                            'u' => {
                                let hi = json_hex4(c, pos)?;
                                let code = if (0xd800..0xdc00).contains(&hi)
                                    && c.get(*pos) == Some(&'\\')
                                    && c.get(*pos + 1) == Some(&'u')
                                {
                                    *pos += 2;
                                    let lo = json_hex4(c, pos)?;
                                    0x10000 + ((hi - 0xd800) << 10) + (lo.wrapping_sub(0xdc00) & 0x3ff)
                                } else {
                                    hi
                                };
                                s.push(char::from_u32(code).unwrap_or('\u{fffd}'));
                            }
                            other => return Err(format!("JSON 未知跳脫 \\{other}")),
                        }
                    }
                    other => s.push(other),
                }
            }
        }
        Some('t') => json_lit(c, pos, "true", Json::Bool(true)),
        Some('f') => json_lit(c, pos, "false", Json::Bool(false)),
        Some('n') => json_lit(c, pos, "null", Json::Null),
        Some(_) => {
            let start = *pos;
            while *pos < c.len() && matches!(c[*pos], '-' | '+' | '.' | 'e' | 'E' | '0'..='9') {
                *pos += 1;
            }
            let raw: String = c[start..*pos].iter().collect();
            raw.parse::<f64>()
                .map(Json::Num)
                .map_err(|_| format!("JSON 數值無效：{raw:?}（位置 {start}）"))
        }
    }
}

fn json_hex4(c: &[char], pos: &mut usize) -> Result<u32, String> {
    let raw: String = c.get(*pos..*pos + 4).ok_or("JSON \\u 不足 4 位")?.iter().collect();
    *pos += 4;
    u32::from_str_radix(&raw, 16).map_err(|_| format!("JSON \\u 無效：{raw}"))
}

fn json_lit(c: &[char], pos: &mut usize, word: &str, value: Json) -> Result<Json, String> {
    let end = *pos + word.chars().count();
    let got: String = c.get(*pos..end).unwrap_or(&[]).iter().collect();
    if got == word {
        *pos = end;
        Ok(value)
    } else {
        Err(format!("JSON 字面值無效（位置 {pos}）"))
    }
}

// ---------------------------------------------------------------------------
// daemon RPC（RPC_WIRE：一行 JSON 請求 + "\n"，一行回應）
// ---------------------------------------------------------------------------

/// RPC 失敗分類：連線階段失敗（daemon 不可達）與其他失敗。
#[derive(Debug, Clone, PartialEq)]
pub enum RpcError {
    Connect(String),
    Other(String),
}

/// 對測試 daemon（`127.0.0.1:<port>`，≠ 48700）送一次 RPC。
pub fn rpc_call(port: u16, method: &str, params_json: &str, timeout: Duration) -> Result<Json, RpcError> {
    assert_ne!(port, PRODUCTION_PORT, "不得連線正式 daemon 埠 {PRODUCTION_PORT}");
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    let mut stream =
        TcpStream::connect_timeout(&addr, timeout).map_err(|e| RpcError::Connect(e.to_string()))?;
    stream
        .set_read_timeout(Some(timeout))
        .and_then(|_| stream.set_write_timeout(Some(timeout)))
        .map_err(|e| RpcError::Other(e.to_string()))?;
    let request = format!("{{\"id\":1,\"method\":\"{method}\",\"params\":{params_json}}}\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|e| RpcError::Other(format!("send {method}: {e}")))?;
    let deadline = Instant::now() + timeout;
    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];
    while !buf.contains(&b'\n') {
        if Instant::now() >= deadline {
            return Err(RpcError::Other(format!("{method} 回應逾時")));
        }
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(e) => return Err(RpcError::Other(format!("recv {method}: {e}"))),
        }
    }
    let line_end = buf.iter().position(|b| *b == b'\n').unwrap_or(buf.len());
    let text = String::from_utf8_lossy(&buf[..line_end]).into_owned();
    if text.trim().is_empty() {
        return Err(RpcError::Other(format!("{method} 回應為空")));
    }
    parse_json(&text).map_err(|e| RpcError::Other(format!("{method} 回應不是 JSON（{e}）：{text}")))
}

/// 防線 3：daemon 起來後的第一件事。回傳 daemon pid；任一不符回 `Err(原因)`。
///
/// - `health.status` 成功且帶 pid，`test_device_allowlist` 恰為 `{enabled:true, coms:["COM250"], source:"env"}`；
/// - `session.list` 成功且 `sessions` 為空陣列（未開任何 COM）。
pub fn verify_daemon_isolated(port: u16) -> Result<u32, String> {
    let status = rpc_call(port, "health.status", "{}", RPC_TIMEOUT).map_err(|e| format!("health.status 失敗：{e:?}"))?;
    if status.get("ok").and_then(Json::as_bool) != Some(true) {
        return Err(format!("health.status 不是 ok：{status:?}"));
    }
    let pid = status
        .get("pid")
        .and_then(Json::as_u32)
        .ok_or_else(|| format!("health.status 缺 pid：{status:?}"))?;
    let allow = status
        .get("test_device_allowlist")
        .ok_or_else(|| format!("allowlist 未生效（health.status 無 test_device_allowlist）：{status:?}"))?;
    let coms: Vec<&str> = allow
        .get("coms")
        .and_then(Json::as_arr)
        .map(|items| items.iter().filter_map(Json::as_str).collect())
        .unwrap_or_default();
    if allow.get("enabled").and_then(Json::as_bool) != Some(true)
        || coms != [ALLOWLIST_COM]
        || allow.get("source").and_then(Json::as_str) != Some("env")
    {
        return Err(format!("allowlist 與預期不符：{allow:?}"));
    }
    let sessions = rpc_call(port, "session.list", "{}", RPC_TIMEOUT).map_err(|e| format!("session.list 失敗：{e:?}"))?;
    if sessions.get("ok").and_then(Json::as_bool) != Some(true) {
        return Err(format!("session.list 不是 ok：{sessions:?}"));
    }
    match sessions.get("sessions").and_then(Json::as_arr) {
        Some([]) => Ok(pid),
        Some(items) => Err(format!("session 數為 {}（必須為 0）：{sessions:?}", items.len())),
        None => Err(format!("session.list 缺 sessions：{sessions:?}")),
    }
}

/// plan API：等到測試 daemon 回應 `health.status`，回傳 pid（逾時 panic）。
pub fn wait_daemon_pid(port: u16, timeout: Duration) -> u32 {
    wait_pid_where(port, timeout, |_| true).unwrap_or_else(|| panic!("{timeout:?} 內測試 daemon 未回應 health.status"))
}

/// plan API：等到測試 daemon 回報與 `old` 不同的 pid（逾時 panic）。
pub fn wait_new_daemon_pid(port: u16, old: u32, timeout: Duration) -> u32 {
    wait_pid_where(port, timeout, |pid| pid != old)
        .unwrap_or_else(|| panic!("{timeout:?} 內排程未重新拉起 daemon（舊 pid {old}）"))
}

/// 同 [`wait_daemon_pid`]，逾時回 `None`（呼叫端附 daemon.log 再判失敗）。
pub fn try_wait_daemon_pid(port: u16, timeout: Duration) -> Option<u32> {
    wait_pid_where(port, timeout, |_| true)
}

/// 同 [`wait_new_daemon_pid`]，逾時回 `None`。
pub fn try_wait_new_daemon_pid(port: u16, old: u32, timeout: Duration) -> Option<u32> {
    wait_pid_where(port, timeout, |pid| pid != old)
}

/// 觀察 windows-task 崩潰後不會自動重新拉起：90 秒內不得出現新 pid，工作狀態須維持非執行中。
///
/// 每 250 ms 探測 staging RPC，每秒查詢具名排程工作，每 5 秒核對 staging 執行檔行程；任何查詢無法
/// 證明安全條件或發現重啟即回傳錯誤。呼叫前應先等工作離開 Running，讓觀察窗只涵蓋崩潰後的靜止狀態。
pub fn observe_no_task_restart(
    env: &dyn SysEnv,
    ctl: &dyn ServiceCtl,
    port: u16,
    task: &str,
    exe: &str,
    old_pid: u32,
    timeout: Duration,
) -> Result<NoRestartObservation, String> {
    assert_test_task_name(task);
    if timeout.is_zero() {
        return Err("OD-83 觀察窗不得為零".into());
    }

    let started = Instant::now();
    let deadline = started + timeout;
    let mut next_task_check = started;
    let mut next_process_check = started;
    let mut rpc_checks = 0usize;
    let mut task_checks = 0usize;
    let mut process_checks = 0usize;

    loop {
        rpc_checks += 1;
        if let Ok(reply) = rpc_call(port, "health.status", "{}", Duration::from_millis(250)) {
            if reply.get("ok").and_then(Json::as_bool) == Some(true) {
                let pid = reply
                    .get("pid")
                    .and_then(Json::as_u32)
                    .ok_or_else(|| format!("health.status 回應成功但沒有 pid：{reply:?}"))?;
                if pid != old_pid {
                    return Err(format!(
                        "90 秒觀察窗內 health.status 出現新 daemon pid {pid}（舊 pid {old_pid}）"
                    ));
                }
                return Err(format!(
                    "taskkill_force 後舊 daemon pid {old_pid} 仍回應 health.status"
                ));
            }
        }

        let now = Instant::now();
        if now >= next_task_check {
            let state = ctl.query_named(&SupervisionMode::WindowsTask, task);
            if !matches!(state, TaskQuery::NotRunning(_)) {
                return Err(format!(
                    "90 秒觀察窗內排程工作狀態不是 NotRunning：{state:?}"
                ));
            }
            task_checks += 1;
            next_task_check = now + Duration::from_secs(1);
        }

        if now >= next_process_check {
            let pids = staging_exe_pids(env, exe)?;
            if pids.contains(&old_pid) {
                return Err(format!(
                    "taskkill_force 後 staging daemon 舊 pid {old_pid} 仍存在"
                ));
            }
            if let Some(pid) = pids.first() {
                return Err(format!(
                    "90 秒觀察窗內 staging daemon 出現新 pid {pid}（舊 pid {old_pid}）"
                ));
            }
            process_checks += 1;
            next_process_check = now + Duration::from_secs(5);
        }

        if now >= deadline {
            break;
        }
        std::thread::sleep(Duration::from_millis(250));
    }

    let final_task_state = ctl.query_named(&SupervisionMode::WindowsTask, task);
    if !matches!(final_task_state, TaskQuery::NotRunning(_)) {
        return Err(format!(
            "90 秒觀察窗結束時排程工作狀態不是 NotRunning：{final_task_state:?}"
        ));
    }
    let final_process_pids = staging_exe_pids(env, exe)?;
    if !final_process_pids.is_empty() {
        return Err(format!(
            "90 秒觀察窗結束時仍有 staging daemon 行程：{final_process_pids:?}"
        ));
    }

    Ok(NoRestartObservation {
        elapsed: started.elapsed(),
        rpc_checks,
        task_checks,
        process_checks,
        final_task_state,
        final_process_pids,
    })
}

#[derive(Debug, Clone)]
pub struct NoRestartObservation {
    pub elapsed: Duration,
    pub rpc_checks: usize,
    pub task_checks: usize,
    pub process_checks: usize,
    pub final_task_state: TaskQuery,
    pub final_process_pids: Vec<u32>,
}

fn wait_pid_where(port: u16, timeout: Duration, ok: impl Fn(u32) -> bool) -> Option<u32> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Ok(status) = rpc_call(port, "health.status", "{}", RPC_TIMEOUT) {
            if let Some(pid) = status.get("pid").and_then(Json::as_u32) {
                if ok(pid) {
                    return Some(pid);
                }
            }
        }
        if Instant::now() >= deadline {
            return None;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

// ---------------------------------------------------------------------------
// Task Scheduler XML（`schtasks /Query /XML`；元素路徑比對，不比對原始字串）
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct XmlTree {
    pub name: String,
    pub text: String,
    pub children: Vec<XmlTree>,
}

/// 最小 XML 解析：宣告、註解、屬性（略過）、自閉合元素、五個預設 entity；忽略縮排與空白（text 會 trim）。
pub fn parse_xml(text: &str) -> Result<XmlTree, String> {
    let mut rest = text.trim_start_matches('\u{feff}');
    let mut stack: Vec<XmlTree> = vec![XmlTree::default()];
    loop {
        let Some(lt) = rest.find('<') else {
            if !rest.trim().is_empty() {
                return Err("XML 根元素外有文字".into());
            }
            break;
        };
        let (txt, after) = rest.split_at(lt);
        if let Some(top) = stack.last_mut() {
            top.text.push_str(&xml_unescape(txt));
        }
        rest = after;
        if let Some(r) = rest.strip_prefix("<?") {
            let end = r.find("?>").ok_or("XML 宣告未結束")?;
            rest = &r[end + 2..];
        } else if let Some(r) = rest.strip_prefix("<!--") {
            let end = r.find("-->").ok_or("XML 註解未結束")?;
            rest = &r[end + 3..];
        } else if let Some(r) = rest.strip_prefix("</") {
            let end = r.find('>').ok_or("XML 結束標籤未結束")?;
            let name = r[..end].trim();
            let mut node = stack.pop().ok_or("XML 結束標籤多於開始標籤")?;
            if node.name != name {
                return Err(format!("XML 標籤不成對：<{}> 與 </{name}>", node.name));
            }
            node.text = node.text.trim().to_string();
            stack.last_mut().ok_or("XML 結構錯誤")?.children.push(node);
            rest = &r[end + 1..];
        } else {
            let r = &rest[1..];
            let end = r.find('>').ok_or("XML 開始標籤未結束")?;
            let inner = &r[..end];
            let self_closing = inner.ends_with('/');
            let inner = inner.trim_end_matches('/');
            let name = inner.split_whitespace().next().ok_or("XML 標籤名稱為空")?.to_string();
            let node = XmlTree { name, ..XmlTree::default() };
            if self_closing {
                stack.last_mut().ok_or("XML 結構錯誤")?.children.push(node);
            } else {
                stack.push(node);
            }
            rest = &r[end + 1..];
        }
    }
    if stack.len() != 1 {
        return Err("XML 元素未全部結束".into());
    }
    let mut doc = stack.pop().unwrap_or_default();
    if doc.children.len() != 1 {
        return Err(format!("XML 必須恰有一個根元素（實際 {}）", doc.children.len()));
    }
    Ok(doc.children.remove(0))
}

fn xml_unescape(s: &str) -> String {
    s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

/// plan API：根元素下以 `/` 分隔的元素路徑之文字（例如 `Settings/RestartOnFailure/Interval`）；找不到 panic。
pub fn xml_text(tree: &XmlTree, path: &str) -> String {
    let mut node = tree;
    for part in path.split('/') {
        node = node
            .children
            .iter()
            .find(|c| c.name == part)
            .unwrap_or_else(|| panic!("XML 缺少元素路徑 {path}（於 {part}）"));
    }
    node.text.clone()
}

/// UTF-16LE／BE（有 BOM）或 UTF-8 → 字串。
pub fn decode_task_xml(bytes: &[u8]) -> String {
    let utf16 = |be: bool| {
        let units: Vec<u16> = bytes[2..]
            .chunks_exact(2)
            .map(|c| if be { u16::from_be_bytes([c[0], c[1]]) } else { u16::from_le_bytes([c[0], c[1]]) })
            .collect();
        String::from_utf16_lossy(&units)
    };
    if bytes.starts_with(&[0xff, 0xfe]) {
        utf16(false)
    } else if bytes.starts_with(&[0xfe, 0xff]) {
        utf16(true)
    } else {
        String::from_utf8_lossy(bytes).into_owned()
    }
}

/// plan API：`schtasks /Query /TN <task> /XML` 並解析（只允許測試工作名稱）。
pub fn query_xml(env: &dyn SysEnv, task: &str) -> XmlTree {
    assert_test_task_name(task);
    let argv = argv(&["schtasks", "/Query", "/TN", task, "/XML"]);
    let out = env
        .run_raw(&argv, None, Some(CMD_TIMEOUT))
        .unwrap_or_else(|e| panic!("{argv:?} 無法執行：{}", e.py_message));
    assert_eq!(out.rc, 0, "{argv:?} rc={} stderr={}", out.rc, String::from_utf8_lossy(&out.stderr));
    parse_xml(&decode_task_xml(&out.stdout)).unwrap_or_else(|e| panic!("Task XML 解析失敗：{e}"))
}

// ---------------------------------------------------------------------------
// 行程：只以 staging 執行檔完整路徑辨識（不以映像名稱比對，避免碰到正式 daemon）
// ---------------------------------------------------------------------------

/// 執行檔路徑的比對鍵：能解析時取 canonical 長路徑（處理 8.3 短檔名、`\\?\` 前綴），再統一 `\` 與小寫。
pub fn normalize_exe_path(path: &str) -> String {
    let canon = std::fs::canonicalize(path)
        .map(|c| c.to_string_lossy().into_owned())
        .unwrap_or_else(|_| path.to_string());
    canon.trim_start_matches(r"\\?\").replace('/', "\\").to_lowercase()
}

/// 解析 `<pid>|<ExecutablePath>` 行，回傳執行檔與 `exe` 相同者的 pid（路徑讀不到的行程一律不匹配）。
pub fn matching_exe_pids(listing: &str, exe: &str) -> Result<Vec<u32>, String> {
    let want = normalize_exe_path(exe);
    let mut pids = Vec::new();
    for line in listing.lines().map(str::trim).filter(|l| !l.is_empty()) {
        let (pid, path) = line.split_once('|').ok_or_else(|| format!("無法解析行程列：{line:?}"))?;
        let pid = pid.trim().parse::<u32>().map_err(|_| format!("無法解析 pid：{line:?}"))?;
        let path = path.trim();
        if !path.is_empty() && normalize_exe_path(path) == want {
            pids.push(pid);
        }
    }
    Ok(pids)
}

/// 執行檔完整路徑等於 `exe` 的 `serialwrapd.exe` 行程 pid（比對在 Rust 端進行，不以映像名稱判定）。
pub fn staging_exe_pids(env: &dyn SysEnv, exe: &str) -> Result<Vec<u32>, String> {
    let script = format!(
        "Get-CimInstance Win32_Process -Filter \"Name='{DAEMON_EXE}'\" | ForEach-Object {{ \"$($_.ProcessId)|$($_.ExecutablePath)\" }}"
    );
    let argv = argv(&["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", &script]);
    let out = env
        .run(&argv, Some(CMD_TIMEOUT))
        .map_err(|e| format!("列舉 staging daemon 失敗：{}", e.py_message))?;
    if out.rc != 0 {
        return Err(format!("列舉 staging daemon rc={} stderr={}", out.rc, out.stderr.trim()));
    }
    matching_exe_pids(&out.stdout, exe)
}

/// plan API：強制終止 staging daemon（`taskkill /F /PID`）；pid 不屬於 staging 執行檔即 panic、不終止。
pub fn taskkill_force(env: &dyn SysEnv, pid: u32, exe: &str) {
    let pids = staging_exe_pids(env, exe).unwrap_or_else(|e| panic!("{e}"));
    assert!(pids.contains(&pid), "pid {pid} 不是 staging daemon（{exe}；目前 {pids:?}），拒絕終止");
    let argv = argv(&["taskkill", "/F", "/PID", &pid.to_string()]);
    let out = env
        .run(&argv, Some(CMD_TIMEOUT))
        .unwrap_or_else(|e| panic!("{argv:?} 無法執行：{}", e.py_message));
    assert_eq!(out.rc, 0, "{argv:?} rc={} stderr={}", out.rc, out.stderr.trim());
}

/// 結束全部 staging daemon（收尾用；錯誤收集回傳，不 panic）。
pub fn kill_staging_daemons(env: &dyn SysEnv, exe: &str) -> Vec<String> {
    let mut errors = Vec::new();
    match staging_exe_pids(env, exe) {
        Ok(pids) => {
            for pid in pids {
                let argv = argv(&["taskkill", "/F", "/PID", &pid.to_string()]);
                match env.run(&argv, Some(CMD_TIMEOUT)) {
                    Ok(out) if out.rc == 0 => {}
                    Ok(out) => errors.push(format!("{argv:?} rc={} stderr={}", out.rc, out.stderr.trim())),
                    Err(e) => errors.push(format!("{argv:?}: {}", e.py_message)),
                }
            }
        }
        Err(e) => errors.push(e),
    }
    errors
}

// ---------------------------------------------------------------------------
// 排程工作：有序停止（P4 graceful_stop 的具名對應）與收尾
// ---------------------------------------------------------------------------

/// 等到工作不在執行中（`NotRunning`）；`QueryFailed` 不視為已停止。
pub fn wait_task_not_running(ctl: &dyn ServiceCtl, name: &str, timeout: Duration) -> bool {
    assert_test_task_name(name);
    let deadline = Instant::now() + timeout;
    loop {
        if matches!(ctl.query_named(&SupervisionMode::WindowsTask, name), TaskQuery::NotRunning(_)) {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

/// 等到具名工作停止或已不存在；查詢失敗不視為停止。
fn wait_task_stopped_or_absent(ctl: &dyn ServiceCtl, name: &str, timeout: Duration) -> TaskQuery {
    assert_test_task_name(name);
    let deadline = Instant::now() + timeout;
    loop {
        let state = ctl.query_named(&SupervisionMode::WindowsTask, name);
        if matches!(state, TaskQuery::NotRunning(_) | TaskQuery::NotFound)
            || Instant::now() >= deadline
        {
            return state;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

/// harness（正式 `serialwrap service stop`）的結果。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct FormalStopReport {
    /// 正式 CLI 的 exit code。
    pub cli_rc: i32,
    pub cli_stdout: String,
    pub cli_stderr: String,
    /// CLI exit 0 且回應 `ok:true`。
    pub ok: bool,
    /// 回應的 `graceful`（缺欄位為 `None`）。
    pub graceful: Option<bool>,
    /// harness 實際執行的監管命令（改寫為測試工作後的 argv、rc）。
    pub ran: Vec<(Vec<String>, i32)>,
    /// harness 實際送出的 RPC 方法（依序）。
    pub rpc: Vec<String>,
    /// harness 拒絕的子行程或連線。
    pub refused: Vec<String>,
    /// 是否執行了 `/End`。
    pub ran_end: bool,
    /// `daemon.stop` 是否實際送出（連線成功後寫出請求）。
    pub sent_stop: bool,
}

/// 解析 harness stdout 的最後一行 JSON（sw-cli `ScopedStopReport::to_json`）。
pub fn parse_harness_output(stdout: &str) -> Result<FormalStopReport, String> {
    let line = stdout
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .ok_or_else(|| "harness stdout 為空".to_string())?;
    let value = parse_json(line).map_err(|error| format!("harness JSON 無效：{error}"))?;
    if !matches!(&value, Json::Obj(_)) {
        return Err("harness JSON 根節點必須是物件".into());
    }
    let string = |value: &Json, key: &str| {
        value
            .get(key)
            .and_then(Json::as_str)
            .map(str::to_string)
            .ok_or_else(|| format!("harness JSON 缺字串欄位 {key}"))
    };
    let integer = |value: &Json, key: &str| match value.get(key) {
        Some(Json::Num(number)) if number.fract() == 0.0 && *number >= f64::from(i32::MIN) && *number <= f64::from(i32::MAX) => Ok(*number as i32),
        _ => Err(format!("harness JSON 缺合法整數欄位 {key}")),
    };
    let strings = |value: &Json, key: &str| -> Result<Vec<String>, String> {
        value
            .get(key)
            .and_then(Json::as_arr)
            .ok_or_else(|| format!("harness JSON 缺陣列欄位 {key}"))?
            .iter()
            .map(|value| value.as_str().map(str::to_string).ok_or_else(|| format!("harness 欄位 {key} 必須只含字串")))
            .collect()
    };
    let rc = integer(&value, "rc")?;
    let stdout = string(&value, "stdout")?;
    let stderr = string(&value, "stderr")?;
    let refused = strings(&value, "refused")?;
    let rpc = strings(&value, "rpc")?;
    let ran_values = value
        .get("ran")
        .and_then(Json::as_arr)
        .ok_or_else(|| "harness JSON 缺陣列欄位 ran".to_string())?;
    let mut ran = Vec::new();
    for value in ran_values {
        if !matches!(value, Json::Obj(_)) {
            return Err("harness ran 項目必須是物件".into());
        }
        let argv = strings(value, "argv")?;
        if argv.is_empty() {
            return Err("harness ran.argv 不得為空".into());
        }
        ran.push((argv, integer(value, "rc")?));
    }
    let cli_value = parse_json(stdout.trim())
        .map_err(|error| format!("正式 CLI stdout JSON 無效：{error}"))?;
    if !matches!(&cli_value, Json::Obj(_)) {
        return Err("正式 CLI stdout JSON 根節點必須是物件".into());
    }
    let cli_ok = cli_value.get("ok").and_then(Json::as_bool) == Some(true);
    let graceful = match cli_value.get("graceful") {
        Some(Json::Bool(value)) => Some(*value),
        None => None,
        _ => return Err("正式 CLI graceful 欄位必須是布林".into()),
    };
    let ran_end = ran.iter().any(|(argv, _)| {
        argv.len() >= 2 && argv[0].eq_ignore_ascii_case("schtasks") && argv[1].to_ascii_lowercase() == "/end"
    });
    let sent_stop = rpc.iter().any(|method| method == "daemon.stop");
    Ok(FormalStopReport {
        cli_rc: rc,
        cli_stdout: stdout,
        cli_stderr: stderr,
        ok: rc == 0 && cli_ok && refused.is_empty(),
        graceful,
        ran,
        rpc,
        refused,
        ran_end,
        sent_stop,
    })
}

/// 實跑測試執行檔旁的 harness（win-runner 把 `SW_WIN_EXTRA_FILES` 複製到測試執行檔所在目錄）；缺檔 → `Err`（證據不足）。
pub fn locate_harness() -> Result<PathBuf, String> {
    let executable = std::env::current_exe().map_err(|error| format!("取得測試執行檔路徑失敗：{error}"))?;
    let harness = executable.with_file_name(HARNESS_EXE);
    if harness.is_file() {
        Ok(harness)
    } else {
        Err(format!("正式停止 harness 不存在（證據不足）：{}", harness.display()))
    }
}

/// 以 harness 執行正式 `serialwrap service stop`（P4 `graceful_stop`），只操作測試工作 `task` 與 staging 埠。
pub fn formal_service_stop(env: &dyn SysEnv, harness: &Path, spec: &StagingSpec, task: &str) -> Result<FormalStopReport, String> {
    if !harness.is_file() {
        return Err(format!("正式停止 harness 不存在：{}", harness.display()));
    }
    if spec.port == 0 || spec.port == PRODUCTION_PORT {
        return Err(format!("測試 RPC 埠不得為 0 或正式埠 {PRODUCTION_PORT}"));
    }
    assert_test_task_name(task);
    let argv = spec.harness_argv(&harness.to_string_lossy(), task);
    let output = env
        .run(&argv, Some(CMD_TIMEOUT))
        .map_err(|error| format!("執行正式停止 harness 失敗：{}", error.py_message))?;
    if output.rc != 0 {
        return Err(format!(
            "正式停止 harness rc={} stderr={} stdout={}",
            output.rc,
            output.stderr.trim(),
            output.stdout.trim()
        ));
    }
    parse_harness_output(&output.stdout)
}

/// 測試工作與 staging daemon 的收尾守衛：成功或 panic 都會執行（`Drop`）；`finish` 另檢查無殘留。
pub struct TaskGuard {
    env: Arc<dyn SysEnv>,
    ctl: Arc<dyn ServiceCtl>,
    name: String,
    exe: String,
    done: bool,
}

impl TaskGuard {
    pub fn new(env: Arc<dyn SysEnv>, ctl: Arc<dyn ServiceCtl>, name: &str, exe: &str) -> TaskGuard {
        assert_test_task_name(name);
        TaskGuard {
            env,
            ctl,
            name: name.to_string(),
            exe: exe.to_string(),
            done: false,
        }
    }

    fn cleanup(&self) -> Vec<String> {
        let mut errors = Vec::new();
        let mode = SupervisionMode::WindowsTask;
        let hold = self.ctl.hold_autostart_named(&mode, &self.name, false);
        let held = hold.is_ok();
        let hold_error = hold
            .err()
            .map(|error| format!("停用測試工作失敗：{error:?}"));

        // /Delete 不會結束仍在執行的批次檔。先停用具名工作，再 /End 並等到停止，
        // 避免批次檔稍後才啟動 serialwrapd.exe，逃過後面的 staging 行程掃描。
        let end_argv = argv(&["schtasks", "/End", "/TN", &self.name]);
        let end_result = match self.env.run(&end_argv, Some(CMD_TIMEOUT)) {
            Ok(output) => format!("rc={} stderr={}", output.rc, output.stderr.trim()),
            Err(error) => format!("執行失敗：{}", error.py_message),
        };
        let stopped = wait_task_stopped_or_absent(&*self.ctl, &self.name, TASK_CLEANUP_TIMEOUT);
        let can_delete = match &stopped {
            TaskQuery::NotFound => true,
            TaskQuery::NotRunning(_) if held => true,
            TaskQuery::NotRunning(_) => {
                errors.push(format!(
                    "排程工作未能確認停用；不刪除以免延遲啟動（hold={hold_error:?}, /End={end_result}, state={stopped:?}）"
                ));
                false
            }
            TaskQuery::Running | TaskQuery::QueryFailed(_) => {
                errors.push(format!(
                    "排程工作在 /End 後仍未確認停止；不刪除（hold={hold_error:?}, /End={end_result}, state={stopped:?}）"
                ));
                false
            }
        };

        if can_delete {
            for record in self.ctl.uninstall_named(&mode, &self.name, false) {
                if record.rc != 0
                    && !matches!(self.ctl.query_named(&mode, &self.name), TaskQuery::NotFound)
                {
                    errors.push(format!("{:?} rc={}", record.argv, record.rc));
                }
            }
            match self.ctl.query_named(&mode, &self.name) {
                TaskQuery::NotFound => {}
                other => errors.push(format!("測試工作刪除後仍存在：{other:?}")),
            }
        }
        errors.extend(kill_staging_daemons(&*self.env, &self.exe));
        match staging_exe_pids(&*self.env, &self.exe) {
            Ok(pids) if pids.is_empty() => {}
            Ok(pids) => errors.push(format!("收尾後仍有 staging daemon：{pids:?}")),
            Err(error) => errors.push(error),
        }
        errors
    }

    /// 收尾並確認：測試工作已不存在、沒有殘留 staging daemon。
    pub fn finish(mut self) -> Result<(), String> {
        self.done = true;
        let mut errors = self.cleanup();
        match self.ctl.query_named(&SupervisionMode::WindowsTask, &self.name) {
            TaskQuery::NotFound => {}
            other => errors.push(format!("測試工作仍存在：{other:?}")),
        }
        match staging_exe_pids(&*self.env, &self.exe) {
            Ok(pids) if pids.is_empty() => {}
            Ok(pids) => errors.push(format!("殘留 staging daemon：{pids:?}")),
            Err(e) => errors.push(e),
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("；"))
        }
    }
}

impl Drop for TaskGuard {
    fn drop(&mut self) {
        if self.done {
            return;
        }
        let errors = self.cleanup();
        if !errors.is_empty() {
            eprintln!("[wintask] 收尾錯誤（請手動確認 {} 已刪除、無殘留 {}）：{errors:?}", self.name, self.exe);
        }
    }
}

// ---------------------------------------------------------------------------
// WAL：最後一筆完整（`raw.wal.ndjson` 最後一行以 \n 結尾、可解析、crc32 相符）
// ---------------------------------------------------------------------------

/// WAL 尾端狀態。T13 test-fakes staging hook 以 COM250 寫入可辨認紀錄，不開啟任何 UART。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WalTail {
    Absent,
    Empty,
    Complete { records: usize, last_seq: u64 },
    Torn(String),
}

pub fn wal_tail_state(dir: &Path) -> WalTail {
    let path: PathBuf = dir.join("raw.wal.ndjson");
    let bytes = match std::fs::read(&path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return WalTail::Absent,
        Err(e) => return WalTail::Torn(format!("無法讀取 {}：{e}", path.display())),
    };
    if bytes.is_empty() {
        return WalTail::Empty;
    }
    if !bytes.ends_with(b"\n") {
        return WalTail::Torn("最後一行沒有以 \\n 結尾".into());
    }
    let text = String::from_utf8_lossy(&bytes);
    let lines: Vec<&str> = text.split('\n').filter(|l| !l.trim_end_matches('\r').is_empty()).collect();
    let Some(last) = lines.last() else {
        return WalTail::Empty;
    };
    let last = last.trim_end_matches('\r');
    let record = match parse_json(last) {
        Ok(r) => r,
        Err(e) => return WalTail::Torn(format!("最後一行不是 JSON（{e}）")),
    };
    let (Some(payload), Some(crc)) = (
        record.get("payload_b64").and_then(Json::as_str),
        record.get("crc32").and_then(Json::as_str),
    ) else {
        return WalTail::Torn("最後一行缺 payload_b64／crc32".into());
    };
    let Some(raw) = b64_decode(payload) else {
        return WalTail::Torn("payload_b64 不是合法 base64".into());
    };
    let actual = format!("{:08x}", crc32(&raw));
    if actual != crc {
        return WalTail::Torn(format!("crc32 不符：紀錄 {crc}、實際 {actual}"));
    }
    let last_seq = match record.get("seq") {
        Some(Json::Num(n)) if *n >= 0.0 => *n as u64,
        _ => return WalTail::Torn("最後一行缺 seq".into()),
    };
    WalTail::Complete { records: lines.len(), last_seq }
}

/// 停止後 WAL 最後一筆完整的證據：只接受至少一筆紀錄且最後一筆可解析、CRC 相符（`Complete`），回傳（筆數, 最後 seq）。
/// 沒有 WAL 或 WAL 為空 → `Err`（證據不足，不得計為 PASS）；最後一筆不完整 → `Err`（失敗）。
pub fn wal_evidence(dir: &Path) -> Result<(usize, u64), String> {
    match wal_tail_state(dir) {
        WalTail::Complete { records, last_seq } if records > 0 => Ok((records, last_seq)),
        WalTail::Absent => Err(format!("證據不足：{} 不存在", dir.join("raw.wal.ndjson").display())),
        WalTail::Empty => Err(format!("證據不足：{} 為空", dir.join("raw.wal.ndjson").display())),
        WalTail::Complete { .. } => Err("證據不足：WAL 沒有紀錄".into()),
        WalTail::Torn(reason) => Err(format!("WAL 尾端不完整：{reason}")),
    }
}

/// 只有至少一筆紀錄且最後一筆 CRC 完整時回 true。
pub fn wal_last_line_crc_ok(dir: &Path) -> bool {
    matches!(wal_tail_state(dir), WalTail::Complete { records, .. } if records > 0)
}

/// 除了完整 WAL tail，也要求最後一筆是 staging daemon 的指定 test-fakes 紀錄。
pub fn wal_test_record_evidence(dir: &Path) -> Result<(usize, u64), String> {
    let evidence = wal_evidence(dir)?;
    let path = dir.join("raw.wal.ndjson");
    let bytes = std::fs::read(&path).map_err(|error| format!("讀取 {} 失敗：{error}", path.display()))?;
    let text = std::str::from_utf8(&bytes).map_err(|error| format!("WAL 不是 UTF-8：{error}"))?;
    let line = text
        .split('\n')
        .rev()
        .map(|line| line.trim_end_matches('\r'))
        .find(|line| !line.is_empty())
        .ok_or_else(|| "證據不足：WAL 沒有紀錄".to_string())?;
    let record = parse_json(line).map_err(|error| format!("最後一筆 WAL JSON 無效：{error}"))?;
    let valid = record.get("com").and_then(Json::as_str) == Some(ALLOWLIST_COM)
        && record.get("dir").and_then(Json::as_str) == Some("RX")
        && record.get("source").and_then(Json::as_str) == Some("test")
        && record
            .get("payload_b64")
            .and_then(Json::as_str)
            .and_then(b64_decode)
            .as_deref()
            == Some(TEST_WAL_PAYLOAD);
    if valid {
        Ok(evidence)
    } else {
        Err("最後一筆 WAL 不是 T13 staging daemon 的指定測試紀錄".into())
    }
}

/// CRC-32（IEEE 802.3，與 `crc32fast`／zlib 相同）。
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for &byte in data {
        crc ^= u32::from(byte);
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xedb8_8320 & mask);
        }
    }
    !crc
}

/// 標準 base64（含 `=` 補位）解碼；非法字元 → `None`。
pub fn b64_decode(s: &str) -> Option<Vec<u8>> {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = Vec::new();
    let (mut buf, mut bits) = (0u32, 0u32);
    for c in s.trim().bytes() {
        if c == b'=' {
            break;
        }
        let v = T.iter().position(|&t| t == c)? as u32;
        buf = (buf << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buf >> bits) as u8);
        }
    }
    Some(out)
}
