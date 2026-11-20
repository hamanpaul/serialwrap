//! P8 Task 13：`wintask_runtime.rs` 的 COM 防線與 support 自測（memory_only，可在 WSL／CI 執行）。
//!
//! 擁有者裁決（2026-11-08「T13 的 Windows 排程工作實跑」）要求的三道防線，本檔以替身驗證其「內容」：
//! - 批次檔先清掉所有繼承的 `SERIALWRAP_*`，再把 STATE／RUN／CONFIG／WAL／LOG 等目錄全部指向 staging，並設
//!   `SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM250`；daemon 只連 staging 埠（≠ 48700）、只用 staging 鎖。
//! - staging config 的 `windows.exclude_coms` 列出 `reg query HKLM\HARDWARE\DEVICEMAP\SERIALCOMM` 的全部 COM，另寫
//!   `windows.test_device_allowlist: [COM250]`；`reg query` 失敗即 fail-closed。
//! - 實跑測試只以具名 API 操作 `sw-p8-test-<pid>`，收尾守衛先於 install、daemon 起來後先驗隔離。
//!
//! 「隔離成立」（`isolation_reason` 回 None、allowlist 生效）以 daemon 自己的判定驗證，見
//! `crates/sw-daemon/tests/wintask_staging_isolation.rs`。本檔不宣稱任何 Windows 原生證據。
mod support;

use support::wintask::*;
use sw_pal::fakes::FakeSysEnv;
use sw_pal::service_ctl::{encode_utf16le_bom, render_task_xml, TaskXmlSpec};
use sw_pal::PalErrorKind;

const ROOT: &str = r"C:\Users\tester\AppData\Local\Temp\sw-p8-test-4242";
const PORT: u16 = 50123;

/// `reg query` 的實際輸出形狀（首尾空行、4 空白縮排、多個空白分隔；com0com 與實體埠混合、含重複值）。
const REG_OUT: &str = "\r\nHKEY_LOCAL_MACHINE\\HARDWARE\\DEVICEMAP\\SERIALCOMM\r\n    \\Device\\Serial0    REG_SZ    COM1\r\n    \\Device\\com0com10    REG_SZ    COM20\r\n    \\Device\\com0com20    REG_SZ    COM21\r\n    \\Device\\com0com11    REG_SZ    COM22\r\n    \\Device\\com0com21    REG_SZ    COM23\r\n    \\Device\\Silabser0    REG_SZ    COM7\r\n    \\Device\\VCP0    REG_SZ    COM7\r\n\r\n";

fn reg_env(rc: i32, out: &str) -> std::sync::Arc<FakeSysEnv> {
    let argv = reg_query_argv();
    let refs: Vec<&str> = argv.iter().map(String::as_str).collect();
    FakeSysEnv::builder().command(&refs, rc, out, if rc == 0 { "" } else { "ERROR: 系統找不到指定的登錄機碼或值。" }).build()
}

fn spec() -> StagingSpec {
    let coms = registry_com_ports(&*reg_env(0, REG_OUT)).expect("reg query 成功");
    StagingSpec::new(ROOT, PORT, coms)
}

fn batch_lines(spec: &StagingSpec) -> Vec<String> {
    let text = spec.render_batch();
    assert!(text.ends_with("\r\n"), "批次檔以 CRLF 結尾");
    assert!(!text.replace("\r\n", "").contains('\n'), "批次檔每行皆為 CRLF");
    text.split("\r\n").filter(|l| !l.is_empty()).map(str::to_string).collect()
}

fn under_root(value: &str) -> bool {
    value.to_ascii_lowercase().starts_with(&format!("{}\\", ROOT.to_ascii_lowercase()))
}

