//! argparse 宣告式規格（依 CPython 3.12.3 的 `sw_core.cli:build_parser` 轉寫）。
use crate::argparse::{
    ActionKind, ActionSpec, ArgValue, Conv, MutexSpec, Nargs, ParserSpec, SubTarget,
    SubparserEntry, SubparsersSpec,
};

pub struct SpecDefaults {
    pub version_line: String,
    pub profile_dir: String,
    /// `daemon start --lock` 預設（Python `LOCK_PATH`；由 `RuntimePaths::lock_path` 提供）。
    pub lock_path: String,
}

pub const GROUP_ORDER: [&str; 20] = [
    "daemon",
    "device",
    "session",
    "alias",
    "cmd",
    "stream",
    "log",
    "file",
    "wal",
    "mcu",
    "remote",
    "connect",
    "benches",
    "event",
    "supervision-mode",
    "service",
    "setup",
    "doctor",
    "profile",
    "skill",
];

fn spec_device() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap device".to_string(),
        description: Some("管理實體 UART 裝置：列舉裝置，以及把 raw device 暫時交給外部工具獨佔再收回。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: Some(SubparsersSpec {
            dest: "device_cmd", required: true, title: None, metavar: Some("<command>"),
            entries: vec![
                SubparserEntry { name: "list", help: Some("列出實體 UART 裝置（real_path 與 by-id）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap device list".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "release", help: Some("釋放 raw 裝置給外部工具獨佔（如 MCU 燒錄），進入 RELEASED 不自動搶回"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap device release".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--source"], dest: "source", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("cli".to_string()), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--reason"], dest: "reason", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "attach", help: Some("收回先前 release 的裝置並重建 console（外部仍持有時回 DEVICE_STILL_HELD，--force 略過）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap device attach".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--force"], dest: "force", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
            ],
        }),
    }
}

