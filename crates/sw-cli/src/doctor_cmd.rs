//! `doctor` 子命令（P4-T13 原生承接；SC-25 `version_pairing`）。
//!
//! 逐項照 Python 基線 `sw_core/doctor_cmd.py`（P1 修正後）與 `inventory/07-platform-mgmt.md` §7.3 的
//! ok 判準、detail、fix 字串；清單順序與 advisory 集合見 [`run_doctor`]。
//! `python`／`pyyaml`／`pyserial` 的檢查對象改為 release manifest 的 daemon runtime（Rust CLI 本身不是 Python；
//! 登記為 SC-25 附註，見 `contracts/behavior_changes.yaml`）。

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::argparse::Namespace;
use crate::assets::PROFILE_ASSETS;
use crate::client;
use crate::ctx::CliCtx;
use crate::endpoint_resolve::{self, EndpointArgs};
use crate::install_layout::{self, LayoutError};
use crate::output;
use crate::pairing::{pairing_item, safe_component_path};
use sw_pal::device_source::windows_model::{exclude_bluetooth, read_bt_ports, SERIALCOMM_KEY};
use sw_pal::PalPathFlavor;
use sw_proto::pycompat::py_strip;
use sw_proto::pyfmt::py_str;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pypath::{self, HomeLookup, PathFlavor};
use sw_proto::release_manifest::ReleaseManifest;
use sw_proto::yaml11::{self, py_str_of, py_truthy_of, YValue};

/// 單一 doctor 項目（輸出形狀：`check`、`ok`、`detail`、`fix`、`advisory`，外加 `extra` 的額外鍵）。
pub struct Check {
    pub check: &'static str,
    pub ok: bool,
    pub detail: String,
    /// `ok` 時為空字串（輸出 `""`）；不 ok 時為修復提示。
    pub fix: Option<String>,
    pub advisory: bool,
    pub extra: PyDict,
}

/// Linux advisory 集合（`DOCTOR_ADVISORY_CHECKS`，逐字對照 Python 基線）。
pub const DOCTOR_ADVISORY_CHECKS: &[&str] = &[
    "systemd",
    "wsl_systemd",
    "devices",
    "other_serialwrap_installs",
    "wal_dir",
    "serialwrap_minicom_on_path",
    "jq_on_path",
    "minicom_on_path",
    "profile_bootloader_prompts",
];

/// Windows advisory 集合（`DOCTOR_ADVISORY_CHECKS_WIN`，逐字對照 Python 基線）。
pub const DOCTOR_ADVISORY_CHECKS_WIN: &[&str] = &[
    "serialwrap_on_path",
    "serialwrapd_on_path",
    "daemon_endpoint",
    "devices",
    "other_serialwrap_installs",
    "wal_dir",
    "profile_bootloader_prompts",
];

/// daemon runtime 無法定位時的修復提示（python／pyyaml／pyserial 共用）。
const FIX_REINSTALL: &str = "重新安裝發佈組合（serialwrap setup）";
/// `version_pairing` 不一致的修復提示（逐字照 Python `_check_version_pairing`）。
const FIX_PAIRING: &str = "以同一發佈組合（bundle）重新安裝 CLI 與 daemon，使 release-manifest.json 與各檔案一致（detail 列出不符項）";
/// 只缺 daemon（`daemon_unreachable`）時的修復提示（逐字照 Python）。
const FIX_UNREACHABLE: &str = "serialwrap daemon start（或 service start）後重跑 doctor";
/// 本 CLI 目前 Python 3.10 下限（`sys.version_info >= (3, 10)`）。
const PY_MIN: (i64, i64) = (3, 10);
/// daemon runtime 的 Python 版本查詢 argv（`-I -c`，逐字）。
pub const PY_VERSION_CODE: &str = "import sys;print('%d.%d.%d'%sys.version_info[:3])";
pub const PYYAML_CODE: &str = "import yaml;print(getattr(yaml,'__version__','已安裝'))";
pub const PYSERIAL_CODE: &str = "import serial;print(getattr(serial,'__version__','已安裝'))";
/// `health.status` 的固定逾時（秒）；doctor 不讀 `--timeout`／`--retries`（01 §4.6）。
const HEALTH_TIMEOUT_S: f64 = 0.5;

// ---- 07 §7.3 固定字串常數（逐列對照；detail／fix 逐字不得改動，A/B 與單元測試已鎖字串）----
/// `python`：版本低於 3.10 的 fix（07 §7.3）。
const FIX_PY_VERSION: &str = "需 Python ≥ 3.10";
/// `pyyaml`：匯入失敗的 fix（07 §7.3）。
const FIX_PYYAML: &str = "安裝 PyYAML（pipx 安裝會自動帶入）";
/// `pyserial`（win）：匯入失敗的 fix（07 §7.3）。
const FIX_PYSERIAL: &str = "安裝 pyserial（pipx 於 Windows 自動帶入；release exe 已內嵌）";
/// `pyyaml`／`pyserial`：匯入失敗的 detail（07 §7.3）。
const DETAIL_NOT_INSTALLED: &str = "未安裝";
/// `<name>_on_path`：找不到時的 detail（07 §7.3）。
const DETAIL_NOT_FOUND: &str = "找不到";
/// `<name>_on_path`：Windows fix（07 §7.3）。
const FIX_PATH_WINDOWS: &str = "將 serialwrap.exe / serialwrapd.exe 所在目錄加入 PATH";
/// `<name>_on_path`：Linux 預設 fix（07 §7.3）。
const FIX_PATH_LINUX: &str = "確認 ~/.local/bin 在 PATH（pipx ensurepath）";
/// `other_serialwrap_installs`：無安裝（07 §7.3）。
const DETAIL_OTHER_NONE: &str = "PATH 上找不到 serialwrap（可能以絕對路徑呼叫）";
/// `other_serialwrap_installs`：單一安裝（07 §7.3）。
const DETAIL_OTHER_SINGLE: &str = "僅偵測到目前這份";
/// `other_serialwrap_installs`：--version 取得失敗（07 §7.3）。
const DETAIL_VERSION_UNAVAILABLE: &str = "無法取得";
/// `other_serialwrap_installs`：多版本不一致 fix（07 §7.3）。
const FIX_OTHER_INSTALLS: &str = "確認各安裝版本一致，或統一改呼叫同一份（建議 pipx 系統安裝／以絕對路徑或 SERIALWRAP_ENDPOINT 釘住）";
/// `dialout`：ok detail（07 §7.3）。
const DETAIL_DIALOUT_OK: &str = "已在 dialout 群組";
/// `dialout`：not ok detail（07 §7.3）。
const DETAIL_DIALOUT_NO: &str = "不在 dialout 群組";
/// `dialout`：fix（07 §7.3）。
const FIX_DIALOUT: &str = "sudo usermod -aG dialout $USER（之後重新登入）";
/// `systemd`：ok detail（07 §7.3）。
const DETAIL_SYSTEMD_OK: &str = "systemd 為 init";
/// `systemd`：not ok detail（07 §7.3）。
const DETAIL_SYSTEMD_NONE: &str = "未偵測到 systemd";
/// `systemd`：fix（07 §7.3）。
const FIX_SYSTEMD: &str =
    "WSL 可於 /etc/wsl.conf 設 [boot] systemd=true 後 wsl --shutdown（on-demand 模式可忽略）";
/// `single_daemon`：fix（07 §7.3）。
const FIX_SINGLE_DAEMON: &str =
    "停掉多餘 daemon（serialwrap service stop；並檢查 systemd-user 與 system 是否同時在跑）";
