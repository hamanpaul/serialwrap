//! P7 Task 3（08-slice23-login-events.md）：全平台 Login FSM（bcm、post_login_cmd、console unlock、RX 洪水反分類、
//! 無效 regex 的 `Raised`、TX 拒絕／部分寫出的 `Aborted`、SC-30 取消）。
//!
//! 斷言值以 Python 基線實測為準：golden `login_fsm_scenarios`（`scripts/rust/capture_p7_goldens.py`）與以同一
//! `p7_fake_bridge.ScriptedBridge` DSL 實跑 `sw_core/login_fsm.py` 的結果（各測試註明）。
//!
//! 偏離計畫原文：`FenceReject::StaleFsmGen` 依 Task 0 核對註記改用 06 既有的 `FsmMismatch`；`LoginRig` 放
//! `tests/common/login_rig.rs`；`sp.user_env` 等欄位位於 `sp.template.*`。
#![cfg(feature = "test-fakes")]

mod common;
#[path = "common/login_rig.rs"]
mod login_rig;

use common::p7::{b64, default_templates, fake_env, golden_rows, profile_from_json, session_profile_for, Row};
use login_rig::LoginRig;
use sw_core::login_fsm::*;
use sw_core::session_auth::{AuthResolution, SessionAuth};
use sw_core::tx_writer::FenceReject;
use sw_pal::fakes::FakeEnv;
use sw_proto::config_types::SessionProfile;
use sw_proto::pyjson::PyValue;

fn auth_from_json(v: &PyValue) -> SessionAuth {
    let s = |k: &str| v.get(k).and_then(PyValue::as_str).map(str::to_string);
    SessionAuth {
        username: s("username"),
        password: s("password"),
        console_unlock: s("console_unlock"),
    }
}

fn sent_triples(row: &Row) -> Vec<(String, Vec<u8>, bool)> {
    row["sent"]
        .as_array()
        .expect("sent")
        .iter()
        .map(|t| {
            let t = t.as_array().expect("triple");
            (
                t[0].as_str().expect("source").to_string(),
                b64(t[1].as_str().expect("payload")),
                t[2].as_bool().expect("log"),
            )
        })
        .collect()
}

fn expected_outcome(row: &Row, entry: &str) -> LoginOutcome {
    if let Some(exc) = row.opt_str("raised") {
        return LoginOutcome::Raised(exc.to_string());
    }
    let result = row["result"].as_array().expect("result");
    if result[0].as_bool() == Some(true) {
        return LoginOutcome::Ready;
    }
    let err = result[1].as_str().expect("err").to_string();
    match entry {
        "probe_ready" => LoginOutcome::NotReady(err),
        _ => LoginOutcome::Failed(err),
    }
}

fn prpl() -> SessionProfile {
    session_profile_for("prpl-template")
}

fn brcm() -> SessionProfile {
    session_profile_for("brcm-template")
}

fn smart4r() -> SessionProfile {
    session_profile_for("smart4R-profile")
}

fn empty_env() -> FakeEnv {
    FakeEnv::default()
}

fn admin_pw() -> SessionAuth {
    SessionAuth {
        username: Some("admin".into()),
        password: Some("pw".into()),
        console_unlock: None,
    }
}

fn root_pw() -> SessionAuth {
    SessionAuth {
        username: Some("root".into()),
        password: Some("pw".into()),
        console_unlock: None,
    }
}

fn unlock_k() -> SessionAuth {
    SessionAuth {
        console_unlock: Some("k".into()),
        ..SessionAuth::default()
    }
}

fn outcome(code: &str, probe: bool) -> LoginOutcome {
    if probe {
        LoginOutcome::NotReady(code.into())
    } else {
        LoginOutcome::Failed(code.into())
    }
}

const S4R: &str = "\r\nroot@Speedport Smart 4 Typ A:~# ";
const PRPL: &str = "\r\nroot@prplOS:~# ";

