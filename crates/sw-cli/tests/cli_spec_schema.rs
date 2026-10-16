//! 比對 root_spec 宣告與 contracts/cli_schema.json 契約的一致性（P3-T10）。
mod common;

use sw_cli::argparse::SubTarget;
use sw_cli::cli_spec::{root_spec, SpecDefaults, GROUP_ORDER};
use sw_proto::pyjson::PyValue;

#[test]
fn cli_spec_matches_schema() {
    let path = common::data_root().join("contracts/cli_schema.json");
    if !path.exists() {
        panic!("P0 contracts 缺少: {}", path.display());
    }
    let schema_text = match std::fs::read_to_string(&path) {
        Ok(t) => t,
        Err(e) => panic!("P0 contracts 缺少: {}: {e}", path.display()),
    };
    let schema = sw_proto::pyjson::loads(&schema_text).expect("cli_schema.json 必須為合規 JSON");

    let root_dict = match &schema {
        PyValue::Dict(d) => d,
        _ => panic!("cli_schema.json 頂層必須為 Object"),
    };

    let spec = root_spec(&SpecDefaults {
        version_line: sw_proto::version::version_line(),
        profile_dir: "/golden/config/profiles".into(),
    });

    assert_eq!(spec.prog, "serialwrap");

    // 驗證全域選項（x-global-options）
    let global_opts = match root_dict.get("x-global-options").expect("x-global-options 缺失") {
        PyValue::List(l) => l,
        _ => panic!("x-global-options 必須為 List"),
    };
    for opt in global_opts {
        let opt_dict = match opt {
            PyValue::Dict(d) => d,
            _ => panic!("global option 必須為 Dict"),
        };
        let flags: Vec<&str> = match opt_dict.get("flags").expect("flags 缺失") {
            PyValue::List(l) => l
                .iter()
                .map(|v| match v {
                    PyValue::Str(s) => s.as_str(),
                    _ => panic!("flag 必須為 Str"),
                })
                .collect(),
            _ => panic!("flags 必須為 List"),
        };
        let matched = spec
            .actions
            .iter()
            .any(|a| flags.iter().any(|&f| a.option_strings.contains(&f)));
        assert!(matched, "全域選項 {flags:?} 未在 root_spec.actions 中定義");
    }

    // 驗證 20 群組（GROUP_ORDER 與 x-groups）
    let sub = spec.sub.as_ref().expect("root_spec 必須有 sub");
    assert_eq!(sub.dest, "cmd");
    assert!(sub.required);

    let groups = match root_dict.get("x-groups").expect("x-groups 缺失") {
        PyValue::List(l) => l,
        _ => panic!("x-groups 必須為 List"),
    };
    assert_eq!(groups.len(), GROUP_ORDER.len(), "群組數量必須為 20");

    for (idx, grp_val) in groups.iter().enumerate() {
        let grp_dict = match grp_val {
            PyValue::Dict(d) => d,
            _ => panic!("group 必須為 Dict"),
        };
        let grp_name = match grp_dict.get("name").expect("group name 缺失") {
            PyValue::Str(s) => s.as_str(),
            _ => panic!("name 必須為 Str"),
        };
        assert_eq!(
            grp_name, GROUP_ORDER[idx],
            "群組順序第 {idx} 項 {grp_name} 與 GROUP_ORDER 不符"
        );

        let stage = match grp_dict.get("stage").expect("stage 缺失") {
            PyValue::Str(s) => s.as_str(),
            _ => panic!("stage 必須為 Str"),
        };

        let entry = sub
            .entries
            .iter()
            .find(|e| e.name == grp_name)
            .unwrap_or_else(|| panic!("群組 {grp_name} 未在 sub.entries 中找到"));

        if stage == "delegate-1a" {
            assert!(
                matches!(entry.target, SubTarget::Delegated),
                "委派群組 {grp_name} 必須為 SubTarget::Delegated"
            );
        } else if stage == "rust-1a" {
            let grp_spec = match &entry.target {
                SubTarget::Native(s) => s.as_ref(),
                _ => panic!("原生群組 {grp_name} 必須為 SubTarget::Native"),
            };

            // 原生群組的子命令驗證
            let subcmds = match grp_dict.get("subcommands") {
                Some(PyValue::List(l)) => l.as_slice(),
                _ => &[],
            };

            if !subcmds.is_empty() {
                let grp_sub = grp_spec.sub.as_ref().unwrap_or_else(|| {
                    panic!("原生群組 {grp_name} 宣告了子命令但 grp_spec.sub 為 None")
                });
                for subcmd_val in subcmds {
                    let subcmd_dict = match subcmd_val {
                        PyValue::Dict(d) => d,
                        _ => panic!("subcommand 必須為 Dict"),
                    };
                    let subcmd_name = match subcmd_dict.get("name").expect("subcmd name 缺失") {
                        PyValue::Str(s) => s.as_str(),
                        _ => panic!("subcmd name 必須為 Str"),
                    };
                    let sub_entry = grp_sub
                        .entries
                        .iter()
                        .find(|e| e.name == subcmd_name)
                        .unwrap_or_else(|| {
                            panic!("群組 {grp_name} 缺少子命令 {subcmd_name}")
                        });

                    let cmd_spec = match &sub_entry.target {
                        SubTarget::Native(s) => s.as_ref(),
                        _ => panic!("子命令 {grp_name} {subcmd_name} 必須為 SubTarget::Native"),
                    };

                    // 比對子命令選項
                    if let Some(PyValue::List(opts)) = subcmd_dict.get("options") {
                        for opt_val in opts {
                            let opt_d = match opt_val {
                                PyValue::Dict(d) => d,
                                _ => panic!("option 必須為 Dict"),
                            };
                            let dest = match opt_d.get("dest") {
                                Some(PyValue::Str(s)) => s.as_str(),
                                _ => continue,
                            };
                            let has_action = cmd_spec.actions.iter().any(|a| a.dest == dest);
                            assert!(
                                has_action,
                                "子命令 {grp_name} {subcmd_name} 缺少 dest={dest} 的 action"
                            );
                        }
                    }
                }
            }
        }
    }
}
