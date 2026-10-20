//! `setup` 的資產物化、WSL systemd 準備與監管 reconcile（P4-T10）。

use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::argparse::{ArgValue, Namespace};
use crate::ctx::CliCtx;
use crate::endpoint_resolve::{self, EndpointArgs};
pub use crate::graceful_stop::DaemonIdentity;
use crate::graceful_stop::{self, StopFailure};
use crate::install_layout;
use crate::output;
use crate::supervisor_defs;
use sw_pal::fs_ops::LinkKind as PalLinkKind;
use sw_pal::service_ctl::{CmdRecord, ServiceAction, SupervisorDefinition};
use sw_pal::{ConsoleKind, PalPathFlavor, SupervisionMode};
use sw_proto::config_types::RuntimeConfig;
use sw_proto::endpoint::{parse_endpoint, Endpoint};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::runtime_paths::EnvSnapshot;
use sw_proto::yaml11::{py_str_of, py_truthy_of, YValue};

pub struct UserDirs {
    pub config: PathBuf,
    pub data: PathBuf,
    pub agents_skill_link: PathBuf,
    pub legacy_agents_skill_link: PathBuf,
    pub bin: PathBuf,
}

const DEFAULT_COM0COM_PAIR: (&str, &str) = sw_pal::com0com::DEFAULT_PAIR;

fn env_path(env: &EnvSnapshot, key: &str, fallback: PathBuf) -> PathBuf {
    env.get(key)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or(fallback)
}

pub fn user_dirs(env: &EnvSnapshot, home: &Path) -> UserDirs {
    let config = env_path(
        env,
        "SERIALWRAP_CONFIG_DIR",
        env_path(env, "XDG_CONFIG_HOME", home.join(".config")).join("serialwrap"),
    );
    let data = env_path(
        env,
        "SERIALWRAP_DATA_DIR",
        env_path(env, "XDG_DATA_HOME", home.join(".local/share")).join("serialwrap"),
    );
    UserDirs {
        config,
        data,
        agents_skill_link: home.join(".agents/skills/serialwrap"),
        legacy_agents_skill_link: home.join(".agents/skills/serialwrap-mcp"),
        bin: home.join(".local/bin"),
    }
}

pub use sw_pal::fs_ops::LinkKind;

pub struct MaterializeReport {
    pub profiles: PathBuf,
    pub skill_link: PathBuf,
    pub skill_link_kind: LinkKind,
    pub bin: Option<PathBuf>,
}

#[derive(Debug)]
pub enum SetupError {
    Io(String),
}

fn io_error(error: sw_pal::PalError) -> SetupError {
    SetupError::Io(error.py_message)
}

fn write_asset(ctx: &CliCtx<'_>, path: &Path, bytes: &[u8]) -> Result<(), SetupError> {
    if let Some(parent) = path.parent() {
        ctx.p.fs.mkdirs(parent).map_err(|error| {
            SetupError::Io(format!(
                "建立目錄 {} 失敗：{}",
                parent.display(),
                error.py_message
            ))
        })?;
    }
    ctx.p.fs.write_file(path, bytes).map_err(|error| {
        SetupError::Io(format!(
            "寫入檔案 {} 失敗：{}",
            path.display(),
            error.py_message
        ))
    })
}

fn asset_tail<'a>(rel: &'a str, prefix: &str) -> Option<&'a str> {
    rel.strip_prefix(prefix)
}

pub fn materialize_assets(
    ctx: &CliCtx<'_>,
    dirs: &UserDirs,
    force: bool,
) -> Result<MaterializeReport, SetupError> {
    let profiles = dirs.config.join("profiles");
    ctx.p.fs.mkdirs(&profiles).map_err(|error| {
        SetupError::Io(format!(
            "建立 profiles 目錄 {} 失敗：{}",
            profiles.display(),
            error.py_message
        ))
    })?;
    for asset in crate::assets::PROFILE_ASSETS {
        let Some(name) = asset_tail(asset.rel, "profiles/") else {
            continue;
        };
        let dest = profiles.join(name);
        if ctx.p.fs.exists(&dest) && !force {
            continue;
        }
        write_asset(ctx, &dest, asset.bytes)?;
    }

    let skill_dir = dirs.data.join("skill");
    ctx.p.fs.mkdirs(&skill_dir).map_err(|error| {
        SetupError::Io(format!(
            "建立 skill 目錄 {} 失敗：{}",
            skill_dir.display(),
            error.py_message
        ))
    })?;
    for asset in crate::assets::SKILL_ASSETS {
        let Some(name) = asset_tail(asset.rel, "skill/") else {
            continue;
        };
        write_asset(ctx, &skill_dir.join(name), asset.bytes)?;
    }
    if let Some(parent) = dirs.agents_skill_link.parent() {
        ctx.p.fs.mkdirs(parent).map_err(|error| {
            SetupError::Io(format!(
                "建立 skill link 目錄 {} 失敗：{}",
                parent.display(),
                error.py_message
            ))
        })?;
    }
    match ctx
        .p
        .fs
        .read_link(&dirs.agents_skill_link)
        .map_err(io_error)?
    {
        Some(_) => ctx
            .p
            .fs
            .remove_tree(&dirs.agents_skill_link)
            .map_err(io_error)?,
        None if ctx.p.fs.exists(&dirs.agents_skill_link) => {
            if ctx.p.fs.is_dir(&dirs.agents_skill_link) {
                ctx.p
                    .fs
                    .remove_tree(&dirs.agents_skill_link)
                    .map_err(io_error)?;
            } else {
                ctx.p
                    .fs
                    .remove_file(&dirs.agents_skill_link)
                    .map_err(io_error)?;
            }
        }
        None => {}
    }
    let skill_link_kind = match ctx
        .p
        .fs
        .symlink_or_copy_dir(&dirs.agents_skill_link, &skill_dir)
        .map_err(|error| {
            SetupError::Io(format!(
                "建立 skill link {} 失敗：{}",
                dirs.agents_skill_link.display(),
                error.py_message
            ))
        })? {
        PalLinkKind::Symlink => LinkKind::Symlink,
        PalLinkKind::Copied => LinkKind::Copied,
    };

    if let Ok(Some(target)) = ctx.p.fs.read_link(&dirs.legacy_agents_skill_link) {
        let target_text = target.to_string_lossy();
        if target
            .file_name()
            .is_some_and(|name| name == "serialwrap-mcp")
            || target_text.contains("custom-skills/serialwrap-mcp")
        {
            ctx.p
                .fs
                .remove_tree(&dirs.legacy_agents_skill_link)
                .map_err(|error| {
                    SetupError::Io(format!(
                        "移除舊 skill link {} 失敗：{}",
                        dirs.legacy_agents_skill_link.display(),
                        error.py_message
                    ))
                })?;
        }
    }

    let bin = if ctx.p.caps.console_kind == ConsoleKind::Pty {
        ctx.p.fs.mkdirs(&dirs.bin).map_err(|error| {
            SetupError::Io(format!(
                "建立 wrapper 目錄 {} 失敗：{}",
                dirs.bin.display(),
                error.py_message
            ))
        })?;
        for asset in crate::assets::TOOL_ASSETS {
            let Some(source_name) = asset_tail(asset.rel, "tools/") else {
                continue;
            };
            let Some((_, dest_name)) = crate::assets::WRAPPER_MAP
                .iter()
                .find(|(rel, _)| *rel == source_name)
            else {
                continue;
            };
            let dest = dirs.bin.join(dest_name);
            write_asset(ctx, &dest, asset.bytes)?;
            ctx.p.fs.set_mode(&dest, 0o755).map_err(|error| {
                SetupError::Io(format!(
                    "設定 wrapper 權限 {} 失敗：{}",
                    dest.display(),
                    error.py_message
                ))
            })?;
        }
        Some(dirs.bin.clone())
    } else {
        None
    };

    Ok(MaterializeReport {
        profiles,
        skill_link: dirs.agents_skill_link.clone(),
        skill_link_kind,
        bin,
    })
}