#[test] // 混合：shell／prpl 列為回歸鎖定，bcm／post_login_cmd／console unlock／flood／raised／detect 列為 RED
fn every_python_scenario_produces_identical_tx_and_result() {
    let templates = default_templates();
    for row in golden_rows("login_fsm_scenarios") {
        let case = &row["case"];
        let sp = profile_from_json(&case["profile"]);
        let rig = LoginRig::from_dsl(case);
        let env = fake_env(&case["env"]);
        let entry = case.get("entry").and_then(PyValue::as_str).expect("entry");
        match entry {
            "detect_template" => {
                let names: Vec<&str> = case["templates"]
                    .as_array()
                    .expect("templates")
                    .iter()
                    .map(|n| n.as_str().expect("name"))
                    .collect();
                let tpls: Vec<_> = names
                    .iter()
                    .map(|n| {
                        templates
                            .iter()
                            .find(|t| t.profile_name == *n)
                            .cloned()
                            .unwrap_or_else(|| panic!("無模板 {n}"))
                    })
                    .collect();
                let timeout = match case.get("probe_timeout_s") {
                    Some(PyValue::Float(f)) => *f,
                    Some(v @ PyValue::Int(_)) => v.as_i64().unwrap() as f64,
                    _ => 3.0,
                };
                let got = detect_template(&rig.io(), &tpls, timeout).expect("detect 不應中止");
                let result = row["result"].as_array().expect("result");
                assert_eq!(
                    got.map(|t| t.profile_name.clone()),
                    result[1].as_str().map(str::to_string),
                    "case {}",
                    row.id()
                );
            }
            "ensure_ready" | "probe_ready" => {
                let got = if entry == "ensure_ready" {
                    ensure_ready(&rig.io(), &sp, &auth_from_json(&case["auth"]), &env)
                } else {
                    probe_ready(&rig.io(), &sp)
                };
                assert_eq!(got, expected_outcome(&row, entry), "case {}", row.id());
            }
            other => panic!("unknown entry {other}"),
        }
        assert_eq!(rig.sent_triples(), sent_triples(&row), "case {} TX 序列", row.id());
        let want = match &row["virtual_elapsed_s"] {
            PyValue::Float(f) => *f,
            v => v.as_i64().unwrap() as f64,
        };
        assert!(
            (rig.virtual_elapsed_s() - want).abs() < 1e-6,
            "case {} 逾時累計 got {} want {want}",
            row.id(),
            rig.virtual_elapsed_s()
        );
    }
}

#[test] // Python 實測：(True, None)，TX 5 筆，virtual 15.0（brcm timeout_s 15）
fn bcm_two_stage_login_then_post_login_sh_then_nonce() {
    let sp = brcm();
    assert_eq!(sp.template.timeout_s, 15.0);
    assert_eq!(sp.template.post_login_cmd, "sh");
    let rig = LoginRig::new()
        .initial_rx("\r\nLogin: ")
        .on_eq("", "\r\nLogin: ")
        .on_eq("admin", "Password: ")
        .on_secret("CM> ")
        .on_eq("sh", "\r\n# ")
        .on_prefix("echo __READY__", "__READY__{nonce}\r\n# ")
        .nonces(&["deadbeef"])
        .build();
    assert_eq!(ensure_ready(&rig.io(), &sp, &admin_pw(), &empty_env()), LoginOutcome::Ready);
    assert_eq!(rig.sent_sources(), ["system", "system", "system:secret", "system", "system"]);
    assert_eq!(rig.sent_payload(2), b"pw\n");
    assert!(!rig.sent_logged(2), "secret 不得進 WAL");
    assert_eq!(rig.sent_payload(3), b"sh\n");
    assert_eq!(rig.sent_payload(4), b"echo __READY__deadbeef\n");
}

#[test]
fn flood_reclassification_threshold_is_inclusive_20000() {
    let sp = prpl();
    for (bytes, want) in [(19_999u64, "PROMPT_UNAVAILABLE"), (20_000, "RX_FLOOD")] {
        let rig = LoginRig::new().initial_rx("garbage").rx_bytes_10s(bytes).build();
        assert_eq!(probe_ready(&rig.io(), &sp), LoginOutcome::NotReady(want.into()));
    }
}

