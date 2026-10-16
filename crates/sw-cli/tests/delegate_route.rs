//! 委派路由分類測試（P3-T10）。
use std::ffi::OsString;
use sw_cli::delegate::{classify, Route};

fn v(s: &str) -> Vec<OsString> {
    s.split_whitespace().map(OsString::from).collect()
}

#[test]
fn classify_table() {
    let d = |g| Route::Delegate { group: g };
    let cases: &[(&str, Route)] = &[
        ("", Route::Native),
        ("session list", Route::Native),
        ("session console-attach --selector A", Route::Native),
        ("daemon status", d("daemon")),
        ("--socket daemon session list", Route::Native),
        ("--sock x service status", d("service")),
        ("--socket=x remote", d("remote")),
        ("-vv setup", d("setup")),
        ("-- doctor", d("doctor")),
        ("-h", Route::Native),
        ("--version daemon", d("daemon")),
        ("--timeout -1 connect x", d("connect")),
        ("--v benches", d("benches")),
        ("--bench", Route::Native),
        ("--unknown supervision-mode", d("supervision-mode")),
        ("--retries=2 connect", d("connect")),
        ("--socket connect", Route::Native),
        ("--endpoint=connect", Route::Native),
        ("--ret 1 daemon start", d("daemon")),
        ("profile test --profile p", Route::Native),
    ];
    for (argv, want) in cases {
        assert_eq!(classify(&v(argv)), *want, "{argv:?}");
    }
}

#[test]
fn non_utf8_tokens() {
    let d = |g| Route::Delegate { group: g };
    let bad = sw_pal::fakes::non_utf8_os_string(&[0xff]);
    assert_eq!(
        classify(&[OsString::from("--socket"), bad.clone(), OsString::from("daemon")]),
        d("daemon")
    );
    assert_eq!(
        classify(&[bad.clone(), OsString::from("daemon")]),
        Route::Native
    );
    assert_eq!(
        classify(&[OsString::from("setup"), OsString::from("--user"), bad]),
        d("setup")
    );
}