pub fn detect_legacy_install(ctx: &CliCtx<'_>, home: &Path) -> Option<PyDict> {
    let paul_tools = home.join(".paul_tools");
    let serialwrap = paul_tools.join("serialwrap");
    if !ctx.p.fs.exists(&serialwrap) {
        return None;
    }
    let minicom_symlink = [paul_tools.join("minicom"), home.join(".local/bin/minicom")]
        .iter()
        .any(|path| ctx.p.fs.read_link(path).ok().flatten().is_some());
    let old_state = Path::new("/tmp/serialwrap/state.json");
    let mut result = PyDict::new();
    result.insert("path", PyValue::Str(paul_tools.display().to_string()));
    result.insert("serialwrap", PyValue::Bool(true));
    result.insert("minicom_symlink", PyValue::Bool(minicom_symlink));
    result.insert(
        "state_json",
        if ctx.p.fs.exists(old_state) {
            PyValue::Str(old_state.display().to_string())
        } else {
            PyValue::Null
        },
    );
    result.insert(
        "hint",
        PyValue::Str(
            "偵測到舊版 ~/.paul_tools 安裝。建議退役步驟：1) 停掉舊 daemon（若仍在跑）；2) 移除 shadow 二進位 ~/.paul_tools/serialwrap 與 minicom 符號連結；3) 從 shell rc 移除 ~/.paul_tools 的 PATH export。新版改以 pipx 安裝（~/.local/bin）並以 XDG 路徑運作，不需 ~/.paul_tools。本指令不會自動刪除任何檔案。"
                .to_string(),
        ),
    );
    Some(result)
}

pub struct WslInfo {
    pub wsl: bool,
    pub already: bool,
    pub enabled_now: bool,
    pub needs_restart: bool,
    pub rc: Option<i32>,
    pub hint: Option<String>,
    pub pending: Vec<Vec<String>>,
}

pub fn ensure_wsl_systemd(
    ctx: &CliCtx<'_>,
    home: &Path,
    target: &SupervisionMode,
    with_sudo: bool,
) -> WslInfo {
    if !ctx.p.env.is_wsl() {
        return WslInfo {
            wsl: false,
            already: false,
            enabled_now: false,
            needs_restart: false,
            rc: None,
            hint: None,
            pending: Vec::new(),
        };
    }
    if ctx.p.env.has_systemd() {
        return WslInfo {
            wsl: true,
            already: true,
            enabled_now: false,
            needs_restart: false,
            rc: None,
            hint: None,
            pending: Vec::new(),
        };
    }

    let existing = ctx
        .p
        .fs
        .read(Path::new("/etc/wsl.conf"))
        .ok()
        .and_then(|bytes| String::from_utf8(bytes).ok())
        .unwrap_or_default();
    let staging = home.join(".local/share/serialwrap/wsl.conf");
    let merged = crate::wslconf::merge_wsl_conf_systemd(&existing);
    let stage_ok = write_asset(ctx, &staging, merged.as_bytes()).is_ok();
    let argv = vec![
        "sudo".to_string(),
        "install".to_string(),
        "-m".to_string(),
        "0644".to_string(),
        staging.display().to_string(),
        "/etc/wsl.conf".to_string(),
    ];
    let wants_systemd = matches!(
        target,
        SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem
    );
    let run = stage_ok && wants_systemd && with_sudo;
    let output = if run {
        ctx.p.env.run(&argv, Some(Duration::from_secs(30))).ok()
    } else {
        None
    };
    let rc = output.as_ref().map(|result| result.rc);
    let enabled_now = rc == Some(0);
    let pending = if !run { vec![argv] } else { Vec::new() };
    WslInfo {
        wsl: true,
        already: false,
        enabled_now,
        needs_restart: enabled_now,
        rc,
        hint: enabled_now.then(|| {
            "已寫入 /etc/wsl.conf [boot] systemd=true；請在 Windows 端執行 `wsl --shutdown`，重新進入 WSL 後再跑一次 `serialwrap setup --system --with-sudo`。".to_string()
        }),
        pending,
    }
}

pub fn render_user_unit(exec_start: &str) -> String {
    supervisor_defs::render_user_unit(exec_start)
}

pub fn render_system_unit(exec_start: &str, run_user: &str) -> String {
    supervisor_defs::render_system_unit(exec_start, run_user)
}

fn mode_value(mode: &SupervisionMode) -> PyValue {
    PyValue::Str(mode.as_str().to_string())
}

fn available_modes(ctx: &CliCtx<'_>) -> Vec<SupervisionMode> {
    ctx.p.service.supported_modes()
}

fn invalid_mode(target: &SupervisionMode, modes: &[SupervisionMode]) -> PyDict {
    let joined = modes
        .iter()
        .map(SupervisionMode::as_str)
        .collect::<Vec<_>>()
        .join("、");
    output::error_resp(
        ErrorCode::InvalidArgs,
        Some(&format!(
            "監管模式 {} 不適用於本平台（可用：{}）",
            target.as_str(),
            joined
        )),
    )
}

fn mode_for_config(config: Option<&RuntimeConfig>) -> SupervisionMode {
    match config.and_then(RuntimeConfig::mode) {
        Some(value) if py_truthy_of(value) => SupervisionMode::parse(&py_str_of(value)),
        _ => SupervisionMode::OnDemand,
    }
}

fn flag(ns: &Namespace, key: &str) -> bool {
    matches!(ns.get(key), ArgValue::Bool(true))
}

/// `setup` 的獨立選用元件步驟；不與 serialwrap 本體安裝交易混在一起。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OptionalStep {
    InstallCom0com { pair: (String, String) },
    RemoveCom0com,
}

const COM0COM_EXPERIMENTAL_LINE: &str =
    "serialwrap: com0com 選用元件為實驗性（Spike 1 未全數通過）";

fn optional_components(com0com: PyDict) -> PyValue {
    let mut components = PyDict::new();
    components.insert("com0com", PyValue::Dict(com0com));
    PyValue::Dict(components)
}

fn com0com_result(
    installed: Option<bool>,
    removed: Option<bool>,
    left_installed: Option<bool>,
    reason: Option<&str>,
    helper_rc: Option<i32>,
    experimental: bool,
) -> PyDict {
    let mut result = PyDict::new();
    if let Some(value) = installed {
        result.insert("installed", PyValue::Bool(value));
    }
    if let Some(value) = removed {
        result.insert("removed", PyValue::Bool(value));
    }
    if let Some(value) = left_installed {
        result.insert("left_installed", PyValue::Bool(value));
    }
    result.insert(
        "reason",
        reason
            .map(|value| PyValue::Str(value.to_string()))
            .unwrap_or(PyValue::Null),
    );
    result.insert(
        "helper_rc",
        helper_rc
            .map(|value| PyValue::Int(PyInt::Small(i128::from(value))))
            .unwrap_or(PyValue::Null),
    );
    result.insert("experimental", PyValue::Bool(experimental));
    result
}

fn optional_failure(
    step: &OptionalStep,
    reason: &str,
    helper_rc: Option<i32>,
    experimental: bool,
) -> PyDict {
    let installing = matches!(step, OptionalStep::InstallCom0com { .. });
    com0com_result(
        installing.then_some(false),
        (!installing).then_some(false),
        None,
        Some(reason),
        helper_rc,
        experimental,
    )
}

fn component_from_bundle(
    ctx: &CliCtx<'_>,
    bundle: &Path,
) -> Result<Option<sw_proto::release_manifest::Com0comComponent>, ()> {
    let path = bundle.join("release-manifest.json");
    if !ctx.p.fs.exists(&path) {
        return Ok(None);
    }
    let bytes = ctx.p.fs.read(&path).map_err(|_| ())?;
    let text = String::from_utf8(bytes).map_err(|_| ())?;
    let manifest = sw_proto::release_manifest::parse_manifest(&text).map_err(|_| ())?;
    Ok(manifest.com0com())
}