#[test] // Python 實測（user_env_fallback）：LOGIN_USER_REQUIRED，TX 只有 "\n"
fn login_user_env_missing_does_not_fall_back_to_profile_username() {
    let mut sp = prpl();
    sp.template.user_env = Some("SW_U".into());
    sp.template.username = Some("root".into());
    sp.template.pass_env = Some("SW_P".into());
    let rig = LoginRig::new()
        .initial_rx("\r\nlogin: ")
        .on_eq("", "\r\nlogin: ")
        .build();
    let out = ensure_ready(&rig.io(), &sp, &SessionAuth::default(), &empty_env());
    assert_eq!(out, LoginOutcome::Failed("LOGIN_USER_REQUIRED".into()));
    assert!(rig.sent_payloads().iter().all(|p| p != b"root\n"), "不得送 sp.username");
}

#[test] // Python 實測（invalid_probe_prompt）：raised error、virtual 0.0、TX 只有 "\n"
fn invalid_prompt_regex_raises_like_python_instead_of_timing_out() {
    let mut sp = prpl();
    sp.template.prompt_regex = "(".into();
    let rig = LoginRig::new().initial_rx("root@prplOS:~# ").build();
    assert_eq!(probe_ready(&rig.io(), &sp), LoginOutcome::Raised("error".into()));
    assert!(rig.virtual_elapsed_s() < 1e-9, "Python 在 re.compile 立即拋出，不消耗 timeout");
    assert_eq!(rig.sent_payloads(), [b"\n".to_vec()]);
}

#[test]
fn refusal_and_partial_write_abort_without_further_tx() {
    let sp = brcm();
    let rig = LoginRig::new()
        .initial_rx("\r\nLogin: ")
        .on_eq("", "\r\nLogin: ")
        .reject_after(1, FenceReject::FsmMismatch)
        .build();
    assert_eq!(
        ensure_ready(&rig.io(), &sp, &admin_pw(), &empty_env()),
        LoginOutcome::Aborted(LoginAbort::Refused(FenceReject::FsmMismatch))
    );
    assert_eq!(rig.sent_triples().len(), 1);
    assert_eq!(rig.tx_attempts(), 2, "被拒後不再呼叫 tx");

    let rig2 = LoginRig::new()
        .initial_rx("\r\nLogin: ")
        .on_eq("", "\r\nLogin: ")
        .on_eq("admin", "Password: ")
        .partial_on(3)
        .build();
    assert_eq!(
        ensure_ready(&rig2.io(), &sp, &admin_pw(), &empty_env()),
        LoginOutcome::Aborted(LoginAbort::TxIncomplete)
    );
    assert_eq!(rig2.sent_triples().len(), 3, "secret 部分寫出後不重送、不再等 prompt");
    assert_eq!(rig2.tx_attempts(), 3);
}

#[test] // NotSubmitted 且無 reject → Aborted(Cancelled)
fn not_submitted_without_reject_is_cancelled() {
    let sp = prpl();
    let rig = LoginRig::new()
        .reject_after(0, FenceReject::Frozen)
        .build();
    assert_eq!(
        probe_ready(&rig.io(), &sp),
        LoginOutcome::Aborted(LoginAbort::Refused(FenceReject::Frozen))
    );
    assert_eq!(rig.tx_attempts(), 1);
}

#[test] // Python 實測（unlock_cfe_history）：(True, None)、virtual 11.05、TX ["\n", secret, nonce]
fn console_unlock_is_not_blocked_by_cfe_banner_history() {
    let sp = smart4r();
    let rig = LoginRig::new()
        .initial_rx("CFE version 1.0\r\n...\r\nLinux booting\r\n")
        .on_secret(S4R)
        .on_prefix("echo __READY__", &format!("\r\n__READY__{{nonce}}{S4R}"))
        .nonces(&["0a1b2c3d"])
        .build();
    assert_eq!(ensure_ready(&rig.io(), &sp, &unlock_k(), &empty_env()), LoginOutcome::Ready);
    assert_eq!(rig.sent_sources(), ["system", "system:secret", "system"]);
    assert_eq!(rig.sent_payload(2), b"echo __READY__0a1b2c3d\n");
    assert!((rig.virtual_elapsed_s() - 11.05).abs() < 1e-6, "{}", rig.virtual_elapsed_s());
}

