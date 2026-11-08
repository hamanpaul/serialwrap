//! 後端選擇對照 `platform_backends._select`（P3-T9；Q-17）。
use sw_pal::select::select_backend;
use sw_pal::BackendKind::{Posix, Win};
use sw_pal::BackendKind;

#[test]
fn select_backend_matches_platform_backends() {
    // (SERIALWRAP_RPC_BACKEND 原值, 原生後端, 期望結果)；空字串為 falsy，視同未設定。
    let table: &[(Option<&str>, BackendKind, Result<BackendKind, &str>)] = &[
        (None, Posix, Ok(Posix)),
        (Some("auto"), Win, Ok(Win)),
        (Some("POSIX"), Win, Ok(Posix)),
        (Some("unix"), Win, Ok(Posix)),
        (Some("termios"), Win, Ok(Posix)),
        (Some("Win32"), Posix, Ok(Win)),
        (Some("windows"), Posix, Ok(Win)),
        (Some("win"), Posix, Ok(Win)),
        (Some(""), Posix, Ok(Posix)),
        (Some("bsd"), Posix, Err("unsupported backend: 'bsd'")),
    ];
    for (raw, native, want) in table {
        let got = select_backend(*raw, *native);
        let want = want.clone().map_err(str::to_string);
        assert_eq!(got, want, "raw={raw:?} native={native:?}");
    }
}
