//! `handoff_stop` 的真 CLI 子行程與隔離環境支援。

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use crate::support::IsolatedEnv;
use sw_proto::pyjson::{self, PyDict, PyValue};

const OLD_VERSION: &str = "0.0.1";

/// 找到帶 test-fakes 的 CLI；明確路徑優先，其次使用測試執行檔所屬 target profile。
pub fn feature_cli_path() -> PathBuf {
    let candidate = std::env::var_os("SW_TEST_SERIALWRAP")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let test_exe = std::env::current_exe().expect("取得 handoff_stop 測試執行檔路徑");
            let profile_dir = test_exe
                .parent()
                .and_then(Path::parent)
                .expect("測試執行檔應位於 target/<profile>/deps");
            profile_dir.join(format!("serialwrap{}", std::env::consts::EXE_SUFFIX))
        });
    require_file(
        candidate,
        "feature CLI；設定 SW_TEST_SERIALWRAP 指向帶 test-fakes 的 serialwrap",
    )
}

/// 正式 CLI 必須由獨立 no-fakes target 建置，禁止退回 feature profile 的共同產物。
pub fn no_fakes_cli_path() -> PathBuf {
    let candidate = std::env::var_os("SW_TEST_SERIALWRAP_NO_FAKES")
        .map(PathBuf::from)
        .expect("缺少 SW_TEST_SERIALWRAP_NO_FAKES；請指向獨立 target 建置的正式 serialwrap");
    require_file(
        candidate,
        "no-fakes CLI；請以獨立 Cargo target directory 建置",
    )
}

fn require_file(candidate: PathBuf, what: &str) -> PathBuf {
    assert!(
        candidate.is_file(),
        "找不到 {what}：{}",
        candidate.display()
    );
    fs::canonicalize(&candidate).unwrap_or(candidate)
}

/// 取得 feature CLI 所屬 bundle 的版本目錄與 release manifest。
pub fn feature_bundle(cli: &Path) -> (PathBuf, PathBuf) {
    let version_dir = cli.parent().and_then(Path::parent).map(Path::to_path_buf);
    let manifest = version_dir
        .as_ref()
        .map(|directory| directory.join("release-manifest.json"))
        .filter(|path| path.is_file())
        .or_else(|| {
            let repository = repository_root();
            let fallback = repository.join("target/s2-par/gpa3/bundle/bin/serialwrap");
            let fallback = fs::canonicalize(fallback).ok()?;
            let fallback_version = fallback.parent()?.parent()?;
            let fallback_manifest = fallback_version.join("release-manifest.json");
            fallback_manifest.is_file().then_some(fallback_manifest)
        })
        .unwrap_or_else(|| {
            panic!(
                "找不到 feature CLI 對應的 release-manifest.json：{}",
                cli.display()
            )
        });
    let version_dir = manifest
        .parent()
        .expect("manifest 的 bundle 版本目錄")
        .to_path_buf();
    assert!(
        manifest.is_file(),
        "feature CLI bundle 缺少 release-manifest.json：{}",
        manifest.display()
    );
    (version_dir, manifest)
}

/// 取得由 mkbundle 產生的舊版安裝來源；manifest 與雜湊不得由測試手寫。
pub fn old_bundle_dir() -> PathBuf {
    let directory = repository_root()
        .join("target/s2-par/gpa3/base-bundle/versions")
        .join(OLD_VERSION);
    let manifest_path = directory.join("release-manifest.json");
    assert!(
        manifest_path.is_file(),
        "找不到隔離安裝用舊版 dev bundle：{}；請執行 `scripts/rust/mkbundle.sh --dev --profile debug --version-override 0.0.1 --out target/s2-par/gpa3/base-bundle`",
        manifest_path.display()
    );
    assert!(directory.join("SHA256SUMS").is_file(), "舊版 bundle 缺少工具產生的 SHA256SUMS");
    let text = fs::read_to_string(&manifest_path).expect("讀取舊版 bundle manifest");
    let PyValue::Dict(manifest) = pyjson::loads(&text).expect("解析舊版 bundle manifest") else {
        panic!("舊版 bundle manifest 頂層須為 object");
    };
    assert_eq!(manifest.get("bundle_version").and_then(PyValue::as_str), Some(OLD_VERSION));
    directory
}

