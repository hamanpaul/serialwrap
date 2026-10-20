//! `supervision-mode` 群組（查詢與 `set MODE`；P4-T6 查詢、P4-T10 驗證、P4-T12 承接切換）。

use crate::argparse::{ArgValue, Namespace};
use crate::ctx::CliCtx;
use crate::endpoint_resolve::{config_read_path, load_runtime_config};
use crate::output;
use sw_proto::config_types::{ConfigError, RuntimeConfig};
use sw_proto::yaml11::{py_str_of, py_truthy_of};

/// `config.yaml` 讀取失敗（DF-07）時的回報形狀。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConfigErrorReport {
    /// `supervision-mode`：stderr 恰一行 `serialwrap: config.yaml 解析失敗: <path>: <摘要>`、exit 1、stdout 空。
    PlainStderr,
    /// `service`、`setup`、`daemon`：stdout `{"error_code":"INTERNAL_ERROR","message":…,"ok":false}`、
    /// stderr 鏡像 `serialwrap: <context> failed: INTERNAL_ERROR: …`、exit 1。
    /// 內含 `<context>`（例如 `service start`）；plan 的 unit variant 無法攜帶鏡像用的 context，故改為帶值。
    JsonInternalError(String),
}

/// DF-07 摘要（非契約文字，G1）：`YamlError::summary()`（`problem (line <l>, column <c>)`，見 plan Task 6）；
/// 換行一律改為空白以維持單行。
fn config_error_summary(err: &ConfigError) -> String {
    let raw = match err {
        ConfigError::Yaml(e) => e.summary(),
        other => other.to_string(),
    };
    raw.replace(['\r', '\n'], " ")
}

/// 載入 `config.yaml`；讀取或解析失敗時依 `kind` 回報並回傳 `Err(1)`（DF-07）。
pub fn load_config_or_report(
    ctx: &mut CliCtx,
    kind: ConfigErrorReport,
) -> Result<RuntimeConfig, i32> {
    match load_runtime_config(ctx) {
        Ok(cfg) => Ok(cfg),
        Err(err) => {
            let message = format!(
                "config.yaml 解析失敗: {}: {}",
                config_read_path(ctx),
                config_error_summary(&err)
            );
            match kind {
                ConfigErrorReport::PlainStderr => {
                    let _ = ctx.io.err_str(&format!("serialwrap: {message}\n"));
                }
                ConfigErrorReport::JsonInternalError(context) => {
                    output::print_internal_error(&mut ctx.io, &context, &message);
                }
            }
            Err(1)
        }
    }
}

/// `supervision-mode`：無 words → 查詢（stdout `"{mode or 'on-demand'}\n"`，純文字、不驗證值；exit 0）。
/// words 非空 → `set MODE` 路徑交給 P4-T10 的 [`run_supervision_set`]（words 形狀與 MODE 驗證由 Task 10 承接）。
pub fn run_supervision_mode(ns: &Namespace, ctx: &mut CliCtx) -> i32 {
    let words: Vec<String> = match ns.get("words") {
        ArgValue::List(items) => items.clone(),
        ArgValue::Str(item) => vec![item.clone()],
        _ => Vec::new(),
    };
    if !words.is_empty() {
        let mode = words.get(1).map(String::as_str).unwrap_or("");
        return run_supervision_set(ns, ctx, mode);
    }
    let cfg = match load_config_or_report(ctx, ConfigErrorReport::PlainStderr) {
        Ok(cfg) => cfg,
        Err(code) => return code,
    };
    // Python：`print(_default_runtime_config().mode() or "on-demand")`（falsy → on-demand；其餘以 str() 輸出）。
    let mode = match cfg.mode() {
        Some(value) if py_truthy_of(value) => py_str_of(value),
        _ => "on-demand".to_string(),
    };
    let _ = ctx.io.out_str(&format!("{mode}\n"));
    0
}

pub fn run_supervision_set(ns: &Namespace, ctx: &mut CliCtx, mode: &str) -> i32 {
    let words: Vec<String> = match ns.get("words") {
        ArgValue::List(items) => items.clone(),
        ArgValue::Str(item) => vec![item.clone()],
        _ => Vec::new(),
    };
    if words.len() != 2 || words.first().map(String::as_str) != Some("set") {
        let response = output::error_resp(
            sw_proto::error_codes::ErrorCode::InvalidArgs,
            Some("用法：serialwrap supervision-mode [set MODE]"),
        );
        let _ = output::print_json(&mut ctx.io, &response);
        let _ = output::mirror_err(&mut ctx.io, &response, Some("supervision-mode"));
        return 2;
    }
    let target = sw_pal::service_ctl::SupervisionMode::parse(mode);
    let supported = ctx.p.service.supported_modes();
    let mode_is_known = matches!(
        target,
        sw_pal::service_ctl::SupervisionMode::OnDemand
            | sw_pal::service_ctl::SupervisionMode::SystemdUser
            | sw_pal::service_ctl::SupervisionMode::SystemdSystem
            | sw_pal::service_ctl::SupervisionMode::WindowsTask
            | sw_pal::service_ctl::SupervisionMode::WindowsService
    );
    if !mode_is_known
        || (!matches!(target, sw_pal::service_ctl::SupervisionMode::OnDemand)
            && !(matches!(target, sw_pal::service_ctl::SupervisionMode::WindowsService)
                && ctx.p.caps.path_flavor == sw_pal::PalPathFlavor::Windows)
            && !supported.contains(&target))
    {
        let joined = supported
            .iter()
            .map(sw_pal::service_ctl::SupervisionMode::as_str)
            .collect::<Vec<_>>()
            .join("、");
        let response = output::error_resp(
            sw_proto::error_codes::ErrorCode::InvalidArgs,
            Some(&format!(
                "監管模式 {} 不適用於本平台（可用：{}）",
                target.as_str(), joined
            )),
        );
        let _ = output::print_json(&mut ctx.io, &response);
        let _ = output::mirror_err(&mut ctx.io, &response, Some("supervision-mode"));
        return 2;
    }
    crate::rollback::run_supervision_switch(
        ctx,
        &target,
        matches!(ns.get("with_sudo"), ArgValue::Bool(true)),
        matches!(ns.get("force"), ArgValue::Bool(true)),
    )
}
