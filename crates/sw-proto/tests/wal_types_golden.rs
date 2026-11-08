mod common;

use sw_proto::pyjson::{PyDict, PyValue};
use sw_proto::wal_types::{wal_recreated_meta, WalRecord};

fn parse_wal_record_input(s: &str) -> WalRecord {
    let parts: Vec<&str> = s.split('\x1f').collect();
    assert_eq!(parts.len(), 13, "WAL record input 必須包含 13 欄");

    let seq: u64 = parts[0].parse().expect("valid seq");
    let mono_ts_ns: u64 = parts[1].parse().expect("valid mono_ts_ns");
    let wall_ts = parts[2].to_string();
    let com = if parts[3].is_empty() {
        None
    } else {
        Some(parts[3].to_string())
    };
    let dir = parts[4].to_string();
    let source = parts[5].to_string();
    let cmd_id = if parts[6].is_empty() {
        None
    } else {
        Some(parts[6].to_string())
    };
    let len: u64 = parts[7].parse().expect("valid len");
    let crc32_hex = parts[8].to_string();
    let payload_b64 = parts[9].to_string();
    let loss_flag = parts[10] == "true";

    // meta 以 pyjson 解析（保留布林、整數、null 與 list 型別）。
    // 舊版為 stub 階段的簡易切分，會把所有值誤判為字串，且無法處理 list／null。
    let meta_json = parts[11];
    let meta = match sw_proto::pyjson::loads(meta_json).expect("meta 必須為合法 JSON") {
        PyValue::Dict(d) => d,
        other => panic!("meta 必須為 JSON 物件，實際為 {other:?}"),
    };

    let rotation_failed = if parts[12].is_empty() {
        None
    } else {
        Some(parts[12] == "true")
    };

    WalRecord {
        seq,
        mono_ts_ns,
        wall_ts,
        com,
        dir,
        source,
        cmd_id,
        len,
        crc32_hex,
        payload_b64,
        loss_flag,
        meta,
        rotation_failed,
    }
}

#[test]
fn test_wal_record_to_line_golden() {
    let mut fails = Vec::new();
    for r in common::golden("wal_line.tsv") {
        let record = parse_wal_record_input(&r.input);
        let got = record.to_line();
        if got != r.expected {
            fails.push(format!("{}: {}\n got  {got}\n want {}", r.id, r.input, r.expected));
        }
    }
    assert!(fails.is_empty(), "{}", fails.join("\n"));
}

/// 對齊 `contracts/wal_meta_events.schema.json` 的 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 分支（arch §3.8，BC-01）：
/// 恰五個 key；`range_known` 為真時 `potentially_lost_seq_range` 為 `[first, last]`，否則為 null（不得猜測）。
#[test]
fn test_wal_recreated_meta() {
    fn int(n: i128) -> PyValue {
        PyValue::Int(sw_proto::pyjson::PyInt::Small(n))
    }
    fn keys(m: &PyDict) -> Vec<&str> {
        let mut k: Vec<&str> = m.0.keys().map(String::as_str).collect();
        k.sort();
        k
    }
    let want_keys = vec!["event", "potentially_lost_seq_range", "previous_identity", "range_known", "reason"];

    let meta = wal_recreated_meta("external_unlink", Some("12:34"), Some((3, 9)));
    assert_eq!(keys(&meta), want_keys);
    assert_eq!(meta.get("event"), Some(&PyValue::Str("WAL_RECREATED_AFTER_EXTERNAL_UNLINK".into())));
    assert_eq!(meta.get("reason"), Some(&PyValue::Str("external_unlink".into())));
    assert_eq!(meta.get("previous_identity"), Some(&PyValue::Str("12:34".into())));
    assert_eq!(meta.get("range_known"), Some(&PyValue::Bool(true)));
    assert_eq!(meta.get("potentially_lost_seq_range"), Some(&PyValue::List(vec![int(3), int(9)])));

    let unknown = wal_recreated_meta("dir_missing", None, None);
    assert_eq!(keys(&unknown), want_keys);
    assert_eq!(unknown.get("reason"), Some(&PyValue::Str("dir_missing".into())));
    assert_eq!(unknown.get("previous_identity"), Some(&PyValue::Null));
    assert_eq!(unknown.get("range_known"), Some(&PyValue::Bool(false)));
    assert_eq!(unknown.get("potentially_lost_seq_range"), Some(&PyValue::Null));

    // 範圍邊界（審查 A-5）：Python `last_seq >= first_seq` 與 schema `first <= last`、`minimum: 1` 的交集。
    // 反向區間與 0 起點不得視為已知；`first == last` 為已知。
    for range in [(9, 3), (0, 5)] {
        let bad = wal_recreated_meta("replaced", None, Some(range));
        assert_eq!(
            bad.get("range_known"),
            Some(&PyValue::Bool(false)),
            "range {range:?}"
        );
        assert_eq!(
            bad.get("potentially_lost_seq_range"),
            Some(&PyValue::Null),
            "range {range:?}"
        );
    }
    let single = wal_recreated_meta("replaced", None, Some((4, 4)));
    assert_eq!(single.get("range_known"), Some(&PyValue::Bool(true)));
    assert_eq!(
        single.get("potentially_lost_seq_range"),
        Some(&PyValue::List(vec![int(4), int(4)]))
    );
}