pub fn unix_endpoint(socket: &Path) -> String {
    socket.display().to_string()
}

/// 建立隔離安裝入口，供 update 路徑辨識既有 current；bundle 內容來自 mkbundle 產物。
pub fn seed_install(bundle_dir: &Path, install_root: &Path) {
    let manifest_text =
        fs::read_to_string(bundle_dir.join("release-manifest.json")).expect("讀取 bundle manifest");
    let manifest = match pyjson::loads(&manifest_text).expect("解析 bundle manifest") {
        PyValue::Dict(manifest) => manifest,
        other => panic!("bundle manifest 頂層須為 object：{other:?}"),
    };
    let target_version = manifest
        .get("bundle_version")
        .and_then(PyValue::as_str)
        .expect("bundle manifest 含 bundle_version");
    assert_ne!(target_version, OLD_VERSION, "測試目前版與更新目標版須不同");

    let old_dir = install_root.join("versions").join(OLD_VERSION);
    copy_tree(bundle_dir, &old_dir);
    let bin = install_root.join("bin");
    fs::create_dir_all(&bin).expect("建立隔離安裝入口目錄");
    for name in ["serialwrap", "serialwrapd"] {
        let link = bin.join(name);
        let target = format!("../versions/{OLD_VERSION}/bin/{name}");
        let status = Command::new("ln")
            .arg("-s")
            .arg(target)
            .arg(&link)
            .status()
            .expect("建立隔離安裝入口 symlink");
        assert!(status.success(), "建立隔離安裝入口失敗：{}", link.display());
    }

    let mut current = PyDict::new();
    current.insert("backup_dir", PyValue::Null);
    current.insert("bundle_version", PyValue::Str(OLD_VERSION.to_string()));
    current.insert("dir", PyValue::Str(format!("versions/{OLD_VERSION}")));
    current.insert("previous", PyValue::Null);
    current.insert(
        "switched_at",
        PyValue::Str("handoff-stop-fixture".to_string()),
    );
    let mut bytes = pyjson::dumps(&PyValue::Dict(current), &pyjson::DISK_STABLE).into_bytes();
    bytes.push(b'\n');
    fs::write(install_root.join("current-release.json"), bytes).expect("寫入隔離 current fixture");
}

/// 以 mkbundle 產生的舊版檔案建立可供 update 驗證的隔離安裝。
pub fn seed_install_from_old_bundle(bundle_dir: &Path, install_root: &Path) {
    let manifest_path = bundle_dir.join("release-manifest.json");
    let text = fs::read_to_string(&manifest_path).expect("讀取舊版 bundle manifest");
    let PyValue::Dict(manifest) = pyjson::loads(&text).expect("解析舊版 bundle manifest") else {
        panic!("舊版 bundle manifest 頂層須為 object");
    };
    let version = manifest
        .get("bundle_version")
        .and_then(PyValue::as_str)
        .expect("舊版 manifest 含 bundle_version");
    assert_eq!(version, OLD_VERSION, "舊版 bundle 版本須符合 fixture 約定");
    let version_dir = install_root.join("versions").join(version);
    copy_tree(bundle_dir, &version_dir);

    let bin = install_root.join("bin");
    fs::create_dir_all(&bin).expect("建立隔離安裝入口目錄");
    for name in ["serialwrap", "serialwrapd"] {
        let link = bin.join(name);
        let status = Command::new("ln")
            .arg("-s")
            .arg(format!("../versions/{version}/bin/{name}"))
            .arg(&link)
            .status()
            .expect("建立隔離安裝入口 symlink");
        assert!(status.success(), "建立隔離安裝入口失敗：{}", link.display());
    }
    let mut current = PyDict::new();
    current.insert("backup_dir", PyValue::Null);
    current.insert("bundle_version", PyValue::Str(version.to_string()));
    current.insert("dir", PyValue::Str(format!("versions/{version}")));
    current.insert("previous", PyValue::Null);
    current.insert("switched_at", PyValue::Str("handoff-stop-fixture".to_string()));
    let mut bytes = pyjson::dumps(&PyValue::Dict(current), &pyjson::DISK_STABLE).into_bytes();
    bytes.push(b'\n');
    fs::write(install_root.join("current-release.json"), bytes).expect("寫入隔離 current fixture");
}