/// `single_daemon`：多開後綴（07 §7.3）。
const DETAIL_MULTI_OPEN: &str = "（偵測到多開）";
/// `endpoint_reachable`：解析失敗（07 §7.3）。
const DETAIL_ENDPOINT_NOT_RESOLVED: &str = "無法解析本 client 的 endpoint（解析失敗），略過";
/// `endpoint_reachable`：未知 socket（07 §7.3）。
const DETAIL_SOCKET_UNKNOWN: &str = "未知（無法從 /proc 讀出 --socket）";
/// `endpoint_reachable`：預設來源標籤（07 §7.3）。
const DETAIL_SOURCE_DEFAULT: &str = "預設（SOCKET_PATH，受 SERIALWRAP_STATE_DIR/XDG 環境變數影響）";
/// `endpoint_reachable`：連不上 fix（07 §7.3）。
const FIX_ENDPOINT_UNREACHABLE: &str = "確認本 client 的 SERIALWRAP_STATE_DIR/config.yaml 是否與該 daemon 一致（可用 --socket/--endpoint 明確指定該路徑繞過解析；daemon 若為 #173 修正後版本，重啟一次即會把實際 socket 同步寫回 config.yaml）";
/// `daemon_endpoint`：可連（07 §7.3）。
const DETAIL_REACHABLE: &str = "可連";
/// `daemon_endpoint`：未在跑（07 §7.3）。
const DETAIL_NOT_RUNNING: &str = "未在跑";
/// `daemon_endpoint`：fix（07 §7.3）。
const FIX_DAEMON_ENDPOINT: &str =
    "serialwrap daemon start（或 serialwrapd.exe --socket tcp://127.0.0.1:48700）";
/// `wal_writable`：略過 detail（07 §7.3）。
const DETAIL_WAL_NOT_REPORTED: &str =
    "daemon 未在跑、無法連線，或為不回報 wal 健康欄位的舊版 daemon；略過";
/// `wal_writable`：目錄不存在原因（07 §7.3）。
const REASON_DIR_MISSING: &str = "目錄不存在";
/// `wal_writable`：目錄不可寫原因（07 §7.3）。
const REASON_DIR_NOT_WRITABLE: &str = "目錄不可寫";
/// `wal_writable`：原因未知（07 §7.3）。
const REASON_UNKNOWN: &str = "未知";
/// `profile_bootloader_prompts`：資產不可讀（07 §7.3）。
const DETAIL_ASSET_UNREADABLE: &str = "資產不可讀，略過";
/// `profile_bootloader_prompts`：一致（07 §7.3）。
const DETAIL_MATCHES_ASSET: &str = "與出貨資產一致";
/// `devices`（Linux）：目錄無法讀取（07 §7.3）。
const DETAIL_UNREADABLE_DIR: &str = "無法讀取";
/// `devices`（Linux）：fix（07 §7.3）。
const FIX_DEVICES_LINUX: &str =
    "確認 USB-serial 已接上且有 /dev/serial/by-id/* （或設 SERIALWRAP_BY_ID_DIR）";
/// `devices`（Windows）：登錄讀取失敗（07 §7.3）。
const DETAIL_SERIALCOMM_FAILED: &str = "無法讀取 SERIALCOMM";
/// `devices`（Windows）：fix（ok 與 not ok 共用）（07 §7.3）。
const FIX_DEVICES_WINDOWS: &str =
    "確認 USB-serial 已接上並出現在裝置管理員（藍牙 COM 會被自動排除）";
/// `wsl_systemd`：非 WSL（07 §7.3）。
const DETAIL_NON_WSL: &str = "非 WSL 環境";
/// `wsl_systemd`：ok detail（07 §7.3）。
const DETAIL_WSL_SYSTEMD_OK: &str = "WSL 已啟用 systemd";
/// `wsl_systemd`：not ok detail（07 §7.3）。
const DETAIL_WSL_SYSTEMD_NONE: &str = "WSL 未啟用 systemd";
/// `wsl_systemd`：fix（07 §7.3）。
const FIX_WSL_SYSTEMD: &str =
    "於 /etc/wsl.conf 設 [boot] systemd=true 後 wsl --shutdown（on-demand 模式可忽略）";
/// `serialwrap_minicom_on_path`：fix（07 §7.3）。
const FIX_MINICOM_WRAPPER: &str = "執行 `serialwrap setup` 物化 minicom wrapper 到 ~/.local/bin（並確認該目錄在 PATH，可用 pipx ensurepath）；human console 一律經 serialwrap-minicom COMx，勿直接對 tty 開 minicom（避免與 daemon two-reader 衝突）";
/// `jq_on_path`：fix（07 §7.3）。
const FIX_JQ: &str = "安裝 jq（Debian/Ubuntu/Mint: sudo apt install jq）——serialwrap-minicom wrapper 解析 session 狀態需要";
/// `minicom_on_path`：fix（07 §7.3）。
const FIX_MINICOM: &str = "安裝 minicom（Debian/Ubuntu/Mint: sudo apt install minicom）";
/// `wal_dir`：shell 與 daemon WAL_DIR 不一致 fix（07 §7.3）。
const FIX_WAL_DIR_MISMATCH: &str = concat!(
    "daemon 由 systemd 管理時 unit 不會繼承 shell 匯出的 env（.bashrc 的 export 對它無效）：",
    "需在 unit 加 Environment=SERIALWRAP_WAL_DIR=<path> 後 systemctl daemon-reload ",
    "並 `serialwrap service restart`；on-demand/前景啟動則需在同一個帶該 env 的 shell ",
    "重跑 `serialwrap daemon stop && serialwrap daemon start`。查詢一律以 ",
    "`serialwrap daemon status` 的 wal_path 為準，不要用 shell env 去猜",
);
/// `wal_writable`：不可寫 fix（07 §7.3）。
const FIX_WAL_WRITABLE: &str = concat!(
    "確認沒有外部工具刪除該目錄（已知案例：testpilot 的 clean_wal() 會 rmtree ",
    "硬編路徑 /tmp/serialwrap/wal，見 hamanpaul/testpilot-core#36）。daemon 會在",
    "下一次寫入時自動重建目錄續寫，但消失期間的紀錄無法復原；需要持久稽核請把 ",
    "WAL_DIR 指向非 /tmp 的路徑（systemd 模式在 unit 加 ",
    "Environment=SERIALWRAP_WAL_DIR=<path> 後 systemctl daemon-reload 並 ",
    "`serialwrap service restart`）",
);
/// `profile_bootloader_prompts`：缺 bootloader_prompts fix（07 §7.3）。
const FIX_PROFILE_PROMPTS: &str = concat!(
    "重新物化資產或手動補上：備份現有檔後執行 `serialwrap setup`，",
    "或把 sw_core/assets/profiles/default.yaml 對應 template 的 bootloader_prompts 段落補進線上檔",
);
// ---- Rust 側契約字串（07 §7.3 之外）----
/// `python`／`pyyaml`／`pyserial` 共用的 daemon runtime 逐項說明前綴（07 §7.3 之外，Rust CLI 專屬）。
const DETAIL_UNLOCATABLE_PREFIX: &str = "無法定位 daemon Python runtime";
const DETAIL_UNRUNNABLE_PREFIX: &str = "無法執行 daemon Python runtime";
const REASON_NO_MANIFEST: &str = "找不到 release manifest";
const REASON_TIMEOUT: &str = "逾時（5 s）";
const RUNTIME_TIMEOUT: Duration = Duration::from_secs(5);
const KEY_RUNTIME_PATH: &str = "components.daemon.runtime.path";
/// `load_release_manifest` 無法讀取或 JSON 無效時的 key（Python 基線同形）。
const KEY_UNREADABLE: &str = "unreadable";

