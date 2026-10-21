//! `service` 群組（start／stop／restart／status；P4-T6 承接）。
//!
//! 對應 `sw_core/cli.py` 的 `service` 分支與 `sw_core/service_ctl.py` `service_action`：監管命令的產生與執行
//! 全部經 PAL `ServiceCtl`（P4-T3）；本檔只負責 config 讀取（DF-07）、結果轉 JSON 與 stderr 鏡像。

use crate::argparse::{ArgValue, Namespace};
use crate::commands::supervision_cmd::{load_config_or_report, ConfigErrorReport};
use crate::ctx::CliCtx;
use crate::output;
use sw_pal::service_ctl::{ServiceAction, ServiceResult, SupervisionMode};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{repr_float, PyDict, PyInt, PyValue};
use sw_proto::yaml11::{py_str_of, py_truthy_of, YMap, YValue};

/// 執行 `service <action> [--with-sudo]`；exit `0`（ok）／`2`（監管動作失敗）／`1`（config 解析失敗，DF-07）。
pub fn run_service(ns: &Namespace, ctx: &mut CliCtx) -> i32 {
    let action_text = ns.str("action").unwrap_or("").to_string();
    let context = format!("service {action_text}");
    let with_sudo = matches!(ns.get("with_sudo"), ArgValue::Bool(true));

    let cfg =
        match load_config_or_report(ctx, ConfigErrorReport::JsonInternalError(context.clone())) {
            Ok(cfg) => cfg,
            Err(code) => return code,
        };
    let Some(action) = ServiceAction::parse(&action_text) else {
        let resp = output::error_resp(ErrorCode::InvalidArgs, None);
        let _ = output::print_json(&mut ctx.io, &resp);
        let _ = output::mirror_err(&mut ctx.io, &resp, Some(&context));
        return 2;
    };

    // Python：`mode = _default_runtime_config().mode() or "on-demand"`（falsy → on-demand）。
    let raw_mode: Option<&YValue> = cfg.mode().filter(|value| py_truthy_of(value));
    let (mode_name, raw_mode_json) = match raw_mode {
        None => ("on-demand".to_string(), None),
        Some(YValue::Str(text)) => (text.clone(), None),
        // 非字串的 truthy 值：Python 原樣帶入結果的 `mode` 欄位（json.dumps 形狀），監管判定以其 str() 進行（皆落入 NO_SYSTEMD）。
        Some(other) => match yvalue_to_pyvalue(other) {
            Ok(raw) => (py_str_of(other), Some(raw)),
            // Python：`_print` 的 `json.dumps` 拋 TypeError，traceback 結束（exit 1、stdout 空）。
            // 此路徑在 on-demand／未知模式分支，未執行任何監管命令，故無副作用可還原。
            Err(message) => {
                let _ = ctx.io.err_str(&format!("{message}\n"));
                return 1;
            }
        },
    };
    let mode = SupervisionMode::parse(&mode_name);
    if matches!(
        mode,
        SupervisionMode::WindowsTask | SupervisionMode::WindowsService
    ) && matches!(action, ServiceAction::Stop | ServiceAction::Restart)
    {
        let (mut resp, code) = service_action_response(ctx, &mode, action, with_sudo, &context);
        if let Some(raw) = raw_mode_json {
            resp.insert("mode", raw);
        }
        let _ = output::print_json(&mut ctx.io, &resp);
        let _ = output::mirror_err(&mut ctx.io, &resp, Some(&context));
        return code;
    }
    // on-demand／未知模式不呼叫任何監管後端（Python `service_action` 亦不呼叫 systemctl）；結果與 NO_SYSTEMD 相同。
    let result = if mode.is_supervised() {
        ctx.p.service.action(&mode, action, with_sudo)
    } else {
        no_systemd_result(&mode, action)
    };

    let mut resp = result_to_pydict(&result);
    if let Some(raw) = raw_mode_json {
        resp.insert("mode", raw);
    }
    let code = if result.ok { 0 } else { 2 };
    let _ = output::print_json(&mut ctx.io, &resp);
    let _ = output::mirror_err(&mut ctx.io, &resp, Some(&context));
    code
}