#[test]
fn batch_clears_inherited_env_then_isolates_every_dir() {
    let s = spec();
    let lines = batch_lines(&s);
    let clear = lines.iter().position(|l| l == BATCH_CLEAR_LINE).expect("必須先清掉繼承的 SERIALWRAP_*");
    let first_set = lines.iter().position(|l| l.starts_with("set \"SERIALWRAP_")).expect("必須設定隔離 env");
    assert!(clear < first_set, "清除必須早於任何 set");
    let sets: Vec<(String, String)> = lines
        .iter()
        .filter_map(|l| l.strip_prefix("set \"").and_then(|r| r.strip_suffix('"')))
        .filter_map(|b| b.split_once('=').map(|(k, v)| (k.to_string(), v.to_string())))
        .collect();
    assert_eq!(sets, s.isolation_env(), "批次檔的 set 行即 isolation_env（順序相同）");
    // daemon 的隔離判定變數（STATE／RUN／CONFIG）與其餘目錄、檔案路徑全部落在 staging。
    for key in [
        "SERIALWRAP_STATE_DIR",
        "SERIALWRAP_RUN_DIR",
        "SERIALWRAP_CONFIG_DIR",
        "SERIALWRAP_WAL_DIR",
        "SERIALWRAP_LOG_DIR",
        "SERIALWRAP_DATA_DIR",
        "SERIALWRAP_EVENTS_DIR",
        "SERIALWRAP_EVENTS_RUNTIME_DIR",
        "SERIALWRAP_EVENTS_LOG_PATH",
        "SERIALWRAP_BY_ID_DIR",
        "SERIALWRAP_BY_PATH_DIR",
        "SERIALWRAP_INSTALL_ROOT",
        "SERIALWRAP_PROFILE_DIR",
        "SERIALWRAP_TTYMCU_PATH",
        "SERIALWRAP_BENCHES_FILE",
        "SERIALWRAP_TEST_SYSTEM_CONFIG_PATH",
    ] {
        let value = sets
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
            .unwrap_or_else(|| panic!("批次檔缺 {key}"));
        assert!(under_root(value), "{key}={value} 不在 staging {ROOT} 內");
    }
    assert!(
        sets.contains(&("SERIALWRAP_TEST_DEVICE_ALLOWLIST".into(), "COM250".into())),
        "防線 1：allowlist 必須為不存在的 COM250"
    );
    // 高優先 env（會蓋過目錄推導）不得重新設定：只由清除迴圈移除。
    for key in ["SERIALWRAP_CONFIG", "SERIALWRAP_ENDPOINT", "SERIALWRAP_TCP_PORT", "SERIALWRAP_RELEASE_MANIFEST"] {
        assert!(!sets.iter().any(|(k, _)| k == key), "{key} 不得出現在批次檔");
    }
}

#[test]
fn batch_runs_staging_daemon_on_test_port_only() {
    let s = spec();
    let lines = batch_lines(&s);
    assert_eq!(lines.first().map(String::as_str), Some("@echo off"));
    assert_eq!(lines.last().map(String::as_str), Some("exit /b %ERRORLEVEL%"), "以 daemon 的 exit code 結束（失敗重啟依據）");
    let daemon: Vec<&String> = lines.iter().filter(|l| l.contains(DAEMON_EXE)).collect();
    assert_eq!(daemon.len(), 1, "恰一行啟動 daemon：{daemon:?}");
    let line = daemon[0];
    assert!(line.starts_with(&format!("\"{ROOT}\\bin\\serialwrapd.exe\"")), "只執行 staging 內的 daemon：{line}");
    assert!(line.contains(&format!("--socket tcp://127.0.0.1:{PORT} ")), "{line}");
    assert!(line.contains(&format!("--lock \"{ROOT}\\run\\serialwrapd.lock\"")), "{line}");
    assert!(line.contains(&format!("--profile-dir \"{ROOT}\\config\\profiles\"")), "{line}");
    assert!(line.ends_with(&format!(">> \"{ROOT}\\daemon.log\" 2>&1")), "{line}");
    let daemon_idx = lines.iter().position(|l| l == line).unwrap_or_default();
    let last_set = lines.iter().rposition(|l| l.starts_with("set \"")).unwrap_or_default();
    assert!(last_set < daemon_idx, "env 必須全部設定完才啟動 daemon");
    assert!(!s.render_batch().contains("48700"), "不得出現正式埠");
}

#[test]
fn config_excludes_every_registry_com_and_allowlists_com250() {
    let s = spec();
    assert_eq!(s.exclude_coms, ["COM1", "COM20", "COM21", "COM22", "COM23", "COM7"]);
    let cfg = sw_pal_free_yaml(&s.render_config());
    assert_eq!(cfg.exclude_coms, s.exclude_coms, "防線 2：exclude_coms 為 registry 全部 COM");
    assert_eq!(cfg.allowlist, ["COM250"], "防線 1 第二層：config allowlist 也只有 COM250");
    let empty = StagingSpec::new(ROOT, PORT, Vec::new());
    assert!(empty.render_config().contains("  exclude_coms: []\n"));
}

/// 本檔不依賴 sw-proto：以行為準解析 render_config 的固定形狀（sw-daemon 端另以 `RuntimeConfig` 驗證同一文字）。
struct StagingConfig {
    exclude_coms: Vec<String>,
    allowlist: Vec<String>,
}