/// release manifest 的定位結果（`version_pairing` 與 daemon runtime 檢查共用）。
enum ManifestState {
    /// 找不到 manifest：不附加 `version_pairing`。
    Missing,
    /// 找到但驗證失敗：`key` 為 Python `invalid release manifest: <key>` 的 key。
    Invalid { key: String },
    /// 驗證通過。
    Valid {
        dir: PathBuf,
        manifest: ReleaseManifest,
    },
}

/// `sw_proto::pypath` 所需的 home 查詢（與 `endpoint_resolve` 相同語意）。
struct EnvHome<'a>(&'a dyn sw_pal::SysEnv);

impl HomeLookup for EnvHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

fn flavor_of(ctx: &CliCtx) -> PathFlavor {
    match ctx.p.caps.path_flavor {
        PalPathFlavor::Posix => PathFlavor::Posix,
        PalPathFlavor::Windows => PathFlavor::Windows,
    }
}

/// Python `bool(v)`：缺值（None）與 `PyValue` 的真值判定。
fn truthy(value: Option<&PyValue>) -> bool {
    match value {
        None | Some(PyValue::Null) => false,
        Some(PyValue::Bool(b)) => *b,
        Some(PyValue::Int(sw_proto::pyjson::PyInt::Small(n))) => *n != 0,
        Some(PyValue::Int(sw_proto::pyjson::PyInt::Big(s))) => !s.is_empty() && s != "0",
        Some(PyValue::Float(f)) => *f != 0.0,
        Some(PyValue::Str(s)) => !s.is_empty(),
        Some(PyValue::List(items)) => !items.is_empty(),
        Some(PyValue::Dict(d)) => !d.0.is_empty(),
    }
}

/// Python `str(v)`；缺值（None）為 `"None"`。
fn str_of(value: Option<&PyValue>) -> String {
    match value {
        None => "None".to_string(),
        Some(v) => py_str(v),
    }
}

/// Python `int(v or 0)`：無法轉為整數者視為 0（doctor 永不拋例外）。
fn int_or_zero(value: Option<&PyValue>) -> i128 {
    match value {
        Some(PyValue::Int(PyInt::Small(n))) => *n,
        Some(PyValue::Bool(b)) => i128::from(*b),
        Some(PyValue::Float(f)) => *f as i128,
        Some(PyValue::Str(s)) => s.trim().parse::<i128>().unwrap_or(0),
        _ => 0,
    }
}

/// 讀取 `SERIALWRAP_BY_ID_DIR`（`_env_path` 語意：空白視為未設定，並 `expanduser`）。
fn by_id_dir(ctx: &CliCtx) -> String {
    let raw = ctx
        .env
        .get("SERIALWRAP_BY_ID_DIR")
        .filter(|value| !py_strip(value).is_empty())
        .unwrap_or("/dev/serial/by-id");
    pypath::expanduser(flavor_of(ctx), raw, &ctx.env, &EnvHome(ctx.p.env.as_ref()))
}