#[test]
fn prompt_timeout_error_by_platform() {
    assert_eq!(prompt_timeout_error("bcm"), "BCM_PROMPT_TIMEOUT");
    assert_eq!(prompt_timeout_error("shell"), "SHELL_PROMPT_TIMEOUT");
    for p in ["prpl", "passthrough", ""] {
        assert_eq!(prompt_timeout_error(p), "PRPL_PROMPT_TIMEOUT");
    }
}

#[test] // SC-35：passthrough 一律 LoginPlan::None，quiet_blocks 亦不合成 PROMPT_UNAVAILABLE
fn choose_login_plan_passthrough_is_none() {
    let ok = AuthResolution {
        reason: "ok",
        env_file_path: None,
    };
    for ready_probe in ["", "echo __READY__${nonce}"] {
        for login_regex in ["$^", "(?mi)^login:\\s*$"] {
            for creds in [false, true] {
                for unlock in [false, true] {
                    let mut sp = prpl();
                    sp.template.platform = "passthrough".into();
                    sp.template.ready_probe = ready_probe.into();
                    sp.template.login_regex = login_regex.into();
                    if creds {
                        sp.template.user_env = Some("U".into());
                        sp.template.pass_env = Some("P".into());
                    }
                    if unlock {
                        sp.template.console_unlock_env = Some("K".into());
                    }
                    for quiet in [false, true] {
                        let auth = (root_pw(), ok.clone());
                        let (plan, err) = choose_login_plan(&sp, true, auth, quiet);
                        assert!(matches!(plan, LoginPlan::None), "{ready_probe}/{login_regex}/{creds}/{unlock}/{quiet}");
                        assert_eq!(err, None);
                    }
                }
            }
        }
    }
}

#[test]
fn is_prompt_timeout_error_suffix() {
    for e in ["BCM_PROMPT_TIMEOUT", "SHELL_PROMPT_TIMEOUT", "LOGIN_PROMPT_TIMEOUT"] {
        assert!(is_prompt_timeout_error(e), "{e}");
    }
    for e in ["PROMPT_TIMEOUT", "PROMPT_UNAVAILABLE", ""] {
        assert!(!is_prompt_timeout_error(e), "{e}");
    }
    let rig = LoginRig::new().rx_bytes_10s(20_000).build();
    let io = rig.io();
    for e in FLOOD_MASKABLE_ERRORS.iter().copied().chain(["BCM_PROMPT_TIMEOUT", "X_PROMPT_TIMEOUT"]) {
        assert_eq!(maybe_reclassify_flood(&io, Some(e.into())), Some("RX_FLOOD".into()), "{e}");
    }
    for e in ["LOGIN_REQUIRED", "CREDENTIALS_UNRESOLVED", "USER_ENV_MISSING", "PROMPT_TIMEOUT"] {
        assert_eq!(maybe_reclassify_flood(&io, Some(e.into())), Some(e.into()), "{e}");
    }
    assert_eq!(maybe_reclassify_flood(&io, None), None);
    let quiet = LoginRig::new().rx_bytes_10s(19_999).build();
    assert_eq!(
        maybe_reclassify_flood(&quiet.io(), Some("PROMPT_UNAVAILABLE".into())),
        Some("PROMPT_UNAVAILABLE".into())
    );
}

#[test]
fn detect_boot_banner_cases() {
    assert!(detect_boot_banner("U-Boot 2024.04"));
    assert!(!detect_boot_banner("u-boot"));
    assert!(detect_boot_banner("CFE version 1.0"));
    assert!(!detect_boot_banner("CFE version"));
    assert!(detect_boot_banner("Hit any key to stop autoboot:  3"));
    assert!(!detect_boot_banner(""));
}