fn manifest_from_bundle(
    ctx: &CliCtx<'_>,
    bundle: &Path,
) -> Result<Option<sw_proto::release_manifest::ReleaseManifest>, ()> {
    let path = bundle.join("release-manifest.json");
    if !ctx.p.fs.exists(&path) {
        return Ok(None);
    }
    let bytes = ctx.p.fs.read(&path).map_err(|_| ())?;
    let text = String::from_utf8(bytes).map_err(|_| ())?;
    sw_proto::release_manifest::parse_manifest(&text)
        .map(Some)
        .map_err(|_| ())
}

fn default_com0com_pair(ctx: &CliCtx<'_>, bundle: &Path) -> Option<(String, String)> {
    component_from_bundle(ctx, bundle)
        .ok()
        .flatten()
        .map(|component| component.default_pair)
}

fn com0com_component_absent(installing: bool) -> PyDict {
    com0com_result(
        installing.then_some(false),
        (!installing).then_some(false),
        None,
        Some("component_absent"),
        None,
        false,
    )
}

fn helper_argv(step: &OptionalStep, pair: &(String, String)) -> Vec<String> {
    let action = match step {
        OptionalStep::InstallCom0com { .. } => "install",
        OptionalStep::RemoveCom0com => "remove",
    };
    vec![
        action.into(),
        "--pair".into(),
        format!("CNCA0={},CNCB0={}", pair.0, pair.1),
    ]
}

/// 執行 bundle manifest 宣告的 com0com 步驟；任何選用元件失敗均不改變基本安裝結果。
pub fn run_optional_step(ctx: &mut CliCtx<'_>, bundle: &Path, step: OptionalStep) -> PyDict {
    let manifest = match manifest_from_bundle(ctx, bundle) {
        Ok(Some(manifest)) => manifest,
        Ok(None) => return optional_failure(&step, "component_absent", None, false),
        Err(()) => return optional_failure(&step, "helper_failed", None, false),
    };
    let Some(component) = manifest.com0com() else {
        return optional_failure(&step, "component_absent", None, false);
    };
    if component.experimental {
        let _ = ctx.io.err_str(&format!("{COM0COM_EXPERIMENTAL_LINE}\n"));
    }
    let pair = match &step {
        OptionalStep::InstallCom0com { pair } => pair,
        OptionalStep::RemoveCom0com => &component.default_pair,
    };
    if pair != &component.default_pair {
        return optional_failure(&step, "helper_failed", None, component.experimental);
    }
    if matches!(&step, OptionalStep::InstallCom0com { .. }) {
        match sw_pal::com0com::list_pairs_from_registry(ctx.p.registry.as_ref()) {
            Ok(pairs)
                if pairs.iter().any(|installed| {
                    installed.a_name.eq_ignore_ascii_case(&pair.0)
                        && installed.b_name.eq_ignore_ascii_case(&pair.1)
                }) =>
            {
                return com0com_result(Some(true), None, None, None, None, component.experimental);
            }
            Ok(_) | Err(_) => {}
        }
    }

    // 只在即將執行提權 helper 前驗證本次使用的已安裝版本目錄。
    if install_layout::verify_bundle(ctx, bundle, &manifest).is_err() {
        return optional_failure(&step, "helper_failed", None, component.experimental);
    }

    let helper = bundle.join(&component.helper.path);
    let args = helper_argv(&step, pair);
    match ctx.p.process.spawn_elevated(&helper, &args) {
        Ok(sw_pal::ElevatedOutcome::Declined) => com0com_result(
            matches!(&step, OptionalStep::InstallCom0com { .. }).then_some(false),
            matches!(&step, OptionalStep::RemoveCom0com).then_some(false),
            None,
            Some("elevation_declined"),
            None,
            component.experimental,
        ),
        Ok(sw_pal::ElevatedOutcome::Exited(0)) => match step {
            OptionalStep::InstallCom0com { .. } => com0com_result(
                Some(true),
                None,
                None,
                None,
                Some(0),
                component.experimental,
            ),
            OptionalStep::RemoveCom0com => com0com_result(
                None,
                Some(true),
                None,
                None,
                Some(0),
                component.experimental,
            ),
        },
        Ok(sw_pal::ElevatedOutcome::Exited(rc)) => {
            optional_failure(&step, "helper_failed", Some(rc), component.experimental)
        }
        Err(_) => optional_failure(&step, "helper_failed", None, component.experimental),
    }
}

/// 回傳目前已安裝版本目錄；提權操作不採用 `SERIALWRAP_RELEASE_MANIFEST` 覆寫。
fn current_installed_bundle(ctx: &CliCtx<'_>) -> Option<PathBuf> {
    let root = install_layout::resolve_install_root(ctx);
    let current = install_layout::read_current(&root).ok()??;
    let flavor = if ctx.p.caps.path_flavor == sw_pal::PalPathFlavor::Windows {
        sw_proto::pypath::PathFlavor::Windows
    } else {
        sw_proto::pypath::PathFlavor::Posix
    };
    if sw_proto::pypath::isabs(flavor, &current.dir) {
        Some(PathBuf::from(current.dir))
    } else {
        Some(root.versions.join(current.bundle_version))
    }
}

fn add_optional_install_result(
    ctx: &mut CliCtx<'_>,
    report: &mut crate::rollback::UpdateReport,
) {
    let result = match current_installed_bundle(ctx) {
        Some(bundle) => match default_com0com_pair(ctx, &bundle) {
            Some(pair) => run_optional_step(ctx, &bundle, OptionalStep::InstallCom0com { pair }),
            None => com0com_component_absent(true),
        },
        None => com0com_component_absent(true),
    };
    report
        .detail
        .insert("optional_components", optional_components(result));
}

/// com0com 為 Windows 專屬選用元件（SC-36）；非 Windows 平台不查登錄、不輸出 `optional_components`。
fn com0com_applicable(ctx: &CliCtx<'_>) -> bool {
    ctx.p.caps.path_flavor == PalPathFlavor::Windows
}

fn installed_com0com_status(ctx: &CliCtx<'_>) -> Option<PyDict> {
    if !com0com_applicable(ctx) {
        return None;
    }
    let pairs = sw_pal::com0com::list_pairs_from_registry(ctx.p.registry.as_ref()).ok()?;
    let exists = pairs.iter().any(|pair| {
        pair.a_name.eq_ignore_ascii_case(DEFAULT_COM0COM_PAIR.0)
            && pair.b_name.eq_ignore_ascii_case(DEFAULT_COM0COM_PAIR.1)
    });
    exists.then(|| com0com_result(Some(true), None, None, None, None, true))
}

fn add_installed_com0com_status(
    ctx: &CliCtx<'_>,
    report: &mut crate::rollback::UpdateReport,
) {
    let Some(result) = installed_com0com_status(ctx) else {
        return;
    };
    report
        .detail
        .insert("optional_components", optional_components(result));
}

fn add_left_installed_report(
    ctx: &CliCtx<'_>,
    report: &mut crate::rollback::UpdateReport,
) {
    if !com0com_applicable(ctx) {
        return;
    }
    let pair_exists = sw_pal::com0com::list_pairs_from_registry(ctx.p.registry.as_ref())
        .is_ok_and(|pairs| {
            pairs.iter().any(|pair| {
                pair.a_name.eq_ignore_ascii_case(DEFAULT_COM0COM_PAIR.0)
                    && pair.b_name.eq_ignore_ascii_case(DEFAULT_COM0COM_PAIR.1)
            })
        });
    if pair_exists {
        let mut detail = PyDict::new();
        detail.insert("left_installed", PyValue::Bool(true));
        let mut components = PyDict::new();
        components.insert("com0com", PyValue::Dict(detail));
        report
            .detail
            .insert("optional_components", PyValue::Dict(components));
    }
}

pub fn resolve_target_mode(ns: &Namespace, ctx: &CliCtx<'_>) -> Result<SupervisionMode, PyDict> {
    let modes = available_modes(ctx);
    let target = if flag(ns, "user") {
        SupervisionMode::SystemdUser
    } else if flag(ns, "system") {
        SupervisionMode::SystemdSystem
    } else if flag(ns, "on_demand") {
        SupervisionMode::OnDemand
    } else if flag(ns, "windows_task") {
        SupervisionMode::WindowsTask
    } else if flag(ns, "windows_service") {
        SupervisionMode::WindowsService
    } else if modes.contains(&SupervisionMode::WindowsTask) {
        SupervisionMode::WindowsTask
    } else if ctx.p.env.has_systemd() && modes.contains(&SupervisionMode::SystemdUser) {
        SupervisionMode::SystemdUser
    } else {
        SupervisionMode::OnDemand
    };
    if modes.contains(&target) {
        Ok(target)
    } else {
        Err(invalid_mode(&target, &modes))
    }
}