fn sw_pal_free_yaml(text: &str) -> StagingConfig {
    let mut cfg = StagingConfig { exclude_coms: Vec::new(), allowlist: Vec::new() };
    let mut current: Option<&str> = None;
    for line in text.lines() {
        match line {
            "windows:" => {}
            "  exclude_coms:" => current = Some("exclude"),
            "  test_device_allowlist:" => current = Some("allow"),
            "  exclude_coms: []" => current = None,
            item if item.starts_with("  - '") && item.ends_with('\'') => {
                let value = item["  - '".len()..item.len() - 1].replace("''", "'");
                match current {
                    Some("exclude") => cfg.exclude_coms.push(value),
                    Some("allow") => cfg.allowlist.push(value),
                    _ => panic!("清單項目不在任何鍵下：{item}"),
                }
            }
            other => panic!("未預期的 config 行：{other:?}"),
        }
    }
    cfg
}

#[test]
fn registry_query_failure_is_fail_closed() {
    assert!(registry_com_ports(&*reg_env(1, "")).is_err(), "rc≠0 必須拒絕（不在未知 COM 狀態下啟動）");
    let argv = reg_query_argv();
    let refs: Vec<&str> = argv.iter().map(String::as_str).collect();
    let env = FakeSysEnv::builder().command_err(&refs, PalErrorKind::NotFound, "reg 不存在").build();
    assert!(registry_com_ports(&*env).is_err(), "無法執行 reg 必須拒絕");
    assert_eq!(registry_com_ports(&*reg_env(0, "\r\nHKEY_LOCAL_MACHINE\\HARDWARE\\DEVICEMAP\\SERIALCOMM\r\n\r\n")), Ok(Vec::new()));
}

#[test]
fn simulated_batch_env_drops_inherited_serialwrap_vars() {
    let s = spec();
    let inherited = vec![
        ("serialwrap_config".to_string(), r"C:\Users\tester\.config\serialwrap\config.yaml".to_string()),
        ("SERIALWRAP_STATE_DIR".to_string(), r"C:\Users\tester\.local\state\serialwrap".to_string()),
        ("SERIALWRAP_ENDPOINT".to_string(), "tcp://127.0.0.1:48700".to_string()),
        ("SERIALWRAP_TEST_KILL_AFTER_SAVE".to_string(), "1".to_string()),
        ("USERPROFILE".to_string(), r"C:\Users\tester".to_string()),
    ];
    let env = simulate_batch_env(&s.render_batch(), &inherited);
    let get = |k: &str| env.iter().find(|(key, _)| key == k).map(|(_, v)| v.clone());
    assert_eq!(get("SERIALWRAP_CONFIG"), None);
    assert_eq!(get("SERIALWRAP_ENDPOINT"), None);
    assert_eq!(get("SERIALWRAP_TEST_KILL_AFTER_SAVE"), None);
    assert_eq!(get("SERIALWRAP_STATE_DIR"), Some(s.state_dir()));
    assert_eq!(get("USERPROFILE"), Some(r"C:\Users\tester".to_string()), "非 SERIALWRAP_ 變數保留");
    for (k, v) in &env {
        if k.starts_with("SERIALWRAP_") && k != "SERIALWRAP_TEST_DEVICE_ALLOWLIST" {
            assert!(under_root(v), "{k}={v} 不在 staging 內");
        }
    }
}

#[test]
fn task_name_guard_only_accepts_test_names() {
    assert_test_task_name("sw-p8-test-4242");
    assert_eq!(test_task_name(77), "sw-p8-test-77");
    for bad in ["serialwrap", "SERIALWRAP", "sw-p8-test-", "sw-p8-test-12x", "serialwrap-throwaway-1", r"\serialwrap"] {
        let result = std::panic::catch_unwind(|| assert_test_task_name(bad));
        assert!(result.is_err(), "名稱 {bad:?} 必須被拒絕");
    }
}

