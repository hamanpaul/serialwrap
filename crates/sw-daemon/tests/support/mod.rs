//! P5-T19 daemon 行程測試共用支援（`daemon_process.rs`、`restart_after_commit.rs`）。
//!
//! 限制（`scripts/rust/check_cfg_boundary.py`）：測試碼不得出現平台 cfg、`std::os` 平台模組與 FFI crate。
//! POSIX 專用能力一律經 `sw_pal`（RPC 連線、PTY 對端）或外部 POSIX 工具（`chmod`、`id`、`kill`）取得。
//! PTY 假 DUT 以既有黑箱 `tests/contract_blackbox/bb_faketarget.py` 的 `FakeTarget` 實作（子行程，stdin/stdout 指令協定）。
#![cfg(feature = "test-fakes")]
#![allow(dead_code)]
#![allow(unused_imports)]

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use sw_pal::traits::{DeviceInfo, EndpointSpec, SockTimeout};
use sw_proto::pyjson::{self, PyValue};

pub use sw_daemon::daemon_main::{run_with_hooks, DaemonHooks, TraceEvent};

#[path = "../common/mod.rs"]
mod common;
pub use common::data_root;

/// P5-T18：`dispatch.rs` 的 `ServiceRig` 與 `deferred_methods`（追加，不改既有支援碼）。
mod service_rig;
pub use service_rig::*;

/// 以 `cargo test` 建置的 daemon 執行檔（`--features test-fakes` 下含測試專用 method）。
pub const DAEMON_BIN: &str = env!("CARGO_BIN_EXE_serialwrapd");
pub const PING: &str = r#"{"id":1,"method":"health.ping"}"#;
pub const STOP: &str = r#"{"id":2,"method":"daemon.stop"}"#;
pub const SHORT: Duration = Duration::from_secs(10);
pub const LONG: Duration = Duration::from_secs(30);

/// 目標 COM0 的 by-id 名稱（隔離 by-id 目錄內的 symlink，指向 PTY slave）。
pub const TARGET_BY_ID: &str = "usb-serialwrap-test-COM0-if00";
/// 假 DUT 的 prompt（與 `bb_faketarget.FakeTarget` 預設 prompt 一致，profile 的 prompt_regex 對應此值）。
pub const TARGET_PROMPT: &str = "root@fake:~# ";

/// 隔離環境要「pop」的高優先變數：以空字串覆寫（daemon 端 strip 後為空即視同未設）。
/// 外層 shell 的同名變數因此無效（與 `tests/conftest.py` 的 pop 規則一致）。
const POPPED_VARS: [&str; 5] = [
    "SERIALWRAP_ENDPOINT",
    "SERIALWRAP_PROFILE_DIR",
    "SERIALWRAP_TTYMCU_PATH",
    "SERIALWRAP_EVENTS_LOG_PATH",
    "SERIALWRAP_DATA_DIR",
];

static SEQ: AtomicUsize = AtomicUsize::new(0);

fn unique_root() -> PathBuf {
    let n = SEQ.fetch_add(1, Ordering::SeqCst);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    // Unix socket 路徑上限約 108 bytes，根目錄刻意保持短。
    let root = std::env::temp_dir().join(format!("sw21-{}-{}-{:x}", std::process::id(), n, nanos));
    fs::create_dir_all(&root).expect("建立隔離根目錄");
    root
}

/// 離開作用域時還原權限（唯讀目錄測試）並刪除整個隔離根目錄。
struct RootGuard(PathBuf);

