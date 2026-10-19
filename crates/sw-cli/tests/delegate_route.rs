//! 委派路由分類測試（P3-T10）；遞迴防護與 argv 指紋測試（P3-T16 追加）。
use std::ffi::OsString;
use sw_cli::delegate::{argv_sha256, check_recursion, classify, DelegateError, Route};

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
        // P4-T6：service 已原生（不再委派）；委派路由以仍委派的 doctor 保留覆蓋。
        ("--sock x service status", Route::Native),
        ("--sock x doctor", d("doctor")),
        ("--socket=x remote", Route::Native),
        ("remote status", Route::Native),
        ("-vv setup", d("setup")),
        ("-- doctor", d("doctor")),
        ("-h", Route::Native),
        ("--version daemon", d("daemon")),
        ("--timeout -1 connect x", d("connect")),
        ("--v benches", d("benches")),
        ("--bench", Route::Native),
        ("--unknown supervision-mode", Route::Native),
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

#[test]
fn recursion_rules() {
    assert_eq!(check_recursion(None, None, "s"), Ok(0));
    assert_eq!(check_recursion(Some("1"), Some("other"), "s"), Ok(1));
    assert_eq!(check_recursion(Some("2"), None, "s"), Err(DelegateError::Recursion));
    assert_eq!(check_recursion(Some("0"), Some("s"), "s"), Err(DelegateError::Recursion));
    assert_eq!(check_recursion(Some("x"), None, "s"), Err(DelegateError::Recursion));
    assert_eq!(check_recursion(Some("１"), None, "s"), Err(DelegateError::Recursion));
}

#[test]
fn argv_sha_stable() {
    let a = argv_sha256(&v("setup --user x"));
    assert_eq!(a.len(), 64);
    assert_eq!(a, argv_sha256(&v("setup --user x")));
    assert_ne!(a, argv_sha256(&v("setup --user y")));
    assert_ne!(argv_sha256(&v("a b")), argv_sha256(&v("ab")));   // 分隔符生效
}