#[test]
fn cleanup_ends_delayed_start_task_before_deleting_it() {
    use std::sync::Arc;

    use sw_pal::fakes::{real_fs, FakeClock};
    use sw_pal::service_ctl::{ServiceCtl, WindowsTaskCtl};

    const TASK: &str = "sw-p8-test-4242";
    const EXE: &str = r"C:\Users\tester\AppData\Local\Temp\sw-p8-test-4242\bin\serialwrapd.exe";
    const STATE_QUERY: &str = "try { (Get-ScheduledTask -TaskName 'sw-p8-test-4242' -ErrorAction Stop).State.ToString() } catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { 'NotFound' } else { exit 3 } }";

    let xml_query = ["schtasks", "/Query", "/TN", TASK, "/XML"];
    let disable = ["schtasks", "/Change", "/TN", TASK, "/DISABLE"];
    let end = ["schtasks", "/End", "/TN", TASK];
    let delete = ["schtasks", "/Delete", "/TN", TASK, "/F"];
    let query = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        STATE_QUERY,
    ];
    let query_owned = query
        .iter()
        .map(|value| value.to_string())
        .collect::<Vec<_>>();

    // 模擬排程工作仍在啟動批次檔，但 daemon 尚未建立：/End 後工作短暫仍為 Running，接著回到 Ready。
    // 行程列舉保持空白，重現原 cleanup 只掃現有 daemon 會漏掉稍後啟動者的窗口。
    let env = FakeSysEnv::builder()
        .command_seq(
            &xml_query,
            &[
                (
                    0,
                    "<Task><Settings><Enabled>true</Enabled></Settings></Task>",
                    "",
                ),
                (
                    0,
                    "<Task><Settings><Enabled>false</Enabled></Settings></Task>",
                    "",
                ),
            ],
        )
        .command(&disable, 0, "", "")
        .command_seq(
            &query,
            &[
                (0, "Running\r\n", ""),
                (0, "Running\r\n", ""),
                (0, "Ready\r\n", ""),
                (0, "NotFound\r\n", ""),
            ],
        )
        .command(&end, 0, "", "")
        .command(&delete, 0, "", "")
        .build();
    let ctl: Arc<dyn ServiceCtl> = Arc::new(WindowsTaskCtl::new(
        env.clone(),
        real_fs(),
        FakeClock::new(),
    ));

    TaskGuard::new(env.clone(), ctl, TASK, EXE)
        .finish()
        .expect("必須先停止延遲啟動中的具名工作，再刪除並確認無殘留 daemon");

    let calls = env.calls();
    let end_at = calls
        .iter()
        .position(|argv| argv == &end)
        .expect("收尾必須對測試工作執行 /End");
    let delete_at = calls
        .iter()
        .position(|argv| argv == &delete)
        .expect("收尾必須刪除測試工作");
    let state_after_end = calls
        .iter()
        .enumerate()
        .filter(|(_, argv)| argv.as_slice() == query_owned.as_slice())
        .map(|(index, _)| index)
        .filter(|index| *index > end_at)
        .collect::<Vec<_>>();
    assert!(end_at < delete_at, "必須先 /End 再 /Delete：{calls:?}");
    assert!(
        state_after_end.iter().any(|index| *index < delete_at),
        "刪除前必須查到 /End 已讓工作離開 Running：{calls:?}"
    );
    assert!(
        calls.iter().enumerate().all(|(index, argv)| {
            !argv.first().is_some_and(|program| program == "taskkill") || index > delete_at
        }),
        "只可在工作停止並刪除後清理既有 staging daemon：{calls:?}"
    );
}

#[test]
#[should_panic(expected = "正式埠")]
fn staging_refuses_production_port() {
    let _ = StagingSpec::new(ROOT, PRODUCTION_PORT, Vec::new());
}

#[test]
fn pick_free_port_never_returns_production_port() {
    for _ in 0..8 {
        assert_ne!(pick_free_port(), PRODUCTION_PORT);
    }
}

