//! P7 Task 2（08-slice23-login-events.md）：`session_auth.rs` 對 Python 基線（`contracts/golden/p7/{env_file,auth_resolution}.ndjson`）
//! 的回歸鎖定與 `SessionAuth` 除錯輸出遮蔽。
//!
//! 偏離計畫原文：計畫以 `support::captured_warnings()` 擷取 WARNING；sw-core 無全域 logger，故以
//! `resolve_session_auth_with_warnings(p, env, read, warn)` 取得同一組訊息（`resolve_session_auth` 簽章不變，
//! 內部轉呼叫並把訊息送往 daemon stderr），並斷言兩者回傳相同結果。
mod common;

use common::p7::{b64, fake_env, fake_read, golden_rows, profile_from_json};
use sw_core::session_auth::*;

#[test] // 回歸鎖定（06 T13 已實作）
fn parse_env_file_matches_python_golden() {
    for row in golden_rows("env_file") {
        let input = b64(row.str("input_b64"));
        match String::from_utf8(input) {
            Ok(text) => {
                let got: Vec<(String, String)> = parse_env_file(&text).into_iter().collect();
                assert_eq!(got, row.pairs("pairs"), "case {}", row.id());
            }
            Err(_) => assert_eq!(row.str("error"), "unreadable", "case {}", row.id()),
        }
    }
}

#[test] // 回歸鎖定
fn resolve_session_auth_matches_python_golden_including_warnings() {
    for row in golden_rows("auth_resolution")
        .into_iter()
        .filter(|r| r.str("layer") == "auth")
    {
        let sp = profile_from_json(&row["profile"]);
        let env = fake_env(&row["environ"]);
        let read = fake_read(&row["files"]);
        let mut warnings = Vec::new();
        let (auth, res) =
            resolve_session_auth_with_warnings(&sp, &env, &read, &mut |w| warnings.push(w));
        assert_eq!(auth.username.as_deref(), row.opt_str("username"), "case {}", row.id());
        assert_eq!(auth.password.is_some(), row.bool("password_set"), "case {}", row.id());
        assert_eq!(
            auth.console_unlock.is_some(),
            row.bool("console_unlock_set"),
            "case {}",
            row.id()
        );
        assert_eq!(res.reason, row.str("reason"), "case {}", row.id());
        assert_eq!(
            res.env_file_path.as_deref(),
            row.opt_str("env_file_path"),
            "case {}",
            row.id()
        );
        assert_eq!(warnings, row.str_vec("warnings"), "case {}", row.id());

        let (auth2, res2) = resolve_session_auth(&sp, &env, &read);
        assert_eq!(res2, res, "case {}", row.id());
        assert_eq!(auth2.username, auth.username, "case {}", row.id());
    }
}

#[test] // 新增行為（RED）
fn debug_output_redacts_secrets() {
    let a = SessionAuth {
        username: Some("u".into()),
        password: Some("hunter2".into()),
        console_unlock: Some("k3y".into()),
    };
    let s = format!("{a:?}");
    assert!(
        s.contains("\"u\"") && !s.contains("hunter2") && !s.contains("k3y"),
        "{s}"
    );
    assert!(s.contains("Some(<redacted>)"), "{s}");
    let none = format!("{:?}", SessionAuth::default());
    assert!(none.contains("None") && !none.contains("redacted"), "{none}");
}
