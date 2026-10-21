//! 比對 root_spec 宣告與 contracts/cli_schema.json 契約的一致性（P3-T10）。
mod common;

use sw_cli::argparse::{ActionKind, ActionSpec, ArgValue, Conv, SubTarget};
use sw_cli::cli_spec::{root_spec, SpecDefaults, GROUP_ORDER};
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

fn object(value: &PyValue) -> &PyDict {
    match value {
        PyValue::Dict(value) => value,
        _ => panic!("schema 項目必須為 Object"),
    }
}

fn list<'a>(value: &'a PyValue, context: &str) -> &'a [PyValue] {
    match value {
        PyValue::List(values) => values,
        _ => panic!("{context} 必須為 Array"),
    }
}

fn string<'a>(value: &'a PyValue, context: &str) -> &'a str {
    match value {
        PyValue::Str(value) => value,
        _ => panic!("{context} 必須為 String"),
    }
}

fn optional_string<'a>(value: Option<&'a PyValue>, context: &str) -> Option<&'a str> {
    value.map(|value| string(value, context))
}

fn action_kind(value: &PyValue) -> ActionKind {
    match string(value, "action") {
        "store" => ActionKind::Store,
        "store_true" => ActionKind::StoreTrue,
        "count" => ActionKind::Count,
        "append" => ActionKind::Append,
        "version" => ActionKind::Version,
        other => panic!("schema action 不在 argparse 子集內：{other}"),
    }
}

fn action_default_matches(expected: &PyValue, actual: &ArgValue, profile_dir: &str) -> bool {
    if let PyValue::Dict(value) = expected {
        if value.0.get("env_default") == Some(&PyValue::Str("PROFILE_DIR".to_string())) {
            return actual == &ArgValue::Str(profile_dir.to_string());
        }
    }
    match (expected, actual) {
        (PyValue::Null, ArgValue::None) => true,
        (PyValue::Bool(expected), ArgValue::Bool(actual)) => expected == actual,
        (PyValue::Str(expected), ArgValue::Str(actual)) => expected == actual,
        (PyValue::Int(PyInt::Small(expected)), ArgValue::Int(PyInt::Small(actual))) => {
            expected == actual
        }
        (PyValue::Int(PyInt::Small(expected)), ArgValue::Count(actual)) => {
            *expected as i64 == *actual
        }
        (PyValue::Int(PyInt::Big(expected)), ArgValue::Int(PyInt::Big(actual))) => {
            expected == actual
        }
        (PyValue::Float(expected), ArgValue::Float(actual)) => expected == actual,
        (PyValue::List(expected), ArgValue::List(actual)) => {
            expected.len() == actual.len()
                && expected.iter().zip(actual).all(
                    |(expected, actual)| matches!(expected, PyValue::Str(value) if value == actual),
                )
        }
        _ => false,
    }
}

fn compare_options(
    actions: &[ActionSpec],
    expected_options: &[PyValue],
    context: &str,
    profile_dir: &str,
) {
    let actual: Vec<&ActionSpec> = actions
        .iter()
        .filter(|action| action.kind != ActionKind::Help)
        .collect();
    assert_eq!(
        actual.len(),
        expected_options.len(),
        "{context} 的 option 數量不符；這也會攔下 Rust 單邊新增的旗標"
    );
    let mut matched = vec![false; actual.len()];

    for expected_value in expected_options {
        let expected = object(expected_value);
        let expected_flags: Vec<&str> =
            list(expected.0.get("flags").expect("option flags 缺失"), "flags")
                .iter()
                .map(|value| string(value, "flag"))
                .collect();
        let Some((index, action)) = actual
            .iter()
            .enumerate()
            .find(|(_, action)| action.option_strings.as_slice() == expected_flags.as_slice())
        else {
            panic!("{context} 缺少或錯置 flags={expected_flags:?}");
        };
        assert!(
            !matched[index],
            "{context} 重複對應 flags={expected_flags:?}"
        );
        matched[index] = true;

        if let Some(expected) = expected.0.get("action") {
            assert_eq!(
                action.kind,
                action_kind(expected),
                "{context} flags={expected_flags:?} action"
            );
        }
        if let Some(expected) = expected.0.get("dest") {
            if let PyValue::Str(expected) = expected {
                assert_eq!(
                    action.dest, expected,
                    "{context} flags={expected_flags:?} dest"
                );
            }
        }
        if let Some(expected) = expected.0.get("type") {
            let expected = string(expected, "type");
            let actual = match action.conv {
                Conv::Str => "str",
                Conv::Int => "int",
                Conv::Float => "float",
            };
            assert_eq!(actual, expected, "{context} flags={expected_flags:?} type");
        }
        if let Some(expected) = expected.0.get("default") {
            assert!(
                action_default_matches(expected, &action.default, profile_dir),
                "{context} flags={expected_flags:?} default 不符 (got {:?}, want {expected:?})",
                action.default
            );
        }
        if let Some(expected) = expected.0.get("choices") {
            let expected: Vec<&str> = list(expected, "choices")
                .iter()
                .map(|value| string(value, "choice"))
                .collect();
            let actual = action
                .choices
                .as_ref()
                .map(|values| values.iter().copied().collect::<Vec<_>>())
                .unwrap_or_default();
            assert_eq!(
                actual, expected,
                "{context} flags={expected_flags:?} choices"
            );
        }
        if let Some(expected) = expected.0.get("required") {
            let PyValue::Bool(expected) = expected else {
                panic!("schema required 必須為 Bool");
            };
            assert_eq!(
                action.required, *expected,
                "{context} flags={expected_flags:?} required"
            );
        }
        if let Some(expected) = optional_string(expected.0.get("metavar"), "metavar") {
            assert_eq!(
                action.metavar,
                Some(expected),
                "{context} flags={expected_flags:?} metavar"
            );
        }
    }
    assert!(matched.into_iter().all(|matched| matched));
}

