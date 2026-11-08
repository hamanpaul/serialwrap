//! P8 Task 13：以 daemon 自己的 SC-19 判定（`daemon_main::resolve_test_allowlist`，內含 `isolation_reason`）證明
//! `wintask_runtime.rs` 的 staging 批次檔使隔離成立、`COM250` allowlist 生效（memory_only，可在 WSL／CI 執行）。
//!
//! 擁有者裁決（2026-11-08「T13 的 Windows 排程工作實跑」）：allowlist 只在隔離條件成立時生效，批次檔必須把
//! STATE／RUN／CONFIG 等目錄全部指向 staging，寫出能讓 `isolation_reason` 回 None 的完整環境。隔離不成立時 daemon 以
//! `TEST_DEVICE_ALLOWLIST_REJECTED` exit 2、不開任何裝置（fail-closed）；本檔另以對照組證明此判定在測試中確實會拒絕，
//! 不是恆真。
//!
//! 批次檔與 config 的產生器與實跑共用同一份 support（`crates/sw-pal/tests/support/wintask.rs`，以 `#[path]` 引入；
//! sw-pal 不能依賴 sw-daemon）。env 由批次檔文字經 `simulate_batch_env` 推得（清掉繼承的 `SERIALWRAP_*` 後逐行 `set`），
//! 繼承的使用者 env 刻意放入指向預設目錄的 `SERIALWRAP_*`，證明批次檔的設定會蓋過繼承值（清除步驟本身由
//! `crates/sw-pal/tests/wintask_staging.rs` 驗證）。本檔不宣稱任何 Windows 原生證據。

#[path = "../../sw-pal/tests/support/wintask.rs"]
#[allow(dead_code)]
mod wintask;

use sw_daemon::daemon_main::resolve_test_allowlist;
use sw_daemon::service::TestAllowlist;
use sw_pal::fakes::env::FakeEnv;
use sw_proto::config_types::RuntimeConfig;
use sw_proto::pypath::{HomeLookup, PathFlavor};
use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths};
use wintask::{simulate_batch_env, StagingSpec, ALLOWLIST_COM};

const HOME: &str = r"C:\Users\tester";
const ROOT: &str = r"C:\Users\tester\AppData\Local\Temp\sw-p8-test-4242";
const PORT: u16 = 50123;

struct Home;

impl HomeLookup for Home {
    fn current_user_home(&self) -> Option<String> {
        Some(HOME.into())
    }
    fn user_home(&self, _user: &str) -> Option<String> {
        Some(HOME.into())
    }
}

fn pairs(items: &[(&str, &str)]) -> Vec<(String, String)> {
    items.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
}

/// 使用者登入環境（排程工作繼承）中與 serialwrap 無關的部分。
fn user_base() -> Vec<(String, String)> {
    pairs(&[
        ("USERPROFILE", HOME),
        ("USERNAME", "tester"),
        ("LOCALAPPDATA", r"C:\Users\tester\AppData\Local"),
        ("TMP", r"C:\Users\tester\AppData\Local\Temp"),
    ])
}

/// daemon 的「預設隔離路徑」（移除隔離變數後解析；Windows 路徑風格、TCP 預設 endpoint）。
fn defaults() -> RuntimePaths {
    RuntimePaths::resolve(
        &EnvSnapshot::from_pairs_fold_upper(user_base()),
        NativeTraits {
            default_endpoint_is_tcp: true,
            path_flavor: PathFlavor::Windows,
        },
        &Home,
    )
    .expect("預設路徑可解析")
}

/// 使用者全域 env 若含指向預設目錄的 `SERIALWRAP_*`（例如正式安裝留下的設定），不得穿透批次檔。
fn user_env_with_inherited_serialwrap() -> Vec<(String, String)> {
    let d = defaults();
    let mut env = user_base();
    env.extend(pairs(&[
        ("SERIALWRAP_STATE_DIR", &d.state_dir),
        ("SERIALWRAP_RUN_DIR", &d.run_dir),
        ("SERIALWRAP_CONFIG_DIR", &d.config_dir),
        ("SERIALWRAP_CONFIG", &format!(r"{}\config.yaml", d.config_dir)),
        ("SERIALWRAP_ENDPOINT", "tcp://127.0.0.1:48700"),
    ]));
    env
}

fn sysenv(env: Vec<(String, String)>) -> FakeEnv {
    let fake = FakeEnv::default();
    fake.vars
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .extend(env);
    fake
}