/// 供 daemon 管理命令重用 `service` 的監管行為與輸出。
pub fn run_service_action(
    ctx: &mut CliCtx<'_>,
    mode: &SupervisionMode,
    action: ServiceAction,
    with_sudo: bool,
    context: &str,
    routed_to: Option<&str>,
) -> i32 {
    run_service_action_with_mode_value(ctx, mode, action, with_sudo, context, routed_to, None)
}

pub fn run_service_action_with_mode_value(
    ctx: &mut CliCtx<'_>,
    mode: &SupervisionMode,
    action: ServiceAction,
    with_sudo: bool,
    context: &str,
    routed_to: Option<&str>,
    raw_mode: Option<PyValue>,
) -> i32 {
    let (mut resp, code) = service_action_response(ctx, mode, action, with_sudo, context);
    if let Some(raw_mode) = raw_mode {
        resp.insert("mode", raw_mode);
    }
    if let Some(route) = routed_to {
        resp.insert("_routed_to", PyValue::Str(route.to_string()));
    }
    let _ = output::print_json(&mut ctx.io, &resp);
    let _ = output::mirror_err(&mut ctx.io, &resp, Some(context));
    code
}

fn service_action_response(
    ctx: &mut CliCtx<'_>,
    mode: &SupervisionMode,
    action: ServiceAction,
    with_sudo: bool,
    context: &str,
) -> (PyDict, i32) {
    if matches!(
        mode,
        SupervisionMode::WindowsTask | SupervisionMode::WindowsService
    ) && matches!(action, ServiceAction::Stop | ServiceAction::Restart)
    {
        let keep_disabled = action == ServiceAction::Restart;
        let phase = if keep_disabled {
            "service_restart_wait_stop"
        } else {
            "service_stop_wait_stop"
        };
        match crate::graceful_stop::graceful_stop(
            ctx,
            mode,
            &crate::endpoint_resolve::local_default_endpoint(ctx),
            with_sudo,
            keep_disabled,
            phase,
        ) {
            Ok(report) => {
                let mut resp = PyDict::new();
                resp.insert("ok", PyValue::Bool(true));
                resp.insert("mode", PyValue::Str(mode.as_str().to_string()));
                resp.insert("action", PyValue::Str(action.as_str().to_string()));
                resp.insert("graceful", PyValue::Bool(report.graceful));
                if let Some(rc) = report.rc {
                    resp.insert("rc", PyValue::Int(PyInt::Small(i128::from(rc))));
                }
                if !report.stdout.is_empty() {
                    resp.insert("stdout", PyValue::Str(report.stdout));
                }
                if !report.stderr.is_empty() {
                    resp.insert("stderr", PyValue::Str(report.stderr));
                }
                if action == ServiceAction::Restart {
                    let enabled = ctx.p.service.set_enabled(mode, true, with_sudo);
                    if enabled.iter().any(|command| command.rc != 0) {
                        let mut failed = service_failure(
                            mode,
                            action,
                            "重新啟用監管項失敗",
                            "service_restart_enable",
                        );
                        failed.insert("graceful", PyValue::Bool(report.graceful));
                        return (failed, 2);
                    }
                    let start = ctx.p.service.action(mode, ServiceAction::Start, with_sudo);
                    let mut started = result_to_pydict(&start);
                    started.insert("action", PyValue::Str("restart".to_string()));
                    started.insert("graceful", PyValue::Bool(report.graceful));
                    return (started, if start.ok { 0 } else { 2 });
                }
                return (resp, 0);
            }
            Err(crate::graceful_stop::StopFailure::Timeout { phase, .. }) => {
                let mut resp = service_failure(mode, action, "daemon 未在期限內結束", phase);
                resp.insert("graceful", PyValue::Bool(false));
                if keep_disabled {
                    resp.insert(
                        "hint",
                        PyValue::Str("舊 daemon 未在 10 秒內結束；工作已停用以免 RestartOnFailure 拉起，確認後執行 serialwrap service start".to_string()),
                    );
                }
                return (resp, 2);
            }
            Err(
                crate::graceful_stop::StopFailure::Service(result)
                | crate::graceful_stop::StopFailure::Hold(result),
            ) => {
                let mut resp = result_to_pydict(&result);
                resp.insert("action", PyValue::Str(action.as_str().to_string()));
                resp.insert("graceful", PyValue::Bool(false));
                return (resp, 2);
            }
            Err(other) => {
                let mut resp =
                    service_failure(mode, action, &format!("daemon 停止失敗：{other:?}"), phase);
                resp.insert("graceful", PyValue::Bool(false));
                return (resp, 2);
            }
        }
    }

    let result = if mode.is_supervised() {
        ctx.p.service.action(mode, action, with_sudo)
    } else {
        no_systemd_result(mode, action)
    };
    let code = if result.ok { 0 } else { 2 };
    (result_to_pydict(&result), code)
}