fn spec_session() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap session".to_string(),
        description: Some("管理 session：列舉與綁定、健康探測（self-test）、recover、console 與 interactive lease、capture log。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: Some(SubparsersSpec {
            dest: "session_cmd", required: true, title: None, metavar: Some("<command>"),
            entries: vec![
                SubparserEntry { name: "list", help: Some("列出所有 session 及其狀態"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session list".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "clear", help: Some("清除 session（detach 後會自動 re-attach；交接外部請改用 device release）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session clear".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "bind", help: Some("把 session 綁定到指定裝置 by-id"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session bind".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--device-by-id"], dest: "device_by_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "pin", help: Some("把 device 釘到指定 profile（最高優先，繞過偵測）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session pin".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias | by-id | by-path".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--profile"], dest: "profile", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("要釘的 profile/template 名".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "unpin", help: Some("解除 device 的 profile pin（保留 sticky）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session unpin".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias | by-id | by-path".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "attach", help: Some("將 session attach 到裝置並建立 bridge"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session attach".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "self-test", help: Some("探測 session 健康度，回報 classification 與 recommended_action"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session self-test".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--probe-timeout"], dest: "probe_timeout_s", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Float, default: ArgValue::Float(2.0), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--strict-human-lock"], dest: "strict_human_lock", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("嚴格模式：若 human interactive lease 仍在使用中則直接回報 busy；預設模式會先暫停 human interactive lease 再做 probe".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "activity", help: Some("顯示 session 的 RX／TX／state 活動"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session activity".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "recover", help: Some("重建 bridge 修復不健康的 session（TARGET_UNRESPONSIVE 時用這個，非 device attach）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session recover".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--timeout"], dest: "recover_timeout_s", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Float, default: ArgValue::Float(2.0), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--force"], dest: "force", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("force clear+reattach if normal recovery fails".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "console-attach", help: Some("附加一個 console reader 到 session"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session console-attach".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--label"], dest: "label", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "console-detach", help: Some("卸除指定的 console reader"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session console-detach".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--client-id"], dest: "client_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "console-list", help: Some("列出 session 上的 console readers"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session console-list".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "interactive-open", help: Some("開啟 interactive lease（給全螢幕互動程式用）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session interactive-open".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--owner"], dest: "owner", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("agent".to_string()), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--timeout"], dest: "interactive_timeout_s", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Float, default: ArgValue::Float(60.0), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--command"], dest: "command", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("".to_string()), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--allow-attached"], dest: "allow_attached", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("允許在 ATTACHED 狀態下開啟 bootloader recovery lease（需通過 bootloader prompt 比對）。 若 session 已有 human interactive lease 則暫停並在 close 時恢復。".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "interactive-send", help: Some("送出按鍵／資料到 interactive lease"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session interactive-send".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--interactive-id"], dest: "interactive_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--data"], dest: "data", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--encoding"], dest: "encoding", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("plain".to_string()), choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "interactive-status", help: Some("讀取 interactive lease 目前畫面與狀態"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session interactive-status".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--interactive-id"], dest: "interactive_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--screen-chars"], dest: "screen_chars", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(2048)), choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "interactive-close", help: Some("關閉 interactive lease"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session interactive-close".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--interactive-id"], dest: "interactive_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "log-start", help: Some("開始該 session 的 capture log"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session log-start".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "log-stop", help: Some("停止該 session 的 capture log"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session log-stop".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "log-status", help: Some("查詢該 session 的 capture log 狀態"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap session log-status".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("session_id | COMx | alias".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
            ],
        }),
    }
}

fn spec_alias() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap alias".to_string(),
        description: Some(
            "管理 session 別名：列舉、指定到 session_id、綁定裝置 by-id 與解除綁定。",
        ),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![ActionSpec {
            option_strings: vec!["-h", "--help"],
            dest: "help",
            kind: ActionKind::Help,
            nargs: Nargs::One,
            conv: Conv::Str,
            default: ArgValue::None,
            choices: None,
            required: false,
            help: Some("show this help message and exit".to_string()),
            metavar: None,
            version: None,
        }],
        mutex: vec![],
        sub: Some(SubparsersSpec {
            dest: "alias_cmd",
            required: true,
            title: None,
            metavar: Some("<command>"),
            entries: vec![
                SubparserEntry {
                    name: "list",
                    help: Some("列出所有 alias 綁定"),
                    target: SubTarget::Native(Box::new(ParserSpec {
                        prog: "serialwrap alias list".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![ActionSpec {
                            option_strings: vec!["-h", "--help"],
                            dest: "help",
                            kind: ActionKind::Help,
                            nargs: Nargs::One,
                            conv: Conv::Str,
                            default: ArgValue::None,
                            choices: None,
                            required: false,
                            help: Some("show this help message and exit".to_string()),
                            metavar: None,
                            version: None,
                        }],
                        mutex: vec![],
                        sub: None,
                    })),
                },
                SubparserEntry {
                    name: "set",
                    help: Some("把 alias 指定到既有 session_id"),
                    target: SubTarget::Native(Box::new(ParserSpec {
                        prog: "serialwrap alias set".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![
                            ActionSpec {
                                option_strings: vec!["-h", "--help"],
                                dest: "help",
                                kind: ActionKind::Help,
                                nargs: Nargs::One,
                                conv: Conv::Str,
                                default: ArgValue::None,
                                choices: None,
                                required: false,
                                help: Some("show this help message and exit".to_string()),
                                metavar: None,
                                version: None,
                            },
                            ActionSpec {
                                option_strings: vec!["--session-id"],
                                dest: "session_id",
                                kind: ActionKind::Store,
                                nargs: Nargs::One,
                                conv: Conv::Str,
                                default: ArgValue::None,
                                choices: None,
                                required: true,
                                help: None,
                                metavar: None,
                                version: None,
                            },
                            ActionSpec {
                                option_strings: vec!["--alias"],
                                dest: "alias",
                                kind: ActionKind::Store,
                                nargs: Nargs::One,
                                conv: Conv::Str,
                                default: ArgValue::None,
                                choices: None,
                                required: true,
                                help: None,
                                metavar: None,
                                version: None,
                            },
                        ],
                        mutex: vec![],
                        sub: None,
                    })),
                },
                SubparserEntry {
                    name: "assign",
                    help: Some("把 alias 綁到裝置 by-id（可附 profile）"),
                    target: SubTarget::Native(Box::new(ParserSpec {
                        prog: "serialwrap alias assign".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![
                            ActionSpec {
                                option_strings: vec!["-h", "--help"],
                                dest: "help",
                                kind: ActionKind::Help,
                                nargs: Nargs::One,
                                conv: Conv::Str,
                                default: ArgValue::None,
                                choices: None,
                                required: false,
                                help: Some("show this help message and exit".to_string()),
                                metavar: None,
                                version: None,
                            },
                            ActionSpec {
                                option_strings: vec!["--by-id"],
                                dest: "by_id",
                                kind: ActionKind::Store,
                                nargs: Nargs::One,
                                conv: Conv::Str,
                                default: ArgValue::None,
                                choices: None,
                                required: true,
                                help: None,
                                metavar: None,
                                version: None,
                            },
                            ActionSpec {
                                option_strings: vec!["--alias"],
                                dest: "alias",
                                kind: ActionKind::Store,
                                nargs: Nargs::One,
                                conv: Conv::Str,
                                default: ArgValue::None,
                                choices: None,
                                required: true,
                                help: None,
                                metavar: None,
                                version: None,
                            },
                            ActionSpec {
                                option_strings: vec!["--profile"],
                                dest: "profile",
                                kind: ActionKind::Store,
                                nargs: Nargs::One,
                                conv: Conv::Str,
                                default: ArgValue::None,
                                choices: None,
                                required: false,
                                help: None,
                                metavar: None,
                                version: None,
                            },
                        ],
                        mutex: vec![],
                        sub: None,
                    })),
                },
                SubparserEntry {
                    name: "unassign",
                    help: Some("移除 alias 綁定"),
                    target: SubTarget::Native(Box::new(ParserSpec {
                        prog: "serialwrap alias unassign".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![
                            ActionSpec {
                                option_strings: vec!["-h", "--help"],
                                dest: "help",
                                kind: ActionKind::Help,
                                nargs: Nargs::One,
                                conv: Conv::Str,
                                default: ArgValue::None,
                                choices: None,
                                required: false,
                                help: Some("show this help message and exit".to_string()),
                                metavar: None,
                                version: None,
                            },
                            ActionSpec {
                                option_strings: vec!["--alias"],
                                dest: "alias",
                                kind: ActionKind::Store,
                                nargs: Nargs::One,
                                conv: Conv::Str,
                                default: ArgValue::None,
                                choices: None,
                                required: true,
                                help: None,
                                metavar: None,
                                version: None,
                            },
                        ],
                        mutex: vec![],
                        sub: None,
                    })),
                },
            ],
        }),
    }
}

fn spec_cmd() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap cmd".to_string(),
        description: Some("向 session 提交命令並取回結果：line 模式看 status，background 模式用 result-tail。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: Some(SubparsersSpec {
            dest: "cmd_cmd", required: true, title: None, metavar: Some("<command>"),
            entries: vec![
                SubparserEntry { name: "submit", help: Some("提交命令到 session（--mode line|background|interactive）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap cmd submit".to_string(),
        description: Some("提交命令到 session（--mode line|background|interactive）。\n\n命令長度限制（--cmd 整條字串，以 UTF-8 位元組計；#129）：\n  - > 4096 bytes：接受但附 CMD_LENGTH_WARNING（過長命令可能造成 UART 緩衝溢位或 prompt timeout）\n  - > 16384 bytes：直接拒絕（CMD_TOO_LONG）\n  - 含 \\n 換行字元：直接拒絕（CMD_CONTAINS_NEWLINE），請拆成多次獨立提交\n\nbroker 對命令內容不做截斷；上述為 broker 對單一 --cmd 參數的上限，\ntarget 端 tty line buffer（常見 4096 bytes，target-dependent）才是物理單行限制。\n上限可由 `serialwrap daemon status` 回應的 limits 欄位執行期查詢，client 不需硬編碼。"),
        epilog: None,
        raw_text: true,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--cmd"], dest: "command_text", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("".to_string()), choices: None, required: false, help: Some("要執行的單行命令（UTF-8 > 4096 bytes warning、> 16384 bytes 拒絕，詳見上方說明）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--source"], dest: "source", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("agent".to_string()), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--mode"], dest: "mode", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("line".to_string()), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--priority"], dest: "priority", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(10)), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--cmd-timeout"], dest: "cmd_timeout_s", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Float, default: ArgValue::Float(10.0), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--expected-duration"], dest: "expected_duration_s", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Float, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--completion-regex"], dest: "completion_regex", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("每命令完成 regex（最多 1024 UTF-8 bytes）；省略則等待 profile prompt".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "status", help: Some("查詢命令狀態與 stdout（line 模式讀這裡）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap cmd status".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--cmd-id"], dest: "cmd_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "result-tail", help: Some("增量讀取 background 命令的結果 chunk"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap cmd result-tail".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--cmd-id"], dest: "cmd_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--from-chunk"], dest: "from_chunk", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(0)), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--limit"], dest: "limit", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(200)), choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "cancel", help: Some("取消執行中的命令"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap cmd cancel".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--cmd-id"], dest: "cmd_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
            ],
        }),
    }
}