fn copy_tree(source: &Path, destination: &Path) {
    fs::create_dir_all(destination).expect("建立隔離安裝版本目錄");
    for entry in fs::read_dir(source).expect("讀取 bundle 版本目錄") {
        let entry = entry.expect("讀取 bundle 項目");
        let source_path = entry.path();
        let destination_path = destination.join(entry.file_name());
        let file_type = entry.file_type().expect("讀取 bundle 項目類型");
        if file_type.is_dir() {
            copy_tree(&source_path, &destination_path);
        } else {
            assert!(
                file_type.is_file(),
                "bundle 版本目錄含特殊檔案：{}",
                source_path.display()
            );
            fs::copy(&source_path, &destination_path).expect("複製隔離安裝檔案");
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InstallSnapshot {
    current: Vec<u8>,
    cli_entry: Option<PathBuf>,
    daemon_entry: Option<PathBuf>,
}

pub fn install_snapshot(install_root: &Path) -> InstallSnapshot {
    InstallSnapshot {
        current: fs::read(install_root.join("current-release.json"))
            .expect("讀取隔離 current-release.json"),
        cli_entry: fs::read_link(install_root.join("bin/serialwrap")).ok(),
        daemon_entry: fs::read_link(install_root.join("bin/serialwrapd")).ok(),
    }
}

pub fn update_report(output: &Output) -> PyDict {
    let stdout = String::from_utf8_lossy(&output.stdout);
    let root = match pyjson::loads(stdout.trim()) {
        Ok(PyValue::Dict(root)) => root,
        other => panic!("setup stdout 不是 JSON object：{other:?}\n{stdout}\n{}", String::from_utf8_lossy(&output.stderr)),
    };
    match root.get("update") {
        Some(PyValue::Dict(report)) => report.clone(),
        other => panic!("setup 回應缺 update report：{other:?}\n{stdout}"),
    }
}

/// 從產品 `update.steps.stop_supervisor.detail.handoff_chain` 讀取交接鏈。
pub fn handoff_chain(report: &PyDict) -> Vec<PyDict> {
    let Some(PyValue::List(steps)) = report.get("steps") else {
        panic!("update report 缺 steps");
    };
    let stops = steps.iter().filter_map(|value| match value {
        PyValue::Dict(step) if step.get("step").and_then(PyValue::as_str) == Some("stop_supervisor") => Some(step),
        _ => None,
    }).collect::<Vec<_>>();
    assert_eq!(stops.len(), 1, "產品 update 必須恰有一個 stop_supervisor step");
    let Some(PyValue::Dict(detail)) = stops[0].get("detail") else {
        panic!("stop_supervisor 缺 detail");
    };
    match detail.get("handoff_chain") {
        Some(PyValue::List(chain)) => chain.iter().map(|value| match value {
            PyValue::Dict(link) => link.clone(),
            other => panic!("產品 handoff_chain 項目不是 object：{other:?}"),
        }).collect(),
        other => panic!("產品 stop_supervisor 未回報 handoff_chain：{other:?}"),
    }
}

pub fn update_step<'a>(report: &'a PyDict, name: &str) -> &'a PyDict {
    let Some(PyValue::List(steps)) = report.get("steps") else {
        panic!("update report 缺 steps");
    };
    let matches = steps.iter().filter_map(|value| match value {
        PyValue::Dict(step) if step.get("step").and_then(PyValue::as_str) == Some(name) => Some(step),
        _ => None,
    }).collect::<Vec<_>>();
    assert_eq!(matches.len(), 1, "產品 update 必須恰有一個 {name} step");
    matches[0]
}

pub fn daemon_pid(output: &Output) -> Option<u32> {
    if !output.status.success() { return None; }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let Ok(PyValue::Dict(response)) = pyjson::loads(stdout.trim()) else { return None; };
    response.get("pid").and_then(PyValue::as_i64).and_then(|pid| u32::try_from(pid).ok())
}

/// 只傳入隔離 HOME、XDG 與 SERIALWRAP 路徑；呼叫端的 PATH/TMPDIR 由子行程啟動器明確補上。
pub fn isolated_cli_env(
    env: &IsolatedEnv,
    endpoint: &str,
    manifest: &Path,
    install_root: &Path,
) -> BTreeMap<String, String> {
    const ALLOWED: [&str; 13] = [
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "SERIALWRAP_STATE_DIR",
        "SERIALWRAP_RUN_DIR",
        "SERIALWRAP_WAL_DIR",
        "SERIALWRAP_CONFIG_DIR",
        "SERIALWRAP_LOG_DIR",
        "SERIALWRAP_EVENTS_DIR",
        "SERIALWRAP_EVENTS_RUNTIME_DIR",
        "SERIALWRAP_BY_ID_DIR",
        "SERIALWRAP_BY_PATH_DIR",
    ];
    let mut vars = BTreeMap::new();
    for key in ALLOWED {
        let value = env
            .vars
            .get(key)
            .unwrap_or_else(|| panic!("隔離環境缺少 {key}"));
        vars.insert(key.to_string(), value.clone());
    }
    vars.insert("XDG_RUNTIME_DIR".to_string(), String::new());
    vars.insert(
        "SERIALWRAP_CONFIG".to_string(),
        env.config_dir.join("config.yaml").display().to_string(),
    );
    vars.insert(
        "SERIALWRAP_INSTALL_ROOT".to_string(),
        install_root.display().to_string(),
    );
    vars.insert(
        "SERIALWRAP_RELEASE_MANIFEST".to_string(),
        manifest.display().to_string(),
    );
    vars.insert(
        "SERIALWRAP_TEST_UPDATE_SOCKET".to_string(),
        endpoint.to_string(),
    );
    vars.insert("SW_TEST_USER".to_string(), "root".to_string());
    vars.insert("SW_TEST_PASS".to_string(), "pw".to_string());
    vars
}

fn artifact_dir() -> PathBuf {
    let repository = repository_root();
    let directory = repository.join("target/s2-par/gpa3");
    fs::create_dir_all(&directory).expect("建立 gpa3 輸出目錄");
    directory
}

fn repository_root() -> PathBuf {
    std::env::current_dir()
        .expect("取得 handoff_stop 測試工作目錄")
        .ancestors()
        .find(|directory| directory.join("crates/sw-daemon/Cargo.toml").is_file())
        .expect("從 handoff_stop 測試工作目錄定位 workspace 根目錄")
        .to_path_buf()
}

fn systemd_service_state(label: &str, edge: &str) -> Vec<u8> {
    let output = Command::new("systemctl")
        .args([
            "--user",
            "show",
            "serialwrap.service",
            "-p",
            "MainPID",
            "-p",
            "ActiveEnterTimestamp",
        ])
        .output()
        .expect("查詢 serialwrap.service 的 MainPID 與 ActiveEnterTimestamp");
    assert!(
        output.status.success(),
        "systemctl --user show serialwrap.service 失敗（{label}/{edge}）：{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let path = artifact_dir().join(format!("systemd-{label}-{edge}.txt"));
    fs::write(&path, &output.stdout).expect("保存 systemd service 快照");
    output.stdout
}

/// 執行 CLI 子行程，前後保存並比較正式 service 的 PID 與啟動時間。
pub fn run_audited_cli(
    cli: &Path,
    vars: &BTreeMap<String, String>,
    args: &[&str],
    label: &str,
    timeout: Duration,
) -> Output {
    let before = systemd_service_state(label, "before");
    let mut command = Command::new(cli);
    command
        .args(args)
        .env_clear()
        .envs(vars)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(path) = std::env::var_os("PATH") {
        command.env("PATH", path);
    }
    if let Some(tmpdir) = std::env::var_os("TMPDIR") {
        command.env("TMPDIR", tmpdir);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            let after = systemd_service_state(label, "after");
            assert_eq!(
                before, after,
                "CLI 啟動失敗期間正式 service 狀態不得改變（{label}）"
            );
            panic!("啟動正式 CLI {} 失敗：{error}", cli.display());
        }
    };
    let deadline = Instant::now() + timeout;
    let mut timed_out = false;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            Ok(None) => {
                timed_out = true;
                let _ = child.kill();
                let _ = child.wait();
                break;
            }
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                let after = systemd_service_state(label, "after");
                assert_eq!(
                    before, after,
                    "檢查 CLI 子行程期間正式 service 狀態不得改變（{label}）"
                );
                panic!("檢查 CLI 子行程失敗：{error}");
            }
        }
    }
    let output = child.wait_with_output().expect("讀取 CLI 子行程輸出");
    let after = systemd_service_state(label, "after");
    assert_eq!(
        before, after,
        "CLI 子行程不得停止或重啟正式 serialwrap.service（{label}）"
    );
    let directory = artifact_dir();
    fs::write(
        directory.join(format!("cli-{label}.stdout")),
        &output.stdout,
    )
    .expect("保存 CLI stdout");
    fs::write(
        directory.join(format!("cli-{label}.stderr")),
        &output.stderr,
    )
    .expect("保存 CLI stderr");
    assert!(!timed_out, "CLI 子行程超過期限 {timeout:?}：{args:?}");
    output
}

