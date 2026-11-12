//! 更新路徑的 daemon 範圍選取器；只由 `test-fakes` 建置編入。
#![allow(dead_code)] // 本項先建立共用入口，呼叫點由後續子項接線。

use crate::ctx::CliCtx;
use std::path::Path;
use sw_pal::proc_scan::DaemonProc;
use sw_proto::endpoint::{parse_endpoint, Endpoint};
use sw_proto::pypath::PathFlavor;
use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths};

const TEST_UPDATE_SOCKET_ENV: &str = "SERIALWRAP_TEST_UPDATE_SOCKET";
const ISOLATION_ENV_VARS: [&str; 3] = [
    "SERIALWRAP_STATE_DIR",
    "SERIALWRAP_RUN_DIR",
    "SERIALWRAP_CONFIG_DIR",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpdateScopeError(String);

impl std::fmt::Display for UpdateScopeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for UpdateScopeError {}

/// 套用測試專用 daemon 範圍；未設定時完整保留掃描結果。
pub fn select_for_update(
    ctx: &CliCtx<'_>,
    operation_endpoint: &str,
    daemons: &[DaemonProc],
) -> Result<Vec<DaemonProc>, UpdateScopeError> {
    let Some(configured_socket) = ctx.env.get(TEST_UPDATE_SOCKET_ENV) else {
        return Ok(daemons.to_vec());
    };
    if configured_socket.is_empty() {
        return Ok(daemons.to_vec());
    }

    if ctx.p.caps.path_flavor != sw_pal::PalPathFlavor::Posix || !ctx.p.caps.af_unix {
        return Err(UpdateScopeError(
            "測試 daemon 範圍只接受 POSIX Unix socket".to_string(),
        ));
    }

    let run_dir = ctx
        .env
        .get("SERIALWRAP_RUN_DIR")
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| {
            UpdateScopeError("已設定測試 socket，但 SERIALWRAP_RUN_DIR 未設定".to_string())
        })?;
    let cwd = std::env::current_dir().map_err(|error| {
        UpdateScopeError(format!("無法取得目前目錄以正規化測試 socket：{error}"))
    })?;
    let home = ctx
        .env
        .get("HOME")
        .map(str::to_string)
        .or_else(|| ctx.p.env.current_user_home());

    let default_env = EnvSnapshot::from_pairs(
        ctx.p
            .env
            .vars()
            .into_iter()
            .filter(|(key, _)| !ISOLATION_ENV_VARS.contains(&key.as_str())),
    );
    let default_paths = RuntimePaths::resolve(
        &default_env,
        NativeTraits {
            default_endpoint_is_tcp: false,
            path_flavor: PathFlavor::Posix,
        },
        &crate::PlatformHome(ctx.p.env.as_ref()),
    )
    .map_err(|error| {
        UpdateScopeError(format!(
            "無法驗證測試 SERIALWRAP_RUN_DIR 隔離狀態：{error:?}"
        ))
    })?;

    let normalized_run_dir = normalize_path(run_dir, &cwd, home.as_deref());
    let normalized_default_run_dir = normalize_path(&default_paths.run_dir, &cwd, home.as_deref());
    if paths_overlap(&normalized_run_dir, &normalized_default_run_dir) {
        return Err(UpdateScopeError(
            "測試 SERIALWRAP_RUN_DIR 與正式預設執行目錄重疊".to_string(),
        ));
    }

    let target = normalize_unix_socket(configured_socket, &cwd, home.as_deref())?;
    let operation_target = normalize_unix_socket(operation_endpoint, &cwd, home.as_deref())?;
    if target != operation_target {
        return Err(UpdateScopeError(
            "測試 socket 與本次操作 endpoint 不一致".to_string(),
        ));
    }

    let target_path = Path::new(&target);
    let run_path = Path::new(&normalized_run_dir);
    if !target_path
        .strip_prefix(run_path)
        .is_ok_and(|relative| !relative.as_os_str().is_empty())
    {
        return Err(UpdateScopeError(
            "測試 socket 不在隔離 SERIALWRAP_RUN_DIR 之下".to_string(),
        ));
    }

    Ok(daemons
        .iter()
        .filter(|daemon| {
            daemon.socket.as_deref().is_some_and(|socket| {
                normalize_unix_socket(socket, &cwd, home.as_deref())
                    .is_ok_and(|normalized| normalized == target)
            })
        })
        .cloned()
        .collect())
}

