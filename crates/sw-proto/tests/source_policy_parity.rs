use sw_proto::pyjson::{self, PyValue, RPC_WIRE};
use sw_proto::source_policy::{check_source, classify_source};

#[test]
fn source_policy_proto_public_api_matches_python() {
    let golden = include_str!("../../../contracts/golden/p5/source_policy.ndjson");
    let mut checked = 0;

    for (index, line) in golden.lines().enumerate() {
        let expected = pyjson::loads(line)
            .unwrap_or_else(|error| panic!("golden 第 {} 行 JSON 無效：{error}", index + 1));
        let input = &expected["input"];
        let raw = match &input["raw"] {
            PyValue::Null => None,
            PyValue::Str(value) => Some(value.as_str()),
            other => panic!("golden 第 {} 行 raw 型別無效：{other:?}", index + 1),
        };
        let default = input["default"].as_str().expect("default 應為字串");
        let field = input["field"].as_str().expect("field 應為字串");
        let normalize_human = input["normalize_human"]
            .as_bool()
            .expect("normalize_human 應為布林值");
        let actual = check_source(raw, default, field, normalize_human);

        assert_eq!(
            actual.source,
            expected["source"].as_str().unwrap(),
            "第 {} 行 source",
            index + 1
        );
        assert_eq!(
            actual.normalized_from.as_deref(),
            expected["normalized_from"].as_str(),
            "第 {} 行 normalized_from",
            index + 1
        );
        assert_eq!(
            actual.raw_source.as_deref(),
            expected["raw_source"].as_str(),
            "第 {} 行 raw_source",
            index + 1
        );
        assert_eq!(
            actual.is_bare_human(),
            expected["is_bare_human"].as_bool().unwrap(),
            "第 {} 行 is_bare_human",
            index + 1
        );
        assert_eq!(
            classify_source(&actual.source),
            expected["source_class"].as_str().unwrap(),
            "第 {} 行 source_class",
            index + 1
        );

        let actual_error = PyValue::from(actual.error);
        assert_eq!(
            pyjson::dumps(&actual_error, &RPC_WIRE).as_bytes(),
            pyjson::dumps(&expected["error"], &RPC_WIRE).as_bytes(),
            "第 {} 行完整 error RPC_WIRE 位元組",
            index + 1
        );
        checked += 1;
    }

    assert!(checked > 0, "Python golden 必須至少有一列");
}
