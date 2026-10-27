//! P7 Task 4（08-slice23-login-events.md）：Autoboot Quiet＋Ready Reconfirm 的純函式（`quiet_gate.rs`）。
//!
//! 斷言值以 Python 基線 golden `contracts/golden/p7/{quiet_rx,gate_responses}.ndjson`（CPython 3.12 擷取）為準。
//! 偏離計畫原文：計畫的 `support::{golden_rows, t, secs, profile_from_json}` 放 `tests/common/p7.rs`；
//! golden 的時間為 Python `time.monotonic()` 秒，此處以 ns（`Mono`）換算。
mod common;

use common::p7::{golden_rows, profile_from_json, Row};
use sw_core::quiet_gate::*;
use sw_core::Mono;
use sw_proto::pyjson::{dumps_dict, PyDict, PyValue, RPC_WIRE};

fn t(s: f64) -> Mono {
    (s * 1e9).round() as Mono
}

fn secs(m: Mono) -> f64 {
    m as f64 / 1e9
}

fn f64_of(v: &PyValue) -> f64 {
    match v {
        PyValue::Float(f) => *f,
        PyValue::Int(_) => v.as_i64().expect("int") as f64,
        other => panic!("不是數值：{other:?}"),
    }
}

fn opt_f64(v: Option<&PyValue>) -> Option<f64> {
    match v {
        None | Some(PyValue::Null) => None,
        Some(v) => Some(f64_of(v)),
    }
}

fn dict(v: &PyValue) -> &PyDict {
    match v {
        PyValue::Dict(d) => d,
        other => panic!("不是 object：{other:?}"),
    }
}

/// golden `quiet` 物件（Python 秒；`0.0`＝未設定）→ `QuietState`。
fn quiet_state(row: &Row, key: &str) -> QuietState {
    let q = dict(&row[key]);
    let g = |k: &str| q.get(k).unwrap_or_else(|| panic!("quiet 欄位 {k} 不存在"));
    let mono = |k: &str| {
        let s = f64_of(g(k));
        if s > 0.0 {
            t(s)
        } else {
            0
        }
    };
    QuietState {
        boot_quiet_until: mono("boot_quiet_until"),
        boot_banner_tail: g("boot_banner_tail").as_str().expect("tail").to_string(),
        pending: g("pending").as_bool().expect("pending"),
        deadline: mono("deadline"),
        attempts: g("attempts").as_i64().expect("attempts") as u32,
        failed: g("failed").as_bool().expect("failed"),
    }
}

#[test] // 回歸鎖定（06 T13 已實作 on_rx_text；預期 PASS）
fn rx_sequences_match_python_golden() {
    for row in golden_rows("quiet_rx") {
        let sp = profile_from_json(&row["profile"]);
        let mut q = QuietState::default();
        let base = f64_of(&row["base_mono"]);
        for step in row["steps"].as_array().expect("steps") {
            let step = dict(step);
            let st = f64_of(step.get("t").expect("t"));
            let now = t(base + st);
            let chunk = step.get("chunk").and_then(PyValue::as_str).expect("chunk");
            q.on_rx_text(chunk, &sp, now);
            let ctx = format!("case {} t={st}", row.id());
            let until_rel = (q.boot_quiet_until != 0).then(|| secs(q.boot_quiet_until) - base);
            match (until_rel, opt_f64(step.get("until_rel"))) {
                (Some(a), Some(b)) => assert!((a - b).abs() < 1e-6, "{ctx} until {a} != {b}"),
                (a, b) => assert_eq!(a, b, "{ctx} until"),
            }
            assert_eq!(
                q.boot_banner_tail,
                step.get("tail").and_then(PyValue::as_str).expect("tail"),
                "{ctx} tail"
            );
            assert_eq!(
                q.pending,
                step.get("pending").and_then(PyValue::as_bool).unwrap(),
                "{ctx} pending"
            );
            assert_eq!(
                q.failed,
                step.get("failed").and_then(PyValue::as_bool).unwrap(),
                "{ctx} failed"
            );
            assert_eq!(
                q.remaining_s(now),
                opt_f64(step.get("quiet_remaining_s")),
                "{ctx} quiet_remaining_s"
            );
            assert_eq!(
                q.reconfirm_remaining_s(now),
                opt_f64(step.get("reconfirm_remaining_s")),
                "{ctx} reconfirm_remaining_s"
            );
        }
    }
}