fn spec_stream() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap stream".to_string(),
        description: Some("即時 tail session 解析後的文字串流（line 事件）。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![ActionSpec {
            option_strings: vec!["-h", "--help"],
            dest: "help",
            kind: ActionKind::Help,
            nargs: Nargs::One,
            conv: Conv::Str,
            default: ArgValue::None,
            choices: None,
            required: false,
            help: Some("show this help message and exit".to_string()),
            metavar: None,
            version: None,
        }],
        mutex: vec![],
        sub: Some(SubparsersSpec {
            dest: "stream_cmd",
            required: true,
            title: None,
            metavar: Some("<command>"),
            entries: vec![SubparserEntry {
                name: "tail",
                help: Some("即時 tail 解析後的文字串流"),
                target: SubTarget::Native(Box::new(ParserSpec {
                    prog: "serialwrap stream tail".to_string(),
                    description: None,
                    epilog: None,
                    raw_text: false,
                    raw_description: false,
                    add_help: true,
                    allow_abbrev: true,
                    actions: vec![
                        ActionSpec {
                            option_strings: vec!["-h", "--help"],
                            dest: "help",
                            kind: ActionKind::Help,
                            nargs: Nargs::One,
                            conv: Conv::Str,
                            default: ArgValue::None,
                            choices: None,
                            required: false,
                            help: Some("show this help message and exit".to_string()),
                            metavar: None,
                            version: None,
                        },
                        ActionSpec {
                            option_strings: vec!["--selector"],
                            dest: "selector",
                            kind: ActionKind::Store,
                            nargs: Nargs::One,
                            conv: Conv::Str,
                            default: ArgValue::None,
                            choices: None,
                            required: false,
                            help: None,
                            metavar: None,
                            version: None,
                        },
                        ActionSpec {
                            option_strings: vec!["--com"],
                            dest: "com",
                            kind: ActionKind::Store,
                            nargs: Nargs::One,
                            conv: Conv::Str,
                            default: ArgValue::None,
                            choices: None,
                            required: false,
                            help: None,
                            metavar: None,
                            version: None,
                        },
                        ActionSpec {
                            option_strings: vec!["--from-seq"],
                            dest: "from_seq",
                            kind: ActionKind::Store,
                            nargs: Nargs::One,
                            conv: Conv::Int,
                            default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(0)),
                            choices: None,
                            required: false,
                            help: None,
                            metavar: None,
                            version: None,
                        },
                        ActionSpec {
                            option_strings: vec!["--limit"],
                            dest: "limit",
                            kind: ActionKind::Store,
                            nargs: Nargs::One,
                            conv: Conv::Int,
                            default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(200)),
                            choices: None,
                            required: false,
                            help: None,
                            metavar: None,
                            version: None,
                        },
                    ],
                    mutex: vec![],
                    sub: None,
                })),
            }],
        }),
    }
}

fn spec_log() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap log".to_string(),
        description: Some("tail raw 或純文字日誌；raw 含 timestamp／source／seq／crc，可做回放與稽核。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: Some(SubparsersSpec {
            dest: "log_cmd", required: true, title: None, metavar: Some("<command>"),
            entries: vec![
                SubparserEntry { name: "tail-raw", help: Some("tail raw 日誌（含 timestamp／source／seq／crc）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap log tail-raw".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--com"], dest: "com", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--from-seq"], dest: "from_seq", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::None, choices: None, required: false, help: Some("省略＝latest 模式（回傳最新 N 筆）；指定 N（含 0）＝range 模式（自 seq>N 起最舊 N 筆，增量讀取用）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--limit"], dest: "limit", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(200)), choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "tail-text", help: Some("tail 純文字日誌"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap log tail-text".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--com"], dest: "com", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--from-seq"], dest: "from_seq", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::None, choices: None, required: false, help: Some("省略＝latest 模式（回傳最新 N 筆）；指定 N（含 0）＝range 模式（自 seq>N 起最舊 N 筆，增量讀取用）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--limit"], dest: "limit", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(200)), choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
            ],
        }),
    }
}