#[test]
fn cli_spec_matches_schema() {
    let path = common::data_root().join("contracts/cli_schema.json");
    if !path.exists() {
        panic!("P0 contracts 缺少: {}", path.display());
    }
    let schema_text = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("P0 contracts 缺少: {}: {error}", path.display()));
    let schema = sw_proto::pyjson::loads(&schema_text).expect("cli_schema.json 必須為合規 JSON");
    let root = object(&schema);
    let spec = root_spec(&SpecDefaults {
        version_line: sw_proto::version::version_line(),
        profile_dir: "/golden/config/profiles".into(),
        lock_path: "/golden/run/serialwrapd.lock".into(),
    });
    let profile_dir = "/golden/config/profiles";

    assert_eq!(spec.prog, "serialwrap");
    let globals = list(
        root.0
            .get("x-global-options")
            .expect("x-global-options 缺失"),
        "x-global-options",
    );
    compare_options(&spec.actions, globals, "root globals", profile_dir);

    let sub = spec.sub.as_ref().expect("root_spec 必須有 sub");
    assert_eq!(sub.dest, "cmd");
    assert!(sub.required);
    let groups = list(root.0.get("x-groups").expect("x-groups 缺失"), "x-groups");
    assert_eq!(
        groups.len(),
        GROUP_ORDER.len(),
        "契約群組數量與 GROUP_ORDER 不符"
    );
    assert_eq!(
        sub.entries.len(),
        groups.len(),
        "Rust-only 或缺漏的命令群組"
    );

    for (index, group_value) in groups.iter().enumerate() {
        let group = object(group_value);
        let name = string(group.0.get("name").expect("group name 缺失"), "group name");
        assert_eq!(name, GROUP_ORDER[index], "群組順序第 {index} 項不符");
        let stage = string(group.0.get("stage").expect("stage 缺失"), "stage");
        let entry = sub
            .entries
            .iter()
            .find(|entry| entry.name == name)
            .unwrap_or_else(|| panic!("群組 {name} 未在 sub.entries 中找到"));

        if name == "remote" {
            assert!(
                matches!(entry.target, SubTarget::Native(_)),
                "P4-T8 的 remote 群組必須原生解析"
            );
            assert!(
                !sw_cli::delegate::DELEGATED_GROUPS.contains(&name),
                "P4-T8 的 remote 群組不得留在 DELEGATED_GROUPS"
            );
            continue;
        }

        // P4-T7：daemon 已原生承接，執行期不得再交由 Python delegate。
        if name == "daemon" {
            assert!(
                matches!(entry.target, SubTarget::Native(_)),
                "P4-T7 的 daemon 群組必須原生解析"
            );
            assert!(
                !sw_cli::delegate::DELEGATED_GROUPS.contains(&name),
                "P4-T7 的 daemon 群組不得留在 DELEGATED_GROUPS"
            );
            continue;
        }

        // P4-T9：connect、benches 已原生承接（SC-27），自 DELEGATED_GROUPS 移除。
        if matches!(name, "connect" | "benches") {
            assert!(
                matches!(entry.target, SubTarget::Native(_)),
                "P4-T9 的 {name} 群組必須原生解析"
            );
            assert!(
                !sw_cli::delegate::DELEGATED_GROUPS.contains(&name),
                "P4-T9 的 {name} 群組不得留在 DELEGATED_GROUPS"
            );
            continue;
        }

        // P4-T6：service、supervision-mode 已原生並自 DELEGATED_GROUPS 移除（同 P4-T8 remote 的處理）；
        // schema 的 stage 仍為 delegate-1a，故只核對原生解析與委派集合不含此群組。
        if matches!(name, "service" | "supervision-mode") {
            assert!(
                matches!(entry.target, SubTarget::Native(_)),
                "P4-T6 的 {name} 群組必須原生解析"
            );
            assert!(
                !sw_cli::delegate::DELEGATED_GROUPS.contains(&name),
                "P4-T6 的 {name} 群組不得留在 DELEGATED_GROUPS"
            );
            continue;
        }

        // P4-T10：setup 已原生承接，自 DELEGATED_GROUPS 移除。
        if name == "setup" {
            assert!(
                matches!(entry.target, SubTarget::Native(_)),
                "P4-T10 的 setup 群組必須原生解析"
            );
            assert!(
                !sw_cli::delegate::DELEGATED_GROUPS.contains(&name),
                "P4-T10 的 setup 群組不得留在 DELEGATED_GROUPS"
            );
            continue;
        }

        // P4-T14：doctor 已原生承接，自 DELEGATED_GROUPS 移除（委派集合清空）。
        if name == "doctor" {
            assert!(
                matches!(entry.target, SubTarget::Native(_)),
                "P4-T14 的 doctor 群組必須原生解析"
            );
            assert!(
                !sw_cli::delegate::DELEGATED_GROUPS.contains(&name),
                "P4-T14 的 doctor 群組不得留在 DELEGATED_GROUPS"
            );
            continue;
        }

        if stage == "delegate-1a" {
            // P4-T1（計畫 Task 1 步驟 4）：委派群組的宣告式 parser 已原生，help／usage 可離線比對；
            // 執行期路由仍由 DELEGATED_GROUPS（classify）決定，群組於各 P4 Task 完成時自委派集合移除。
            assert!(
                matches!(entry.target, SubTarget::Native(_)),
                "委派群組 {name} 的宣告式 parser 必須為 Native（P4-T1）"
            );
            assert!(
                sw_cli::delegate::DELEGATED_GROUPS.contains(&name),
                "委派群組 {name} 必須仍在 DELEGATED_GROUPS（執行期路由）"
            );
            continue;
        }
        assert_eq!(stage, "rust-1a", "未知 CLI stage：{stage}");
        let group_spec = match &entry.target {
            SubTarget::Native(parser) => parser.as_ref(),
            SubTarget::Delegated => panic!("原生群組 {name} 必須為 Native"),
        };
        let group_options = list(
            group.0.get("options").expect("group options 缺失"),
            "group options",
        );
        compare_options(
            &group_spec.actions,
            group_options,
            &format!("group {name}"),
            profile_dir,
        );

        let expected_commands = match group.0.get("subcommands") {
            Some(PyValue::List(values)) => values.as_slice(),
            Some(PyValue::Null) | None => &[],
            Some(_) => panic!("subcommands 必須為 Array 或 null"),
        };
        let actual_commands = group_spec
            .sub
            .as_ref()
            .map(|sub| sub.entries.as_slice())
            .unwrap_or_default();
        assert_eq!(
            actual_commands.len(),
            expected_commands.len(),
            "group {name} 子命令數量不符"
        );
        for (command_value, command_entry) in expected_commands.iter().zip(actual_commands) {
            let command = object(command_value);
            let command_name = string(
                command.0.get("name").expect("subcommand name 缺失"),
                "subcommand name",
            );
            assert_eq!(
                command_name, command_entry.name,
                "group {name} 子命令順序或名稱不符"
            );
            let command_spec = match &command_entry.target {
                SubTarget::Native(parser) => parser.as_ref(),
                SubTarget::Delegated => panic!("子命令 {name} {command_name} 不得為委派"),
            };
            let options = list(
                command.0.get("options").expect("subcommand options 缺失"),
                "subcommand options",
            );
            compare_options(
                command_spec.actions.as_slice(),
                options,
                &format!("{name} {command_name}"),
                profile_dir,
            );
        }
    }
}