#[test]
fn matches_login_or_password_uses_cleaned_and_raw() {
    let mut sp = prpl();
    sp.template.login_regex = r"(?mi)^.*login:\s*$".into();
    assert!(matches_login_or_password("\x1b[1m(none) login:\x1b[0m", &sp));
    sp.template.login_regex = "(".into();
    sp.template.password_regex = "(".into();
    assert!(!matches_login_or_password("login: ", &sp), "無效 regex 略過");
    assert!(!matches_login_or_password("", &prpl()));
}

#[test] // EVT-D-C1 KEEP：Python SessionProfile 預設 login_regex 為雙反斜線，對 "login: " 不匹配
fn default_login_regex_double_backslash_never_matches() {
    let row = golden_rows("auth_resolution")
        .into_iter()
        .find(|r| r.id() == "auth_resolution:001")
        .expect("golden");
    let mut sp = profile_from_json(&row["profile"]);
    assert_eq!(sp.template.login_regex, r"(?mi)^login:\\s*$");
    sp.template.password_regex.clone_from(&sp.template.login_regex);
    assert!(!matches_login_or_password("login: ", &sp));
}

#[test] // Python 實測（post_login_guard）：LOGIN_REQUIRED、virtual 0.0、未送 post_login_cmd
fn post_login_guard_returns_login_required() {
    let mut sp = prpl();
    sp.template.post_login_cmd = "sh".into();
    let rig = LoginRig::new()
        .on_eq("", "\r\nroot@prplOS:~# \r\nlogin: ")
        .build();
    assert_eq!(
        ensure_ready(&rig.io(), &sp, &root_pw(), &empty_env()),
        LoginOutcome::Failed("LOGIN_REQUIRED".into())
    );
    assert_eq!(rig.sent_payloads(), [b"\n".to_vec()]);
}

#[test] // Python 實測（post_login_timeout／post_login_relogin）：virtual 30.0
fn post_login_timeout_split() {
    for (relogin, want) in [(false, "POST_LOGIN_CMD_TIMEOUT"), (true, "LOGIN_REQUIRED")] {
        let mut b = LoginRig::new()
            .on_eq("", "\r\nLogin: ")
            .on_eq("admin", "\r\nPassword: ")
            .on_secret("\r\nCM> ");
        if relogin {
            b = b.on_eq("sh", "\r\nPassword: ");
        }
        let rig = b.build();
        assert_eq!(
            ensure_ready(&rig.io(), &brcm(), &admin_pw(), &empty_env()),
            LoginOutcome::Failed(want.into())
        );
        assert_eq!(rig.sent_payloads().last().unwrap(), b"sh\n");
        assert!((rig.virtual_elapsed_s() - 30.0).abs() < 1e-6);
    }
}

#[test]
fn nonce_timeout_split() {
    for (relogin, want) in [(false, "READY_NONCE_TIMEOUT"), (true, "LOGIN_REQUIRED")] {
        let mut b = LoginRig::new().on_eq("", PRPL);
        if relogin {
            b = b.on_prefix("echo __READY__", "\r\nlogin: ");
        }
        let rig = b.build();
        assert_eq!(probe_ready(&rig.io(), &prpl()), outcome(want, true));
    }
}

#[test] // Python 實測（anchored_echo_only）：READY_NONCE_TIMEOUT、virtual 125.05
fn console_unlock_nonce_anchored_pattern() {
    let rig = LoginRig::new()
        .idle_s(10.0)
        .on_eq("", S4R)
        .on_prefix("echo __READY__", "echo __READY__{nonce}\r\n")
        .build();
    assert_eq!(
        ensure_ready(&rig.io(), &smart4r(), &unlock_k(), &empty_env()),
        LoginOutcome::Failed("READY_NONCE_TIMEOUT".into())
    );
    assert!((rig.virtual_elapsed_s() - 125.05).abs() < 1e-6, "{}", rig.virtual_elapsed_s());
}