fn pydict_argvs(argvs: &[Vec<String>]) -> PyValue {
    PyValue::List(
        argvs
            .iter()
            .map(|argv| PyValue::List(argv.iter().cloned().map(PyValue::Str).collect()))
            .collect(),
    )
}

fn py_strings(items: &[String]) -> PyValue {
    PyValue::List(items.iter().cloned().map(PyValue::Str).collect())
}

fn add_result_argvs(result: &mut PyDict, key: &str, records: &[CmdRecord]) {
    let argvs: Vec<Vec<String>> = records.iter().map(|record| record.argv.clone()).collect();
    result.insert(key, pydict_argvs(&argvs));
}

pub(crate) fn home_dir(ctx: &CliCtx<'_>) -> PathBuf {
    let env_home = if ctx.p.caps.path_flavor == PalPathFlavor::Windows {
        ctx.env
            .get("USERPROFILE")
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .or_else(|| {
                let drive = ctx.env.get("HOMEDRIVE")?;
                let path = ctx.env.get("HOMEPATH")?;
                Some(format!("{drive}{path}"))
            })
    } else {
        ctx.env
            .get("HOME")
            .filter(|value| !value.is_empty())
            .map(str::to_string)
    };
    env_home
        .or_else(|| ctx.p.env.current_user_home())
        .or_else(|| ctx.env.get("HOME").map(str::to_string))
        .or_else(|| ctx.env.get("USERPROFILE").map(str::to_string))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("~"))
}

fn path_join(ctx: &CliCtx<'_>, base: &Path, child: &str) -> PathBuf {
    if ctx.p.caps.path_flavor == PalPathFlavor::Windows {
        PathBuf::from(format!("{}\\{}", base.display(), child.replace('/', "\\")))
    } else {
        // `posixpath.join`：POSIX 產物（systemd unit 等）一律以 `/` 分隔，不受主機分隔符影響。
        PathBuf::from(sw_proto::pypath::join(
            sw_proto::pypath::PathFlavor::Posix,
            &base.to_string_lossy(),
            &[child],
        ))
    }
}

fn system_run_user(ctx: &CliCtx<'_>) -> String {
    ctx.p
        .env
        .var("SUDO_USER")
        .filter(|value| !value.is_empty())
        .or_else(|| ctx.p.env.var("USER").filter(|value| !value.is_empty()))
        .or_else(|| ctx.p.env.var("LOGNAME").filter(|value| !value.is_empty()))
        .unwrap_or_else(|| ctx.p.env.user_identity())
}

pub(crate) fn stage_system_unit(ctx: &CliCtx<'_>, home: &Path) -> Result<PathBuf, SetupError> {
    let staging = home.join(".local/share/serialwrap/serialwrap.service");
    let root = install_layout::resolve_install_root(ctx);
    let executable = path_join(
        ctx,
        &root.bin,
        if ctx.p.caps.path_flavor == PalPathFlavor::Windows {
            "serialwrapd.exe"
        } else {
            "serialwrapd"
        },
    );
    let exec_start = format!(
        "{} --socket /run/serialwrap/serialwrapd.sock --profile-dir /etc/serialwrap/profiles",
        executable.display()
    );
    let run_user = system_run_user(ctx);
    write_asset(
        ctx,
        &staging,
        render_system_unit(&exec_start, &run_user).as_bytes(),
    )?;
    Ok(staging)
}

pub(crate) fn stage_system_profiles(ctx: &CliCtx<'_>, home: &Path) -> Result<PathBuf, SetupError> {
    let staging = home.join(".local/share/serialwrap/system-profiles");
    ctx.p.fs.mkdirs(&staging).map_err(io_error)?;
    for asset in crate::assets::PROFILE_ASSETS {
        let Some(name) = asset_tail(asset.rel, "profiles/") else {
            continue;
        };
        write_asset(ctx, &staging.join(name), asset.bytes)?;
    }
    Ok(staging)
}

fn system_install_plan(
    ctx: &CliCtx<'_>,
    home: &Path,
    include_start: bool,
) -> Result<Vec<Vec<String>>, SetupError> {
    let unit = stage_system_unit(ctx, home)?;
    let profiles = stage_system_profiles(ctx, home)?;
    let mut commands = vec![
        vec![
            "sudo".to_string(),
            "install".to_string(),
            "-m".to_string(),
            "0644".to_string(),
            unit.display().to_string(),
            "/etc/systemd/system/serialwrap.service".to_string(),
        ],
        vec![
            "sudo".to_string(),
            "mkdir".to_string(),
            "-p".to_string(),
            "/etc/serialwrap/profiles".to_string(),
        ],
        vec![
            "sudo".to_string(),
            "cp".to_string(),
            "-r".to_string(),
            format!("{}/.", profiles.display()),
            "/etc/serialwrap/profiles".to_string(),
        ],
        vec![
            "sudo".to_string(),
            "systemctl".to_string(),
            "daemon-reload".to_string(),
        ],
        vec![
            "sudo".to_string(),
            "systemctl".to_string(),
            "enable".to_string(),
            "serialwrap".to_string(),
        ],
    ];
    if include_start {
        commands.push(vec![
            "sudo".to_string(),
            "systemctl".to_string(),
            "start".to_string(),
            "serialwrap".to_string(),
        ]);
    }
    Ok(commands)
}

fn run_command(ctx: &CliCtx<'_>, argv: &[String]) -> CmdRecord {
    let rc = ctx
        .p
        .env
        .run(argv, None)
        .map(|output| output.rc)
        .unwrap_or(-1);
    CmdRecord {
        argv: argv.to_vec(),
        rc,
    }
}

fn run_records(ctx: &CliCtx<'_>, argvs: &[Vec<String>]) -> Vec<CmdRecord> {
    argvs.iter().map(|argv| run_command(ctx, argv)).collect()
}

fn service_argv(mode: &SupervisionMode, action: ServiceAction, with_sudo: bool) -> Vec<String> {
    match mode {
        SupervisionMode::SystemdUser => vec![
            "systemctl".into(),
            "--user".into(),
            action.as_str().into(),
            "serialwrap".into(),
        ],
        SupervisionMode::SystemdSystem if with_sudo => vec![
            "sudo".into(),
            "systemctl".into(),
            action.as_str().into(),
            "serialwrap".into(),
        ],
        SupervisionMode::WindowsTask if action == ServiceAction::Start => vec![
            "schtasks".into(),
            "/Run".into(),
            "/TN".into(),
            "serialwrap".into(),
        ],
        SupervisionMode::WindowsService if action == ServiceAction::Start => {
            vec!["sc.exe".into(), "start".into(), "serialwrap".into()]
        }
        _ => Vec::new(),
    }
}

fn extend_records(ran: &mut Vec<Vec<String>>, records: &[CmdRecord]) {
    ran.extend(records.iter().map(|record| record.argv.clone()));
}