impl Drop for RootGuard {
    fn drop(&mut self) {
        let _ = Command::new("chmod").arg("-R").arg("u+w").arg(&self.0).status();
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// 隔離實例（OD-11）：9 個目錄變數指向根目錄下的獨立路徑，5 個高優先變數 pop。
///
/// `vars` 為 `BTreeMap`，可直接 `Command::envs(&env.vars)`。欄位宣告順序即 Drop 順序：
/// 先停 PTY 假 DUT，最後才刪根目錄。
pub struct IsolatedEnv {
    pub vars: BTreeMap<String, String>,
    pub root: PathBuf,
    /// `<run>/serialwrapd.sock`（預設 endpoint）。
    pub socket: PathBuf,
    /// `<run>/serialwrapd.lock`。
    pub lock: PathBuf,
    pub run_dir: PathBuf,
    pub state_dir: PathBuf,
    pub config_dir: PathBuf,
    pub wal_dir: PathBuf,
    pub by_id_dir: PathBuf,
    pub by_path_dir: PathBuf,
    pub log_dir: PathBuf,
    pub events_dir: PathBuf,
    pub events_rt_dir: PathBuf,
    pub profile_dir: PathBuf,
    /// 系統設定檔路徑（`SERIALWRAP_TEST_SYSTEM_CONFIG_PATH`；預設不建立）。
    pub system_config: PathBuf,
    /// 假 DUT 的 PTY slave 路徑（僅 `isolated_env_with_pty_target` 有值）。
    pub pty_slave: PathBuf,
    target: Option<FakeTargetProc>,
    _root: RootGuard,
}

/// 建立隔離環境（尚未寫入任何 profile／config）。
pub fn isolated_env() -> IsolatedEnv {
    let root = unique_root();
    let guard = RootGuard(root.clone());
    let dir = |name: &str| -> PathBuf {
        let p = root.join(name);
        fs::create_dir_all(&p).expect("建立隔離目錄");
        p
    };
    let run_dir = dir("run");
    let state_dir = dir("state");
    let config_dir = dir("config");
    let wal_dir = dir("wal");
    let by_id_dir = dir("by-id");
    let by_path_dir = dir("by-path");
    let log_dir = dir("blog");
    let events_dir = dir("events.d");
    let events_rt_dir = dir("events-rt");
    let profile_dir = config_dir.join("profiles");
    fs::create_dir_all(&profile_dir).expect("建立 profiles 目錄");
    let home = dir("home");
    let system_config = root.join("system-config").join("config.yaml");

    let mut vars = BTreeMap::new();
    let pairs: [(&str, PathBuf); 9] = [
        ("SERIALWRAP_STATE_DIR", state_dir.clone()),
        ("SERIALWRAP_RUN_DIR", run_dir.clone()),
        ("SERIALWRAP_WAL_DIR", wal_dir.clone()),
        ("SERIALWRAP_CONFIG_DIR", config_dir.clone()),
        ("SERIALWRAP_LOG_DIR", log_dir.clone()),
        ("SERIALWRAP_EVENTS_DIR", events_dir.clone()),
        ("SERIALWRAP_EVENTS_RUNTIME_DIR", events_rt_dir.clone()),
        ("SERIALWRAP_BY_ID_DIR", by_id_dir.clone()),
        ("SERIALWRAP_BY_PATH_DIR", by_path_dir.clone()),
    ];
    for (key, value) in pairs {
        vars.insert(key.to_string(), value.display().to_string());
    }
    vars.insert(
        "SERIALWRAP_TEST_SYSTEM_CONFIG_PATH".to_string(),
        system_config.display().to_string(),
    );
    vars.insert("HOME".to_string(), home.display().to_string());
    vars.insert("XDG_CONFIG_HOME".to_string(), home.join(".config").display().to_string());
    vars.insert("XDG_STATE_HOME".to_string(), home.join(".state").display().to_string());
    vars.insert("XDG_DATA_HOME".to_string(), home.join(".data").display().to_string());
    vars.insert("XDG_RUNTIME_DIR".to_string(), String::new());
    vars.insert("SW_TEST_USER".to_string(), "root".to_string());
    vars.insert("SW_TEST_PASS".to_string(), "pw".to_string());
    for key in POPPED_VARS {
        vars.insert(key.to_string(), String::new());
    }
    // 額外 pop：config 路徑與測試白名單只由個別案例顯式設定。
    vars.insert("SERIALWRAP_CONFIG".to_string(), String::new());
    vars.insert("SERIALWRAP_TEST_DEVICE_ALLOWLIST".to_string(), String::new());

    IsolatedEnv {
        vars,
        root,
        socket: run_dir.join("serialwrapd.sock"),
        lock: run_dir.join("serialwrapd.lock"),
        run_dir,
        state_dir,
        config_dir,
        wal_dir,
        by_id_dir,
        by_path_dir,
        log_dir,
        events_dir,
        events_rt_dir,
        profile_dir,
        system_config,
        pty_slave: PathBuf::new(),
        target: None,
        _root: guard,
    }
}

/// 隔離環境＋COM0 的 PTY 假 DUT（shell 平台 profile，`root`／`pw`，prompt `root@fake:~# `）。
pub fn isolated_env_with_pty_target() -> IsolatedEnv {
    let mut env = isolated_env();
    write_target_config(&env.config_dir, &env.by_id_dir);
    let (target, slave) = FakeTargetProc::start(&env.root, &env.by_id_dir, TARGET_BY_ID);
    env.pty_slave = slave;
    env.target = Some(target);
    env
}

/// 寫入 `<CONFIG_DIR>/profiles/target.yaml`：一個 shell profile（`shell-test`）與 COM0 target（device_by_id 指向隔離 by-id 目錄）。
pub fn write_target_config(config_dir: &Path, by_id_dir: &Path) {
    let device = by_id_dir.join(TARGET_BY_ID);
    let yaml = format!(
        r#"profiles:
  shell-test:
    platform: shell
    prompt_regex: "(?m)^root@fake:~# "
    login_regex: "(?mi)login:\\s*$"
    password_regex: "(?mi)password:\\s*$"
    user_env: "SW_TEST_USER"
    pass_env: "SW_TEST_PASS"
    ready_probe: "echo __READY__${{nonce}}"
    timeout_s: 15
targets:
  - com: COM0
    profile: shell-test
    device_by_id: "{}"
"#,
        device.display()
    );
    // 修正（P5-T19）：daemon 只從 `PROFILE_DIR`（`<CONFIG_DIR>/profiles`）載入 profile（Python `load_profiles(args.profile_dir)`），
    // `config.yaml` 不含 profile。原預寫版本寫入 `config.yaml`，Python 與 Rust 兩端都載不到 COM0 target。
    let profile_dir = config_dir.join("profiles");
    fs::create_dir_all(&profile_dir).expect("建立 profiles 目錄");
    fs::write(profile_dir.join("target.yaml"), yaml).expect("寫入 profiles/target.yaml");
}

impl IsolatedEnv {
    /// 假 DUT 收到的全部位元組（自 PTY 假 DUT 啟動至今）。
    pub fn target_rx(&mut self) -> Vec<u8> {
        self.target
            .as_mut()
            .expect("無 PTY 假 DUT：請改用 isolated_env_with_pty_target()")
            .rx_bytes()
    }

    /// 假 DUT 暫停回應 `seconds` 秒（`FakeTarget.pause_responding`：輸入照記錄，回應延到暫停結束）。
    pub fn target_pause(&mut self, seconds: f64) {
        self.target
            .as_mut()
            .expect("無 PTY 假 DUT：請改用 isolated_env_with_pty_target()")
            .pause(seconds);
    }

    /// 讓假 DUT 不再回 prompt（DUT 靜默）。
    pub fn target_mute(&mut self) {
        self.target
            .as_mut()
            .expect("無 PTY 假 DUT：請改用 isolated_env_with_pty_target()")
            .mute();
    }
}

/// PTY 假 DUT 子行程（以 `bb_faketarget.FakeTarget` 驅動；協定：stdin 一行一指令，stdout 一行一回應）。
///
/// 指令：`RX`（回 `RX <hex>`，假 DUT 收到的全部位元組）、`MUTE`（改 prompt 為不可匹配字串，回 `OK`）、`EXIT`。
/// `MUTE` 為暫時手段：Task 15a 的 `FakeTarget.pause_responding` 落地後改用之。
pub struct FakeTargetProc {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

const RUNNER_PY: &str = r#"
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import os
from bb_faketarget import FakeTarget
class _Attachable(FakeTarget):
    # P5-T19 修正：比照 test_bb_daemon_e2e.py 的 DaemonAttachableTarget。FakeTarget 在 start() 後仍持有 slave fd，
    # daemon 的 BC-04 外部 holder 偵測（Python probe_holders／Rust UartExclusive）會把本行程視為外部 holder
    # （ATTACH_FAILED:DeviceHeldError）；連結建立後立即關閉 slave，reader 在 daemon 關閉／重開 slave 的 EIO 後持續存活。
    def _run_loop(self):
        while not self._stop.is_set():
            super()._run_loop()
            if self._stop.wait(0.05):
                break
ft = _Attachable(Path(sys.argv[2]))
ft.start(Path(sys.argv[3]), sys.argv[4])
if ft.slave_fd is not None:
    os.close(ft.slave_fd)
    ft.slave_fd = None
print("READY " + ft.slave_path, flush=True)
for raw in sys.stdin:
    cmd = raw.strip()
    if cmd == "RX":
        print("RX " + ft.rx_log(timeout_s=0.5).hex(), flush=True)
    elif cmd == "MUTE":
        ft.prompt = "muted-dut> "
        print("OK", flush=True)
    elif cmd.startswith("PAUSE "):
        ft.pause_responding(float(cmd.split(" ", 1)[1]))
        print("OK", flush=True)
    elif cmd == "EXIT":
        break
ft.stop()
"#;

impl FakeTargetProc {
    fn start(workdir: &Path, by_id_dir: &Path, name: &str) -> (FakeTargetProc, PathBuf) {
        // 以 repo 根目錄定位（不依賴 cwd：`cargo_l1_results.py` 等 runner 不在 crate 目錄執行測試 binary）。
        let lib = data_root().join("tests/contract_blackbox");
        let mut child = Command::new("python3")
            .arg("-I")
            .arg("-c")
            .arg(RUNNER_PY)
            .arg(&lib)
            .arg(workdir)
            .arg(by_id_dir)
            .arg(name)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("啟動 PTY 假 DUT（python3）");
        let stdin = child.stdin.take().expect("假 DUT stdin");
        let mut stdout = BufReader::new(child.stdout.take().expect("假 DUT stdout"));
        let mut line = String::new();
        stdout.read_line(&mut line).expect("讀取假 DUT 就緒行");
        let slave = line
            .strip_prefix("READY ")
            .unwrap_or_else(|| panic!("假 DUT 未就緒：{line:?}"))
            .trim()
            .to_string();
        (FakeTargetProc { child, stdin, stdout }, PathBuf::from(slave))
    }

    fn send(&mut self, cmd: &str) -> String {
        writeln!(self.stdin, "{cmd}").expect("送出假 DUT 指令");
        self.stdin.flush().ok();
        let mut line = String::new();
        self.stdout.read_line(&mut line).expect("讀取假 DUT 回應");
        line.trim_end().to_string()
    }

    fn rx_bytes(&mut self) -> Vec<u8> {
        let reply = self.send("RX");
        decode_hex(reply.strip_prefix("RX ").unwrap_or(""))
    }

    fn mute(&mut self) {
        assert_eq!(self.send("MUTE"), "OK", "假 DUT MUTE 失敗");
    }

    fn pause(&mut self, seconds: f64) {
        assert_eq!(self.send(&format!("PAUSE {seconds}")), "OK", "假 DUT PAUSE 失敗");
    }
}

impl Drop for FakeTargetProc {
    fn drop(&mut self) {
        let _ = writeln!(self.stdin, "EXIT");
        let _ = self.stdin.flush();
        std::thread::sleep(Duration::from_millis(50));
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn decode_hex(s: &str) -> Vec<u8> {
    s.as_bytes()
        .chunks(2)
        .filter(|c| c.len() == 2)
        .map(|c| u8::from_str_radix(std::str::from_utf8(c).expect("hex 字元"), 16).expect("hex 位元組"))
        .collect()
}

/// 由測試啟動的 daemon 行程；stdout 丟棄，stderr 寫入隔離根目錄檔案（可於執行中讀取）。
pub struct DaemonProc {
    child: Child,
    stderr_path: PathBuf,
}

impl DaemonProc {
    pub fn spawn(env: &IsolatedEnv, args: &[&str]) -> DaemonProc {
        Self::spawn_with(env, &env.vars, args)
    }

    pub fn spawn_with(env: &IsolatedEnv, vars: &BTreeMap<String, String>, args: &[&str]) -> DaemonProc {
        let n = SEQ.fetch_add(1, Ordering::SeqCst);
        let stderr_path = env.root.join(format!("daemon-{n}.stderr"));
        let file = File::create(&stderr_path).expect("建立 stderr 檔");
        let child = Command::new(DAEMON_BIN)
            .args(args)
            .envs(vars)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::from(file))
            .spawn()
            .expect("啟動 serialwrapd");
        DaemonProc { child, stderr_path }
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    pub fn stderr(&self) -> String {
        fs::read_to_string(&self.stderr_path).unwrap_or_default()
    }

    /// 輪詢等待 stderr 出現指定片段（daemon 仍在執行時亦可用）。
    pub fn wait_stderr_contains(&self, needle: &str, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        loop {
            let text = self.stderr();
            if text.contains(needle) {
                return;
            }
            if Instant::now() >= deadline {
                panic!("stderr 在 {timeout:?} 內未出現 {needle:?}：{text:?}");
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    /// 行程是否仍在執行（未結束）。
    pub fn try_alive(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    /// 等待行程結束；逾時 panic（不留下殘留行程：Drop 會 kill）。
    pub fn wait(&mut self, timeout: Duration) -> ExitStatus {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(status) = self.child.try_wait().expect("try_wait") {
                return status;
            }
            if Instant::now() >= deadline {
                panic!("serialwrapd 未在 {timeout:?} 內結束");
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
}

impl Drop for DaemonProc {
    fn drop(&mut self) {
        if matches!(self.child.try_wait(), Ok(None)) {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

/// 一次性執行結果（exit code 與完整 stderr）。
pub struct Output {
    pub code: Option<i32>,
    pub stderr: String,
}

/// 以 `vars` 啟動 daemon（`args` 原樣）並等待其結束。
pub fn run_to_exit(env: &IsolatedEnv, vars: &BTreeMap<String, String>, args: &[&str]) -> Output {
    let mut d = DaemonProc::spawn_with(env, vars, args);
    let status = d.wait(LONG);
    Output {
        code: status.code(),
        stderr: d.stderr(),
    }
}

/// 以測試執行檔自身重新啟動一個測試函式（子行程模式）。子行程以環境變數 `S21_CHILD=<mode>` 辨識，
/// 並在測試函式內呼叫 in-process 的 `run_with_hooks` 後以其 exit code 結束。
pub fn run_child_test(test_name: &str, mode: &str, env: &IsolatedEnv) -> Output {
    let exe = std::env::current_exe().expect("測試執行檔路徑");
    let out = Command::new(exe)
        .args(["--exact", test_name, "--nocapture", "--test-threads=1"])
        .envs(&env.vars)
        .env("S21_CHILD", mode)
        .stdin(Stdio::null())
        .output()
        .expect("啟動子行程測試");
    Output {
        code: out.status.code(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
    }
}

/// 送一行 NDJSON 請求並取回第一行回應；連線失敗、對端斷線或未收到回應時回傳空字串。
pub fn rpc_once(socket: &Path, line: &str) -> String {
    let platform = sw_pal::native();
    let endpoint = EndpointSpec::Unix(socket.as_os_str().to_owned());
    let mut stream = match platform
        .rpc
        .connect(&endpoint, SockTimeout::Blocking(Duration::from_secs(20)))
    {
        Ok(s) => s,
        Err(_) => return String::new(),
    };
    let mut request = line.as_bytes().to_vec();
    request.push(b'\n');
    if stream.send_all(&request).is_err() {
        return String::new();
    }
    let mut out = Vec::new();
    let mut buf = [0u8; 4096];
    while !out.contains(&b'\n') {
        match stream.recv(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => out.extend_from_slice(&buf[..n]),
        }
    }
    String::from_utf8_lossy(&out).lines().next().unwrap_or("").to_string()
}

/// `{"id":1,"method":…,"params":…}` 的便利包裝；回應解析為 `PyValue`（失敗時為 `Null`）。
pub fn rpc_value(socket: &Path, method: &str, params: &str) -> PyValue {
    let line = format!(r#"{{"id":1,"method":"{method}","params":{params}}}"#);
    pyjson::loads(&rpc_once(socket, &line)).unwrap_or(PyValue::Null)
}

/// 輪詢直到 `health.ping` 回 `ok:true`；逾時 panic。
pub fn wait_socket(socket: &Path, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    loop {
        if rpc_value(socket, "health.ping", "{}")["ok"] == true {
            return;
        }
        if Instant::now() >= deadline {
            panic!("daemon 未在 {timeout:?} 內就緒：{}", socket.display());
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// `session.get_state` 的 `session` 物件（找不到時為 `Null`）。
pub fn get_state(socket: &Path, com: &str) -> PyValue {
    rpc_value(socket, "session.get_state", &format!(r#"{{"selector":"{com}"}}"#))["session"].clone()
}

/// 輪詢直到 COM 進入 READY；逾時 panic（需要 PTY 假 DUT 與 shell 登入）。
pub fn wait_ready(env: &IsolatedEnv, com: &str) {
    let deadline = Instant::now() + LONG;
    loop {
        let state = get_state(&env.socket, com);
        if state["state"] == "READY" {
            return;
        }
        if Instant::now() >= deadline {
            // F91／F111：逾時時一併輸出 last_error 與假 DUT PTY 的其他持有者（含 cmdline），
            // 讓「外部行程搶開測試 PTY」（F94）這類環境干擾在失敗訊息中即可辨識，而非只剩狀態名。
            panic!(
                "{com} 未在 30 s 內進入 READY，最後狀態：{:?}，last_error：{:?}，{} 的持有者：{:?}",
                state["state"],
                state["last_error"],
                env.pty_slave.display(),
                path_holders(&env.pty_slave)
            );
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// 掃 `/proc/<pid>/fd` 找出開著 `path` 的行程（排除本測試行程），回 `(pid, cmdline)`。
///
/// 只用於失敗訊息的診斷：`/proc` 不存在或不可讀時回空清單，不影響判定。
pub fn path_holders(path: &Path) -> Vec<(u32, String)> {
    let me = std::process::id();
    // fd 的 readlink 為實體路徑：傳入 symlink（例如 by-id）時先解析（審查 7af6567_9b81d9a #3）。
    let path = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let mut out = Vec::new();
    let Ok(procs) = fs::read_dir("/proc") else {
        return out;
    };
    for entry in procs.flatten() {
        let Some(pid) = entry.file_name().to_str().and_then(|s| s.parse::<u32>().ok()) else {
            continue;
        };
        if pid == me {
            continue;
        }
        let Ok(fds) = fs::read_dir(entry.path().join("fd")) else {
            continue;
        };
        if fds.flatten().any(|fd| fs::read_link(fd.path()).map(|t| t == path).unwrap_or(false)) {
            let cmd = fs::read(entry.path().join("cmdline"))
                .map(|b| String::from_utf8_lossy(&b).replace('\0', " ").trim().to_string())
                .unwrap_or_default();
            out.push((pid, cmd));
        }
    }
    out
}

/// 測試自行 spawn 的子行程守衛：測試 panic 時 kill 並回收，避免子行程成為孤兒、
/// 持有 cargo 的輸出管線使整個 `cargo test` 卡住（F111 的第二段症狀）。
pub struct ChildGuard(pub Child);

impl ChildGuard {
    pub fn wait(&mut self) -> ExitStatus {
        self.0.wait().unwrap()
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        if matches!(self.0.try_wait(), Ok(None)) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

/// `health.status` 回應（`wal.healthy`、`test_device_allowlist` 等欄位）。
pub fn health_status(socket: &Path) -> PyValue {
    rpc_value(socket, "health.status", "{}")
}

/// `command.get` 的 `command` 物件（找不到時為 `Null`）。
pub fn command_get(socket: &Path, cmd_id: &str) -> PyValue {
    rpc_value(socket, "command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#))["command"].clone()
}

/// 以 `session.list` 取得全部 session（依 session_id 排序）。
pub fn session_list(socket: &Path) -> PyValue {
    rpc_value(socket, "session.list", "{}")["sessions"].clone()
}

/// 以 `command.submit` 送出命令（selector 為 COM；命令字串放在 `cmd`，對應 inventory §6.2 的參數鏈）。
pub fn submit(socket: &Path, selector: &str, command: &str) -> PyValue {
    rpc_value(
        socket,
        "command.submit",
        &format!(r#"{{"selector":"{selector}","cmd":"{command}"}}"#),
    )
}

/// 確認 PTY slave 可被獨占以外的方式（一般 `open(O_RDWR)`）開啟：TIOCEXCL 已解除時成功。
pub fn pty_can_open_exclusively(path: &Path) -> bool {
    fs::OpenOptions::new().read(true).write(true).open(path).is_ok()
}

/// 目前是否為 root（root 下 chmod 無效，唯讀類案例須 skip 並列為證據不足）。
pub fn is_root() -> bool {
    Command::new("id")
        .arg("-u")
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim() == "0")
        .unwrap_or(false)
}

pub fn chmod(path: &Path, mode: &str) {
    let status = Command::new("chmod")
        .arg(mode)
        .arg(path)
        .status()
        .expect("執行 chmod");
    assert!(status.success(), "chmod {mode} {} 失敗", path.display());
}

/// in-process 測試用的平台夾具種類（僅列出尚未接線的需求，見 `in_process_platform`）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fixture {
    /// 無額外腳本（只需裝置掃描）。
    None,
    /// READY 的 shell 目標；前景命令 BlockUntilCancel，另有排隊命令。
    ReadyShellBlockingWithQueue,
    /// READY 的 shell 目標；session writer 以 BlockForever 卡住（drain 收斂不了）。
    BlockForeverWriter,
    /// WAL sync 每次失敗（FakeWalFs 故障注入）。
    WalSyncFailure,
}

/// 建立 in-process 測試用的 `sw_pal::Platform`：環境變數取自 `vars`（不改動行程環境）、裝置來源為 `devices`、夾具為 `fixture`。
///
/// 以 `sw_pal::native()` 為底（實例鎖、RPC listener、FsOps、時鐘為原生），替換：
/// - `env`：`FakeEnv`（內容＝`vars`）；
/// - `devices`：`FakeDeviceSource`（初始快照＝`devices`，依列舉順序）；
/// - `serial`／`uart_excl`：`FakeSerialBackend`（每個裝置以 `real_path` 登錄）／無持有者；
/// - `wal_fs`：原生 WalFs；`Fixture::WalSyncFailure` 時包一層「`sync` 一律失敗」。
///
/// profile 寫入 `<SERIALWRAP_CONFIG_DIR>/profiles/inproc.yaml`：`Fixture::None` 只有 passthrough template（動態 session 停 ATTACHED）；
/// 其餘夾具為 shell template `t`＋target `COM0`（第一個裝置），FakeSerial 假 DUT 回 prompt 與 ready nonce。
/// `BlockForeverWriter`／`WalSyncFailure` 由夾具自帶的驅動 thread 完成「等 READY → 動作 → `daemon.stop`」（子行程模式無其他 client）。
pub fn in_process_platform(
    vars: &BTreeMap<String, String>,
    devices: Vec<(String, DeviceInfo)>,
    fixture: Fixture,
) -> sw_pal::Platform {
    use sw_pal::fakes::{FakeDeviceSource, FakeEnv, FakeSerialBackend, FakeUartExclusive, FakeWriteStep};
    use std::sync::Arc;

    let config_dir = PathBuf::from(vars.get("SERIALWRAP_CONFIG_DIR").expect("vars 須含 SERIALWRAP_CONFIG_DIR"));
    let socket = PathBuf::from(vars.get("SERIALWRAP_RUN_DIR").expect("vars 須含 SERIALWRAP_RUN_DIR")).join("serialwrapd.sock");
    let profile_dir = config_dir.join("profiles");
    fs::create_dir_all(&profile_dir).expect("建立 profiles 目錄");
    let yaml = match fixture {
        Fixture::None => "profiles:\n  p:\n    platform: passthrough\n    ready_probe: ''\n".to_string(),
        _ => format!(
            "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {}}}\n",
            devices.first().map(|(k, _)| k.clone()).unwrap_or_default()
        ),
    };
    fs::write(profile_dir.join("inproc.yaml"), yaml).expect("寫入 in-process profile");

    let serial = FakeSerialBackend::new();
    let mut first_dev = None;
    for (_, info) in &devices {
        let dev = serial.add_device(&info.real_path);
        if fixture != Fixture::None {
            install_shell_replies(&dev);
            if first_dev.is_none() {
                first_dev = Some(dev.clone());
            }
        }
    }
    // READY 之後的第一筆寫入（agent 命令）依夾具阻塞：ready nonce 回應的同時排入寫入腳本。
    if let Some(dev) = &first_dev {
        let step = match fixture {
            Fixture::ReadyShellBlockingWithQueue => Some(FakeWriteStep::BlockUntilCancel { accept_before_cancel: 0 }),
            Fixture::BlockForeverWriter => Some(FakeWriteStep::BlockForever),
            _ => None,
        };
        if let Some(step) = step {
            let handle = dev.clone();
            let armed = std::sync::atomic::AtomicBool::new(false);
            dev.on_tx_match(
                r"__READY__[0-9a-f]+",
                Box::new(move |_line: &[u8]| {
                    if !armed.swap(true, Ordering::SeqCst) {
                        handle.script_write(vec![step.clone()]);
                    }
                    Vec::new()
                }),
            );
        }
    }

    let env = FakeEnv::default();
    *env.vars.lock().unwrap() = vars.clone();
    let mut p = sw_pal::native();
    p.env = Arc::new(env);
    p.serial = serial;
    p.uart_excl = Arc::new(FakeUartExclusive { report: Default::default() });
    let snapshot = devices.clone();
    p.devices = Box::new(move |_sel: sw_pal::traits::DeviceSelection| -> Box<dyn sw_pal::traits::DeviceSource> {
        Box::new(FakeDeviceSource::new(snapshot.clone()).0)
    });
    if fixture == Fixture::WalSyncFailure {
        p.wal_fs = Arc::new(SyncFailWalFs(p.wal_fs.clone()));
    }

    match fixture {
        Fixture::BlockForeverWriter => {
            std::thread::spawn(move || {
                wait_socket(&socket, LONG);
                wait_state_in_process(&socket, "COM0", "READY");
                let r = submit(&socket, "COM0", "stuck");
                assert!(r["ok"] == true, "BlockForever 夾具的命令應被接受：{r:?}");
                std::thread::sleep(Duration::from_millis(300));
                rpc_once(&socket, STOP);
            });
        }
        Fixture::WalSyncFailure => {
            std::thread::spawn(move || {
                wait_socket(&socket, LONG);
                wait_state_in_process(&socket, "COM0", "READY");
                rpc_once(&socket, STOP);
            });
        }
        _ => {}
    }
    p
}

/// 輪詢 session 狀態（in-process 夾具驅動 thread 用；逾時 panic）。
fn wait_state_in_process(socket: &Path, com: &str, want: &str) {
    let deadline = Instant::now() + LONG;
    loop {
        if get_state(socket, com)["state"] == want {
            return;
        }
        assert!(Instant::now() < deadline, "{com} 未在 30 s 內進入 {want}");
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// shell 假 DUT（FakeSerial）：`\n` → prompt；`__READY__<nonce>` → nonce＋prompt（與 `ServiceRig` 相同）。
fn install_shell_replies(dev: &sw_pal::fakes::FakeDevice) {
    service_rig::install_shell_replies(dev);
}

/// WAL 檔案層：委派原生實作，但每個檔案的 `sync` 一律回 EIO（`shutdown_wal_sync_failure_exits_1`）。
struct SyncFailWalFs(std::sync::Arc<dyn sw_pal::traits::WalFs>);

struct SyncFailFile(Box<dyn sw_pal::traits::WalFile>);

impl sw_pal::traits::WalFile for SyncFailFile {
    fn write_all_flush(&mut self, bytes: &[u8]) -> Result<(), sw_pal::traits::WalWriteError> {
        self.0.write_all_flush(bytes)
    }
    fn sync(&mut self) -> sw_pal::traits::PalResult<()> {
        Err(sw_pal::traits::PalError::from_errno(5))
    }
    fn identity(&self) -> sw_pal::traits::PalResult<sw_pal::traits::FileIdentity> {
        self.0.identity()
    }
    fn unlinked(&self) -> sw_pal::traits::PalResult<bool> {
        self.0.unlinked()
    }
    fn close(self: Box<Self>) -> sw_pal::traits::PalResult<()> {
        self.0.close()
    }
}

impl sw_pal::traits::WalFs for SyncFailWalFs {
    fn open_append(&self, path: &Path) -> sw_pal::traits::PalResult<Box<dyn sw_pal::traits::WalFile>> {
        Ok(Box::new(SyncFailFile(self.0.open_append(path)?)))
    }
    fn path_identity(&self, path: &Path) -> sw_pal::traits::PalResult<Option<sw_pal::traits::FileIdentity>> {
        self.0.path_identity(path)
    }
    fn size(&self, path: &Path) -> sw_pal::traits::PalResult<Option<u64>> {
        self.0.size(path)
    }
    fn tail_needs_termination(&self, path: &Path) -> sw_pal::traits::PalResult<bool> {
        self.0.tail_needs_termination(path)
    }
    fn read_all(&self, path: &Path) -> sw_pal::traits::PalResult<Option<Vec<u8>>> {
        self.0.read_all(path)
    }
    fn rename_noreplace_or_suffix(&self, from: &Path, base_to: &Path) -> sw_pal::traits::PalResult<PathBuf> {
        self.0.rename_noreplace_or_suffix(from, base_to)
    }
    fn sync_dir(&self, dir: &Path) -> sw_pal::traits::PalResult<()> {
        self.0.sync_dir(dir)
    }
    fn ensure_dir(&self, dir: &Path) -> sw_pal::traits::PalResult<()> {
        self.0.ensure_dir(dir)
    }
    fn dir_writable(&self, dir: &Path) -> bool {
        self.0.dir_writable(dir)
    }
}

/// 子行程模式：`restart_after_commit` 的 state.json 提交後中止（hook 於 `StateStore` commit 成功後觸發 abort）。
pub fn child_daemon_abort_after_commit() {
    let hooks = DaemonHooks {
        before_state_commit: None,
        after_state_commit: Some(Box::new(|| std::process::abort())),
        trace: None,
    };
    let code = run_with_hooks(vec!["serialwrapd".to_string()], sw_pal::native(), hooks);
    std::process::exit(code)
}

/// 子行程模式：`state.json` 提交前中止（hook 於 commit 前觸發 abort）。
pub fn child_daemon_abort_before_commit() {
    let hooks = DaemonHooks {
        before_state_commit: Some(Box::new(|| std::process::abort())),
        after_state_commit: None,
        trace: None,
    };
    let code = run_with_hooks(vec!["serialwrapd".to_string()], sw_pal::native(), hooks);
    std::process::exit(code)
}

/// 子行程模式取得目前行程的環境變數（即父行程以 `run_child_test` 注入的隔離 vars）。
pub fn process_vars() -> BTreeMap<String, String> {
    std::env::vars().collect()
}

/// `slice21_scope.yaml` 的 `deferred_methods` 中，`slice` 不在 `delivered`（已交付切片）內的方法名，依檔案順序
/// （07 增補 #24；parity-amendments-2026-10-26 #186）。只認 `deferred_methods:` 區段內的
/// `- {method: <名>, slice: "<X.Y>"}` 流式列（該檔唯一格式），遇下一個頂層鍵即停止；不依賴 `ServiceRig`。
pub fn deferred_methods(yaml: &str, delivered: &[&str]) -> Vec<String> {
    fn field<'a>(item: &'a str, key: &str) -> Option<&'a str> {
        item.split(',').find_map(|kv| {
            let (k, v) = kv.split_once(':')?;
            (k.trim() == key).then(|| v.trim().trim_matches(|c| c == '"' || c == '\''))
        })
    }
    let mut out = Vec::new();
    let mut in_section = false;
    for line in yaml.lines() {
        if !line.starts_with([' ', '-', '#']) && !line.is_empty() {
            in_section = line.trim_end().starts_with("deferred_methods:");
            continue;
        }
        let Some(item) = in_section.then(|| line.trim().strip_prefix("- {")).flatten() else { continue };
        let item = item.split('}').next().unwrap_or_default();
        let (Some(method), Some(slice)) = (field(item, "method"), field(item, "slice")) else {
            panic!("deferred_methods 列格式不符：{line}");
        };
        if !delivered.contains(&slice) {
            out.push(method.to_string());
        }
    }
    out
}

/// P5-T17：以 `sw_proto::pyjson::loads` 讀 repo 內 golden（不引入 serde；`framing.rs` 使用）。
pub mod serde_free {
    use std::path::PathBuf;
    use sw_proto::pyjson::{self, PyValue};

    /// `contracts/golden/rpc/line_limit.json` 的兩個門檻（內容不含 `\n` 的位元組數）。
    pub struct Golden {
        pub max_ok_bytes: usize,
        pub first_close_bytes: usize,
    }

    /// 由 crate 目錄往上找 repo 相對路徑 `rel`（不依賴 cwd，理由同 `FakeTargetProc::start`）。
    pub fn repo_file(rel: &str) -> PathBuf {
        let mut dir = super::data_root();
        loop {
            let candidate = dir.join(rel);
            if candidate.is_file() {
                return candidate;
            }
            assert!(dir.pop(), "找不到 repo 檔案 {rel}");
        }
    }

    /// 讀取 line_limit golden。
    pub fn load(rel: &str) -> Golden {
        let text = std::fs::read_to_string(repo_file(rel)).expect("讀取 golden");
        let PyValue::Dict(d) = pyjson::loads(&text).expect("golden 為合法 JSON") else {
            panic!("golden 頂層須為 object");
        };
        let int = |k: &str| {
            d.get(k)
                .and_then(PyValue::as_i64)
                .unwrap_or_else(|| panic!("golden 缺 {k}")) as usize
        };
        Golden {
            max_ok_bytes: int("max_ok_bytes"),
            first_close_bytes: int("first_close_bytes"),
        }
    }
}