/// config.yaml 的 `socket_path`（非空字串才採用；Python `cfg_sock` 的 truthy 語意）。
fn cfg_socket(ctx: &CliCtx) -> Option<String> {
    let rc = endpoint_resolve::safe_runtime_config(ctx)?;
    match rc.socket_path() {
        Some(YValue::Str(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    }
}

/// `endpoint = cfg_sock or _local_default_endpoint()`（`wal_dir`、`wal_writable` 共用，無 dangling fallback）。
fn plain_endpoint(ctx: &CliCtx) -> String {
    cfg_socket(ctx).unwrap_or_else(|| endpoint_resolve::local_default_endpoint(ctx))
}

/// `health.status`（0.5 s 固定逾時）；失敗一律回空字典（doctor 永不拋例外）。
fn health_status(ctx: &CliCtx, endpoint: &str) -> PyDict {
    client::call(
        ctx,
        endpoint,
        "health.status",
        &PyValue::Dict(PyDict::new()),
        HEALTH_TIMEOUT_S,
        &PyInt::Small(0),
    )
    .map(|outcome| outcome.resp)
    .unwrap_or_else(|_| PyDict::new())
}

/// 掃描本機 serialwrapd（依 pid 排序；掃描失敗視為無行程，與 `detect_multi_open` 的 OSError 分支一致）。
fn daemon_instances(ctx: &CliCtx) -> Vec<sw_pal::DaemonProc> {
    let mut daemons = ctx.p.procs.serialwrapd_instances().unwrap_or_default();
    daemons.sort_by_key(|daemon| daemon.pid);
    daemons
}

/// 定位並驗證 release manifest（`find_release_manifest` + `load_release_manifest`）。
fn manifest_state(ctx: &CliCtx) -> ManifestState {
    match install_layout::locate_manifest(ctx) {
        Ok((path, manifest)) => match validate_manifest(&manifest.raw) {
            Ok(()) => ManifestState::Valid {
                dir: manifest_dir(&path),
                manifest,
            },
            Err(key) => ManifestState::Invalid { key },
        },
        Err(LayoutError::ManifestNotFound(_)) => ManifestState::Missing,
        Err(LayoutError::Parse { path, .. }) => ManifestState::Invalid {
            key: parse_failure_key(ctx, &path),
        },
        // 路徑存在但讀取失敗（權限、I/O）：Python 基線為 invalid release manifest: unreadable，不得視為未找到。
        Err(_) => ManifestState::Invalid {
            key: KEY_UNREADABLE.to_string(),
        },
    }
}

/// 解析失敗的 Python 同形 key：以位元組重跑 `load_release_manifest` 的結構驗證。
///
/// Windows 的 `current-release.json` 解析失敗不是 manifest，不可拿來重驗（否則會誤報 `schema_version`）。
fn parse_failure_key(ctx: &CliCtx, path: &Path) -> String {
    if path.file_name().and_then(|name| name.to_str()) == Some("current-release.json") {
        return KEY_UNREADABLE.to_string();
    }
    let Ok(bytes) = ctx.p.fs.read(path) else {
        return KEY_UNREADABLE.to_string();
    };
    let Ok(text) = String::from_utf8(bytes) else {
        return KEY_UNREADABLE.to_string();
    };
    match sw_proto::pyjson::loads(&text) {
        Err(_) => KEY_UNREADABLE.to_string(),
        Ok(PyValue::Dict(root)) => match validate_manifest(&root) {
            Err(key) => key,
            // 結構驗證通過、僅 sw_proto 嚴格解析拒絕：回報該可選欄位的 key（與 parse_manifest 同序）。
            Ok(()) => parse_only_rejection_key(&root),
        },
        Ok(_) => "root".to_string(),
    }
}

/// `sw_proto::parse_manifest` 比 Python 基線多出的嚴格規則（Python `load_release_manifest` 不檢查這些可選欄位）。
///
/// 回傳第一個違規欄位的 dotted key；無違規時回 `unreadable`（僅在解析失敗但無對應規則時出現，屬防禦性後備）。
fn parse_only_rejection_key(root: &PyDict) -> String {
    let state_schema = lookup(root, "components.daemon.state_schema");
    if matches!(state_schema, Some(value) if !matches!(value, PyValue::Str(_))) {
        return "components.daemon.state_schema".to_string();
    }
    let task_xml = lookup(root, "components.supervisor.task_xml_template_sha256");
    let task_xml_bad = match task_xml {
        None => false,
        Some(PyValue::Str(digest)) => !is_lower_sha256_hex(digest),
        Some(_) => true,
    };
    if task_xml_bad {
        return "components.supervisor.task_xml_template_sha256".to_string();
    }
    KEY_UNREADABLE.to_string()
}

/// 64 位小寫十六進位（與 `sw_proto::release_manifest` 的 `is_sha256_hex` 相同）。
fn is_lower_sha256_hex(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn manifest_dir(path: &Path) -> PathBuf {
    path.parent().map(Path::to_path_buf).unwrap_or_default()
}

/// 巢狀字典取值（dotted path；中間節點非字典則為 None）。
fn lookup<'a>(root: &'a PyDict, dotted: &str) -> Option<&'a PyValue> {
    let mut parts = dotted.split('.');
    let mut node = root.get(parts.next()?)?;
    for part in parts {
        match node {
            PyValue::Dict(d) => node = d.get(part)?,
            _ => return None,
        }
    }
    Some(node)
}

fn is_nonempty_str(value: Option<&PyValue>) -> bool {
    matches!(value, Some(PyValue::Str(s)) if !s.trim().is_empty())
}

/// `sw_core/release_manifest.py` `load_release_manifest` 的結構驗證（逐步對照；回傳第一個不合法的 key）。
fn validate_manifest(root: &PyDict) -> Result<(), String> {
    const REQUIRED: &[(&str, &[&str])] = &[
        (
            "",
            &[
                "schema_version",
                "bundle_version",
                "stage",
                "created_at",
                "target",
                "git_revision",
                "contract_revision",
                "components",
                "pairing",
            ],
        ),
        (
            "components",
            &[
                "cli",
                "daemon",
                "python_cli_delegate",
                "assets",
                "supervisor",
            ],
        ),
        (
            "components.cli",
            &[
                "implementation",
                "version",
                "git_revision",
                "path",
                "sha256",
            ],
        ),
        (
            "components.daemon",
            &[
                "implementation",
                "version",
                "git_revision",
                "launcher",
                "entry",
                "runtime",
                "package",
            ],
        ),
        ("components.daemon.launcher", &["path", "sha256"]),
        (
            "components.daemon.runtime",
            &["path", "python_version", "sha256"],
        ),
        (
            "components.daemon.package",
            &["name", "version", "wheel_sha256", "tree_sha256"],
        ),
        ("components.python_cli_delegate", &["entry", "tree_sha256"]),
        (
            "components.assets",
            &["skill_sha256", "skill_windows_sha256", "profiles_sha256"],
        ),
        (
            "components.supervisor",
            &["default_mode", "definition_sha256"],
        ),
        ("pairing", &["rule", "cli_version", "daemon_version"]),
    ];
    const STR_LEAVES: &[&str] = &[
        "bundle_version",
        "stage",
        "created_at",
        "target",
        "git_revision",
        "contract_revision",
        "components.cli.implementation",
        "components.cli.version",
        "components.cli.git_revision",
        "components.cli.path",
        "components.cli.sha256",
        "components.daemon.implementation",
        "components.daemon.version",
        "components.daemon.git_revision",
        "components.daemon.launcher.path",
        "components.daemon.launcher.sha256",
        "components.daemon.runtime.path",
        "components.daemon.runtime.python_version",
        "components.daemon.runtime.sha256",
        "components.daemon.package.name",
        "components.daemon.package.version",
        "components.daemon.package.tree_sha256",
        "components.python_cli_delegate.tree_sha256",
        "components.assets.skill_sha256",
        "components.assets.skill_windows_sha256",
        "components.assets.profiles_sha256",
        "components.supervisor.default_mode",
        "components.supervisor.definition_sha256",
        "pairing.rule",
        "pairing.cli_version",
        "pairing.daemon_version",
    ];
    const LIST_LEAVES: &[&str] = &[
        "components.daemon.entry",
        "components.python_cli_delegate.entry",
    ];

    match root.get("schema_version") {
        Some(PyValue::Int(PyInt::Small(1))) => {}
        Some(PyValue::Float(f)) if *f == 1.0 => {}
        _ => return Err("schema_version".to_string()),
    }
    for (prefix, keys) in REQUIRED {
        let node: Option<&PyDict> = if prefix.is_empty() {
            Some(root)
        } else {
            match lookup(root, prefix) {
                Some(PyValue::Dict(d)) => Some(d),
                _ => None,
            }
        };
        let Some(node) = node else {
            return Err((*prefix).to_string());
        };
        for key in *keys {
            if !node.0.contains_key(*key) {
                return Err(if prefix.is_empty() {
                    (*key).to_string()
                } else {
                    format!("{prefix}.{key}")
                });
            }
        }
    }
    for dotted in STR_LEAVES {
        if !is_nonempty_str(lookup(root, dotted)) {
            return Err((*dotted).to_string());
        }
    }
    for dotted in LIST_LEAVES {
        let valid = match lookup(root, dotted) {
            Some(PyValue::List(items)) => {
                !items.is_empty() && items.iter().all(|item| is_nonempty_str(Some(item)))
            }
            _ => false,
        };
        if !valid {
            return Err((*dotted).to_string());
        }
    }
    let wheel = lookup(root, "components.daemon.package.wheel_sha256");
    if !matches!(wheel, None | Some(PyValue::Null)) && !is_nonempty_str(wheel) {
        return Err("components.daemon.package.wheel_sha256".to_string());
    }
    if !matches!(lookup(root, "pairing.rule"), Some(PyValue::Str(rule)) if rule == "exact") {
        return Err("pairing.rule".to_string());
    }
    Ok(())
}

/// daemon runtime 的定位結果：可執行路徑（字串），或無法定位的原因。
fn daemon_runtime(ctx: &CliCtx, state: &ManifestState) -> Result<String, String> {
    match state {
        ManifestState::Missing => Err(REASON_NO_MANIFEST.to_string()),
        ManifestState::Invalid { key } => Err(format!("invalid release manifest: {key}")),
        ManifestState::Valid { dir, manifest } => {
            let relative = match lookup(&manifest.raw, KEY_RUNTIME_PATH) {
                Some(PyValue::Str(s)) => s.replace('\\', "/"),
                _ => return Err(format!("invalid release manifest: {KEY_RUNTIME_PATH}")),
            };
            // 與 pairing 相同的 bundle 內相對路徑限制：絕對路徑或含 `..` 者不得執行（避免跑到 bundle 外的直譯器）。
            if !safe_component_path(&relative) {
                return Err(format!("invalid release manifest: {KEY_RUNTIME_PATH}"));
            }
            let runtime = dir.join(&relative);
            if !ctx.p.fs.exists(&runtime) {
                return Err(format!("daemon runtime 不存在：{}", runtime.display()));
            }
            Ok(runtime.to_string_lossy().into_owned())
        }
    }
}

/// `-I -c <code>` 的執行結果；區分「有執行但 rc≠0」、「無法執行或逾時」（對應 07 §7.3 不同的 detail）。
enum RuntimeRun {
    /// rc 0：stdout 去尾端空白。
    Output(String),
    /// 有執行但 rc≠0（例如模組匯入失敗）。
    NonZero(i32),
    /// spawn 失敗或逾時。
    Unrunnable(String),
}

/// 以 daemon runtime 執行 `-I -c <code>`（5 s）。
fn run_runtime(ctx: &CliCtx, runtime: &str, code: &str) -> RuntimeRun {
    let argv = vec![
        runtime.to_string(),
        "-I".to_string(),
        "-c".to_string(),
        code.to_string(),
    ];
    match ctx.p.env.run(&argv, Some(RUNTIME_TIMEOUT)) {
        Ok(out) if out.rc == 0 => RuntimeRun::Output(py_strip(&out.stdout).to_string()),
        // 逾時：POSIX／Windows 的 run 慣例為 rc -1 且 stderr 為 TIMEOUT。
        Ok(out) if out.rc == -1 && out.stderr == "TIMEOUT" => {
            RuntimeRun::Unrunnable(REASON_TIMEOUT.to_string())
        }
        Ok(out) => RuntimeRun::NonZero(out.rc),
        Err(error) => RuntimeRun::Unrunnable(error.py_message),
    }
}

/// `python`：daemon runtime 的 Python 版本（`>= 3.10`）。
fn check_python(ctx: &CliCtx, state: &ManifestState) -> Check {
    let runtime = match daemon_runtime(ctx, state) {
        Ok(runtime) => runtime,
        Err(reason) => return unlocatable("python", &reason),
    };
    match run_runtime(ctx, &runtime, PY_VERSION_CODE) {
        RuntimeRun::Output(version) => {
            let ok = version_at_least(&version, PY_MIN);
            Check {
                check: "python",
                ok,
                detail: version,
                fix: Some(if ok {
                    String::new()
                } else {
                    FIX_PY_VERSION.to_string()
                }),
                advisory: false,
                extra: PyDict::new(),
            }
        }
        RuntimeRun::NonZero(rc) => unrunnable("python", &format!("rc={rc}")),
        RuntimeRun::Unrunnable(reason) => unrunnable("python", &reason),
    }
}

/// `python` 等三項共用：daemon runtime 無法定位時的統一回報。
fn unlocatable(name: &'static str, reason: &str) -> Check {
    Check {
        check: name,
        ok: false,
        detail: format!("{DETAIL_UNLOCATABLE_PREFIX}（{reason}）"),
        fix: Some(FIX_REINSTALL.to_string()),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// daemon runtime 存在但無法執行（spawn 失敗、逾時或 python 版本查詢 rc≠0）。
fn unrunnable(name: &'static str, reason: &str) -> Check {
    Check {
        check: name,
        ok: false,
        detail: format!("{DETAIL_UNRUNNABLE_PREFIX}（{reason}）"),
        fix: Some(FIX_REINSTALL.to_string()),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// `pyyaml`／`pyserial`：以 daemon runtime 匯入模組；匯入失敗（rc≠0）回 `未安裝` 與 07 §7.3 安裝提示。
fn check_module(
    ctx: &CliCtx,
    state: &ManifestState,
    name: &'static str,
    code: &str,
    fix_hint: &'static str,
) -> Check {
    let runtime = match daemon_runtime(ctx, state) {
        Ok(runtime) => runtime,
        Err(reason) => return unlocatable(name, &reason),
    };
    match run_runtime(ctx, &runtime, code) {
        RuntimeRun::Output(detail) => Check {
            check: name,
            ok: true,
            detail,
            fix: Some(String::new()),
            advisory: false,
            extra: PyDict::new(),
        },
        RuntimeRun::NonZero(_) => Check {
            check: name,
            ok: false,
            detail: DETAIL_NOT_INSTALLED.to_string(),
            fix: Some(fix_hint.to_string()),
            advisory: false,
            extra: PyDict::new(),
        },
        RuntimeRun::Unrunnable(reason) => unrunnable(name, &reason),
    }
}

/// 解析 `"主.次.修"`（或以點分隔的前綴）是否 ≥ `min`。
fn version_at_least(text: &str, min: (i64, i64)) -> bool {
    let mut parts = text.split('.').map(|part| part.trim().parse::<i64>());
    let major = parts.next().and_then(Result::ok);
    let minor = parts.next().and_then(Result::ok);
    match (major, minor) {
        (Some(major), Some(minor)) => (major, minor) >= min,
        _ => false,
    }
}

/// `<name>_on_path`：`which(name)` 命中即 ok（detail 為路徑或 `找不到`）。
fn check_on_path(ctx: &CliCtx, name: &str, check: &'static str, fix_hint: &str) -> Check {
    let path = ctx.p.env.which(name);
    let ok = path.is_some();
    Check {
        check,
        ok,
        detail: path
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(|| DETAIL_NOT_FOUND.to_string()),
        fix: Some(if ok {
            String::new()
        } else {
            fix_hint.to_string()
        }),
        advisory: false,
        extra: PyDict::new(),
    }
}

fn check_serialwrap_on_path(ctx: &CliCtx, windows: bool) -> Check {
    check_on_path(ctx, "serialwrap", "serialwrap_on_path", path_hint(windows))
}

fn check_serialwrapd_on_path(ctx: &CliCtx, windows: bool) -> Check {
    check_on_path(
        ctx,
        "serialwrapd",
        "serialwrapd_on_path",
        path_hint(windows),
    )
}

fn path_hint(windows: bool) -> &'static str {
    if windows {
        FIX_PATH_WINDOWS
    } else {
        FIX_PATH_LINUX
    }
}

/// `other_serialwrap_installs`：多份安裝且版本不一致時 not ok（不同版本以 `；` 串接）。
fn check_other_installs(ctx: &CliCtx) -> Check {
    let paths = ctx.p.env.which_all("serialwrap");
    if paths.len() <= 1 {
        let detail = if paths.is_empty() {
            DETAIL_OTHER_NONE.to_string()
        } else {
            DETAIL_OTHER_SINGLE.to_string()
        };
        return Check {
            check: "other_serialwrap_installs",
            ok: true,
            detail,
            fix: Some(String::new()),
            advisory: false,
            extra: PyDict::new(),
        };
    }
    let mut versions: Vec<String> = Vec::new();
    let mut entries: Vec<String> = Vec::new();
    for path in &paths {
        let program = path.to_string_lossy().into_owned();
        let argv = vec![program.clone(), "--version".to_string()];
        let version = match ctx.p.env.run(&argv, Some(Duration::from_secs(2))) {
            Ok(out) if out.rc == 0 && !py_strip(&out.stdout).is_empty() => {
                py_strip(&out.stdout).to_string()
            }
            _ => DETAIL_VERSION_UNAVAILABLE.to_string(),
        };
        entries.push(format!("{program}={version}"));
        versions.push(version);
    }
    let distinct: HashSet<&String> = versions.iter().collect();
    let ok = distinct.len() == 1;
    Check {
        check: "other_serialwrap_installs",
        ok,
        detail: entries.join("；"),
        fix: Some(if ok {
            String::new()
        } else {
            FIX_OTHER_INSTALLS.to_string()
        }),
        advisory: false,
        extra: PyDict::new(),
    }
}

fn check_dialout(ctx: &CliCtx) -> Check {
    let ok = ctx.p.env.user_in_group("dialout");
    Check {
        check: "dialout",
        ok,
        detail: if ok {
            DETAIL_DIALOUT_OK.to_string()
        } else {
            DETAIL_DIALOUT_NO.to_string()
        },
        fix: Some(if ok {
            String::new()
        } else {
            FIX_DIALOUT.to_string()
        }),
        advisory: false,
        extra: PyDict::new(),
    }
}

fn check_systemd(ctx: &CliCtx) -> Check {
    let ok = ctx.p.env.has_systemd();
    Check {
        check: "systemd",
        ok,
        detail: if ok {
            DETAIL_SYSTEMD_OK.to_string()
        } else {
            DETAIL_SYSTEMD_NONE.to_string()
        },
        fix: Some(if ok {
            String::new()
        } else {
            FIX_SYSTEMD.to_string()
        }),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// `supervision_mode`：恆 ok；detail 為 config.yaml 的 `supervision_mode`，缺省為 `on-demand`。
fn check_supervision_mode(ctx: &CliCtx) -> Check {
    let mode = endpoint_resolve::safe_runtime_config(ctx)
        .and_then(|rc| rc.mode().cloned())
        .filter(py_truthy_of)
        .map(|value| py_str_of(&value))
        .unwrap_or_else(|| "on-demand".to_string());
    Check {
        check: "supervision_mode",
        ok: true,
        detail: mode,
        fix: Some(String::new()),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// `single_daemon`：同機 serialwrapd 多開（>1）即 not ok。
fn check_single_daemon(ctx: &CliCtx) -> Check {
    let count = daemon_instances(ctx).len();
    let multi_open = count > 1;
    Check {
        check: "single_daemon",
        ok: !multi_open,
        detail: format!(
            "{count} 個 serialwrapd 在跑{}",
            if multi_open { DETAIL_MULTI_OPEN } else { "" }
        ),
        fix: Some(if multi_open {
            FIX_SINGLE_DAEMON.to_string()
        } else {
            String::new()
        }),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// `endpoint_reachable`：本 client 解析到的 endpoint 與實際執行中的 daemon 是否一致且可連（#173）。
fn check_endpoint_reachable(ctx: &mut CliCtx) -> Check {
    let resolved = endpoint_resolve::resolve_endpoint_info(
        ctx,
        &EndpointArgs {
            endpoint: None,
            socket: None,
            bench: None,
        },
    );
    let (endpoint, source) = match resolved {
        Ok(found) => (found.endpoint.clone(), source_label(ctx, found.source)),
        Err(_) => {
            return Check {
                check: "endpoint_reachable",
                ok: true,
                detail: DETAIL_ENDPOINT_NOT_RESOLVED.to_string(),
                fix: Some(String::new()),
                advisory: false,
                extra: PyDict::new(),
            };
        }
    };
    let daemons = daemon_instances(ctx);
    if daemons.is_empty() {
        return Check {
            check: "endpoint_reachable",
            ok: true,
            detail: format!(
                "未偵測到執行中的 serialwrapd（on-demand 模式正常）；本 client 解析到 {endpoint}（來源：{source}）"
            ),
            fix: Some(String::new()),
            advisory: false,
            extra: PyDict::new(),
        };
    }
    if endpoint_resolve::endpoint_alive(ctx, &endpoint) {
        return Check {
            check: "endpoint_reachable",
            ok: true,
            detail: format!("本 client 解析到 {endpoint}（來源：{source}），可連上執行中 daemon"),
            fix: Some(String::new()),
            advisory: false,
            extra: PyDict::new(),
        };
    }
    let mut sockets: Vec<String> = daemons
        .iter()
        .map(|daemon| {
            daemon
                .socket
                .clone()
                .unwrap_or_else(|| DETAIL_SOCKET_UNKNOWN.to_string())
        })
        .collect();
    sockets.sort();
    sockets.dedup();
    Check {
        check: "endpoint_reachable",
        ok: false,
        detail: format!(
            "本 client 解析到 {endpoint}（來源：{source}）連不上；目前執行中 daemon 綁在 {}",
            sockets.join("、")
        ),
        fix: Some(FIX_ENDPOINT_UNREACHABLE.to_string()),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// 來源標籤（Python `_resolve_default_endpoint_with_source` 的文字）。
fn source_label(ctx: &CliCtx, source: &str) -> String {
    match source {
        "config.yaml" => "config.yaml".to_string(),
        "config.yaml -> canonical fallback" => format!(
            "config.yaml（記錄 {} 不可連，#108 dangling fallback 改用 canonical）",
            cfg_socket(ctx).unwrap_or_default()
        ),
        _ => DETAIL_SOURCE_DEFAULT.to_string(),
    }
}

/// `daemon_endpoint`（Windows，advisory）：cfg 僅採用 `tcp://` 值，否則本機 canonical。
fn check_daemon_endpoint(ctx: &CliCtx) -> Check {
    let endpoint = cfg_socket(ctx)
        .filter(|value| value.starts_with("tcp://"))
        .unwrap_or_else(|| endpoint_resolve::local_default_endpoint(ctx));
    let ok = endpoint_resolve::endpoint_alive(ctx, &endpoint);
    Check {
        check: "daemon_endpoint",
        ok,
        detail: format!(
            "{endpoint}（{}）",
            if ok {
                DETAIL_REACHABLE
            } else {
                DETAIL_NOT_RUNNING
            }
        ),
        fix: Some(if ok {
            String::new()
        } else {
            FIX_DAEMON_ENDPOINT.to_string()
        }),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// `wal_dir`（advisory）：daemon 實際生效 WAL_DIR 與 shell `SERIALWRAP_WAL_DIR` 是否一致。
fn check_wal_dir(ctx: &CliCtx) -> Check {
    let endpoint = plain_endpoint(ctx);
    let resp = health_status(ctx, &endpoint);
    let wal_path = resp.get("wal_path");
    if !truthy(resp.get("ok")) || !truthy(wal_path) {
        return Check {
            check: "wal_dir",
            ok: true,
            detail: format!(
                "daemon 未在跑或無法連線，僅顯示本地端解析值：WAL_DIR={}",
                ctx.paths.wal_dir
            ),
            fix: Some(String::new()),
            advisory: false,
            extra: PyDict::new(),
        };
    }
    let flavor = flavor_of(ctx);
    let daemon_dir = pypath::dirname(flavor, &str_of(wal_path));
    let shell_override = py_strip(ctx.env.get("SERIALWRAP_WAL_DIR").unwrap_or("")).to_string();
    let home = EnvHome(ctx.p.env.as_ref());
    let mismatch = !shell_override.is_empty()
        && pypath::normpath(
            flavor,
            &pypath::expanduser(flavor, &shell_override, &ctx.env, &home),
        ) != pypath::normpath(flavor, &daemon_dir);
    if mismatch {
        return Check {
            check: "wal_dir",
            ok: false,
            detail: format!(
                "daemon 實際生效 WAL_DIR={daemon_dir}，與 shell SERIALWRAP_WAL_DIR={shell_override} 不一致"
            ),
            fix: Some(FIX_WAL_DIR_MISMATCH.to_string()),
            advisory: false,
            extra: PyDict::new(),
        };
    }
    Check {
        check: "wal_dir",
        ok: true,
        detail: format!("daemon 實際生效 WAL_DIR={daemon_dir}"),
        fix: Some(String::new()),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// `wal_writable`（非 advisory）：daemon 回報的 WAL 目錄是否真的存在且可寫（#189）。
fn check_wal_writable(ctx: &CliCtx) -> Check {
    let endpoint = plain_endpoint(ctx);
    let resp = health_status(ctx, &endpoint);
    let wal = match resp.get("wal") {
        Some(PyValue::Dict(wal)) if truthy(resp.get("ok")) => wal,
        _ => {
            return Check {
                check: "wal_writable",
                ok: true,
                detail: DETAIL_WAL_NOT_REPORTED.to_string(),
                fix: Some(String::new()),
                advisory: false,
                extra: PyDict::new(),
            };
        }
    };
    if truthy(wal.get("healthy")) {
        let mut detail = format!(
            "WAL 目錄可寫：{}（current_seq={}）",
            str_of(wal.get("wal_dir")),
            str_of(wal.get("current_seq"))
        );
        if truthy(wal.get("recreated_count")) {
            detail.push_str(&format!(
                "；曾自癒重建 {} 次（消失期間的紀錄無法復原）",
                str_of(wal.get("recreated_count"))
            ));
        }
        return Check {
            check: "wal_writable",
            ok: true,
            detail,
            fix: Some(String::new()),
            advisory: false,
            extra: PyDict::new(),
        };
    }
    let mut reasons: Vec<String> = Vec::new();
    if !truthy(wal.get("wal_dir_exists")) {
        reasons.push(REASON_DIR_MISSING.to_string());
    } else if !truthy(wal.get("wal_dir_writable")) {
        reasons.push(REASON_DIR_NOT_WRITABLE.to_string());
    }
    if int_or_zero(wal.get("current_seq")) > 0 && !truthy(wal.get("wal_file_exists")) {
        reasons.push(format!(
            "已寫過 {} 筆紀錄但現行 WAL 檔不存在",
            str_of(wal.get("current_seq"))
        ));
    }
    if truthy(wal.get("write_failures")) {
        reasons.push(format!(
            "append 失敗 {} 次（最後一次：{}）",
            str_of(wal.get("write_failures")),
            str_of(wal.get("last_write_error"))
        ));
    }
    let reason_text = if reasons.is_empty() {
        REASON_UNKNOWN.to_string()
    } else {
        reasons.join("；")
    };
    Check {
        check: "wal_writable",
        ok: false,
        detail: format!(
            "WAL 目錄 {} 異常：{reason_text}",
            str_of(wal.get("wal_dir"))
        ),
        fix: Some(FIX_WAL_WRITABLE.to_string()),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// `profiles` 下的 template 名單（Python：缺省或假值視為空；非字典即 None）。
fn profiles_entries(root: &YValue) -> Option<Vec<(YValue, YValue)>> {
    let YValue::Map(map) = root else {
        return None;
    };
    match map.get_str("profiles") {
        None => Some(Vec::new()),
        Some(value) if !py_truthy_of(value) => Some(Vec::new()),
        Some(YValue::Map(profiles)) => Some(profiles.0.clone()),
        Some(_) => None,
    }
}

/// 純函式：出貨資產有 `bootloader_prompts` 而線上檔沒有的 template 名單（排序後）。
fn profile_templates_missing_bootloader_prompts(asset_text: &str, live_text: &str) -> Vec<String> {
    let (Ok(asset), Ok(live)) = (yaml11::safe_load(asset_text), yaml11::safe_load(live_text))
    else {
        return Vec::new();
    };
    let (Some(asset_profiles), Some(live_profiles)) =
        (profiles_entries(&asset), profiles_entries(&live))
    else {
        return Vec::new();
    };
    let mut drifted: Vec<String> = Vec::new();
    for (name, template) in &asset_profiles {
        let YValue::Map(template) = template else {
            continue;
        };
        if !template
            .get_str("bootloader_prompts")
            .is_some_and(py_truthy_of)
        {
            continue;
        }
        let Some((_, YValue::Map(live_template))) =
            live_profiles.iter().find(|(key, _)| key == name)
        else {
            continue;
        };
        if !live_template
            .get_str("bootloader_prompts")
            .is_some_and(py_truthy_of)
        {
            drifted.push(py_str_of(name));
        }
    }
    drifted.sort();
    drifted
}

/// `profile_bootloader_prompts`（advisory）：線上 profile 是否漏了出貨資產的 `bootloader_prompts`（#162 C4）。
fn check_profile_bootloader_prompts(ctx: &CliCtx) -> Check {
    let asset_bytes = PROFILE_ASSETS
        .iter()
        .find(|asset| asset.rel == "profiles/default.yaml")
        .map(|asset| asset.bytes);
    let live_path = pypath::join(flavor_of(ctx), &ctx.paths.profile_dir, &["default.yaml"]);
    let asset_text = asset_bytes.and_then(|bytes| String::from_utf8(bytes.to_vec()).ok());
    let Some(asset_text) = asset_text else {
        return Check {
            check: "profile_bootloader_prompts",
            ok: true,
            detail: DETAIL_ASSET_UNREADABLE.to_string(),
            fix: Some(String::new()),
            advisory: false,
            extra: PyDict::new(),
        };
    };
    let live_text = ctx
        .p
        .fs
        .read(Path::new(&live_path))
        .ok()
        .and_then(|bytes| String::from_utf8(bytes).ok());
    let Some(live_text) = live_text else {
        return Check {
            check: "profile_bootloader_prompts",
            ok: true,
            detail: format!("線上 profile 未物化（{live_path}），略過"),
            fix: Some(String::new()),
            advisory: false,
            extra: PyDict::new(),
        };
    };
    let drifted = profile_templates_missing_bootloader_prompts(&asset_text, &live_text);
    if drifted.is_empty() {
        return Check {
            check: "profile_bootloader_prompts",
            ok: true,
            detail: DETAIL_MATCHES_ASSET.to_string(),
            fix: Some(String::new()),
            advisory: false,
            extra: PyDict::new(),
        };
    }
    Check {
        check: "profile_bootloader_prompts",
        ok: false,
        detail: format!(
            "線上 {live_path} 的 {} 缺 bootloader_prompts（出貨資產有）",
            drifted.join(", ")
        ),
        fix: Some(FIX_PROFILE_PROMPTS.to_string()),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// `devices`（Linux，advisory）：by-id 目錄條目數（含 dangling 項）；目錄不存在為 `0`。
fn check_devices_linux(ctx: &CliCtx) -> Check {
    let dir = PathBuf::from(by_id_dir(ctx));
    let (ok, detail) = if ctx.p.fs.is_dir(&dir) {
        match ctx.p.fs.list_dir(&dir) {
            Ok(entries) => (!entries.is_empty(), entries.len().to_string()),
            Err(_) => (false, DETAIL_UNREADABLE_DIR.to_string()),
        }
    } else {
        (false, "0".to_string())
    };
    Check {
        check: "devices",
        ok,
        detail,
        fix: Some(if ok {
            String::new()
        } else {
            FIX_DEVICES_LINUX.to_string()
        }),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// config.yaml 的 `windows.exclude_coms`（每次重讀；缺席或格式錯誤即空集合，PLT-O-16）。
fn load_exclude_coms(ctx: &CliCtx) -> HashSet<String> {
    let path = PathBuf::from(endpoint_resolve::config_read_path(ctx));
    if !ctx.p.fs.exists(&path) {
        return HashSet::new();
    }
    let Ok(bytes) = ctx.p.fs.read(&path) else {
        return HashSet::new();
    };
    let Ok(text) = String::from_utf8(bytes) else {
        return HashSet::new();
    };
    let Ok(YValue::Map(root)) = yaml11::safe_load(&text) else {
        return HashSet::new();
    };
    let Some(YValue::Map(windows)) = root.get_str("windows") else {
        return HashSet::new();
    };
    match windows.get_str("exclude_coms") {
        Some(YValue::Seq(items)) => items.iter().map(py_str_of).collect(),
        _ => HashSet::new(),
    }
}

/// `devices`（Windows，advisory）：SERIALCOMM 扣除藍牙與 `windows.exclude_coms`。
fn check_devices_windows(ctx: &CliCtx) -> Check {
    let Ok(serialcomm) = ctx.p.registry.enum_values(SERIALCOMM_KEY) else {
        return Check {
            check: "devices",
            ok: false,
            detail: DETAIL_SERIALCOMM_FAILED.to_string(),
            fix: Some(FIX_DEVICES_WINDOWS.to_string()),
            advisory: false,
            extra: PyDict::new(),
        };
    };
    let bt_ports = read_bt_ports(&*ctx.p.registry);
    let manual = load_exclude_coms(ctx);
    let kept = exclude_bluetooth(&serialcomm, &bt_ports, &manual);
    let excluded = serialcomm.len().saturating_sub(kept.len());
    let ok = !kept.is_empty();
    let mut detail = if ok {
        let mut sorted = kept.clone();
        sorted.sort();
        format!("{}（{}）", kept.len(), sorted.join(", "))
    } else {
        "0".to_string()
    };
    if excluded > 0 {
        detail.push_str(&format!("；排除 {excluded}（藍牙/exclude_coms）"));
    }
    Check {
        check: "devices",
        ok,
        detail,
        fix: Some(if ok {
            String::new()
        } else {
            FIX_DEVICES_WINDOWS.to_string()
        }),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// `wsl_systemd`（Linux，advisory）：非 WSL 恆 ok；WSL 需 systemd。
fn check_wsl_systemd(ctx: &CliCtx) -> Check {
    let is_wsl = ctx.p.env.is_wsl();
    let ok = !is_wsl || ctx.p.env.has_systemd();
    let detail = if !is_wsl {
        DETAIL_NON_WSL
    } else if ok {
        DETAIL_WSL_SYSTEMD_OK
    } else {
        DETAIL_WSL_SYSTEMD_NONE
    };
    Check {
        check: "wsl_systemd",
        ok,
        detail: detail.to_string(),
        fix: Some(if ok {
            String::new()
        } else {
            FIX_WSL_SYSTEMD.to_string()
        }),
        advisory: false,
        extra: PyDict::new(),
    }
}

/// `version_pairing`（SC-25，非 advisory；只在找到 manifest 時附加）。
fn check_version_pairing(ctx: &mut CliCtx, state: &ManifestState) -> Option<Check> {
    let (dir, manifest) = match state {
        ManifestState::Missing => return None,
        ManifestState::Invalid { key } => {
            let mut extra = PyDict::new();
            extra.insert("mismatches", PyValue::List(Vec::new()));
            return Some(Check {
                check: "version_pairing",
                ok: false,
                detail: format!("invalid release manifest: {key}"),
                fix: Some(FIX_PAIRING.to_string()),
                advisory: false,
                extra,
            });
        }
        ManifestState::Valid { dir, manifest } => (dir, manifest),
    };
    let daemon_version = probe_daemon_version(ctx);
    let item = pairing_item(manifest, dir, ctx.cli_version, daemon_version.as_deref());
    let ok = matches!(item.get("ok"), Some(PyValue::Bool(true)));
    let detail = match item.get("detail") {
        Some(PyValue::Str(s)) => s.clone(),
        _ => String::new(),
    };
    let mismatches = match item.get("mismatches") {
        Some(PyValue::List(items)) => items.clone(),
        _ => Vec::new(),
    };
    let only_unreachable = matches!(
        mismatches.as_slice(),
        [PyValue::Str(only)] if only == "daemon_unreachable"
    );
    let fix = if ok {
        String::new()
    } else if only_unreachable {
        FIX_UNREACHABLE.to_string()
    } else {
        FIX_PAIRING.to_string()
    };
    let mut extra = PyDict::new();
    extra.insert("mismatches", PyValue::List(mismatches));
    Some(Check {
        check: "version_pairing",
        ok,
        detail,
        fix: Some(fix),
        advisory: false,
        extra,
    })
}

/// daemon 版本以 `health.status` 唯讀探測（endpoint 與 CLI 相同解析，含 #108 fallback）；不可達回 None。
fn probe_daemon_version(ctx: &mut CliCtx) -> Option<String> {
    let endpoint = endpoint_resolve::resolve_endpoint_info(
        ctx,
        &EndpointArgs {
            endpoint: None,
            socket: None,
            bench: None,
        },
    )
    .ok()?
    .endpoint;
    let resp = health_status(ctx, &endpoint);
    if !truthy(resp.get("ok")) {
        return None;
    }
    match resp.get("version") {
        Some(PyValue::Str(version)) if !version.trim().is_empty() => Some(version.clone()),
        _ => None,
    }
}

fn check_to_dict(check: &Check) -> PyDict {
    let mut dict = PyDict::new();
    dict.insert("advisory", PyValue::Bool(check.advisory));
    dict.insert("check", PyValue::Str(check.check.to_string()));
    dict.insert("detail", PyValue::Str(check.detail.clone()));
    dict.insert("fix", PyValue::Str(check.fix.clone().unwrap_or_default()));
    dict.insert("ok", PyValue::Bool(check.ok));
    for (key, value) in check.extra.0.iter() {
        dict.insert(key, value.clone());
    }
    dict
}

/// 執行全部檢查（平台清單以 `caps.path_flavor` 選擇；只有找到 manifest 時才附加 `version_pairing`）。
pub fn run_doctor(ctx: &mut CliCtx) -> Vec<Check> {
    let windows = ctx.p.caps.path_flavor == PalPathFlavor::Windows;
    let state = manifest_state(ctx);
    let mut checks = if windows {
        vec![
            check_python(ctx, &state),
            check_module(ctx, &state, "pyyaml", PYYAML_CODE, FIX_PYYAML),
            check_module(ctx, &state, "pyserial", PYSERIAL_CODE, FIX_PYSERIAL),
            check_serialwrap_on_path(ctx, true),
            check_serialwrapd_on_path(ctx, true),
            check_other_installs(ctx),
            check_supervision_mode(ctx),
            check_daemon_endpoint(ctx),
            check_wal_dir(ctx),
            check_wal_writable(ctx),
            check_profile_bootloader_prompts(ctx),
            check_devices_windows(ctx),
        ]
    } else {
        vec![
            check_python(ctx, &state),
            check_module(ctx, &state, "pyyaml", PYYAML_CODE, FIX_PYYAML),
            check_serialwrap_on_path(ctx, false),
            check_serialwrapd_on_path(ctx, false),
            check_other_installs(ctx),
            check_dialout(ctx),
            check_on_path(
                ctx,
                "serialwrap-minicom",
                "serialwrap_minicom_on_path",
                FIX_MINICOM_WRAPPER,
            ),
            check_on_path(ctx, "jq", "jq_on_path", FIX_JQ),
            check_on_path(ctx, "minicom", "minicom_on_path", FIX_MINICOM),
            check_systemd(ctx),
            check_supervision_mode(ctx),
            check_single_daemon(ctx),
            check_endpoint_reachable(ctx),
            check_wal_dir(ctx),
            check_wal_writable(ctx),
            check_profile_bootloader_prompts(ctx),
            check_devices_linux(ctx),
            check_wsl_systemd(ctx),
        ]
    };
    if let Some(pairing) = check_version_pairing(ctx, &state) {
        checks.push(pairing);
    }
    let advisory = if windows {
        DOCTOR_ADVISORY_CHECKS_WIN
    } else {
        DOCTOR_ADVISORY_CHECKS
    };
    for check in &mut checks {
        check.advisory = advisory.contains(&check.check);
    }
    checks
}

/// `serialwrap doctor`：印出 `{"checks":[…],"ok":overall}`；exit 恆為 0。
pub fn run_doctor_cmd(_ns: &Namespace, ctx: &mut CliCtx) -> i32 {
    let checks = run_doctor(ctx);
    let overall = checks.iter().all(|check| check.ok || check.advisory);
    let mut doc = PyDict::new();
    doc.insert(
        "checks",
        PyValue::List(
            checks
                .iter()
                .map(|check| PyValue::Dict(check_to_dict(check)))
                .collect(),
        ),
    );
    doc.insert("ok", PyValue::Bool(overall));
    let _ = output::print_json(&mut ctx.io, &doc);
    0
}