fn spec_file() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap file".to_string(),
        description: Some("透過 UART 在本機與 target 之間推送（push）或拉取（pull）檔案。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: Some(SubparsersSpec {
            dest: "file_cmd", required: true, title: None, metavar: Some("<command>"),
            entries: vec![
                SubparserEntry { name: "push", help: Some("透過 UART 推送本機檔案到 target"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap file push".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--local"], dest: "local", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--remote"], dest: "remote", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--chunk-size"], dest: "chunk_size", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(512)), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--chunk-timeout"], dest: "chunk_timeout_s", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Float, default: ArgValue::None, choices: None, required: false, help: Some("單一 chunk 等待 target 回應的逾時秒數（預設：沿用 profile timeout_s，#157）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--ack-mode"], dest: "ack_mode", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("auto".to_string()), choices: Some(vec!["auto", "echo", "none"]), required: false, help: Some("chunk 命令行送出方式（#161）：auto=有 echo-ACK 原語即節流（預設）、echo=強制 echo-ACK、none=legacy 整行送出（急件換吞吐）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--source"], dest: "source", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("agent".to_string()), choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "pull", help: Some("透過 UART 從 target 拉取檔案到本機"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap file pull".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--remote"], dest: "remote", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--local"], dest: "local", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--chunk-timeout"], dest: "chunk_timeout_s", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Float, default: ArgValue::None, choices: None, required: false, help: Some("整段 base64 讀取的逾時秒數（預設：沿用 profile timeout_s，#157）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--ack-mode"], dest: "ack_mode", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("auto".to_string()), choices: Some(vec!["auto", "echo", "none"]), required: false, help: Some("介面對齊 file push（#161）；pull 端目前僅驗證、不影響行為".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--source"], dest: "source", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("agent".to_string()), choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
            ],
        }),
    }
}

fn spec_wal() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap wal".to_string(),
        description: Some("操作 write-ahead log（WAL）：匯出區段、重設與查詢目前 seq。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![ActionSpec {
            option_strings: vec!["-h", "--help"],
            dest: "help",
            kind: ActionKind::Help,
            nargs: Nargs::One,
            conv: Conv::Str,
            default: ArgValue::None,
            choices: None,
            required: false,
            help: Some("show this help message and exit".to_string()),
            metavar: None,
            version: None,
        }],
        mutex: vec![],
        sub: Some(SubparsersSpec {
            dest: "wal_cmd",
            required: true,
            title: None,
            metavar: Some("<command>"),
            entries: vec![
                SubparserEntry {
                    name: "export",
                    help: Some("匯出 WAL 區段（--from-seq／--to-seq／--limit）"),
                    target: SubTarget::Native(Box::new(ParserSpec {
                        prog: "serialwrap wal export".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![
                            ActionSpec {
                                option_strings: vec!["-h", "--help"],
                                dest: "help",
                                kind: ActionKind::Help,
                                nargs: Nargs::One,
                                conv: Conv::Str,
                                default: ArgValue::None,
                                choices: None,
                                required: false,
                                help: Some("show this help message and exit".to_string()),
                                metavar: None,
                                version: None,
                            },
                            ActionSpec {
                                option_strings: vec!["--from-seq"],
                                dest: "from_seq",
                                kind: ActionKind::Store,
                                nargs: Nargs::One,
                                conv: Conv::Int,
                                default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(0)),
                                choices: None,
                                required: false,
                                help: None,
                                metavar: None,
                                version: None,
                            },
                            ActionSpec {
                                option_strings: vec!["--to-seq"],
                                dest: "to_seq",
                                kind: ActionKind::Store,
                                nargs: Nargs::One,
                                conv: Conv::Int,
                                default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(0)),
                                choices: None,
                                required: false,
                                help: None,
                                metavar: None,
                                version: None,
                            },
                            ActionSpec {
                                option_strings: vec!["--limit"],
                                dest: "limit",
                                kind: ActionKind::Store,
                                nargs: Nargs::One,
                                conv: Conv::Int,
                                default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(1000)),
                                choices: None,
                                required: false,
                                help: None,
                                metavar: None,
                                version: None,
                            },
                        ],
                        mutex: vec![],
                        sub: None,
                    })),
                },
                SubparserEntry {
                    name: "reset",
                    help: Some("重設 WAL"),
                    target: SubTarget::Native(Box::new(ParserSpec {
                        prog: "serialwrap wal reset".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![ActionSpec {
                            option_strings: vec!["-h", "--help"],
                            dest: "help",
                            kind: ActionKind::Help,
                            nargs: Nargs::One,
                            conv: Conv::Str,
                            default: ArgValue::None,
                            choices: None,
                            required: false,
                            help: Some("show this help message and exit".to_string()),
                            metavar: None,
                            version: None,
                        }],
                        mutex: vec![],
                        sub: None,
                    })),
                },
                SubparserEntry {
                    name: "current-seq",
                    help: Some("顯示目前 WAL seq"),
                    target: SubTarget::Native(Box::new(ParserSpec {
                        prog: "serialwrap wal current-seq".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![ActionSpec {
                            option_strings: vec!["-h", "--help"],
                            dest: "help",
                            kind: ActionKind::Help,
                            nargs: Nargs::One,
                            conv: Conv::Str,
                            default: ArgValue::None,
                            choices: None,
                            required: false,
                            help: Some("show this help message and exit".to_string()),
                            metavar: None,
                            version: None,
                        }],
                        mutex: vec![],
                        sub: None,
                    })),
                },
            ],
        }),
    }
}