#[test] // 新增（RED）：submit／execute 兩層 gate 回應與 Python 位元組相同
fn gate_responses_are_byte_identical_to_python() {
    for row in golden_rows("gate_responses") {
        let got = match row.str("layer") {
            // 計畫：submit_gate_reject 只對「非 daemon 內部 human console 路徑」呼叫；trusted_human 列由呼叫端略過。
            "submit" if row.bool("trusted_human") => None,
            "submit" => {
                // golden 檔以 sort_keys 落地，`public_session` 鍵序已排序；回應內的 `session` 保留 Python
                // `to_public_dict()` 原始鍵序，故有回應時以它為輸入（內容與 `public_session` 相同，先驗證）。
                let input = match row.opt_str("response") {
                    Some(resp) => {
                        let PyValue::Dict(r) =
                            sw_proto::pyjson::loads(resp).expect("response JSON")
                        else {
                            panic!("response 須為 object")
                        };
                        let session = dict(r.get("session").expect("session")).clone();
                        let mut a: Vec<_> = session
                            .0
                            .iter()
                            .map(|(k, v)| (k.clone(), format!("{v:?}")))
                            .collect();
                        let mut b: Vec<_> = dict(&row["public_session"])
                            .0
                            .iter()
                            .map(|(k, v)| (k.clone(), format!("{v:?}")))
                            .collect();
                        a.sort();
                        b.sort();
                        assert_eq!(a, b, "case {} session 內容", row.id());
                        session
                    }
                    None => dict(&row["public_session"]).clone(),
                };
                submit_gate_reject(&input, row.str("source"))
            }
            "execute" => {
                let mut q = quiet_state(&row, "quiet");
                let got = execute_gate_reject(&mut q, row.str("com"), t(f64_of(&row["now"])));
                let after = quiet_state(&row, "quiet_after");
                assert_eq!(
                    (q.pending, q.failed),
                    (after.pending, after.failed),
                    "case {} quiet_after",
                    row.id()
                );
                got
            }
            l => panic!("{l}"),
        };
        let got_s = got.map(|d| dumps_dict(&d, &RPC_WIRE));
        assert_eq!(
            got_s.as_deref(),
            row.opt_str("response"),
            "case {}",
            row.id()
        );
    }
}

#[test] // 新增：提示文字與 Python f-string 常數一致（300 s／5 次）
fn hint_constant_matches_limits_and_golden() {
    assert!(READY_UNCONFIRMED_HINT.contains("READY 已逾 300s／5 次"));
    assert_eq!(READY_RECONFIRM_MAX_ATTEMPTS, 5);
    let golden = golden_rows("gate_responses")
        .into_iter()
        .find_map(|r| {
            r.opt_str("response")
                .filter(|s| s.contains("READY_UNCONFIRMED"))
                .map(str::to_string)
        })
        .expect("golden 含 READY_UNCONFIRMED");
    assert!(golden.contains(READY_UNCONFIRMED_HINT));
}

fn prpl() -> sw_proto::config_types::SessionProfile {
    let row = golden_rows("quiet_rx")
        .into_iter()
        .next()
        .expect("quiet_rx 第一列");
    profile_from_json(&row["profile"])
}

#[test] // 新增（RED）：`_boot_quiet_blocks_probe`＝quiet 進行中且無 console_unlock_env；passthrough 恆 false（SC-35）
fn console_unlock_profile_is_not_probe_blocked() {
    let mut sp = prpl();
    let mut q = QuietState::default();
    q.arm(t(0.0));
    assert!(boot_quiet_blocks_probe(&q, &sp, t(1.0)));
    assert!(
        !boot_quiet_blocks_probe(&q, &sp, t(181.0)),
        "quiet 到期後不再擋"
    );
    sp.template.console_unlock_env = Some("SMART4R_CONSOLE_UNLOCK".into());
    assert!(!boot_quiet_blocks_probe(&q, &sp, t(1.0)));
    sp.template.console_unlock_env = Some(String::new());
    assert!(
        boot_quiet_blocks_probe(&q, &sp, t(1.0)),
        "空字串 env 名稱等同未宣告（Python falsy）"
    );
    sp.template.console_unlock_env = None;
    sp.template.platform = "passthrough".into();
    assert!(!boot_quiet_blocks_probe(&q, &sp, t(1.0)));
}

#[test] // 新增（RED）：agent gate＝先 expire，再 quiet_active||pending（`agent_gate_active`）
fn agent_gate_active_expires_first() {
    let mut q = QuietState::default();
    q.arm(t(0.0));
    assert!(q.agent_gate_active(t(1.0)));
    q.clear_quiet();
    assert!(
        q.agent_gate_active(t(1.0)),
        "quiet 解除但 pending 仍 gate（#162）"
    );
    assert!(
        !q.agent_gate_active(t(300.0)),
        "deadline 到 → expire 翻 failed、pending=false"
    );
    assert!(q.failed);
}