#[test] // X3：USER_ENV_MISSING 永不外傳（Python 實測 user_env_missing_login／nologin）
fn user_env_missing_unreachable() {
    let mut sp = prpl();
    sp.template.user_env = Some("SW_U".into());
    sp.template.pass_env = Some("SW_P".into());
    let rig = LoginRig::new().on_eq("", "\r\nlogin: ").build();
    assert_eq!(
        ensure_ready(&rig.io(), &sp, &SessionAuth::default(), &empty_env()),
        LoginOutcome::Failed("LOGIN_USER_REQUIRED".into())
    );
    let rig = LoginRig::new().build();
    assert_eq!(
        ensure_ready(&rig.io(), &sp, &SessionAuth::default(), &empty_env()),
        LoginOutcome::Failed("LOGIN_PROMPT_TIMEOUT".into())
    );
    assert!((rig.virtual_elapsed_s() - 20.0).abs() < 1e-6);
}

#[test] // Python 實測（pass_required／pass_missing）
fn password_resolution_errors() {
    let user_only = SessionAuth {
        username: Some("root".into()),
        ..SessionAuth::default()
    };
    for (pass_env, want) in [(None, "PASS_ENV_REQUIRED"), (Some("SW_P"), "PASS_ENV_MISSING")] {
        let mut sp = prpl();
        sp.template.pass_env = pass_env.map(str::to_string);
        let rig = LoginRig::new()
            .on_eq("", "\r\nlogin: ")
            .on_eq("root", "\r\nPassword: ")
            .build();
        assert_eq!(
            ensure_ready(&rig.io(), &sp, &user_only, &empty_env()),
            LoginOutcome::Failed(want.into())
        );
        assert_eq!(rig.sent_payloads(), [b"\n".to_vec(), b"root\n".to_vec()]);
    }
}

#[test] // Python 實測（unlock_idle_timeout 3.001／unlock_bootloader 5.05／unlock_prompt_timeout 131.05）
fn console_unlock_idle_timeout_and_bootloader() {
    let mut short = smart4r();
    short.template.timeout_s = 3.0;
    let rig = LoginRig::new().build();
    assert_eq!(
        ensure_ready(&rig.io(), &short, &unlock_k(), &empty_env()),
        LoginOutcome::Failed("CONSOLE_UNLOCK_IDLE_TIMEOUT".into())
    );
    assert_eq!(rig.tx_attempts(), 0, "靜默前完全不 TX");
    assert!((rig.virtual_elapsed_s() - 3.001).abs() < 1e-6, "{}", rig.virtual_elapsed_s());

    let rig = LoginRig::new().initial_rx("\r\nCFE> ").build();
    assert_eq!(
        ensure_ready(&rig.io(), &smart4r(), &unlock_k(), &empty_env()),
        LoginOutcome::Failed("CONSOLE_UNLOCK_BOOTLOADER".into())
    );
    assert_eq!(rig.tx_attempts(), 0);
    assert!((rig.virtual_elapsed_s() - 5.05).abs() < 1e-6, "{}", rig.virtual_elapsed_s());

    let rig = LoginRig::new().idle_s(10.0).build();
    assert_eq!(
        ensure_ready(&rig.io(), &smart4r(), &unlock_k(), &empty_env()),
        LoginOutcome::Failed("CONSOLE_UNLOCK_PROMPT_TIMEOUT".into())
    );
    assert_eq!(rig.sent_sources(), ["system", "system:secret"]);
    assert!(!rig.sent_logged(1));
    assert!((rig.virtual_elapsed_s() - 131.05).abs() < 1e-6, "{}", rig.virtual_elapsed_s());

    let rig = LoginRig::new().build();
    assert_eq!(
        ensure_ready(&rig.io(), &smart4r(), &SessionAuth::default(), &empty_env()),
        LoginOutcome::Failed("CREDENTIALS_UNRESOLVED".into())
    );
    assert_eq!(rig.tx_attempts(), 0);
}

