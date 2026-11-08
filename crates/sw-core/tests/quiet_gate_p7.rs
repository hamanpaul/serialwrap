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
        boot_evidence_wait_until: 0,
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

#[test] // 回歸（SC-03）：execute 層 quiet 剩 37.26 s → retry_after_s 37.3；pending-only → 5.0；failed → READY_UNCONFIRMED
fn execute_time_quiet_has_retry_after_s() {
    let mut q = QuietState::default();
    q.arm(t(0.0));
    let now = t(180.0 - 37.26);
    let r = execute_gate_reject(&mut q, "COM0", now).expect("quiet → 拒絕");
    assert_eq!(
        dumps_dict(&r, &RPC_WIRE),
        r#"{"ok":false,"error_code":"AUTOBOOT_QUIET","selector":"COM0","retry_after_s":37.3}"#
    );
    q.clear_quiet();
    let r = execute_gate_reject(&mut q, "COM0", t(200.0)).expect("pending-only → 拒絕");
    assert_eq!(r.get("retry_after_s"), Some(&PyValue::Float(5.0)));
    let r = execute_gate_reject(&mut q, "COM0", t(300.0)).expect("deadline → failed");
    assert_eq!(
        r.get("error_code"),
        Some(&PyValue::from("READY_UNCONFIRMED"))
    );
    assert_eq!(r.get("retry_after_s"), None);
    assert_eq!(
        r.get("recommended_action"),
        Some(&PyValue::from("self_test"))
    );
}

// ── WP5-6 item1：boot evidence wait 狀態（`SessionRuntime.begin_boot_evidence_wait`／`note_boot_evidence`／
// `boot_evidence_wait_active`，`session_manager.py:800-812`；public 投影 `:898-902`）。預期值為 Python 基線
// 以相同時間點實算（`SessionRuntime`＋`_update_boot_quiet_locked`，time.monotonic 固定），非手推。

/// `boot_evidence_wait_active(now)` 與 public `boot_evidence_wait_remaining_s` 的組合斷言。
fn assert_wait(q: &QuietState, now_s: f64, active: bool, remaining: Option<f64>, ctx: &str) {
    assert_eq!(
        q.boot_evidence_wait_active(t(now_s)),
        active,
        "{ctx} t={now_s} active"
    );
    assert_eq!(
        q.boot_evidence_wait_remaining_s(t(now_s)),
        remaining,
        "{ctx} t={now_s} remaining"
    );
}

#[test] // 新增（RED）：60 s 寬限邊界、Python 一位小數捨入、begin 不碰 reconfirm、confirm／expire 清除點
fn wp56_wait_boundaries_and_clearers() {
    // `constants.py:209`：BOOT_EVIDENCE_GRACE_S＝60.0，且小於 READY_RECONFIRM_MAX_S（300 s）。
    assert_eq!(sw_proto::constants::BOOT_EVIDENCE_GRACE_S, 60.0);

    // 邊界：未設定（0）不生效；begin(1000) → [1000, 1060) 有效，1060 起失效（`now < until`）。
    let mut q = QuietState::default();
    assert_eq!(q.boot_evidence_wait_until, 0);
    assert_wait(&q, 1000.0, false, None, "未設定");
    q.begin_boot_evidence_wait(t(1000.0));
    assert_eq!(q.boot_evidence_wait_until, t(1060.0));
    for (now, active, remaining) in [
        (1000.0, true, Some(60.0)),
        (1022.74, true, Some(37.3)),
        // 剩 0.25 s：Python `round(0.25, 1)` 半數取偶 → 0.2。
        (1059.75, true, Some(0.2)),
        // 59.99 s 仍有效，剩餘 round(0.01, 1)＝0.0（不是 None）。
        (1059.99, true, Some(0.0)),
        (1060.0, false, None),
        (1070.0, false, None),
    ] {
        assert_wait(&q, now, active, remaining, "boundaries");
    }
    // 再次 begin 以新的 now 重新起算（Python 直接覆寫 until）。
    q.begin_boot_evidence_wait(t(1010.0));
    assert_eq!(q.boot_evidence_wait_until, t(1070.0));
    assert_wait(&q, 1010.0, true, Some(60.0), "rebegin");
    // begin 只動等待欄位：未 pending 時不 arm quiet、不建 reconfirm。
    assert!(!q.pending && q.deadline == 0 && !q.quiet_active(t(1010.0)));

    // reboot arm(1000) 後 begin(1001)：不延長 300 s deadline、不動 pending／attempts／quiet。
    let mut q = QuietState::default();
    q.arm(t(1000.0));
    q.begin_boot_evidence_wait(t(1001.0));
    assert_eq!(q.boot_evidence_wait_until, t(1061.0));
    assert_eq!((q.pending, q.deadline, q.attempts), (true, t(1300.0), 0));
    assert_eq!(q.remaining_s(t(1001.0)), Some(179.0));
    assert_eq!(q.reconfirm_remaining_s(t(1001.0)), Some(299.0));
    assert_wait(&q, 1001.0, true, Some(60.0), "begin_keeps_deadline");

    // `clear_boot_quiet`（`:706-716`）只清 until／tail：等待與 pending 皆保留。
    q.clear_quiet();
    assert!(!q.quiet_active(t(1001.0)));
    assert_eq!((q.pending, q.deadline), (true, t(1300.0)));
    assert_eq!(q.boot_evidence_wait_until, t(1061.0));
    assert_wait(&q, 1001.0, true, Some(60.0), "clear_quiet_keeps");

    // `arm_boot_quiet`（`:663-692`）本身不清等待（reboot 路徑另於 `:6411` 自行歸零）；重臂亦不延長 deadline。
    q.arm(t(1002.0));
    assert_eq!(q.boot_evidence_wait_until, t(1061.0));
    assert_eq!(q.deadline, t(1300.0));
    assert_wait(&q, 1002.0, true, Some(59.0), "arm_keeps_wait");

    // `expire_ready_reconfirm`（`:740-761`）未翻轉時不清等待。
    q.attempts = 2;
    assert!(!q.expire(t(1010.0), false));
    assert_eq!(q.boot_evidence_wait_until, t(1061.0));
    assert_wait(&q, 1010.0, true, Some(51.0), "expire_not_hit");
    // deadline 到 → 翻入 failed 終態，同時清等待（`:759`）。
    assert!(q.expire(t(1300.0), false));
    assert!(q.failed && !q.pending);
    assert_eq!(q.boot_evidence_wait_until, 0);
    assert_wait(&q, 1300.0, false, None, "expire_flip");

    // 非 pending：force 也不翻轉，等待保留。
    let mut q = QuietState::default();
    q.begin_boot_evidence_wait(t(1000.0));
    assert!(!q.expire(t(1001.0), true));
    assert_eq!(q.boot_evidence_wait_until, t(1060.0));
    assert_wait(&q, 1001.0, true, Some(59.0), "expire_not_pending");
    // force 翻轉同樣清等待；expire 不清 quiet。
    q.arm(t(1000.0));
    q.begin_boot_evidence_wait(t(1000.0));
    assert!(q.expire(t(1001.0), true));
    assert_eq!(q.boot_evidence_wait_until, 0);
    assert!(q.quiet_active(t(1001.0)));
    assert_wait(&q, 1001.0, false, None, "expire_force");

    // `confirm_ready`（`:718-732`）清等待與全部 reconfirm 欄位。
    let mut q = QuietState::default();
    q.arm(t(1000.0));
    q.begin_boot_evidence_wait(t(1001.0));
    q.confirm_ready();
    assert_eq!(q.boot_evidence_wait_until, 0);
    assert_eq!(
        (q.pending, q.deadline, q.attempts, q.failed),
        (false, 0, 0, false)
    );
    assert_wait(&q, 1001.0, false, None, "confirm");
}