fn service_failure(
    mode: &SupervisionMode,
    action: ServiceAction,
    message: &str,
    phase: &'static str,
) -> PyDict {
    let mut resp = PyDict::new();
    resp.insert("ok", PyValue::Bool(false));
    resp.insert("mode", PyValue::Str(mode.as_str().to_string()));
    resp.insert("action", PyValue::Str(action.as_str().to_string()));
    resp.insert(
        "error_code",
        PyValue::Str(
            if phase.starts_with("service_") && phase.contains("wait_stop") {
                ErrorCode::Timeout.as_str()
            } else {
                ErrorCode::InternalError.as_str()
            }
            .to_string(),
        ),
    );
    resp.insert("message", PyValue::Str(message.to_string()));
    resp.insert("phase", PyValue::Str(phase.to_string()));
    resp
}

/// `ServiceResult` → 回應物件（Python `service_action` 的 dict 形狀）。
///
/// 必有 `ok`、`mode`、`action`；命令實際執行過時含 `rc`、`stdout`、`stderr`；失敗時含 `error_code`、`hint`，
/// `phase` 只在 PAL 提供時出現。鍵順序不影響輸出（`CLI_STDOUT` 為 `sort_keys`）。
pub fn result_to_pydict(result: &ServiceResult) -> PyDict {
    let mut dict = PyDict::new();
    dict.insert("ok", PyValue::Bool(result.ok));
    dict.insert("mode", PyValue::Str(result.mode.clone()));
    dict.insert("action", PyValue::Str(result.action.clone()));
    if let Some(rc) = result.rc {
        dict.insert("rc", PyValue::Int(PyInt::Small(i128::from(rc))));
    }
    if let Some(stdout) = &result.stdout {
        dict.insert("stdout", PyValue::Str(stdout.clone()));
    }
    if let Some(stderr) = &result.stderr {
        dict.insert("stderr", PyValue::Str(stderr.clone()));
    }
    if let Some(code) = result.error_code {
        dict.insert("error_code", PyValue::Str(code.to_string()));
    }
    if let Some(hint) = &result.hint {
        dict.insert("hint", PyValue::Str(hint.clone()));
    }
    if let Some(phase) = result.phase {
        dict.insert("phase", PyValue::Str(phase.to_string()));
    }
    dict
}

/// on-demand／未知模式的 `NO_SYSTEMD` 結果（Python `service_action` 的 fallback 分支）。
///
/// 文字與 PAL `service_ctl::no_systemd` 相同；CLI 就地組裝，使 `sw-pal` 不為本 Task 放寬可見性（P4-T6 範圍只含 sw-cli／sw-proto）。
/// 測試 `mgmt_service::service_on_demand_prints_no_systemd_and_mirrors_stderr` 以逐位元組比對此文字。
fn no_systemd_result(mode: &SupervisionMode, action: ServiceAction) -> ServiceResult {
    ServiceResult {
        ok: false,
        mode: mode.as_str().to_string(),
        action: action.as_str().to_string(),
        rc: None,
        stdout: None,
        stderr: None,
        error_code: Some(ErrorCode::NoSystemd.as_str()),
        hint: Some(
            "目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。"
                .into(),
        ),
        phase: None,
    }
}