/// 執行隔離 CLI 子行程；不繼承 caller 的設定或狀態路徑。
pub fn run_cli(cli: &Path, vars: &BTreeMap<String, String>, args: &[&str], timeout: Duration) -> Output {
    try_run_cli(cli, vars, args, timeout)
        .unwrap_or_else(|| panic!("serialwrap CLI {:?} 啟動失敗或超過期限 {timeout:?}", args))
}

fn try_run_cli(cli: &Path, vars: &BTreeMap<String, String>, args: &[&str], timeout: Duration) -> Option<Output> {
    let mut command = Command::new(cli);
    command.args(args).env_clear().envs(vars)
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    if let Some(path) = std::env::var_os("PATH") { command.env("PATH", path); }
    if let Some(tmpdir) = std::env::var_os("TMPDIR") { command.env("TMPDIR", tmpdir); }
    let mut child = command.spawn().ok()?;
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return child.wait_with_output().ok(),
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}

/// 僅清理隔離 endpoint 回報、且 executable 位於本測試安裝目錄的產品繼任者。
pub struct ProductDaemonCleanup {
    cli: PathBuf,
    vars: BTreeMap<String, String>,
    endpoint: String,
    install_root: PathBuf,
    expected_pid: Option<u32>,
    active: bool,
}

impl ProductDaemonCleanup {
    pub fn new(cli: PathBuf, vars: BTreeMap<String, String>, endpoint: String, install_root: PathBuf, run_dir: &Path) -> ProductDaemonCleanup {
        let socket = endpoint.strip_prefix("unix://").unwrap_or(&endpoint);
        assert!(Path::new(socket).is_absolute(), "cleanup socket 必須是絕對路徑");
        assert!(Path::new(socket).starts_with(run_dir), "cleanup socket 必須位於隔離 run dir：{socket}");
        ProductDaemonCleanup { cli, vars, endpoint, install_root, expected_pid: None, active: true }
    }