#[test]
fn console_at_bootloader_invalid_regex_raises() {
    assert_eq!(
        console_at_bootloader("CFE> ", &["(".to_string()]),
        Err(LoginOutcome::Raised("error".into()))
    );
    assert_eq!(console_at_bootloader("x\r\nCFE> \r\n", &["^CFE> $".to_string()]), Ok(true));
    assert_eq!(
        console_at_bootloader("CFE> \r\nLinux booting\r\n", &["^CFE> $".to_string()]),
        Ok(false),
        "只看尾行"
    );
    assert_eq!(console_at_bootloader("", &["^$".to_string()]), Ok(true));
}

#[test] // Python 實測（detect_all_three）＋golden detect 列
fn detect_template_skips_passthrough_and_unlock() {
    let all = default_templates();
    let pick = |names: &[&str]| -> Vec<_> {
        names
            .iter()
            .map(|n| all.iter().find(|t| t.profile_name == *n).cloned().unwrap())
            .collect()
    };
    let tpls = pick(&["uboot-template", "smart4R-profile", "prpl-template"]);
    let rig = LoginRig::new()
        .on_eq("", &format!("\r\n=> {S4R}{PRPL}"))
        .build();
    let got = detect_template(&rig.io(), &tpls, 3.0).unwrap();
    assert_eq!(got.map(|t| t.profile_name.as_str()), Some("prpl-template"));
    assert_eq!(rig.sleeps_s(), [3.0], "time.sleep(probe_timeout_s) 一次");

    let tpls = pick(&["uboot-template", "smart4R-profile", "op3-template", "prpl-template"]);
    let rig = LoginRig::new().on_eq("", "\r\nlogin: ").build();
    let got = detect_template(&rig.io(), &tpls, 3.0).unwrap();
    assert_eq!(got.map(|t| t.profile_name.as_str()), Some("op3-template"));

    let tpls = pick(&["uboot-template", "prpl-template"]);
    let rig = LoginRig::new().build();
    assert!(detect_template(&rig.io(), &tpls, 3.0).unwrap().is_none());
    assert_eq!(rig.sent_payloads(), [b"\n".to_vec()]);
}

#[test]
fn bootloader_prompt_last_nonblank_line_uses_py_splitlines() {
    let p = ["^=> $".to_string()];
    assert_eq!(matches_any_bootloader_prompt("=> \n\n", &p), Some("^=> $".into()));
    assert_eq!(matches_any_bootloader_prompt("=> \nroot# ", &p), None);
    assert_eq!(matches_any_bootloader_prompt("x\r=> ", &p), Some("^=> $".into()));
    assert_eq!(
        matches_any_bootloader_prompt("=> ", &["(".to_string(), "^=> $".to_string()]),
        Some("^=> $".into()),
        "無效 regex 略過"
    );
    assert_eq!(matches_any_bootloader_prompt("", &p), None);
    assert_eq!(matches_any_bootloader_prompt("=> ", &[]), None);
}

#[test] // SC-30：等待中 cancel → Aborted(Cancelled)，之後不再 TX
fn cancel_ends_wait_and_sends_nothing_more() {
    let rig = LoginRig::new().cancel_at(2.0).build();
    assert_eq!(
        probe_ready(&rig.io(), &prpl()),
        LoginOutcome::Aborted(LoginAbort::Cancelled)
    );
    assert_eq!(rig.tx_attempts(), 1);
    assert!((rig.virtual_elapsed_s() - 2.0).abs() < 1e-6, "{}", rig.virtual_elapsed_s());

    // console unlock 的靜默等待同樣可取消（完全不 TX）。
    let rig = LoginRig::new().cancel_at(1.0).build();
    assert_eq!(
        ensure_ready(&rig.io(), &smart4r(), &unlock_k(), &empty_env()),
        LoginOutcome::Aborted(LoginAbort::Cancelled)
    );
    assert_eq!(rig.tx_attempts(), 0);
}

// ── parity-amendments-2026-10-26 追加（Task 3）──────────────────────────────