fn spec_mcu() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap mcu".to_string(),
        description: Some(
            "查詢 MCU flash pattern 清單與目前 flash 端點狀態（候選 COM port 與 is_flashing）。",
        ),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![ActionSpec {
            option_strings: vec!["-h", "--help"],
            dest: "help",
            kind: ActionKind::Help,
            nargs: Nargs::One,
            conv: Conv::Str,
            default: ArgValue::None,
            choices: None,
            required: false,
            help: Some("show this help message and exit".to_string()),
            metavar: None,
            version: None,
        }],
        mutex: vec![],
        sub: Some(SubparsersSpec {
            dest: "mcu_cmd",
            required: true,
            title: None,
            metavar: Some("<command>"),
            entries: vec![
                SubparserEntry {
                    name: "patterns",
                    help: Some(
                        "列出所有已知 MCU 家族 flash pattern（family／probe／expect／baud）",
                    ),
                    target: SubTarget::Native(Box::new(ParserSpec {
                        prog: "serialwrap mcu patterns".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![ActionSpec {
                            option_strings: vec!["-h", "--help"],
                            dest: "help",
                            kind: ActionKind::Help,
                            nargs: Nargs::One,
                            conv: Conv::Str,
                            default: ArgValue::None,
                            choices: None,
                            required: false,
                            help: Some("show this help message and exit".to_string()),
                            metavar: None,
                            version: None,
                        }],
                        mutex: vec![],
                        sub: None,
                    })),
                },
                SubparserEntry {
                    name: "status",
                    help: Some("顯示 flash 端點狀態：候選 COM port 清單與目前是否 flashing"),
                    target: SubTarget::Native(Box::new(ParserSpec {
                        prog: "serialwrap mcu status".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![ActionSpec {
                            option_strings: vec!["-h", "--help"],
                            dest: "help",
                            kind: ActionKind::Help,
                            nargs: Nargs::One,
                            conv: Conv::Str,
                            default: ArgValue::None,
                            choices: None,
                            required: false,
                            help: Some("show this help message and exit".to_string()),
                            metavar: None,
                            version: None,
                        }],
                        mutex: vec![],
                        sub: None,
                    })),
                },
            ],
        }),
    }
}

fn spec_event() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap event".to_string(),
        description: Some("event-trigger 規則註冊表與 matcher 控制：新增／刪除／列舉／啟用停用規則與檢視觸發紀錄。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: Some(SubparsersSpec {
            dest: "event_cmd", required: true, title: None, metavar: Some("<command>"),
            entries: vec![
                SubparserEntry { name: "add", help: Some("從 JSON 檔註冊或更新一條規則"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap event add".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--file"], dest: "file", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "rm", help: Some("依 id 刪除一條規則"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap event rm".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec![], dest: "rule_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "list", help: Some("列出規則（可依 selector／owner 過濾）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap event list".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--owner"], dest: "owner", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "show", help: Some("依 id 顯示單一規則內容"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap event show".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec![], dest: "rule_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "enable", help: Some("啟用指定 selector 的規則 matcher"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap event enable".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "disable", help: Some("停用指定 selector 的規則 matcher"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap event disable".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "status", help: Some("顯示 matcher 狀態（可依 selector 過濾）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap event status".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "reset", help: Some("重設規則計數／狀態（--rule-id 或 --selector 擇一）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap event reset".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--rule-id"], dest: "rule_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
            MutexSpec { required: true, dests: vec!["rule_id", "selector"] },
        ],
        sub: None,
    })) },
                SubparserEntry { name: "reload", help: Some("重新載入規則註冊表"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap event reload".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
                SubparserEntry { name: "tail", help: Some("檢視規則觸發紀錄（可依 rule-id／selector 過濾）"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap event tail".to_string(),
        description: None,
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--rule-id"], dest: "rule_id", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--selector"], dest: "selector", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["-n"], dest: "n", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(50)), choices: None, required: false, help: None, metavar: None, version: None },
            ActionSpec { option_strings: vec!["--since"], dest: "since", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::None, choices: None, required: false, help: None, metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
            ],
        }),
    }
}

