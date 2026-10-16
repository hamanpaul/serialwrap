mod common;

use sw_proto::{
    pypath::PathFlavor,
    runtime_paths::{EnvSnapshot, NativeTraits, PathsError, RuntimePaths},
};

struct H;
impl sw_proto::pypath::HomeLookup for H {
    fn current_user_home(&self) -> Option<String> {
        Some("/golden/pw-home".into())
    }
    fn user_home(&self, _: &str) -> Option<String> {
        None
    }
}

fn run(file: &str, native: NativeTraits) {
    let mut fails = Vec::new();
    for r in common::golden(file) {
        let env = EnvSnapshot::from_pairs(common::env_pairs(&r.input));
        let got = match RuntimePaths::resolve(&env, native, &H) {
            Ok(p) => common::paths_json(&p),
            Err(PathsError::InvalidTcpPort { .. }) => "ERR:ValueError".into(),
        };
        if got != r.expected {
            fails.push(format!("{}: {}\n got  {got}\n want {}", r.id, r.input, r.expected));
        }
    }
    assert!(fails.is_empty(), "{}", fails.join("\n"));
}

#[test]
fn posix() {
    run(
        "runtime_paths.posix.tsv",
        NativeTraits {
            default_endpoint_is_tcp: false,
            path_flavor: PathFlavor::Posix,
        },
    )
}

#[test]
fn windows() {
    run(
        "runtime_paths.windows.tsv",
        NativeTraits {
            default_endpoint_is_tcp: true,
            path_flavor: PathFlavor::Windows,
        },
    )
}

fn env_of(pairs: &[(&str, &str)]) -> EnvSnapshot {
    EnvSnapshot::from_pairs(pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())))
}

/// 對齊 Python `sw_core/cli.py::_bench_state_path`（cli.py:1115-1122）：
/// `SERIALWRAP_STATE_DIR` strip 後非空才採用（並 expanduser），否則 `XDG_STATE_HOME`（falsy 時 `~/.local/state`）＋`serialwrap`。
/// 期望值以 CPython 3.12.3 於隔離 env 實跑取得。
#[test]
fn test_bench_state_path() {
    let cases: &[(&[(&str, &str)], &str)] = &[
        (&[("HOME", "/h")], "/h/.local/state/serialwrap/benches.state.json"),
        (&[("HOME", "/h"), ("SERIALWRAP_STATE_DIR", "/s")], "/s/benches.state.json"),
        (
            &[("HOME", "/h"), ("SERIALWRAP_STATE_DIR", "   "), ("XDG_STATE_HOME", "/x")],
            "/x/serialwrap/benches.state.json",
        ),
        (&[("HOME", "/h"), ("SERIALWRAP_STATE_DIR", "~/st")], "/h/st/benches.state.json"),
        (&[("HOME", "/h"), ("XDG_STATE_HOME", "")], "/h/.local/state/serialwrap/benches.state.json"),
    ];
    for (pairs, want) in cases {
        let got = sw_proto::runtime_paths::bench_state_path(&env_of(pairs), PathFlavor::Posix, &H);
        assert_eq!(&got, want, "env={pairs:?}");
    }
}

/// SC-09（plan 04 Task 7；逐字對齊 P1 `constants.resolve_config_path(for_write=False)`，02 T15）：
/// `$SERIALWRAP_CONFIG` strip 後非空 → expanduser 後原樣回傳（即使不存在）；
/// 否則 `CONFIG_DIR/config.yaml` 存在 → 它；否則 `<system_config_dir>/config.yaml` 存在 → 它；否則 `CONFIG_DIR/config.yaml`。
/// 註：P1 Python 實作不在本 worktree，期望值依凍結計畫文字撰寫。
#[test]
fn test_resolve_config_path_sc09() {
    let native = NativeTraits {
        default_endpoint_is_tcp: false,
        path_flavor: PathFlavor::Posix,
    };
    const USER: &str = "/cfg/config.yaml";
    const SYS: &str = "/etc/serialwrap/config.yaml";
    let cases: &[(&str, &[(&str, &str)], &[&str], &str)] = &[
        ("explicit_missing_wins", &[("SERIALWRAP_CONFIG", "~/x.yaml")], &[USER, SYS], "/h/x.yaml"),
        ("explicit_abs", &[("SERIALWRAP_CONFIG", "/a/b.yaml")], &[], "/a/b.yaml"),
        ("explicit_blank_ignored", &[("SERIALWRAP_CONFIG", "  ")], &[USER, SYS], USER),
        ("user_beats_system", &[], &[USER, SYS], USER),
        ("system_fallback", &[], &[SYS], SYS),
        ("neither_defaults_user", &[], &[], USER),
    ];
    for (id, extra, existing, want) in cases {
        let mut pairs: Vec<(&str, &str)> = vec![("HOME", "/h"), ("SERIALWRAP_CONFIG_DIR", "/cfg")];
        pairs.extend_from_slice(extra);
        let env = env_of(&pairs);
        let paths = RuntimePaths::resolve(&env, native, &H).expect("paths");
        assert_eq!(paths.config_yaml, USER, "{id}: config_yaml");
        let exists = |p: &str| existing.contains(&p);
        let got = sw_proto::runtime_paths::resolve_config_path(
            &env,
            &paths,
            "/etc/serialwrap",
            PathFlavor::Posix,
            &H,
            &exists,
        );
        assert_eq!(&got, want, "{id}");
    }
}