#[test] // #113（B:F-20）：Python 實測 skip_unlock_existing_prompt 5.05、blank_line_within_1s 6.0、blank_line_after_1s 11.6
fn existing_shell_prompt_skips_unlock_blank_line_1s() {
    let nonce_ok = format!("\r\n__READY__{{nonce}}{S4R}");
    let rig = LoginRig::new()
        .initial_rx(S4R)
        .on_prefix("echo __READY__", &nonce_ok)
        .build();
    assert_eq!(ensure_ready(&rig.io(), &smart4r(), &unlock_k(), &empty_env()), LoginOutcome::Ready);
    assert_eq!(rig.sent_payloads(), [b"echo __READY__deadbeef\n".to_vec()], "已有 prompt 不送空行、不送 secret");
    assert!((rig.virtual_elapsed_s() - 5.05).abs() < 1e-6, "{}", rig.virtual_elapsed_s());

    for (after, want_elapsed) in [(0.95, 6.0), (1.5, 11.6)] {
        let rig = LoginRig::new()
            .idle_s(10.0)
            .on_eq_after("", S4R, after)
            .on_prefix("echo __READY__", &nonce_ok)
            .build();
        assert_eq!(ensure_ready(&rig.io(), &smart4r(), &unlock_k(), &empty_env()), LoginOutcome::Ready);
        assert_eq!(rig.sent_sources(), ["system", "system"], "空行辨識後不送 secret（after={after}）");
        assert!(
            (rig.virtual_elapsed_s() - want_elapsed).abs() < 1e-6,
            "after={after} elapsed {}",
            rig.virtual_elapsed_s()
        );
    }
}

#[test] // #114（B:F-25）：Python 實測 invalid_login_regex 10.0／invalid_unlock_prompt 5.05／invalid_probe_prompt 0.0
fn invalid_regex_raises_immediately() {
    let mut sp = prpl();
    sp.template.login_regex = "(".into();
    let rig = LoginRig::new().build();
    assert_eq!(
        ensure_ready(&rig.io(), &sp, &root_pw(), &empty_env()),
        LoginOutcome::Raised("error".into())
    );
    assert_eq!(rig.sent_payloads(), [b"\n".to_vec()]);
    assert!((rig.virtual_elapsed_s() - 10.0).abs() < 1e-6, "只消耗 probe 的 timeout");

    let mut sp = smart4r();
    sp.template.prompt_regex = "(".into();
    let rig = LoginRig::new().idle_s(10.0).build();
    assert_eq!(
        ensure_ready(&rig.io(), &sp, &unlock_k(), &empty_env()),
        LoginOutcome::Raised("error".into())
    );
    assert_eq!(rig.tx_attempts(), 0, "不送空行與 secret");
    assert!((rig.virtual_elapsed_s() - 5.05).abs() < 1e-6, "{}", rig.virtual_elapsed_s());
}

#[test] // 審查 60b3dd5：`not_submitted_without_reject_is_cancelled` 實際帶 reject；此處補真正「NotSubmitted 且無 reject」臂
fn not_submitted_no_reject_aborts_cancelled_without_further_tx() {
    let sp = brcm();
    let rig = LoginRig::new()
        .initial_rx("\r\nLogin: ")
        .on_eq("", "\r\nLogin: ")
        .not_submitted_on(2)
        .build();
    assert_eq!(
        ensure_ready(&rig.io(), &sp, &admin_pw(), &empty_env()),
        LoginOutcome::Aborted(LoginAbort::Cancelled)
    );
    assert_eq!(
        rig.tx_attempts(),
        2,
        "NotSubmitted 後不再呼叫 tx（不自動重送）"
    );
    assert_eq!(rig.sent_triples().len(), 1);
}

#[test] // 計畫 Task 3：`Unknown` → `Aborted(TxIncomplete)`，之後不再 TX
fn unknown_outcome_aborts_tx_incomplete_without_further_tx() {
    let sp = brcm();
    let rig = LoginRig::new()
        .initial_rx("\r\nLogin: ")
        .on_eq("", "\r\nLogin: ")
        .unknown_on(2)
        .build();
    assert_eq!(
        ensure_ready(&rig.io(), &sp, &admin_pw(), &empty_env()),
        LoginOutcome::Aborted(LoginAbort::TxIncomplete)
    );
    assert_eq!(rig.tx_attempts(), 2);
}