fn check_windows_task_install(records: &[CmdRecord]) -> Result<(), ReconcileError> {
    if let Some(failed) = records.iter().find(|record| record.rc != 0) {
        return Err(ReconcileError::Config(format!(
            "windows-task 工作註冊失敗，rc={}",
            failed.rc
        )));
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OldStop {
    Reconcile,
    HandedOff {
        token: String,
        ran: Vec<Vec<String>>,
    },
}

pub struct ReconcileRequest {
    pub old_mode: SupervisionMode,
    pub target_mode: SupervisionMode,
    pub home: PathBuf,
    pub daemon_running: bool,
    pub daemon_id: DaemonIdentity,
    pub old_endpoint: String,
    pub any_flashing: bool,
    pub with_sudo: bool,
    pub force: bool,
    pub hold_target: bool,
    pub socket_path: Option<String>,
    pub config_path: PathBuf,
    pub old_stop: OldStop,
}

#[derive(Debug)]
pub enum ReconcileError {
    FlashingBusy(String),
    WaitOldExitTimeout { partial: PyDict },
    Config(String),
}

fn result_base(
    req: &ReconcileRequest,
    mode: &SupervisionMode,
    ran: &[Vec<String>],
    pending: &[Vec<String>],
) -> PyDict {
    let mut result = PyDict::new();
    result.insert("mode", mode_value(mode));
    result.insert("transitioned", PyValue::Bool(false));
    result.insert("applied", PyValue::Bool(true));
    result.insert("ran", pydict_argvs(ran));
    result.insert("pending_sudo", pydict_argvs(pending));
    let _ = req;
    result
}

fn pending_only(req: &ReconcileRequest, pending: &[Vec<String>], warning: Option<&str>) -> PyDict {
    let mut result = PyDict::new();
    result.insert("mode", mode_value(&req.old_mode));
    result.insert("requested_mode", mode_value(&req.target_mode));
    result.insert("transitioned", PyValue::Bool(false));
    result.insert("applied", PyValue::Bool(false));
    result.insert("ran", pydict_argvs(&[]));
    result.insert("pending_sudo", pydict_argvs(pending));
    if let Some(warning) = warning {
        result.insert("warnings", py_strings(&[warning.to_string()]));
    }
    result
}

fn windows_service_definition(ctx: &CliCtx<'_>, socket: &str, held: bool) -> SupervisorDefinition {
    let root = install_layout::resolve_install_root(ctx);
    let executable = path_join(ctx, &root.bin, "serialwrapd.exe");
    SupervisorDefinition {
        mode: SupervisionMode::WindowsService,
        name: "serialwrap".to_string(),
        exec: vec![
            executable.display().to_string(),
            "--socket".to_string(),
            socket.to_string(),
        ],
        working_dir: Some(root.bin),
        run_user: None,
        env: Vec::new(),
        definition_path: None,
        register_held: held,
    }
}

fn windows_task_definition(ctx: &CliCtx<'_>, socket: &str) -> SupervisorDefinition {
    let root = install_layout::resolve_install_root(ctx);
    let executable = path_join(ctx, &root.bin, "serialwrapd.exe");
    let definition_path = path_join(ctx, &root.supervisor, "serialwrap.task.xml");
    SupervisorDefinition {
        mode: SupervisionMode::WindowsTask,
        name: "serialwrap".to_string(),
        exec: vec![
            executable.display().to_string(),
            "--socket".to_string(),
            socket.to_string(),
        ],
        working_dir: Some(root.bin),
        run_user: None,
        env: Vec::new(),
        definition_path: Some(definition_path),
        register_held: false,
    }
}

fn pending_privileged(
    ctx: &CliCtx<'_>,
    req: &ReconcileRequest,
) -> Result<Option<PyDict>, ReconcileError> {
    let mut pending = Vec::new();
    let changes_mode = req.old_mode != req.target_mode;
    let old_system_transition = changes_mode && req.old_mode == SupervisionMode::SystemdSystem;
    let target_system = req.target_mode == SupervisionMode::SystemdSystem;
    let windows_service_transition = changes_mode
        && (req.old_mode == SupervisionMode::WindowsService
            || req.target_mode == SupervisionMode::WindowsService);

    if old_system_transition && !req.with_sudo {
        pending.push(vec![
            "sudo".into(),
            "systemctl".into(),
            "stop".into(),
            "serialwrap".into(),
        ]);
        pending.push(vec![
            "sudo".into(),
            "systemctl".into(),
            "disable".into(),
            "serialwrap".into(),
        ]);
    }
    if target_system && !req.with_sudo {
        pending.extend(
            system_install_plan(ctx, &req.home, req.old_mode != req.target_mode).map_err(
                |error| ReconcileError::Config(format!("system unit staging 失敗：{error:?}")),
            )?,
        );
    }

    if windows_service_transition {
        if req.target_mode == SupervisionMode::WindowsService {
            let def =
                windows_service_definition(ctx, req.socket_path.as_deref().unwrap_or(""), false);
            pending.extend(ctx.p.service.install_plan(&def));
        }
        if req.old_mode == SupervisionMode::WindowsService {
            pending.push(vec!["sc.exe".into(), "stop".into(), "serialwrap".into()]);
            pending.push(vec![
                "sc.exe".into(),
                "config".into(),
                "serialwrap".into(),
                "start=".into(),
                "disabled".into(),
            ]);
        }
        let warning = "windows-service 依 OD-12 須完成多使用者認證／授權後才可部署；本版只列出 pending_sudo 指令，不自動安裝";
        return Ok(Some(pending_only(req, &pending, Some(warning))));
    }

    if !pending.is_empty() {
        return Ok(Some(pending_only(req, &pending, None)));
    }
    Ok(None)
}

pub fn reconcile_stops_old(_ctx: &CliCtx<'_>, req: &ReconcileRequest) -> bool {
    if req.old_mode == req.target_mode
        || req.old_mode == SupervisionMode::WindowsService
        || req.target_mode == SupervisionMode::WindowsService
        || (!req.with_sudo
            && (req.old_mode == SupervisionMode::SystemdSystem
                || req.target_mode == SupervisionMode::SystemdSystem))
    {
        return false;
    }
    match req.old_mode {
        SupervisionMode::OnDemand => req.daemon_running,
        SupervisionMode::SystemdUser
        | SupervisionMode::SystemdSystem
        | SupervisionMode::WindowsTask => true,
        SupervisionMode::WindowsService | SupervisionMode::Other(_) => false,
    }
}

fn stop_old(
    ctx: &mut CliCtx<'_>,
    req: &ReconcileRequest,
    ran: &mut Vec<Vec<String>>,
) -> Result<(), ReconcileError> {
    match &req.old_stop {
        OldStop::HandedOff {
            token,
            ran: handed_ran,
        } => {
            let _ = token;
            ran.extend(handed_ran.iter().cloned());
            if matches!(
                req.old_mode,
                SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem
            ) {
                let commands = ctx
                    .p
                    .service
                    .set_enabled(&req.old_mode, false, req.with_sudo);
                extend_records(ran, &commands);
            }
        }
        OldStop::Reconcile => match req.old_mode {
            SupervisionMode::OnDemand if req.daemon_running => {
                let _ = crate::client::call(
                    ctx,
                    &req.old_endpoint,
                    "daemon.stop",
                    &PyValue::Dict(PyDict::new()),
                    2.0,
                    &PyInt::Small(0),
                );
                ran.push(vec!["serialwrap".into(), "daemon".into(), "stop".into()]);
            }
            SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem => {
                let stopped =
                    ctx.p
                        .service
                        .action(&req.old_mode, ServiceAction::Stop, req.with_sudo);
                if let Some(rc) = stopped.rc {
                    ran.push(service_argv(
                        &req.old_mode,
                        ServiceAction::Stop,
                        req.with_sudo,
                    ));
                    let _ = rc;
                }
                let commands = ctx
                    .p
                    .service
                    .set_enabled(&req.old_mode, false, req.with_sudo);
                extend_records(ran, &commands);
            }
            SupervisionMode::WindowsTask => {
                let report = graceful_stop::graceful_stop(
                    ctx,
                    &req.old_mode,
                    &req.old_endpoint,
                    req.with_sudo,
                    true,
                    "reconcile_wait_old_exit",
                );
                match report {
                    Ok(report) => {
                        ran.extend(report.ran);
                    }
                    Err(StopFailure::Timeout { ran: stop_ran, .. }) => {
                        ran.extend(stop_ran);
                        return Err(ReconcileError::WaitOldExitTimeout {
                            partial: partial_result(req, ran, &[]),
                        });
                    }
                    Err(error) => {
                        return Err(ReconcileError::Config(format!(
                            "停止舊 windows-task 失敗：{error:?}"
                        )));
                    }
                }
            }
            SupervisionMode::WindowsService | SupervisionMode::Other(_) => {}
            SupervisionMode::OnDemand => {}
        },
    }
    Ok(())
}

fn start_new(
    ctx: &mut CliCtx<'_>,
    req: &ReconcileRequest,
    same_mode: bool,
    ran: &mut Vec<Vec<String>>,
) -> Result<(), ReconcileError> {
    match req.target_mode {
        SupervisionMode::OnDemand => {}
        SupervisionMode::SystemdUser => {
            let unit = req.home.join(".config/systemd/user/serialwrap.service");
            write_asset(
                ctx,
                &unit,
                render_user_unit("%h/.local/bin/serialwrapd").as_bytes(),
            )
            .map_err(|error| ReconcileError::Config(format!("寫入 user unit 失敗：{error:?}")))?;
            let def = SupervisorDefinition {
                mode: SupervisionMode::SystemdUser,
                name: "serialwrap".to_string(),
                exec: vec!["%h/.local/bin/serialwrapd".to_string()],
                working_dir: None,
                run_user: None,
                env: Vec::new(),
                definition_path: Some(unit),
                register_held: same_mode || req.hold_target,
            };
            let install = ctx.p.service.install(&def, false);
            extend_records(ran, &install);
            if same_mode {
                let enabled = ctx.p.service.set_enabled(&req.target_mode, true, false);
                extend_records(ran, &enabled);
            } else if let Some(user) = ctx
                .p
                .env
                .var("USER")
                .filter(|value| !value.is_empty())
                .or_else(|| ctx.p.env.var("LOGNAME").filter(|value| !value.is_empty()))
            {
                let argv = vec!["loginctl".into(), "enable-linger".into(), user];
                let record = run_command(ctx, &argv);
                ran.push(record.argv.clone());
                let _ = record.rc;
            }
        }
        SupervisionMode::SystemdSystem => {
            let commands = system_install_plan(ctx, &req.home, !same_mode && !req.hold_target)
                .map_err(|error| {
                    ReconcileError::Config(format!("system unit staging 失敗：{error:?}"))
                })?;
            let file_commands = &commands[..3];
            let file_records = run_records(ctx, file_commands);
            extend_records(ran, &file_records);
            let root = install_layout::resolve_install_root(ctx);
            let unit = req.home.join(".local/share/serialwrap/serialwrap.service");
            let def = SupervisorDefinition {
                mode: SupervisionMode::SystemdSystem,
                name: "serialwrap".into(),
                exec: vec![format!("{} --socket /run/serialwrap/serialwrapd.sock --profile-dir /etc/serialwrap/profiles", path_join(ctx, &root.bin, "serialwrapd").display())],
                working_dir: None,
                run_user: Some(ctx.p.env.user_identity()),
                env: vec![("SERIALWRAP_STATE_DIR".into(), "/var/lib/serialwrap".into())],
                definition_path: Some(unit),
                register_held: same_mode || req.hold_target,
            };
            let install = ctx.p.service.install(&def, true);
            extend_records(ran, &install);
            if same_mode {
                let enabled = ctx.p.service.set_enabled(&req.target_mode, true, true);
                extend_records(ran, &enabled);
            }
        }
        SupervisionMode::WindowsTask => {
            let def = windows_task_definition(ctx, req.socket_path.as_deref().unwrap_or(""));
            let install = ctx.p.service.install(&def, false);
            extend_records(ran, &install);
            check_windows_task_install(&install)?;
            if !same_mode && !req.hold_target {
                let result = ctx
                    .p
                    .service
                    .action(&req.target_mode, ServiceAction::Start, false);
                if let Some(rc) = result.rc {
                    ran.push(service_argv(&req.target_mode, ServiceAction::Start, false));
                    if rc != 0 {
                        return Err(ReconcileError::Config(format!(
                            "啟動 windows-task 失敗（rc={rc}）"
                        )));
                    }
                }
            }
        }
        SupervisionMode::WindowsService | SupervisionMode::Other(_) => {}
    }
    Ok(())
}

fn state_path(ctx: &CliCtx<'_>, home: &Path, mode: &SupervisionMode) -> PathBuf {
    if mode == &SupervisionMode::SystemdSystem {
        let run_user = system_run_user(ctx);
        ctx.p
            .env
            .user_home(&run_user)
            .map(PathBuf::from)
            .unwrap_or_else(|| home.to_path_buf())
            .join(".local/state/serialwrap/state.json")
    } else {
        PathBuf::from(&ctx.paths.state_dir).join("state.json")
    }
}

fn migrate_state(ctx: &CliCtx<'_>, req: &ReconcileRequest) {
    if req.old_mode == req.target_mode
        || matches!(
            req.old_mode,
            SupervisionMode::WindowsTask | SupervisionMode::WindowsService
        )
        || matches!(
            req.target_mode,
            SupervisionMode::WindowsTask | SupervisionMode::WindowsService
        )
    {
        return;
    }
    let source = state_path(ctx, &req.home, &req.old_mode);
    let dest = state_path(ctx, &req.home, &req.target_mode);
    if source != dest {
        let _ = crate::state_migrate::migrate_legacy_state(ctx.p.fs.as_ref(), &source, &dest);
    }
}

fn partial_result(req: &ReconcileRequest, ran: &[Vec<String>], pending: &[Vec<String>]) -> PyDict {
    let mut partial = PyDict::new();
    partial.insert("ok", PyValue::Bool(false));
    partial.insert(
        "error_code",
        PyValue::Str(ErrorCode::Timeout.as_str().into()),
    );
    partial.insert("phase", PyValue::Str("reconcile_wait_old_exit".into()));
    partial.insert("mode", mode_value(&req.old_mode));
    partial.insert("requested_mode", mode_value(&req.target_mode));
    partial.insert("transitioned", PyValue::Bool(false));
    partial.insert("applied", PyValue::Bool(false));
    add_result_argvs(
        &mut partial,
        "ran",
        &ran.iter()
            .map(|argv| CmdRecord {
                argv: argv.clone(),
                rc: 0,
            })
            .collect::<Vec<_>>(),
    );
    partial.insert("pending_sudo", pydict_argvs(pending));
    partial
}

pub fn reconcile(ctx: &mut CliCtx<'_>, req: &ReconcileRequest) -> Result<PyDict, ReconcileError> {
    if req.any_flashing && !req.force {
        return Err(ReconcileError::FlashingBusy(
            "flash 進行中，拒絕 setup（可用 --force 覆寫）".to_string(),
        ));
    }
    if let Some(result) = pending_privileged(ctx, req)? {
        return Ok(result);
    }

    let same_mode = req.old_mode == req.target_mode;
    let mut ran = Vec::new();
    if matches!(&req.old_stop, OldStop::HandedOff { .. })
        || (!same_mode && reconcile_stops_old(ctx, req))
    {
        stop_old(ctx, req, &mut ran)?;
    }

    let should_wait = !same_mode
        && matches!(
            req.old_mode,
            SupervisionMode::OnDemand
                | SupervisionMode::SystemdUser
                | SupervisionMode::SystemdSystem
                | SupervisionMode::WindowsTask
        );
    if should_wait {
        let mut identity = req.daemon_id.clone();
        if !identity.known {
            identity = crate::commands::daemon_cmd::capture_reconcile_daemon_identity(
                ctx,
                &req.old_endpoint,
            )
            .map_err(|_| ReconcileError::WaitOldExitTimeout {
                partial: partial_result(req, &ran, &[]),
            })?;
        }
        if !crate::commands::daemon_cmd::wait_daemon_gone(
            ctx,
            &req.old_endpoint,
            &identity,
            Duration::from_secs(10),
        ) {
            return Err(ReconcileError::WaitOldExitTimeout {
                partial: partial_result(req, &ran, &[]),
            });
        }
    }

    if !same_mode {
        migrate_state(ctx, req);
    }
    start_new(ctx, req, same_mode, &mut ran)?;

    let mut config = endpoint_resolve::load_runtime_config(ctx)
        .map_err(|error| ReconcileError::Config(error.to_string()))?;
    config.set_mode(req.target_mode.as_str(), req.socket_path.as_deref());
    endpoint_resolve::save_runtime_config(ctx, &config)
        .map_err(|error| ReconcileError::Config(error.to_string()))?;

    let mut result = result_base(req, &req.target_mode, &ran, &[]);
    result.insert("transitioned", PyValue::Bool(!same_mode));
    Ok(result)
}

fn rpc_response(ctx: &CliCtx<'_>, endpoint: &str, method: &str, timeout: f64) -> Option<PyDict> {
    crate::client::call(
        ctx,
        endpoint,
        method,
        &PyValue::Dict(PyDict::new()),
        timeout,
        &PyInt::Small(0),
    )
    .ok()
    .map(|outcome| outcome.resp)
}

fn emit_error(ctx: &mut CliCtx<'_>, response: &PyDict, context: &str, exit: i32) -> i32 {
    let _ = output::print_json(&mut ctx.io, response);
    let _ = output::mirror_err(&mut ctx.io, response, Some(context));
    exit
}

/// 安裝類請求（rollback／bundle 首次安裝或更新）一律在入口取得安裝鎖；走到這裡代表入口判定與分支不一致，fail closed。
fn missing_install_lock(ctx: &mut CliCtx<'_>) -> i32 {
    let mut response = output::error_resp(
        ErrorCode::InternalError,
        Some("安裝類 setup 未持有安裝鎖（入口判定與分支不一致）"),
    );
    response.insert("phase", PyValue::Str("install_lock".into()));
    emit_error(ctx, &response, "setup", 2)
}

pub fn run_setup(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    if flag(ns, "rollback") && (flag(ns, "with_com0com") || flag(ns, "remove_com0com")) {
        let response = output::error_resp(
            ErrorCode::InvalidArgs,
            Some("--rollback 不得與 --with-com0com 或 --remove-com0com 同時使用"),
        );
        return emit_error(ctx, &response, "setup", 2);
    }
    if flag(ns, "remove_com0com") {
        let result = if let Some(bundle) = current_installed_bundle(ctx) {
            run_optional_step(ctx, &bundle, OptionalStep::RemoveCom0com)
        } else {
            optional_failure(
                &OptionalStep::RemoveCom0com,
                "component_absent",
                None,
                false,
            )
        };
        let mut response = PyDict::new();
        let failed = matches!(
            result.get("reason"),
            Some(PyValue::Str(reason)) if reason == "elevation_declined" || reason == "helper_failed"
        );
        response.insert("ok", PyValue::Bool(!failed));
        if failed {
            response.insert(
                "error_code",
                PyValue::Str(ErrorCode::InternalError.as_str().to_string()),
            );
        }
        response.insert("optional_components", optional_components(result));
        let _ = output::print_json(&mut ctx.io, &response);
        return if failed { 2 } else { 0 };
    }

    let install_root = install_layout::resolve_install_root(ctx);
    // 安裝鎖只由 update／rollback／首次安裝（§12.1）整筆持有；一般 setup（Task 10 reconcile，與 Python 雙邊對齊）
    // 不得在安裝根新增 `.install.lock`。安裝根已有鎖檔或殘留 journal 時（曾安裝或更新中斷），一般 setup 仍先取鎖，
    // 與進行中的 updater 互斥並在鎖內處理殘留 journal。
    let install_request = flag(ns, "rollback")
        || (matches!(
            install_layout::classify_exe(ctx, &install_root),
            install_layout::ExeLocation::External(_)
        ) && !matches!(
            install_layout::locate_manifest(ctx),
            Err(install_layout::LayoutError::ManifestNotFound(_))
        ));
    let needs_install_lock = install_request
        || ctx.p.fs.exists(&install_root.root.join(".install.lock"))
        || ctx.p.fs.exists(&install_root.root.join("install-journal.json"));
    let mut install_lock = if needs_install_lock {
        match install_layout::install_lock(ctx, &install_root) {
            Ok(lock) => Some(lock),
            Err(response) => return emit_error(ctx, &response, "setup", 2),
        }
    } else {
        None
    };
    if let Err(code) = crate::commands::supervision_cmd::load_config_or_report(
        ctx,
        crate::commands::supervision_cmd::ConfigErrorReport::JsonInternalError("setup".to_string()),
    ) {
        return code;
    }
    let recovered_journal = crate::rollback::recover_journal(ctx, &install_root);
    if let Some(recovery) = recovered_journal.as_ref().filter(|recovery| !recovery.ok) {
        let mut response = PyDict::new();
        response.insert("ok", PyValue::Bool(false));
        if let Some(code) = recovery.error_code {
            response.insert("error_code", PyValue::Str(code.into()));
        }
        response.insert("phase", PyValue::Str("install_recover".into()));
        if let Some(reason) = &recovery.reason {
            response.insert("reason", PyValue::Str(reason.clone()));
        }
        if let Some(message) = &recovery.message {
            response.insert("message", PyValue::Str(message.clone()));
        }
        response.insert(
            "recovered_journal",
            crate::rollback::recovery_value(recovery),
        );
        return emit_error(ctx, &response, "setup", 2);
    }
    let recovered_autostart_hold = match crate::rollback::recover_autostart_hold(ctx) {
        Ok(value) => value,
        Err(mut response) => {
            response.insert("phase", PyValue::Str("supervision_set_recover".into()));
            return emit_error(ctx, &response, "setup", 2);
        }
    };
    if flag(ns, "rollback") {
        let Some(lock) = install_lock.take() else {
            return missing_install_lock(ctx);
        };
        let mut report = crate::rollback::run_rollback_locked(
            ctx,
            flag(ns, "with_sudo"),
            flag(ns, "force"),
            lock,
        );
        add_left_installed_report(ctx, &mut report);
        return crate::rollback::emit_update_report(
            ctx,
            &report,
            recovered_journal.as_ref(),
            recovered_autostart_hold.as_ref(),
        );
    }
    if matches!(
        install_layout::classify_exe(ctx, &install_root),
        install_layout::ExeLocation::External(_)
    ) {
        match install_layout::locate_manifest(ctx) {
            Ok((manifest, _)) => {
                let Some(bundle_dir) = manifest.parent() else {
                    let response = output::error_resp(
                        ErrorCode::InvalidArgs,
                        Some("release manifest 沒有父目錄"),
                    );
                    return emit_error(ctx, &response, "setup", 2);
                };
                let current = match install_layout::read_current(&install_root) {
                    Ok(current) => current,
                    Err(error) => {
                        let mut response =
                            output::error_resp(ErrorCode::InvalidArgs, Some(&error.to_string()));
                        response.insert("phase", PyValue::Str("update_verify".into()));
                        response.insert("reason", PyValue::Str("current_read_failed".into()));
                        return emit_error(ctx, &response, "setup", 2);
                    }
                };
                let Some(lock) = install_lock.take() else {
                    return missing_install_lock(ctx);
                };
                let mut report = if current.is_some() {
                    let target = match resolve_target_mode(ns, ctx) {
                        Ok(target) => target,
                        Err(response) => return emit_error(ctx, &response, "setup", 2),
                    };
                    crate::rollback::run_update_locked(
                        ctx,
                        bundle_dir,
                        &target,
                        flag(ns, "with_sudo"),
                        flag(ns, "force"),
                        lock,
                    )
                } else {
                    crate::rollback::run_install_locked(ctx, bundle_dir, ns, lock)
                };
                if report.ok && flag(ns, "with_com0com") {
                    add_optional_install_result(ctx, &mut report);
                } else if report.ok {
                    add_installed_com0com_status(ctx, &mut report);
                }
                return crate::rollback::emit_update_report(
                    ctx,
                    &report,
                    recovered_journal.as_ref(),
                    recovered_autostart_hold.as_ref(),
                );
            }
            Err(install_layout::LayoutError::ManifestNotFound(_)) => {}
            Err(error) => {
                let response = output::error_resp(ErrorCode::InvalidArgs, Some(&error.to_string()));
                return emit_error(ctx, &response, "setup", 2);
            }
        }
    }
    let target = match resolve_target_mode(ns, ctx) {
        Ok(mode) => mode,
        Err(response) => return emit_error(ctx, &response, "setup", 2),
    };
    let with_sudo = flag(ns, "with_sudo");
    let force = flag(ns, "force");
    let home = home_dir(ctx);
    let dirs = user_dirs(&ctx.env, &home);
    let legacy = detect_legacy_install(ctx, &home).map_or(PyValue::Null, PyValue::Dict);
    let old_config = match crate::commands::supervision_cmd::load_config_or_report(
        ctx,
        crate::commands::supervision_cmd::ConfigErrorReport::JsonInternalError("setup".to_string()),
    ) {
        Ok(config) => config,
        Err(code) => return code,
    };
    let old_mode = mode_for_config(Some(&old_config));

    let endpoint_result = endpoint_resolve::resolve_endpoint_info(
        ctx,
        &EndpointArgs {
            endpoint: ns.str("endpoint"),
            socket: ns.str("socket"),
            bench: None,
        },
    );
    let old_endpoint = endpoint_result
        .as_ref()
        .map(|resolved| resolved.endpoint.clone())
        .unwrap_or_else(|_| endpoint_resolve::local_default_endpoint(ctx));
    let daemon_running = rpc_response(ctx, &old_endpoint, "health.ping", 0.5)
        .is_some_and(|response| response.get_truthy("ok"));
    let any_flashing = rpc_response(ctx, &old_endpoint, "mcu.status", 0.5)
        .is_some_and(|response| response.get_truthy("flashing"));
    if any_flashing && !force {
        let response = output::error_resp(
            ErrorCode::FlashingBusy,
            Some("flash 進行中，拒絕 setup（可用 --force 覆寫）"),
        );
        return emit_error(ctx, &response, "setup", 2);
    }

    let daemon_id = if daemon_running {
        crate::commands::daemon_cmd::capture_setup_daemon_identity(ctx, &old_endpoint)
    } else {
        DaemonIdentity::default()
    };

    if let Err(error) = materialize_assets(ctx, &dirs, force) {
        let message = format!("資產物化失敗：{error:?}");
        return output::print_internal_error(&mut ctx.io, "setup", &message);
    }

    let wsl = ensure_wsl_systemd(ctx, &home, &target, with_sudo);
    if wsl.needs_restart {
        let mut response = PyDict::new();
        response.insert("ok", PyValue::Bool(true));
        response.insert("legacy", legacy);
        response.insert("wsl_systemd_enabled", PyValue::Bool(true));
        response.insert("needs_restart", PyValue::Bool(true));
        response.insert("requested_mode", mode_value(&target));
        if let Some(recovered) = recovered_autostart_hold.clone() {
            response.insert("recovered_autostart_hold", PyValue::Dict(recovered));
        }
        if let Some(recovered) = recovered_journal.as_ref() {
            response.insert(
                "recovered_journal",
                crate::rollback::recovery_value(recovered),
            );
        }
        if let Some(hint) = wsl.hint {
            response.insert("hint", PyValue::Str(hint));
        }
        let _ = output::print_json(&mut ctx.io, &response);
        return 0;
    }

    let mut preserved_remote_socket = None;
    if old_mode == SupervisionMode::OnDemand
        && target == SupervisionMode::OnDemand
        && !endpoint_resolve::rpc_backend_is_win(ctx)
        && ns.str("socket").is_none()
        && ns.str("endpoint").is_none()
    {
        if let Some(YValue::Str(socket)) = old_config.socket_path() {
            if matches!(parse_endpoint(socket), Ok(Endpoint::Tcp { .. })) {
                preserved_remote_socket = Some(socket.clone());
            }
        }
    }
    let socket_path = if target == SupervisionMode::SystemdSystem {
        "/run/serialwrap/serialwrapd.sock".to_string()
    } else if matches!(
        target,
        SupervisionMode::WindowsTask | SupervisionMode::WindowsService
    ) || endpoint_resolve::rpc_backend_is_win(ctx)
    {
        endpoint_resolve::local_default_endpoint(ctx)
    } else if let Some(socket) = preserved_remote_socket {
        socket
    } else {
        ctx.paths.socket_path.clone()
    };

    let req = ReconcileRequest {
        old_mode,
        target_mode: target,
        home,
        daemon_running,
        daemon_id,
        old_endpoint,
        any_flashing,
        with_sudo,
        force,
        hold_target: false,
        socket_path: Some(socket_path),
        config_path: endpoint_resolve::config_write_path(ctx),
        old_stop: OldStop::Reconcile,
    };
    let mut setup = match reconcile(ctx, &req) {
        Ok(result) => result,
        Err(ReconcileError::FlashingBusy(message)) => {
            let response = output::error_resp(ErrorCode::FlashingBusy, Some(&message));
            return emit_error(ctx, &response, "setup", 2);
        }
        Err(ReconcileError::WaitOldExitTimeout { mut partial }) => {
            let mut pending = pydict_argvs(&wsl.pending)
                .as_array()
                .cloned()
                .unwrap_or_default();
            if let Some(PyValue::List(current)) = partial.get("pending_sudo") {
                pending.extend(current.clone());
            }
            if !pending.is_empty() {
                partial.insert("pending_sudo", PyValue::List(pending));
            }
            return emit_error(ctx, &partial, "setup", 1);
        }
        Err(ReconcileError::Config(message)) => {
            return output::print_internal_error(&mut ctx.io, "setup", &message);
        }
    };

    if wsl.rc.is_some() {
        let mut ran = match setup.get("ran") {
            Some(PyValue::List(items)) => items.clone(),
            _ => Vec::new(),
        };
        let stage = req.home.join(".local/share/serialwrap/wsl.conf");
        ran.insert(
            0,
            PyValue::List(
                ["sudo", "install", "-m", "0644"]
                    .into_iter()
                    .map(|value| PyValue::Str(value.into()))
                    .chain([
                        PyValue::Str(stage.display().to_string()),
                        PyValue::Str("/etc/wsl.conf".into()),
                    ])
                    .collect(),
            ),
        );
        setup.insert("ran", PyValue::List(ran));
    }
    let mut pending = pydict_argvs(&wsl.pending)
        .as_array()
        .cloned()
        .unwrap_or_default();
    if let Some(PyValue::List(current)) = setup.get("pending_sudo") {
        pending.extend(current.clone());
    }
    if !pending.is_empty() {
        setup.insert("pending_sudo", PyValue::List(pending.clone()));
    }

    let mut response = PyDict::new();
    response.insert("ok", PyValue::Bool(true));
    response.insert("legacy", legacy);
    response.insert("setup", PyValue::Dict(setup));
    if let Some(recovered) = recovered_autostart_hold {
        response.insert("recovered_autostart_hold", PyValue::Dict(recovered));
    }
    if let Some(recovered) = recovered_journal.as_ref() {
        response.insert(
            "recovered_journal",
            crate::rollback::recovery_value(recovered),
        );
    }
    response.insert(
        "doctor_hint",
        PyValue::Str("serialwrap doctor 可驗證環境".into()),
    );
    response.insert(
        "console_hint",
        PyValue::Str("human console 請用 serialwrap-minicom COMx（勿直接 minicom -D /dev/ttyUSBx，避免與 daemon two-reader 衝突）".into()),
    );
    let optional_bundle = current_installed_bundle(ctx);
    // 選用 helper 只讀不可變的已安裝版本；不要讓安裝鎖跨越 UAC 等待。
    drop(install_lock);
    if flag(ns, "with_com0com") {
        let result = if let Some(bundle) = optional_bundle.as_deref() {
            match default_com0com_pair(ctx, &bundle) {
                Some(pair) => {
                    run_optional_step(ctx, bundle, OptionalStep::InstallCom0com { pair })
                }
                None => com0com_component_absent(true),
            }
        } else {
            com0com_component_absent(true)
        };
        response.insert("optional_components", optional_components(result));
    } else if let Some(result) = installed_com0com_status(ctx) {
        response.insert("optional_components", optional_components(result));
    }
    if !pending.is_empty() {
        response.insert("pending_sudo", PyValue::List(pending));
    }
    let _ = output::print_json(&mut ctx.io, &response);
    0
}
