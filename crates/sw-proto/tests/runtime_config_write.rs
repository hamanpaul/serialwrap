//! `RuntimeConfig` YAML 寫入與 `set_mode`／`set_socket` 測試（P4-T6）。
//!
//! 依據 `docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 6 規範撰寫。

use sw_proto::config_types::RuntimeConfig;
use sw_proto::yaml11::YValue;

/// P4-T6 六個 config 形狀案例及其 PyYAML 6.0.3 safe_dump(allow_unicode=true, sort_keys=true) 期望值。
const CONFIG_CASES: &[(&str, &str)] = &[
    // 1. 空 dict
    ("{}", "{}\n"),
    // 2. 只有 mode
    (
        "supervision_mode: on-demand\n",
        "supervision_mode: on-demand\n",
    ),
    // 3. mode＋tcp socket
    (
        "supervision_mode: windows-task\nsocket_path: tcp://127.0.0.1:48700\n",
        "socket_path: tcp://127.0.0.1:48700\nsupervision_mode: windows-task\n",
    ),
    // 4. 含 windows: {exclude_coms: [COM3, COM10]}
    (
        "windows:\n  exclude_coms:\n  - COM3\n  - COM10\n",
        "windows:\n  exclude_coms:\n  - COM3\n  - COM10\n",
    ),
    // 5. 含中文值
    (
        "supervision_mode: systemd-user\ngreeting: 你好世界\n",
        "greeting: 你好世界\nsupervision_mode: systemd-user\n",
    ),
    // 6. 含需引號的字串 \"on\"
    (
        "supervision_mode: on-demand\ntag: 'on'\n",
        "supervision_mode: on-demand\ntag: 'on'\n",
    ),
];

#[test]
fn runtime_config_to_yaml_matches_pyyaml_golden() {
    for (idx, (input_yaml, expected_yaml)) in CONFIG_CASES.iter().enumerate() {
        let cfg = RuntimeConfig::from_text(Some(input_yaml))
            .unwrap_or_else(|e| panic!("第 {idx} 個案例 YAML 解析失敗：{e}"));
        let actual = cfg.to_yaml();
        assert_eq!(
            &actual,
            expected_yaml,
            "第 {idx} 個案例 to_yaml 輸出與 PyYAML golden 不符：\nwant:\n{expected_yaml}\ngot:\n{actual}"
        );
    }
}

#[test]
fn runtime_config_set_mode_keeps_other_keys() {
    let initial = "windows:\n  exclude_coms:\n  - COM3\n  - COM10\nsocket_path: /run/existing.sock\nsupervision_mode: on-demand\n";
    let mut cfg = RuntimeConfig::from_text(Some(initial)).expect("初始 config 解析失敗");

    // set_mode 保留原有 windows: 與未覆寫的 socket_path（socket_path 為 None 時不動）
    cfg.set_mode("systemd-user", None);
    assert_eq!(
        cfg.mode().and_then(|v| match v {
            YValue::Str(s) => Some(s.as_str()),
            _ => None,
        }),
        Some("systemd-user")
    );
    assert_eq!(
        cfg.socket_path().and_then(|v| match v {
            YValue::Str(s) => Some(s.as_str()),
            _ => None,
        }),
        Some("/run/existing.sock"),
        "socket_path 在 None 時不動"
    );
    assert!(
        cfg.data.get_str("windows").is_some(),
        "原有 windows: 欄位必須保留"
    );

    // socket_path Some 才覆寫
    cfg.set_mode("windows-task", Some("tcp://127.0.0.1:48700"));
    assert_eq!(
        cfg.mode().and_then(|v| match v {
            YValue::Str(s) => Some(s.as_str()),
            _ => None,
        }),
        Some("windows-task")
    );
    assert_eq!(
        cfg.socket_path().and_then(|v| match v {
            YValue::Str(s) => Some(s.as_str()),
            _ => None,
        }),
        Some("tcp://127.0.0.1:48700")
    );
    assert!(
        cfg.data.get_str("windows").is_some(),
        "原有 windows: 欄位必須保留"
    );

    // set_socket 單獨更新 socket_path
    cfg.set_socket("tcp://127.0.0.1:48701");
    assert_eq!(
        cfg.socket_path().and_then(|v| match v {
            YValue::Str(s) => Some(s.as_str()),
            _ => None,
        }),
        Some("tcp://127.0.0.1:48701")
    );
    assert_eq!(
        cfg.mode().and_then(|v| match v {
            YValue::Str(s) => Some(s.as_str()),
            _ => None,
        }),
        Some("windows-task")
    );
}