#[test]
fn runtime_test_only_uses_named_task_apis() {
    let runtime = include_str!("wintask_runtime.rs");
    let support = include_str!("support/wintask.rs");
    // 固定名稱 `serialwrap` 的 ServiceCtl 方法一律不得出現（具名版本 `*_named` 與 release_autostart(hold) 例外）。
    for (file, text) in [("wintask_runtime.rs", runtime), ("support/wintask.rs", support)] {
        for call in [".uninstall(", ".set_enabled(", ".hard_stop(", ".hold_autostart(", ".query(", ".action(", ".autostart_hold_state("] {
            assert!(!text.contains(call), "{file} 不得呼叫固定名稱 serialwrap 的 {call}");
        }
        assert!(!text.contains("tcp://127.0.0.1:48700"), "{file} 不得連線正式 daemon");
    }
    assert!(!runtime.contains("\"serialwrap\""), "實跑測試不得出現正式工作名稱字面值");
    assert_eq!(support.matches("\"serialwrap\"").count(), 1, "support 只在名稱守門常數出現正式工作名稱");
    // 收尾守衛先於 install；防線 3 在首次啟動後、強制終止前。
    let guard = runtime.find("TaskGuard::new(").expect("必須建立收尾守衛");
    let install = runtime.find("ctl.install(").expect("必須安裝測試工作");
    let first_check = runtime.find("checked(pid0").expect("首次啟動後必須先驗隔離");
    let kill = runtime.find("taskkill_force(").expect("必須強制終止以驗證失敗重啟");
    let second_check = runtime.find("checked(pid1").expect("重啟後必須再驗隔離");
    let stop = runtime.rfind("formal_service_stop(").expect("必須經正式 serialwrap service stop（P4 graceful_stop）有序停止");
    assert!(guard < install && install < first_check && first_check < kill && kill < second_check && second_check < stop);
    // 停止序只能來自正式 sw-cli 路徑（harness 行程）；support 不得再有自行重寫的停止流程。
    for (file, text) in [("wintask_runtime.rs", runtime), ("support/wintask.rs", support)] {
        assert!(!text.contains("graceful_stop_for_test"), "{file} 不得自行實作停止序");
    }
    assert!(!runtime.contains("\"/End\""), "實跑案例不得自行組 /End");
    let cleanup = support
        .find("fn cleanup(&self)")
        .expect("必須有具名收尾守衛");
    let cleanup_body = &support[cleanup..];
    let hold = cleanup_body
        .find(".hold_autostart_named(")
        .expect("收尾先停用具名工作");
    let cleanup_end = cleanup_body
        .find("\"/End\"")
        .expect("收尾守衛可 /End 具名測試工作");
    let wait = cleanup_body
        .find("wait_task_stopped_or_absent(")
        .expect("/End 後必須確認工作已停止");
    let uninstall = cleanup_body
        .find(".uninstall_named(")
        .expect("停止後才可刪除具名測試工作");
    assert!(
        hold < cleanup_end && cleanup_end < wait && wait < uninstall,
        "cleanup 必須停用、/End、確認停止後再刪除"
    );
    // harness 在 install 前就位（缺檔即證據不足，不建立工作）；WAL 證據在收尾確認之後才判定。
    let harness = runtime.find("locate_harness(").expect("必須先找到正式停止路徑 harness");
    assert!(harness < install, "harness 必須在 install 前確認存在");
    let finish = runtime.find("guard.finish(").expect("必須確認收尾");
    let wal = runtime.find("wal_evidence(").expect("必須以嚴格 WAL 證據判定");
    assert!(stop < finish && finish < wal, "WAL 證據在有序停止與收尾確認之後");
    assert!(!runtime.contains("wal_last_line_crc_ok("), "實跑不得用寬鬆判定");
}

#[test]
fn support_xml_parser_reads_task_xml_by_element_path() {
    let xml = render_task_xml(&TaskXmlSpec {
        name: "sw-p8-test-4242".into(),
        user_id: r"HOST\tester".into(),
        command: format!(r"{ROOT}\run-daemon.cmd"),
        arguments: String::new(),
        working_dir: ROOT.into(),
        enabled: true,
    });
    for doc in [xml.clone(), decode_task_xml(&encode_utf16le_bom(&xml))] {
        let tree = parse_xml(&doc).expect("Task XML 可解析");
        assert_eq!(tree.name, "Task");
        assert_eq!(xml_text(&tree, "Settings/ExecutionTimeLimit"), "PT0S");
        assert_eq!(xml_text(&tree, "Settings/MultipleInstancesPolicy"), "IgnoreNew");
        assert_eq!(xml_text(&tree, "Settings/RestartOnFailure/Interval"), "PT1M");
        assert_eq!(xml_text(&tree, "Settings/RestartOnFailure/Count"), "999");
        assert_eq!(xml_text(&tree, "Actions/Exec/WorkingDirectory"), ROOT);
        assert_eq!(xml_text(&tree, "RegistrationInfo/URI"), r"\sw-p8-test-4242");
    }
    assert!(parse_xml("<a><b></a>").is_err(), "不成對標籤必須拒絕");
    let missing = std::panic::catch_unwind(|| {
        let tree = parse_xml("<Task><Settings/></Task>").unwrap();
        xml_text(&tree, "Settings/ExecutionTimeLimit")
    });
    assert!(missing.is_err(), "缺元素必須 panic，不得回空字串");
}