#[test] // 新增（RED）：banner 解除等待（`_update_boot_quiet_locked` `:4560-4564` 先 note 再 arm），不延長 reconfirm
fn wp56_banner_releases_wait_without_extending_reconfirm() {
    let sp = prpl();
    // reboot 命令 arm(1000) → 2 s 內 prompt 回來：清 quiet＋begin(1001.5)（actor 端 `:6463-6466` 的狀態效果）。
    let mut q = QuietState::default();
    q.arm(t(1000.0));
    q.clear_quiet();
    q.begin_boot_evidence_wait(t(1001.5));
    q.attempts = 2;

    // shutdown 雜訊（含平台字串）不是開機證據：等待保留，quiet 維持未 armed。
    let r = q.on_rx_text(
        "EXT4-fs (flash-data): unmounting filesystem\r\nSMCOS: rebooting system...\r\n",
        &sp,
        t(1005.0),
    );
    assert_eq!(r, QuietTransition::None);
    assert_wait(&q, 1005.0, true, Some(56.5), "shutdown noise");
    assert!(!q.quiet_active(t(1005.0)));

    // banner 被切在兩個 chunk：前半不解除。
    let r = q.on_rx_text("...\r\nHit any key to sto", &sp, t(1020.0));
    assert_eq!(r, QuietTransition::None);
    assert_wait(&q, 1020.0, true, Some(41.5), "banner 前半");

    // 後半拼回 tail 命中 banner：解除等待並重臂 quiet；pending、原 deadline、attempts 保留。
    let r = q.on_rx_text("p autoboot:  3 ", &sp, t(1020.5));
    assert_eq!(r, QuietTransition::Armed);
    assert_eq!(q.boot_evidence_wait_until, 0);
    assert_wait(&q, 1020.5, false, None, "banner 後半");
    assert!(q.quiet_active(t(1020.5)));
    assert_eq!(q.remaining_s(t(1020.5)), Some(180.0));
    assert_eq!(
        (q.pending, q.deadline, q.attempts, q.failed),
        (true, t(1300.0), 2, false)
    );
    assert_eq!(q.reconfirm_remaining_s(t(1020.5)), Some(279.5));

    // 單一 chunk 的 U-Boot 版本行同樣解除。
    let mut q = QuietState::default();
    q.arm(t(1000.0));
    q.clear_quiet();
    q.begin_boot_evidence_wait(t(1001.5));
    let r = q.on_rx_text(
        "U-Boot TPL 2024.04 (Oct 27 2026 - 15:48:00 +0800)\r\n",
        &sp,
        t(1017.0),
    );
    assert_eq!(r, QuietTransition::Armed);
    assert_wait(&q, 1017.0, false, None, "banner 單 chunk");
    assert_eq!((q.pending, q.deadline), (true, t(1300.0)));
    assert_eq!(q.reconfirm_remaining_s(t(1017.0)), Some(283.0));

    // passthrough：`_update_boot_quiet_locked` 開頭即 return（SC-35），banner 不 note、等待保留。
    let mut pt = sp.clone();
    pt.template.platform = "passthrough".into();
    let mut q = QuietState::default();
    q.begin_boot_evidence_wait(t(1000.0));
    let r = q.on_rx_text("U-Boot 2024.04\r\n", &pt, t(1010.0));
    assert_eq!(r, QuietTransition::None);
    assert_eq!(q.boot_evidence_wait_until, t(1060.0));
    assert_wait(&q, 1010.0, true, Some(50.0), "passthrough banner");
}