    fn status_pid(&self) -> Option<u32> {
        let output = try_run_cli(&self.cli, &self.vars,
            &["--socket", &self.endpoint, "daemon", "status"], Duration::from_secs(5))?;
        daemon_pid(&output)
    }

    fn stop_owned(&self, expected: Option<u32>) -> bool {
        let Some(pid) = self.status_pid() else { return false; };
        if expected.is_some_and(|expected| expected != pid) { return false; }
        let Ok(exe) = fs::canonicalize(format!("/proc/{pid}/exe")) else { return false; };
        let Ok(versions) = fs::canonicalize(self.install_root.join("versions")) else { return false; };
        if !exe.starts_with(&versions) || !is_product_daemon_executable(pid, &exe, &versions) {
            return false;
        }
        let start_ticks = process_start_ticks(pid);
        let _stopped = try_run_cli(&self.cli, &self.vars,
            &["--socket", &self.endpoint, "daemon", "stop"], Duration::from_secs(20))
            .is_some_and(|output| output.status.success());
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            let same = process_start_ticks(pid) == start_ticks
                && fs::canonicalize(format!("/proc/{pid}/exe")).ok().as_deref() == Some(exe.as_path());
            if !same { return true; }
            thread::sleep(Duration::from_millis(20));
        }
        let still_same = process_start_ticks(pid) == start_ticks
            && fs::canonicalize(format!("/proc/{pid}/exe")).ok().as_deref() == Some(exe.as_path());
        if still_same && start_ticks.is_some() {
            let _ = Command::new("kill").args(["-TERM", &pid.to_string()]).status();
            let deadline = Instant::now() + Duration::from_secs(10);
            while Instant::now() < deadline {
                let same = process_start_ticks(pid) == start_ticks
                    && fs::canonicalize(format!("/proc/{pid}/exe")).ok().as_deref() == Some(exe.as_path());
                if !same { return true; }
                thread::sleep(Duration::from_millis(20));
            }
        }
        false
    }

    pub fn stop(&mut self, expected_pid: u32) {
        let actual = self.status_pid();
        assert_eq!(actual, Some(expected_pid), "隔離 endpoint 的繼任 daemon PID 必須符合產品回報：{actual:?}");
        self.expected_pid = Some(expected_pid);
        assert!(self.stop_owned(Some(expected_pid)), "無法安全停止隔離安裝的繼任 daemon PID {expected_pid}");
        self.active = false;
    }
}