fn normalize_path(path: &str, cwd: &Path, home: Option<&str>) -> String {
    crate::commands::daemon_cmd::normalize_endpoint_for_compare_with_home(
        path,
        cwd,
        PathFlavor::Posix,
        home,
    )
}

fn paths_overlap(left: &str, right: &str) -> bool {
    let left = Path::new(left);
    let right = Path::new(right);
    left.starts_with(right) || right.starts_with(left)
}

fn normalize_unix_socket(
    endpoint: &str,
    cwd: &Path,
    home: Option<&str>,
) -> Result<String, UpdateScopeError> {
    let Endpoint::Unix(path) = parse_endpoint(endpoint).map_err(|error| {
        UpdateScopeError(format!("測試 socket 不是有效 Unix endpoint：{error:?}"))
    })?
    else {
        return Err(UpdateScopeError(
            "測試 socket 必須是 Unix endpoint".to_string(),
        ));
    };
    if path.is_empty() {
        return Err(UpdateScopeError(
            "測試 Unix socket 路徑不可為空".to_string(),
        ));
    }
    Ok(normalize_path(&path, cwd, home))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ctx::TextIo;
    use sw_pal::fakes::{platform, sysenv::FakeSysEnv};
    use sw_pal::traits::SysEnv;
    use sw_proto::pypath::PathFlavor;
    use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths};

    const HOME: &str = "/tmp/sw-gpa3/home";
    const RUN_DIR: &str = "/tmp/sw-gpa3/scope-test/run";
    const DEFAULT_RUN_DIR: &str = "/tmp/sw-gpa3/home/.local/state/serialwrap/run";
    const SOCKET: &str = "/tmp/sw-gpa3/scope-test/run/serialwrapd.sock";

    fn daemon(pid: u32, socket: Option<&str>, start_ticks: Option<u64>) -> DaemonProc {
        DaemonProc {
            pid,
            socket: socket.map(str::to_string),
            tree: vec![sw_pal::proc_scan::ProcIdentity { pid, start_ticks }],
        }
    }

    fn with_context<R>(
        scope: Option<&str>,
        run_dir: Option<&str>,
        f: impl FnOnce(&CliCtx<'_>) -> R,
    ) -> R {
        let mut builder = FakeSysEnv::builder()
            .home(Path::new(HOME))
            .var("HOME", HOME);
        if let Some(scope) = scope {
            builder = builder.var(TEST_UPDATE_SOCKET_ENV, scope);
        }
        if let Some(run_dir) = run_dir {
            builder = builder.var("SERIALWRAP_RUN_DIR", run_dir);
        }
        let sysenv = builder.build();
        let platform = platform().env(sysenv.clone()).build();
        let env = EnvSnapshot::from_pairs(sysenv.vars());
        let native = NativeTraits {
            default_endpoint_is_tcp: false,
            path_flavor: PathFlavor::Posix,
        };
        let home = crate::PlatformHome(platform.env.as_ref());
        let paths = RuntimePaths::resolve(&env, native, &home).expect("測試環境路徑應有效");
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let ctx = CliCtx {
            p: &platform,
            env,
            paths,
            io: TextIo::new(&mut stdout, &mut stderr, "\n"),
            verbose: 0,
            cli_version: "test",
            autostart_lock: None,
        };
        f(&ctx)
    }

    #[test]
    fn unset_or_empty_scope_preserves_the_full_scan() {
        let procs = vec![
            daemon(11, Some(SOCKET), Some(101)),
            daemon(12, Some("/tmp/external/serialwrapd.sock"), Some(102)),
            daemon(13, None, Some(103)),
        ];
        with_context(None, None, |ctx| {
            assert_eq!(select_for_update(ctx, SOCKET, &procs).unwrap(), procs);
        });
        with_context(Some(""), None, |ctx| {
            assert_eq!(select_for_update(ctx, SOCKET, &procs).unwrap(), procs);
        });
    }

    #[test]
    fn configured_scope_must_match_the_operation_endpoint() {
        with_context(Some(SOCKET), Some(RUN_DIR), |ctx| {
            let error = select_for_update(ctx, "/tmp/other/run/serialwrapd.sock", &[])
                .expect_err("不同 endpoint 必須 fail closed");
            assert!(!error.0.is_empty());
        });
    }

    #[test]
    fn scoped_scan_keeps_only_matching_socket_and_preserves_process_identity() {
        let target = daemon(21, Some(SOCKET), Some(2101));
        let external = daemon(22, Some("/tmp/external/serialwrapd.sock"), Some(2201));
        let unknown = daemon(23, None, Some(2301));
        with_context(Some(SOCKET), Some(RUN_DIR), |ctx| {
            assert_eq!(
                select_for_update(ctx, SOCKET, &[target.clone(), external, unknown]).unwrap(),
                vec![target]
            );
        });
    }

    #[test]
    fn successor_with_the_same_socket_is_kept_even_with_a_new_pid() {
        let first = daemon(31, Some(SOCKET), Some(3101));
        let successor = daemon(32, Some(SOCKET), Some(3201));
        with_context(Some(SOCKET), Some(RUN_DIR), |ctx| {
            assert_eq!(
                select_for_update(ctx, SOCKET, &[first.clone(), successor.clone()]).unwrap(),
                vec![first, successor]
            );
        });
    }

    #[test]
    fn path_and_unix_url_forms_normalize_to_the_same_socket() {
        let socket_with_parent = "/tmp/sw-gpa3/scope-test/run/../run/serialwrapd.sock";
        let expected = daemon(
            41,
            Some("unix:///tmp/sw-gpa3/scope-test/run/serialwrapd.sock"),
            Some(4101),
        );
        with_context(
            Some(&format!("unix://{socket_with_parent}")),
            Some("/tmp/sw-gpa3/scope-test/run/../run"),
            |ctx| {
                assert_eq!(
                    select_for_update(ctx, SOCKET, std::slice::from_ref(&expected)).unwrap(),
                    vec![expected]
                );
            },
        );
    }

    #[test]
    fn missing_default_or_non_isolated_run_dir_rejects_a_configured_scope() {
        with_context(Some(SOCKET), None, |ctx| {
            assert!(select_for_update(ctx, SOCKET, &[]).is_err());
        });
        with_context(Some(SOCKET), Some(DEFAULT_RUN_DIR), |ctx| {
            assert!(select_for_update(ctx, SOCKET, &[]).is_err());
        });
        let broad_run_dir = "/tmp/sw-gpa3/home";
        let socket_below_broad_run =
            "/tmp/sw-gpa3/home/.local/state/serialwrap/run/serialwrapd.sock";
        with_context(Some(socket_below_broad_run), Some(broad_run_dir), |ctx| {
            assert!(select_for_update(ctx, socket_below_broad_run, &[]).is_err());
        });
    }

    #[test]
    fn socket_outside_the_isolated_run_dir_is_rejected() {
        with_context(
            Some("/tmp/external/serialwrapd.sock"),
            Some(RUN_DIR),
            |ctx| {
                assert!(select_for_update(ctx, "/tmp/external/serialwrapd.sock", &[]).is_err());
            },
        );
    }

    #[test]
    fn scope_with_non_unix_endpoint_is_rejected() {
        with_context(Some("tcp://127.0.0.1:48700"), Some(RUN_DIR), |ctx| {
            assert!(select_for_update(ctx, "tcp://127.0.0.1:48700", &[]).is_err());
        });
    }
}