fn spec_profile(d: &SpecDefaults) -> ParserSpec {
    ParserSpec {
        prog: "serialwrap profile".to_string(),
        description: Some("離線驗證 profile 的 prompt/login/password/bootloader regex 是否命中一段樣本文字（#174）。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: Some(SubparsersSpec {
            dest: "profile_cmd", required: true, title: None, metavar: Some("<command>"),
            entries: vec![
                SubparserEntry { name: "test", help: Some("對樣本文字逐一跑 prompt/login/password/bootloader regex，回報是否命中"), target: SubTarget::Native(Box::new(ParserSpec {
        prog: "serialwrap profile test".to_string(),
        description: Some("從 --profile-dir 載入 profile YAML，找出 --profile 指定的 template，\n對 --sample 檔案內容依序跑 prompt_regex／login_regex／password_regex／\nbootloader_prompts（逐條），stdout 印 JSON 回報各 regex 是否命中與命中的行。\n純離線診斷：不連 daemon、不碰任何 UART，exit code 恆為 0。"),
        epilog: None,
        raw_text: true,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--profile"], dest: "profile", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("要測試的 template 名稱（如 brcm-template）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--sample"], dest: "sample", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("樣本文字檔路徑（如一段真實 console capture log）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--profile-dir"], dest: "profile_dir", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str(d.profile_dir.clone()), choices: None, required: false, help: Some(format!("profile YAML 目錄（預設: {}）", d.profile_dir)), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    })) },
            ],
        }),
    }
}

fn spec_skill() -> ParserSpec {
    ParserSpec {
        prog: "serialwrap skill".to_string(),
        description: Some("把內嵌的 serialwrap 操作指南（agent skill／操作手冊）原文印到 stdout（#131）。\n唯讀、不需 daemon；可重導存檔：serialwrap skill --platform windows > SKILL_WINDOWS.md"),
        epilog: None,
        raw_text: true,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--platform"], dest: "platform", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str("auto".to_string()), choices: Some(vec!["auto", "linux", "windows"]), required: false, help: Some("指南平台；auto（預設）依目前平台選擇".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    }
}

// ---- daemon（P4-T1：Python 3.12.3 argparse 逐字對照；由 scripts/rust/capture_cli_help.py 的 apply_rust_only 擴充）
fn spec_daemon(d: &SpecDefaults) -> ParserSpec {
    ParserSpec {
        prog: "serialwrap daemon".to_string(),
        description: Some("管理 serialwrap daemon 行程：啟動、停止與查詢執行狀態。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: Some(SubparsersSpec {
            dest: "daemon_cmd", required: true, title: None, metavar: Some("<command>"),
            entries: vec![
                SubparserEntry { name: "start", help: Some("啟動 daemon（--foreground 可前景執行；systemd 模式重導 service start）"), target: SubTarget::Native(Box::new(
                    ParserSpec {
                        prog: "serialwrap daemon start".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![
                            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
                            ActionSpec { option_strings: vec!["--profile-dir"], dest: "profile_dir", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str(d.profile_dir.clone()), choices: None, required: false, help: Some(format!("profile YAML 目錄（預設: {}）", d.profile_dir)), metavar: None, version: None },
                            ActionSpec { option_strings: vec!["--lock"], dest: "lock", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Str(d.lock_path.clone()), choices: None, required: false, help: None, metavar: None, version: None },
                            ActionSpec { option_strings: vec!["--foreground"], dest: "foreground", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: None, metavar: None, version: None },
                            ActionSpec { option_strings: vec!["--with-sudo"], dest: "with_sudo", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("systemd-system 模式下，daemon start 重導至 service start 時以 sudo 執行".to_string()), metavar: None, version: None },
                            ActionSpec { option_strings: vec!["--force-spawn"], dest: "force_spawn", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("跳過『已有其他 socket 的 serialwrapd 在跑』防線，強制在本次目標 socket 另外 spawn（#173；預設拒絕，避免同機誤開第二個 daemon 造成 two-reader）".to_string()), metavar: None, version: None },
                        ],
                        mutex: vec![
                        ],
                        sub: None,
                    }
                )) },
                SubparserEntry { name: "stop", help: Some("停止執行中的 daemon"), target: SubTarget::Native(Box::new(
                    ParserSpec {
                        prog: "serialwrap daemon stop".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![
                            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
                            ActionSpec { option_strings: vec!["--with-sudo"], dest: "with_sudo", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("systemd-system 模式下，daemon stop 重導至 service stop 時以 sudo 執行".to_string()), metavar: None, version: None },
                        ],
                        mutex: vec![
                        ],
                        sub: None,
                    }
                )) },
                SubparserEntry { name: "status", help: Some("顯示 daemon 狀態（pid／sessions／devices／log 路徑／多開偵測 multi_open）"), target: SubTarget::Native(Box::new(
                    ParserSpec {
                        prog: "serialwrap daemon status".to_string(),
                        description: None,
                        epilog: None,
                        raw_text: false,
                        raw_description: false,
                        add_help: true,
                        allow_abbrev: true,
                        actions: vec![
                            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
                        ],
                        mutex: vec![
                        ],
                        sub: None,
                    }
                )) },
            ],
        }),
    }
}

// ---- remote（P4-T1：Python 3.12.3 argparse 逐字對照；由 scripts/rust/capture_cli_help.py 的 apply_rust_only 擴充）
fn spec_remote(_d: &SpecDefaults) -> ParserSpec {
    ParserSpec {
        prog: "serialwrap remote".to_string(),
        description: Some("serialwrap remote：外包系統 ssh 建立 -R（expose，把本機 daemon 推到對端）／-L（connect，relay 情境把對端 port 拉回本機 loopback）隧道，background 常駐。
  serialwrap remote user@host:7777        # -R 預設：expose 本機 daemon
  serialwrap remote -L user@relay:7777    # connect（relay/雙 NAT）
  serialwrap remote                        # 列目前隧道（status）
  serialwrap remote close 7777|all         # 拆除
安全：只透過 ssh-tunnel、單租戶/可信 relay 或 --remote-socket；不可對網路直接開放。"),
        epilog: None,
        raw_text: false,
        raw_description: true,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["-R"], dest: "reverse", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("reverse/expose（預設）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["-L"], dest: "forward", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("forward/connect（relay）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--autossh"], dest: "autossh", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("以 autossh 斷線自動重連".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--local"], dest: "local", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::None, choices: None, required: false, help: Some("-L 本機 loopback port（預設=對端 port）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--remote-socket"], dest: "remote_socket", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("硬化：-R 建遠端 unix socket／-L 連該 socket（共享 relay 建議）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--ready-timeout"], dest: "ready_timeout", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Float, default: ArgValue::Float(10.0), choices: None, required: false, help: Some("readiness 確認上限秒數（逾時回 starting）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--ssh-opt"], dest: "ssh_opt", kind: ActionKind::Append, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::List(vec![]), choices: None, required: false, help: Some("透傳額外 ssh 參數（可重複），如 --ssh-opt=-p --ssh-opt=2222".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec![], dest: "words", kind: ActionKind::Store, nargs: Nargs::ZeroOrMore, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("[user@]host:port ｜ status ｜ close <port|all>".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    }
}

// ---- connect（P4-T1：Python 3.12.3 argparse 逐字對照；由 scripts/rust/capture_cli_help.py 的 apply_rust_only 擴充）
fn spec_connect(_d: &SpecDefaults) -> ParserSpec {
    ParserSpec {
        prog: "serialwrap connect".to_string(),
        description: Some("從 benches.yaml 解析 bench 代號，重用既有 remote -L spawn 路徑建立或拆除隧道。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--close"], dest: "close", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("拆除該代號對應 local_port 的隧道，並清除 endpoint 記憶".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec![], dest: "code", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("benches.yaml 的 bench 代號".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    }
}

// ---- benches（P4-T1：Python 3.12.3 argparse 逐字對照；由 scripts/rust/capture_cli_help.py 的 apply_rust_only 擴充）
fn spec_benches(_d: &SpecDefaults) -> ParserSpec {
    ParserSpec {
        prog: "serialwrap benches".to_string(),
        description: Some("列出 benches.yaml 各代號、目前記住的 endpoint，以及本機 connect tunnel 是否仍存活。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    }
}

// ---- supervision-mode（P4-T1：Python 3.12.3 argparse 逐字對照；由 scripts/rust/capture_cli_help.py 的 apply_rust_only 擴充）
fn spec_supervision_mode(_d: &SpecDefaults) -> ParserSpec {
    ParserSpec {
        prog: "serialwrap supervision-mode".to_string(),
        description: Some("印出 config.yaml 中的 supervision_mode，未設定時預設為 on-demand。供 shell 腳本（如 minicom_router.sh）查詢。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--force"], dest: "force", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("相容旗標：不覆寫交接護欄（flash、傳輸、活躍操作或探測不明時一律拒絕）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--with-sudo"], dest: "with_sudo", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("允許代跑需要 root／系統管理員權限的指令".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec![], dest: "words", kind: ActionKind::Store, nargs: Nargs::ZeroOrMore, conv: Conv::Str, default: ArgValue::None, choices: None, required: true, help: Some("不帶參數：印出目前監管模式；set MODE：切換並執行 reconcile".to_string()), metavar: Some("set MODE"), version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    }
}

// ---- service（P4-T1：Python 3.12.3 argparse 逐字對照；由 scripts/rust/capture_cli_help.py 的 apply_rust_only 擴充）
fn spec_service(_d: &SpecDefaults) -> ParserSpec {
    ParserSpec {
        prog: "serialwrap service".to_string(),
        description: Some("包裝 systemctl，按 config.yaml 的 supervision_mode 決定呼叫方式。
systemd-user 模式：免 sudo。
systemd-system 模式：start/stop/restart 需 root（加 --with-sudo 代跑）。
on-demand 模式：不可用。"),
        epilog: None,
        raw_text: false,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--with-sudo"], dest: "with_sudo", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("systemd-system 模式的特權動作（start/stop/restart）以 sudo 執行".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec![], dest: "action", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: Some(vec!["start", "stop", "restart", "status"]), required: true, help: Some("要執行的 systemctl 動作".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    }
}

// ---- setup（P4-T1：Python 3.12.3 argparse 逐字對照；由 scripts/rust/capture_cli_help.py 的 apply_rust_only 擴充）
fn spec_setup(_d: &SpecDefaults) -> ParserSpec {
    ParserSpec {
        prog: "serialwrap setup".to_string(),
        description: Some("物化套件資產（profiles／agent skill／minicom wrappers）到使用者位置，並 reconcile 監管模式（先停舊、再起新）。
未指定模式時自動偵測（有 systemd → systemd-user，否則 on-demand）。
system scope 的特權動作需 --with-sudo，否則只回報待執行的 sudo 指令。"),
        epilog: None,
        raw_text: true,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--user"], dest: "user", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("設為 systemd-user 模式（免 sudo）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--system"], dest: "system", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("設為 systemd-system 模式（特權動作需 --with-sudo）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--on-demand"], dest: "on_demand", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("設為 on-demand 模式（無 systemd 時的降級備援）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--force"], dest: "force", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("覆蓋既有 profiles，並在 flash 進行中仍強制切換".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--with-sudo"], dest: "with_sudo", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("systemd-system 模式下允許執行特權 sudo 指令（安裝 unit／enable／start）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--windows-task"], dest: "windows_task", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("Windows：以工作排程器登入自啟監管（Windows 預設）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--windows-service"], dest: "windows_service", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("Windows：以 Windows 服務監管（OD-12：只列出待執行指令）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--rollback"], dest: "rollback", kind: ActionKind::StoreTrue, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Bool(false), choices: None, required: false, help: Some("回滾到上一個完整發佈組合（Previous）".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
            MutexSpec { required: false, dests: vec!["user", "system", "on_demand", "windows_task", "windows_service", "rollback"] },
        ],
        sub: None,
    }
}

// ---- doctor（P4-T1：Python 3.12.3 argparse 逐字對照；由 scripts/rust/capture_cli_help.py 的 apply_rust_only 擴充）
fn spec_doctor(_d: &SpecDefaults) -> ParserSpec {
    ParserSpec {
        prog: "serialwrap doctor".to_string(),
        description: Some("對安裝與執行環境做一系列唯讀檢查並印出 JSON 報告（依平台選擇檢查清單，#131）。
Linux：systemd／wsl_systemd／devices／serialwrap_minicom_on_path／jq_on_path／
minicom_on_path 為 advisory（缺少不致命，不拉低整體 ok；#149）。
Windows：serialwrap_on_path／serialwrapd_on_path／daemon_endpoint／devices 為 advisory。"),
        epilog: None,
        raw_text: true,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    }
}

pub fn root_spec(d: &SpecDefaults) -> ParserSpec {
    let mut spec = ParserSpec {
        prog: "serialwrap".to_string(),
        description: Some("serialwrap client（支援本機 Unix socket 與遠端 endpoint）"),
        epilog: Some("examples:\n  serialwrap session list\n  serialwrap --endpoint tcp://127.0.0.1:7777 session list\n  serialwrap --endpoint tcp://127.0.0.1:7777 cmd submit --selector COM0 --cmd 'uname -a'"),
        raw_text: true,
        raw_description: false,
        add_help: true,
        allow_abbrev: true,
        actions: vec![
            ActionSpec { option_strings: vec!["-h", "--help"], dest: "help", kind: ActionKind::Help, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("show this help message and exit".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--version"], dest: "version", kind: ActionKind::Version, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("顯示版本後離開".to_string()), metavar: None, version: Some(d.version_line.clone()) },
            ActionSpec { option_strings: vec!["-v", "--verbose"], dest: "verbose", kind: ActionKind::Count, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::Count(0), choices: None, required: false, help: Some("提高 CLI trace 詳細度（-v=INFO，-vv=DEBUG；優先於 SERIALWRAP_LOG_LEVEL）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--socket"], dest: "socket", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("本機 daemon 的 Unix socket 路徑（未指定時依 config.yaml 與 XDG 執行期目錄解析，可用 SERIALWRAP_RUN_DIR 覆寫）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--endpoint"], dest: "endpoint", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("遠端 daemon endpoint，例如 tcp://127.0.0.1:7777（優先於 --socket）".to_string()), metavar: Some(sw_proto::constants::METAVAR_ENDPOINT), version: None },
            ActionSpec { option_strings: vec!["--bench"], dest: "bench", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Str, default: ArgValue::None, choices: None, required: false, help: Some("使用 connect 記住的 bench 代號解析 endpoint（優先序低於 --endpoint/--socket，高於 config fallback）".to_string()), metavar: Some(sw_proto::constants::METAVAR_CODE), version: None },
            ActionSpec { option_strings: vec!["--timeout"], dest: "timeout_s", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Float, default: ArgValue::None, choices: None, required: false, help: Some("RPC timeout 秒數（未指定：一般方法 5.0；長操作 session attach/recover/self-test/console-attach 自動採固定 45.0 的 floor，#123）".to_string()), metavar: None, version: None },
            ActionSpec { option_strings: vec!["--retries"], dest: "retries", kind: ActionKind::Store, nargs: Nargs::One, conv: Conv::Int, default: ArgValue::Int(sw_proto::pyjson::PyInt::Small(0)), choices: None, required: false, help: Some("TIMEOUT／連線失敗時的重試次數，僅作用於冪等唯讀方法白名單（指數退避 0.5s 起、單次上限 5s；預設: 0）".to_string()), metavar: None, version: None },
        ],
        mutex: vec![
        ],
        sub: None,
    };
    let group_help: [&str; 20] = [
        "管理 serialwrap daemon（啟動／停止／狀態）",
        "實體 UART 裝置列舉與 handoff（release／attach）",
        "session 生命週期、探測、recover、console 與 interactive 操作",
        "session 別名與 by-id 綁定管理",
        "提交命令並讀取結果（line／background）",
        "即時 tail 解析後的文字事件串流",
        "raw／text 日誌 tail（含 timestamp／seq／crc）",
        "透過 UART 推送／拉取檔案",
        "write-ahead log 匯出／重設／seq 查詢",
        "MCU flash pattern 查詢與 flash 端點狀態",
        "按需開關 ssh 反向隧道，讓遠端 agent 連本機 daemon（-R 預設 expose）",
        "以 bench 代號建立或拆除 connect 隧道",
        "列出 benches.yaml 代號與記住的 endpoint/tunnel 狀態",
        "event-trigger 規則註冊與 matcher 控制",
        "顯示有效的監管模式（on-demand、systemd-user 或 systemd-system）",
        "透過 systemctl 管理 serialwrap systemd service（systemd 監管模式適用）",
        "安裝資產並設定監管模式（systemd-user／systemd-system／on-demand）",
        "診斷安裝與執行環境（平台感知：Linux 檢 dialout／systemd／by-id 裝置／human console 就緒（serialwrap-minicom／jq／minicom），Windows 檢 pyserial／daemon endpoint／COM 列舉）",
        "離線 profile regex 診斷（免連 daemon／免碰 UART）",
        "輸出操作指南（skill）原文到 stdout（--platform windows 為 Windows 操作指南）",
    ];
    let entries = GROUP_ORDER
        .iter()
        .copied()
        .zip(group_help)
        .map(|(name, help)| {
            // P4-T1：八個委派群組的宣告式 parser 一律原生（help／usage 可離線比對）；執行期路由仍由
            // `DELEGATED_GROUPS`／`classify` 決定，群組於各 P4 Task 完成時自委派集合移除（Task 14 清空）。
            let native = match name {
                "daemon" => spec_daemon(d),
                "remote" => spec_remote(d),
                "connect" => spec_connect(d),
                "benches" => spec_benches(d),
                "supervision-mode" => spec_supervision_mode(d),
                "service" => spec_service(d),
                "setup" => spec_setup(d),
                "doctor" => spec_doctor(d),
                "device" => spec_device(),
                "session" => spec_session(),
                "alias" => spec_alias(),
                "cmd" => spec_cmd(),
                "stream" => spec_stream(),
                "log" => spec_log(),
                "file" => spec_file(),
                "wal" => spec_wal(),
                "mcu" => spec_mcu(),
                "event" => spec_event(),
                "profile" => spec_profile(d),
                "skill" => spec_skill(),
                _ => unreachable!("缺少原生 CLI 群組規格: {name}"),
            };
            let target = SubTarget::Native(Box::new(native));
            SubparserEntry {
                name,
                help: Some(help),
                target,
            }
        })
        .collect();
    spec.sub = Some(SubparsersSpec {
        dest: "cmd",
        required: true,
        title: Some("command groups"),
        metavar: Some("<group>"),
        entries,
    });
    spec
}