fn spec() -> StagingSpec {
    StagingSpec::new(ROOT, PORT, vec!["COM1".into(), "COM20".into(), "COM21".into()])
}

fn decide(env: Vec<(String, String)>, spec: &StagingSpec) -> TestAllowlist {
    let cfg = RuntimeConfig::from_text(Some(&spec.render_config())).expect("staging config 可解析");
    resolve_test_allowlist(&sysenv(env), &cfg, &defaults())
}

#[test]
fn staging_batch_env_makes_daemon_isolation_hold_and_allowlist_effective() {
    let s = spec();
    // 前提：staging 根目錄與使用者預設目錄不同（否則隔離判定沒有意義）。
    let d = defaults();
    for dir in [&d.state_dir, &d.run_dir, &d.config_dir] {
        assert!(!dir.to_lowercase().starts_with(&ROOT.to_lowercase()), "預設目錄 {dir} 不得位於 staging 內");
    }
    let env = simulate_batch_env(&s.render_batch(), &user_env_with_inherited_serialwrap());
    let allow = decide(env, &s);
    assert_eq!(allow.rejected_reason, None, "isolation_reason 必須回 None");
    assert!(allow.enabled, "allowlist 必須生效：{allow:?}");
    assert_eq!(allow.coms, [ALLOWLIST_COM]);
    assert_eq!(allow.source, "env", "以批次檔的 SERIALWRAP_TEST_DEVICE_ALLOWLIST 為準");
}

#[test]
fn staging_config_parsed_by_daemon_excludes_registry_coms() {
    let s = spec();
    let cfg = RuntimeConfig::from_text(Some(&s.render_config())).expect("staging config 可解析");
    assert_eq!(cfg.windows_exclude_coms(), ["COM1", "COM20", "COM21"], "防線 2：daemon 讀到的 exclude_coms");
    assert_eq!(
        cfg.windows_test_device_allowlist(),
        Ok(Some(vec![ALLOWLIST_COM.to_string()])),
        "防線 1 第二層：config allowlist"
    );
    // env allowlist 缺席時，config allowlist 仍在隔離成立下生效（source=config）。
    let mut env = simulate_batch_env(&s.render_batch(), &user_base());
    env.retain(|(k, _)| k != "SERIALWRAP_TEST_DEVICE_ALLOWLIST");
    let allow = decide(env, &s);
    assert!(allow.enabled && allow.source == "config" && allow.coms == [ALLOWLIST_COM], "{allow:?}");
}

/// 對照組：同一判定在隔離不成立時確實拒絕（daemon 以 exit 2 拒絕啟動、不開任何裝置）。
#[test]
fn without_staging_batch_daemon_rejects_allowlist() {
    let s = spec();
    // 只帶 allowlist、沿用使用者預設目錄：STATE_DIR 等於預設 → 拒絕。
    let mut inherited = user_env_with_inherited_serialwrap();
    inherited.push(("SERIALWRAP_TEST_DEVICE_ALLOWLIST".into(), ALLOWLIST_COM.into()));
    let allow = decide(inherited, &s);
    assert!(!allow.enabled);
    let reason = allow.rejected_reason.expect("隔離不成立必須拒絕");
    assert!(reason.contains("SERIALWRAP_STATE_DIR equals default"), "{reason}");
    // 完全沒有隔離變數 → 拒絕（unset）。
    let mut bare = user_base();
    bare.push(("SERIALWRAP_TEST_DEVICE_ALLOWLIST".into(), ALLOWLIST_COM.into()));
    let reason = decide(bare, &s).rejected_reason.expect("未設隔離變數必須拒絕");
    assert!(reason.contains("SERIALWRAP_STATE_DIR unset"), "{reason}");
}

/// 批次檔少設任一個隔離判定變數，daemon 都會拒絕：證明三者都由批次檔提供，而非恰好繼承。
#[test]
fn every_isolation_var_comes_from_the_batch() {
    let s = spec();
    for var in ["SERIALWRAP_STATE_DIR", "SERIALWRAP_RUN_DIR", "SERIALWRAP_CONFIG_DIR"] {
        let mut env = simulate_batch_env(&s.render_batch(), &user_env_with_inherited_serialwrap());
        let before = env.len();
        env.retain(|(k, _)| k != var);
        assert_eq!(env.len(), before - 1, "批次檔必須設定 {var}");
        let reason = decide(env, &s).rejected_reason.unwrap_or_else(|| panic!("缺 {var} 必須拒絕"));
        assert!(reason.contains(var), "{var}: {reason}");
    }
}