/// YAML 值 → `json.dumps`（`sort_keys=True`）可產生的 Python 值。
///
/// 對應 Python 行為：
/// - mapping key：str 原樣；bool → `true`／`false`；None → `null`；int／float 以 Python `repr` 形式（NaN／inf 同 json）。
/// - 不可序列化的值（date／datetime、bytes、set）與不可作 key 的型別 → `Err(TypeError 訊息)`，exit 1、stdout 空。
/// - `sort_keys=True` 下 str 與數值 key、或 None 與其他 key 無法比較（Python `sorted` 的 `TypeError`）→ `Err`。
///
/// 已知殘差（非契約，`supervision_mode` 非字串本即異常輸入）：數值型 key 的輸出順序由 `pyjson` 以字串排序，
/// 與 Python 的數值排序可能不同（例如 `{10: a, 9: b}`）。
pub(crate) fn yvalue_to_pyvalue(value: &YValue) -> Result<PyValue, String> {
    Ok(match value {
        YValue::Null => PyValue::Null,
        YValue::Bool(flag) => PyValue::Bool(*flag),
        YValue::Int(int) => PyValue::Int(int.clone()),
        YValue::Float(float) => PyValue::Float(*float),
        YValue::Str(text) => PyValue::Str(text.clone()),
        YValue::Seq(items) => PyValue::List(
            items
                .iter()
                .map(yvalue_to_pyvalue)
                .collect::<Result<Vec<_>, _>>()?,
        ),
        YValue::Map(map) => PyValue::Dict(map_to_pydict(map)?),
        other => return Err(not_serializable(other)),
    })
}

/// YAML mapping → `PyDict`（key 轉換與 `sort_keys` 可比較性檢查，見 [`yvalue_to_pyvalue`]）。
fn map_to_pydict(map: &YMap) -> Result<PyDict, String> {
    let mut keys: Vec<String> = Vec::with_capacity(map.0.len());
    let (mut has_str, mut has_num, mut has_none) = (false, false, false);
    for (key, _) in &map.0 {
        keys.push(match key {
            YValue::Str(text) => {
                has_str = true;
                text.clone()
            }
            YValue::Bool(flag) => {
                has_num = true;
                String::from(if *flag { "true" } else { "false" })
            }
            YValue::Int(int) => {
                has_num = true;
                match int {
                    PyInt::Small(n) => n.to_string(),
                    PyInt::Big(digits) => digits.clone(),
                }
            }
            YValue::Float(float) => {
                has_num = true;
                json_float_key(*float)
            }
            YValue::Null => {
                has_none = true;
                "null".to_string()
            }
            other => {
                return Err(format!(
                    "TypeError: keys must be str, int, float, bool or None, not {}",
                    py_type_name(other)
                ))
            }
        });
    }
    // Python `sorted(dict.items())`：str 與數值互不可比；None 只能單獨存在（dict key 唯一）。
    if (has_str && has_num) || (has_none && keys.len() > 1) {
        return Err("TypeError: '<' not supported between instances of 'str' and 'int'".into());
    }
    let mut pairs = Vec::with_capacity(keys.len());
    for (json_key, (_, value)) in keys.into_iter().zip(&map.0) {
        pairs.push((json_key, yvalue_to_pyvalue(value)?));
    }
    Ok(PyDict::from_pairs(pairs))
}

/// `json.dumps` 的 float key：`float.__repr__`，非有限值為 `NaN`／`Infinity`／`-Infinity`。
fn json_float_key(value: f64) -> String {
    if value.is_nan() {
        "NaN".to_string()
    } else if value == f64::INFINITY {
        "Infinity".to_string()
    } else if value == f64::NEG_INFINITY {
        "-Infinity".to_string()
    } else {
        repr_float(value)
    }
}

/// Python 型別名（TypeError 訊息用）。
fn py_type_name(value: &YValue) -> &'static str {
    match value {
        YValue::Null => "NoneType",
        YValue::Bool(_) => "bool",
        YValue::Int(_) => "int",
        YValue::Float(_) => "float",
        YValue::Str(_) => "str",
        YValue::Timestamp { py_str } => {
            if py_str.contains(' ') {
                "datetime"
            } else {
                "date"
            }
        }
        YValue::Binary(_) => "bytes",
        YValue::Seq(_) => "list",
        YValue::Map(_) => "dict",
        YValue::Set(_) => "set",
    }
}

/// 不可 JSON 序列化的值的 `TypeError` 訊息（Python `json.dumps` 的最後一行 traceback 文字）。
fn not_serializable(value: &YValue) -> String {
    format!(
        "TypeError: Object of type {} is not JSON serializable",
        py_type_name(value)
    )
}