#[test]
fn support_json_parser_reads_health_status() {
    let line = r#"{"id":1,"ok":true,"pid":4321,"sessions":[],"test_device_allowlist":{"enabled":true,"coms":["COM250"],"source":"env"},"note":"\u4e2d\"x\"","n":-1.5e2,"nil":null}"#;
    let j = parse_json(line).expect("JSON 可解析");
    assert_eq!(j.get("pid").and_then(Json::as_u32), Some(4321));
    assert_eq!(j.get("sessions").and_then(Json::as_arr).map(<[Json]>::len), Some(0));
    let allow = j.get("test_device_allowlist").expect("allowlist");
    assert_eq!(allow.get("source").and_then(Json::as_str), Some("env"));
    assert_eq!(j.get("note").and_then(Json::as_str), Some("中\"x\""));
    assert_eq!(j.get("n"), Some(&Json::Num(-150.0)));
    assert_eq!(j.get("nil"), Some(&Json::Null));
    assert!(parse_json(r#"{"ok":true"#).is_err());
    assert!(parse_json(r#"{"ok":true} x"#).is_err());
}

#[test]
fn support_wal_tail_detects_torn_and_crc_mismatch() {
    assert_eq!(crc32(b"123456789"), 0xcbf4_3926, "IEEE CRC-32 標準向量");
    assert_eq!(b64_decode("aGVsbG8="), Some(b"hello".to_vec()));
    let dir = tempfile::tempdir().expect("tempdir");
    let wal = dir.path().join("raw.wal.ndjson");
    assert_eq!(wal_tail_state(dir.path()), WalTail::Absent);
    std::fs::write(&wal, b"").unwrap();
    assert_eq!(wal_tail_state(dir.path()), WalTail::Empty);
    let rec = |seq: u32, payload: &[u8], crc: u32| {
        format!(
            r#"{{"seq":{seq},"payload_b64":"{}","crc32":"{crc:08x}"}}"#,
            b64(payload)
        )
    };
    let good = format!("{}\n{}\n", rec(1, b"ls\n", crc32(b"ls\n")), rec(2, b"ok", crc32(b"ok")));
    std::fs::write(&wal, &good).unwrap();
    assert_eq!(wal_tail_state(dir.path()), WalTail::Complete { records: 2, last_seq: 2 });
    assert!(wal_last_line_crc_ok(dir.path()));
    std::fs::write(&wal, good.replace('\n', "\r\n")).unwrap();
    assert_eq!(wal_tail_state(dir.path()), WalTail::Complete { records: 2, last_seq: 2 }, "接受舊 Windows 檔的 CRLF（BC-01）");
    std::fs::write(&wal, &good[..good.len() - 5]).unwrap();
    assert!(matches!(wal_tail_state(dir.path()), WalTail::Torn(_)), "截斷的最後一行必須判 Torn");
    assert!(!wal_last_line_crc_ok(dir.path()));
    std::fs::write(&wal, format!("{}\n", rec(3, b"ok", crc32(b"ok") ^ 1))).unwrap();
    assert!(matches!(wal_tail_state(dir.path()), WalTail::Torn(_)), "crc 不符必須判 Torn");
}

#[test]
fn wal_without_records_is_insufficient_evidence() {
    // 審查 P8X-e66ed8d-1：沒有 WAL 或 WAL 為空時沒有「最後一筆」可驗，不得判完整（證據不足，不得計為 PASS）。
    let dir = tempfile::tempdir().expect("tempdir");
    let wal = dir.path().join("raw.wal.ndjson");
    assert!(!wal_last_line_crc_ok(dir.path()), "沒有 raw.wal.ndjson 不得判最後一筆完整");
    let absent = wal_evidence(dir.path()).expect_err("沒有 WAL 必須回證據不足");
    assert!(absent.contains("證據不足"), "{absent}");
    std::fs::write(&wal, b"").unwrap();
    assert!(!wal_last_line_crc_ok(dir.path()), "空 WAL 不得判最後一筆完整");
    let empty = wal_evidence(dir.path()).expect_err("空 WAL 必須回證據不足");
    assert!(empty.contains("證據不足"), "{empty}");
    let rec = |seq: u32, payload: &[u8], crc: u32| {
        format!(r#"{{"seq":{seq},"payload_b64":"{}","crc32":"{crc:08x}"}}"#, b64(payload))
    };
    let good = format!("{}\n{}\n", rec(1, b"ls\n", crc32(b"ls\n")), rec(7, b"ok", crc32(b"ok")));
    std::fs::write(&wal, &good).unwrap();
    assert_eq!(wal_evidence(dir.path()), Ok((2, 7)));
    assert!(wal_last_line_crc_ok(dir.path()));
    std::fs::write(&wal, &good[..good.len() - 5]).unwrap();
    let torn = wal_evidence(dir.path()).expect_err("截斷的最後一行必須失敗");
    assert!(!torn.contains("證據不足"), "截斷是失敗、不是證據不足：{torn}");
    assert!(!wal_last_line_crc_ok(dir.path()));
}

#[test]
fn wal_test_record_evidence_requires_the_staging_daemon_record() {
    let dir = tempfile::tempdir().expect("tempdir");
    let wal = dir.path().join("raw.wal.ndjson");
    let crc = crc32(TEST_WAL_PAYLOAD);
    let row = |source: &str| {
        format!(
            r#"{{"seq":4,"com":"COM250","dir":"RX","source":"{source}","payload_b64":"{}","crc32":"{crc:08x}"}}"#,
            b64(TEST_WAL_PAYLOAD)
        )
    };
    std::fs::write(&wal, format!("{}\n", row("test"))).unwrap();
    assert_eq!(wal_test_record_evidence(dir.path()), Ok((1, 4)));

    std::fs::write(&wal, format!("{}\n", row("uart"))).unwrap();
    let wrong_source = wal_test_record_evidence(dir.path()).expect_err("一般 UART 紀錄不能代替 staging hook 證據");
    assert!(wrong_source.contains("指定測試紀錄"), "{wrong_source}");
}

/// sw-cli `ScopedStopReport::to_json` 的實際輸出（`crates/sw-cli/tests/wintask_scoped_stop.rs` 以同一字串驗證產生端）。
const REPORT_JSON: &str = r#"{"ran":[{"argv":["schtasks","/Change","/TN","sw-p8-test-4242","/DISABLE"],"rc":0},{"argv":["schtasks","/Change","/TN","sw-p8-test-4242","/ENABLE"],"rc":0}],"rc":0,"refused":[],"rpc":["health.status","daemon.stop","health.ping"],"stderr":"","stdout":"{\"action\":\"stop\",\"graceful\":true,\"mode\":\"windows-task\",\"ok\":true}\n"}"#;

#[test]
fn harness_output_parses_formal_stop_report() {
    let r = parse_harness_output(&format!("{REPORT_JSON}\r\n")).expect("harness 輸出可解析");
    assert_eq!(r.cli_rc, 0);
    assert!(r.ok);
    assert_eq!(r.graceful, Some(true));
    assert!(r.sent_stop, "daemon.stop 已送出");
    assert!(!r.ran_end, "正常結束時沒有 /End");
    assert!(r.refused.is_empty());
    let name = |v: &str| vec!["schtasks".to_string(), "/Change".into(), "/TN".into(), "sw-p8-test-4242".into(), v.into()];
    assert_eq!(r.ran, vec![(name("/DISABLE"), 0), (name("/ENABLE"), 0)]);
    assert_eq!(r.rpc, vec!["health.status".to_string(), "daemon.stop".into(), "health.ping".into()]);
    assert_eq!(r.cli_stdout, "{\"action\":\"stop\",\"graceful\":true,\"mode\":\"windows-task\",\"ok\":true}\n");

    // 執行了 /End、CLI 失敗、harness 拒絕過命令：逐項反映，不得被當成正常停止。
    let hard = REPORT_JSON
        .replace(r#""rc":0,"refused":[]"#, r#""rc":2,"refused":["sc.exe stop serialwrap"]"#)
        .replace(r#"{"argv":["schtasks","/Change","/TN","sw-p8-test-4242","/ENABLE"],"rc":0}"#, r#"{"argv":["schtasks","/End","/TN","sw-p8-test-4242"],"rc":1}"#)
        .replace(r#"\"graceful\":true"#, r#"\"graceful\":false"#)
        .replace(r#"\"ok\":true"#, r#"\"ok\":false"#)
        .replace(r#""daemon.stop","#, "");
    let r = parse_harness_output(&hard).expect("harness 輸出可解析");
    assert_eq!(r.cli_rc, 2);
    assert!(!r.ok);
    assert_eq!(r.graceful, Some(false));
    assert!(r.ran_end);
    assert!(!r.sent_stop);
    assert_eq!(r.refused, vec!["sc.exe stop serialwrap".to_string()]);
    // exit 0 但回應 ok:false 仍不是成功；ok:true 但 exit≠0 亦然。
    let mixed = REPORT_JSON.replace(r#""rc":0,"refused""#, r#""rc":2,"refused""#);
    assert!(!parse_harness_output(&mixed).unwrap().ok);
    for bad in ["", "\r\n", "not json", r#"{"rc":0}"#, r#"{"ran":[],"rc":0,"refused":[],"rpc":[],"stderr":"","stdout":"not json"}"#] {
        assert!(parse_harness_output(bad).is_err(), "不完整的 harness 輸出必須拒絕：{bad:?}");
    }
}

#[test]
fn harness_argv_scopes_cli_to_staging_and_test_port() {
    let s = spec();
    assert_eq!(s.render_cli_config(), CLI_CONFIG);
    assert_eq!(CLI_CONFIG, "supervision_mode: windows-task\n", "正式 service stop 必須走 windows-task 分支");
    assert!(under_root(&s.cli_config_path()), "{}", s.cli_config_path());
    let cli_dir = s.cli_config_path().rsplit_once('\\').map(|(d, _)| d.to_string()).unwrap();
    assert!(s.dirs().contains(&cli_dir), "CLI config 目錄必須事先建立：{cli_dir}");
    assert_ne!(s.cli_config_path(), s.config_yaml(), "daemon 的 staging config 不變");

    let harness = format!(r"{ROOT}\bin\{HARNESS_EXE}");
    let argv = s.harness_argv(&harness, "sw-p8-test-4242");
    assert_eq!(argv[..5], [harness.clone(), "--task".into(), "sw-p8-test-4242".into(), "--port".into(), PORT.to_string()]);
    let rest = &argv[5..];
    assert_eq!(rest.len() % 2, 0, "{rest:?}");
    let vars: Vec<(String, String)> = rest
        .chunks(2)
        .map(|c| {
            assert_eq!(c[0], "--var", "{rest:?}");
            let (k, v) = c[1].split_once('=').expect("K=V");
            (k.to_string(), v.to_string())
        })
        .collect();
    assert_eq!(vars, s.harness_vars());
    let get = |k: &str| vars.iter().find(|(key, _)| key == k).map(|(_, v)| v.clone());
    assert_eq!(get("SERIALWRAP_CONFIG"), Some(s.cli_config_path()));
    for key in ["SERIALWRAP_CONFIG_DIR", "SERIALWRAP_STATE_DIR", "SERIALWRAP_RUN_DIR", "SERIALWRAP_WAL_DIR", "SERIALWRAP_LOG_DIR"] {
        let v = get(key).unwrap_or_else(|| panic!("缺 {key}"));
        assert!(under_root(&v), "{key}={v}");
    }
    for (k, v) in &vars {
        assert!(k.starts_with("SERIALWRAP_"), "只傳 SERIALWRAP_* 給 harness：{k}");
        assert!(under_root(v), "{k}={v} 必須落在 staging");
        assert!(!v.contains("48700"), "{k}={v}");
    }
    for reserved in ["SERIALWRAP_TCP_PORT", "SERIALWRAP_SOCKET", "SERIALWRAP_TEST_DEVICE_ALLOWLIST"] {
        assert_eq!(get(reserved), None, "{reserved} 不得由 support 指定");
    }
    for bad in ["serialwrap", "Serialwrap", "sw-p8-test-x"] {
        let s = s.clone();
        let h = harness.clone();
        assert!(std::panic::catch_unwind(move || s.harness_argv(&h, bad)).is_err(), "{bad} 必須拒絕");
    }
}

fn b64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(T[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

#[test]
fn win_runner_forwards_schtasks_authorization() {
    // 實跑的授權旗標必須經 WSLENV 轉送到 Windows 行程；未設定時不得預設（runner 只轉送已存在的變數）。
    let runner = include_str!("../../../scripts/rust/win-runner.sh");
    let line = runner
        .lines()
        .find(|l| l.starts_with("for name in ") && l.contains("SW_TEST_SERIALWRAPD"))
        .expect("win-runner.sh 的 WSLENV 轉送清單");
    assert!(line.split_whitespace().any(|w| w == "SW_TEST_ALLOW_SCHTASKS"), "{line}");
    assert!(!runner.contains("SW_TEST_ALLOW_SCHTASKS=1"), "runner 不得自行授權");
}

#[test]
fn staging_pid_match_uses_full_exe_path_only() {
    // 正式 daemon 與其他使用者（ExecutablePath 讀不到）的行程絕不匹配；比對不分大小寫與 `/`、`\`。
    let listing = "4242|C:\\Users\\tester\\AppData\\Local\\Temp\\sw-p8-test-4242\\bin\\serialwrapd.exe\r\n\
                   77|C:\\Users\\tester\\.local\\share\\serialwrap\\bin\\serialwrapd.exe\r\n\
                   88|\r\n\
                   99|c:/users/TESTER/appdata/local/temp/sw-p8-test-4242/BIN/serialwrapd.exe\r\n";
    let exe = format!(r"{ROOT}\bin\serialwrapd.exe");
    assert_eq!(matching_exe_pids(listing, &exe), Ok(vec![4242, 99]));
    assert_eq!(matching_exe_pids("", &exe), Ok(vec![]));
    assert!(matching_exe_pids("not-a-row\r\n", &exe).is_err(), "無法解析的列必須拒絕，不得當成無殘留");
}