impl Drop for ProductDaemonCleanup {
    fn drop(&mut self) {
        if self.active { let _ = self.stop_owned(self.expected_pid); }
    }
}

fn process_start_ticks(pid: u32) -> Option<u64> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let fields = stat.rsplit_once(')')?.1.split_whitespace().collect::<Vec<_>>();
    fields.get(19)?.parse().ok()
}

fn is_product_daemon_executable(pid: u32, exe: &Path, versions: &Path) -> bool {
    if exe.file_name().and_then(|name| name.to_str()) == Some("serialwrapd") {
        return true;
    }
    if exe.file_name().and_then(|name| name.to_str()) != Some("python3") {
        return false;
    }
    let Ok(cmdline) = fs::read(format!("/proc/{pid}/cmdline")) else { return false; };
    let args = cmdline
        .split(|byte| *byte == 0)
        .filter(|arg| !arg.is_empty())
        .map(String::from_utf8_lossy)
        .collect::<Vec<_>>();
    args.len() >= 4
        && Path::new(args[0].as_ref()) == exe
        && args[1] == "-I"
        && args[2] == "-B"
        && Path::new(args[3].as_ref()).starts_with(versions)
        && Path::new(args[3].as_ref()).file_name().and_then(|name| name.to_str()) == Some("serialwrapd.py")
}
