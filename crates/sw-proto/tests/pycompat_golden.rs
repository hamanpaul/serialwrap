mod common;
use sw_proto::{
    pycompat as pc, pyfmt,
    pypath::{self, PathFlavor},
};

struct FakeHome;
impl pypath::HomeLookup for FakeHome {
    fn current_user_home(&self) -> Option<String> {
        Some("/golden/pw-home".into())
    }
    fn user_home(&self, u: &str) -> Option<String> {
        (u == "bob").then(|| "/golden/bob-home".into())
    }
}

fn check(file: &str, f: impl Fn(&str, &str) -> String) {
    let fails: Vec<String> = common::golden(file)
        .into_iter()
        .filter_map(|r| {
            let got = f(&r.op, &r.input);
            (got != r.expected).then(|| {
                format!(
                    "{} {} {:?}: got {:?} want {:?}",
                    r.id, r.op, r.input, got, r.expected
                )
            })
        })
        .collect();
    assert!(
        fails.is_empty(),
        "{} 筆不符：\n{}",
        fails.len(),
        fails[..fails.len().min(20)].join("\n")
    );
}

#[test]
fn pycompat_golden() {
    check("pycompat.tsv", |op, s| match op {
        "int" => pc::py_int_parse(s)
            .map(|i| sw_proto::pyjson::dumps(&i.into(), &sw_proto::pyjson::RPC_WIRE))
            .unwrap_or("ERR".into()),
        "float" => pc::py_float_parse(s).map(pyfmt::py_repr_float).unwrap_or("ERR".into()),
        "repr_str" => pyfmt::py_repr_str(s),
        "strip" => pc::py_strip(s).to_string(),
        "splitlines" => sw_proto::pyjson::dumps(
            &pc::splitlines(s)
                .into_iter()
                .map(|x| x.into())
                .collect::<Vec<_>>()
                .into(),
            &sw_proto::pyjson::RPC_EARLY,
        ),
        "decode_replace" => pc::decode_utf8_replace(&common::unhex_pub(s)),
        "decode_strict" => match pc::decode_utf8_strict(&common::unhex_pub(s)) {
            Ok(t) => t,
            Err(e) => e.to_string(),
        },
        "py_str" => pyfmt::py_str(&sw_proto::pyjson::loads(s).unwrap()),
        "py_repr" => pyfmt::py_repr(&sw_proto::pyjson::loads(s).unwrap()),
        "oserror" => common::oserror_from_case(s).to_string(),
        other => panic!("unknown op {other}"),
    });
}

#[test]
fn pypath_posix_golden() {
    let env = sw_proto::runtime_paths::EnvSnapshot::from_pairs([(
        "HOME".to_string(),
        "/golden/home".to_string(),
    )]);
    check("pypath.posix.tsv", |op, s| {
        common::pypath_dispatch(PathFlavor::Posix, op, s, &env, &FakeHome)
    });
}

#[test]
fn pypath_windows_golden() {
    // 與 gen_pycompat.py 的前提一致：USERPROFILE=C:\Users\golden、USERNAME=golden。
    let env = sw_proto::runtime_paths::EnvSnapshot::from_pairs([
        ("USERPROFILE".to_string(), r"C:\Users\golden".to_string()),
        ("USERNAME".to_string(), "golden".to_string()),
    ]);
    check("pypath.windows.tsv", |op, s| {
        common::pypath_dispatch(PathFlavor::Windows, op, s, &env, &FakeHome)
    });
}

#[test]
fn py_repr_float_forwards_to_pyjson() {
    for x in [1.5, 1e16, -0.0, f64::NAN, f64::INFINITY] {
        assert_eq!(pyfmt::py_repr_float(x), sw_proto::pyjson::repr_float(x));
    }
}
