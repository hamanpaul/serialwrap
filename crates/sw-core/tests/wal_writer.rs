//! slice21 Task 7：sw-core `WalWriter` 紅燈測試（BC-01、DF-03、DF-17；R2-F3 部分寫入協定）。
//!
//! 斷言來源：`docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 7（Step 1）。
//! 本檔只描述行為；`WalWriter`、`FakeWalFs`、`FakeClock` 尚為骨架（`todo!()`），
//! 因此所有案例應在執行期因未實作而失敗，而非語法或收集錯誤。

#![cfg(feature = "test-fakes")]

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

mod common;

use common::repo_path;
use sw_core::wal::{
    rows_to_text_lines, WalAppend, WalAppendReq, WalConfig, WalHealthFanout, WalHealthSink,
    WalWriter,
};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::wal_fs::FakeWalFs;
use sw_pal::{FileIdentity, PalError};
use sw_proto::pyjson::{loads, PyDict, PyValue};

const DIR: &str = "/w";
const WAL: &str = "/w/raw.wal.ndjson";
const MIRROR: &str = "/w/raw.mirror.log";
/// 預設 rotate 門檻（`DEFAULT_WAL_ROTATE_BYTES`，§1.1.3）。
const DEFAULT_ROTATE: u64 = 67_108_864;
/// 測試用小門檻（rotate 行為）。
const ROTATE_200: u64 = 200;
const MONO_1: u64 = 5_798_622_898_976;
/// 2026-10-15T03:06:17.663778Z
const WALL_1: i64 = 1_792_033_577_663_778;
/// 2026-10-15T03:06:17Z 的 UTC 歸檔後綴（DF-03：`%Y%m%d-%H%M%S`）。
const SUFFIX_1: &str = "20261015-030617";
const ENOSPC_MSG: &str = "[Errno 28] No space left on device";

// ---------------------------------------------------------------------------
// 共用輔助
// ---------------------------------------------------------------------------

fn p(s: &str) -> &Path {
    Path::new(s)
}

fn writer(rotate: u64) -> (WalWriter, Arc<FakeWalFs>, Arc<FakeClock>) {
    let fs = FakeWalFs::new();
    let clock = FakeClock::at(MONO_1, WALL_1);
    let w = WalWriter::open(
        WalConfig {
            dir: PathBuf::from(DIR),
            rotate_bytes: rotate,
        },
        fs.clone(),
        clock.clone(),
    );
    (w, fs, clock)
}

fn req<'a>(dir: &'a str, source: &'a str, payload: &'a [u8]) -> WalAppendReq<'a> {
    WalAppendReq {
        com: "COM0",
        dir,
        source,
        payload,
        cmd_id: None,
        loss_flag: false,
        meta: None,
    }
}

fn enospc() -> PalError {
    PalError::io(28, ENOSPC_MSG)
}

fn eacces() -> PalError {
    PalError::io(13, "[Errno 13] Permission denied")
}

fn eio() -> PalError {
    PalError::io(5, "[Errno 5] Input/output error")
}

fn read_bytes(fs: &FakeWalFs, path: &str) -> Vec<u8> {
    fs.contents(p(path))
        .unwrap_or_else(|| panic!("{path} 應存在"))
}

fn read_text(fs: &FakeWalFs, path: &str) -> String {
    String::from_utf8(read_bytes(fs, path)).expect("WAL 檔案應為 UTF-8（payload 已 base64）")
}

/// 逐行解析為 JSON object；非 object 行（殘行、壞行）略過。
fn dict_lines(text: &str) -> Vec<PyDict> {
    text.lines()
        .filter_map(|l| match loads(l) {
            Ok(PyValue::Dict(d)) => Some(d),
            _ => None,
        })
        .collect()
}

fn parse_dict(line: &str) -> PyDict {
    match loads(line) {
        Ok(PyValue::Dict(d)) => d,
        other => panic!("預期 JSON object，實得 {other:?}：{line}"),
    }
}

fn seq_of(d: &PyDict) -> Option<i64> {
    d.get("seq").and_then(PyValue::as_i64)
}

fn str_of<'a>(d: &'a PyDict, k: &str) -> Option<&'a str> {
    d.get(k).and_then(PyValue::as_str)
}

fn seqs(text: &str) -> Vec<i64> {
    dict_lines(text).iter().filter_map(seq_of).collect()
}

fn meta_of(d: &PyDict) -> &PyDict {
    match d.get("meta") {
        Some(PyValue::Dict(m)) => m,
        other => panic!("預期 meta 為 object，實得 {other:?}"),
    }
}

fn first_meta_line(text: &str) -> PyDict {
    dict_lines(text)
        .into_iter()
        .find(|d| str_of(d, "dir") == Some("META"))
        .expect("現行檔應含 META 行")
}

fn seq_list(rows: &[PyDict]) -> Vec<i64> {
    rows.iter().filter_map(seq_of).collect()
}

fn int_list(v: Option<&PyValue>) -> Option<Vec<i64>> {
    v?.as_array()?.iter().map(PyValue::as_i64).collect()
}

fn pv_dict(v: &PyValue) -> &PyDict {
    match v {
        PyValue::Dict(d) => d,
        other => panic!("預期 object，實得 {other:?}"),
    }
}

fn has_archive(fs: &FakeWalFs, base: &str, suffix: &str) -> bool {
    fs.contents(p(&format!("{base}.{suffix}"))).is_some()
}

const MS_60: Duration = Duration::from_millis(60);

/// 健康狀態 fence 的測試替身（以 `WalHealthSink` 模擬 session fence；TxWriter／Fence 屬 Task 9）。
#[derive(Default)]
struct Fence(Mutex<bool>);

impl Fence {
    fn flag(&self) -> bool {
        *self.0.lock().unwrap()
    }
}

impl WalHealthSink for Fence {
    fn set_wal_unhealthy(&self, unhealthy: bool) {
        *self.0.lock().unwrap() = unhealthy;
    }
}

// ---------------------------------------------------------------------------
// 程式碼案例（5）
// ---------------------------------------------------------------------------

#[test]
fn rx_record_bytes_match_python_golden() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    let a = w.append(req("RX", "uart", b"hello\r\n\x1b[0m\x0b\x7f\xff"));
    assert_eq!(
        a,
        WalAppend {
            seq: 1,
            ok: true,
            loss_flag: false
        }
    );
    assert_eq!(
        read_text(&fs, WAL),
        "{\"cmd_id\":null,\"com\":\"COM0\",\"crc32\":\"a81bc28c\",\"dir\":\"RX\",\"len\":14,\"loss_flag\":false,\"meta\":{},\"mono_ts_ns\":5798622898976,\"payload_b64\":\"aGVsbG8NChtbMG0Lf/8=\",\"seq\":1,\"source\":\"uart\",\"wall_ts\":\"2026-10-15T03:06:17.663778+00:00\"}\n"
    );
    assert_eq!(
        read_bytes(&fs, MIRROR),
        b"hello\r\n\\x1b[0m\\x0b\\x7f\\xff".to_vec()
    );
}

#[test]
fn meta_empty_payload_and_sorted_meta_keys() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    let mut m = PyDict::new();
    m.insert("z", PyValue::from(1_i64));
    m.insert("a", PyValue::from("中"));
    w.append(WalAppendReq {
        com: "COM0",
        dir: "TX",
        source: "agent:x",
        payload: "é".as_bytes(),
        cmd_id: Some("c1"),
        loss_flag: false,
        meta: Some(m),
    });
    let mut ev = PyDict::new();
    ev.insert("event", PyValue::from("x"));
    w.append(WalAppendReq {
        com: "COM0",
        dir: "META",
        source: "system",
        payload: b"",
        cmd_id: None,
        loss_flag: false,
        meta: Some(ev),
    });
    let text = read_text(&fs, WAL);
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines.len(), 2, "{text}");
    assert!(
        lines[0].starts_with("{\"cmd_id\":\"c1\",\"com\":\"COM0\",\"crc32\":\"0e048d3e\",\"dir\":\"TX\",\"len\":2,\"loss_flag\":false,\"meta\":{\"a\":\"中\",\"z\":1},"),
        "{}",
        lines[0]
    );
    assert!(
        lines[1].contains("\"crc32\":\"00000000\",\"dir\":\"META\",\"len\":0,"),
        "{}",
        lines[1]
    );
    assert!(lines[1].contains("\"payload_b64\":\"\","), "{}", lines[1]);
    // META（空 payload）不寫 mirror
    assert_eq!(read_bytes(&fs, MIRROR), b"\\xc3\\xa9".to_vec());
}

#[test]
fn external_unlink_writes_recreate_meta_then_record() {
    let (w, fs, clock) = writer(DEFAULT_ROTATE);
    for _ in 0..3 {
        w.append(req("RX", "device", b"x"));
    }
    fs.external_unlink(p(WAL));
    clock.advance(MS_60); // 超過 50 ms 節流
    let a = w.append(req("RX", "device", b"y"));
    assert_eq!(a.seq, 5);
    let text = read_text(&fs, WAL);
    let first = parse_dict(text.lines().next().expect("重建後檔案至少含一行"));
    assert_eq!(seq_of(&first), Some(4));
    assert_eq!(str_of(&first, "dir"), Some("META"));
    // P1 T05 凍結：外層 com 為 "WAL"，不出現在 COM0 的 tail
    assert_eq!(str_of(&first, "com"), Some("WAL"));
    assert_eq!(str_of(&first, "source"), Some("system"));
    assert_eq!(first.get("loss_flag"), Some(&PyValue::Bool(true)));
    let meta = meta_of(&first);
    assert_eq!(
        str_of(meta, "event"),
        Some("WAL_RECREATED_AFTER_EXTERNAL_UNLINK")
    );
    assert_eq!(str_of(meta, "reason"), Some("external_unlink"));
    assert_eq!(
        int_list(meta.get("potentially_lost_seq_range")),
        Some(vec![1, 3])
    );
    assert_eq!(meta.get("range_known"), Some(&PyValue::Bool(true)));
    assert_eq!(w.health().recreated_count, 1);
}

#[test]
fn reopen_failure_marks_unhealthy_and_trips_sink() {
    struct Recorder(Mutex<Vec<bool>>);
    impl WalHealthSink for Recorder {
        fn set_wal_unhealthy(&self, unhealthy: bool) {
            self.0.lock().unwrap().push(unhealthy);
        }
    }
    let (w, fs, clock) = writer(DEFAULT_ROTATE);
    let fan = WalHealthFanout::new();
    w.set_health_sink(fan.clone());
    let sink = Arc::new(Recorder(Mutex::new(Vec::new())));
    fan.register(sink.clone());
    w.append(req("RX", "device", b"x"));
    fs.external_unlink(p(WAL));
    fs.fail_next_opens(p(WAL), 3, enospc());
    clock.advance(MS_60);
    let a = w.append(req("RX", "device", b"y"));
    assert!(!a.ok && a.loss_flag);
    let h = w.health();
    assert!(!h.healthy);
    assert_eq!(h.unhealthy_reason, Some("recreate_failed"));
    assert_eq!(
        h.last_write_error.as_deref(),
        Some("OSError: [Errno 28] No space left on device")
    );
    // register 時初始化為 false，之後轉為 true
    assert_eq!(*sink.0.lock().unwrap(), vec![false, true]);
}

#[test]
fn wal_partial_write_suffix_failure_terminates_tail_next_record_parses() {
    // R2-F3；對應 P1 T02 test_partial_write_suffix_failure_terminates_tail
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    fs.fail_next_write_partial(p(WAL), 10, enospc()); // 第一次只寫出 JSON 前綴 10 bytes 即 ENOSPC
    fs.fail_next_writes(p(WAL), 1, enospc()); // 補寫剩餘部分也失敗
    let a = w.append(req("RX", "device", b"x"));
    assert_eq!(
        a,
        WalAppend {
            seq: 1,
            ok: false,
            loss_flag: true
        }
    );
    let calls = fs.write_calls(p(WAL));
    let line1 = calls[0].clone();
    // 只補寫 data[sent..]，絕不整行重寫
    assert_eq!(calls, vec![line1.clone(), line1[10..].to_vec()]);
    // 補寫在同一 handle，不重開
    assert_eq!(fs.open_calls(p(WAL)), 1);
    // 檔案只有前綴，沒有「前綴＋完整行」
    assert_eq!(read_bytes(&fs, WAL), line1[..10].to_vec());
    let h = w.health();
    assert_eq!(h.write_failures, 1);
    // 補寫失敗不得標為健康
    assert!(!h.healthy);
    assert_eq!(h.unhealthy_reason, Some("write_failed"));
    assert_eq!(
        h.last_write_error.as_deref(),
        Some("OSError: [Errno 28] No space left on device")
    );

    let b = w.append(req("RX", "device", b"y"));
    assert_eq!(
        b,
        WalAppend {
            seq: 2,
            ok: true,
            loss_flag: false
        }
    );
    let calls = fs.write_calls(p(WAL));
    assert_eq!(calls.len(), 3);
    // 先終結殘行（dirty_tail，arch §5.8 第 5 點）
    assert_eq!(calls[2][0], b'\n');
    let mut expect = line1[..10].to_vec();
    expect.extend_from_slice(&calls[2]);
    let bytes = read_bytes(&fs, WAL);
    assert_eq!(bytes, expect);
    let text = String::from_utf8(bytes).unwrap();
    let lines: Vec<&str> = text.split_terminator('\n').collect();
    assert_eq!(lines.len(), 2);
    // 殘行獨立成行，讀取端判為無效
    assert!(loads(lines[0]).is_err());
    let rec = parse_dict(lines[1]);
    assert_eq!(seq_of(&rec), Some(2));
    // 無串接的 NDJSON 行
    assert!(lines.iter().all(|l| l.matches("\"seq\":").count() <= 1));
    // 讀取 API 略過殘行、只回 seq 2
    let (rows, _) = w.tail_raw_with_meta(None, None, 100).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(seq_of(&rows[0]), Some(2));
    let h = w.health();
    assert!(h.healthy);
    assert_eq!(h.unhealthy_reason, None);
}

// ---------------------------------------------------------------------------
// 表格案例（35）
// ---------------------------------------------------------------------------

#[test]
fn wall_ts_omits_zero_microseconds() {
    let fs = FakeWalFs::new();
    let clock = FakeClock::at(MONO_1, 1_792_033_577_000_000); // ...:17.000000
    let w = WalWriter::open(
        WalConfig {
            dir: PathBuf::from(DIR),
            rotate_bytes: DEFAULT_ROTATE,
        },
        fs.clone(),
        clock.clone(),
    );
    w.append(req("RX", "uart", b"x"));
    let text = read_text(&fs, WAL);
    assert!(
        text.contains("\"wall_ts\":\"2026-10-15T03:06:17+00:00\""),
        "{text}"
    );
}

#[test]
fn rotate_before_write_at_threshold_utc_suffix() {
    let (w, fs, _) = writer(ROTATE_200);
    w.append(req("RX", "uart", b"x"));
    let size = read_bytes(&fs, WAL).len();
    assert!(
        size as u64 >= ROTATE_200,
        "前置條件：第一筆後檔案應已達 rotate 門檻（實際 {size}）"
    );
    // 寫前檢查 → 輪替
    w.append(req("RX", "uart", b"x"));
    assert!(has_archive(&fs, WAL, SUFFIX_1), "WAL 應以 UTC 後綴歸檔");
    // 新檔第一筆 seq = 舊檔最後 seq + 1
    assert_eq!(seqs(&read_text(&fs, WAL)).first(), Some(&2));
    // mirror 依自身大小獨立判斷：未達門檻不輪替
    assert!(!has_archive(&fs, MIRROR, SUFFIX_1));
    assert_eq!(read_bytes(&fs, MIRROR), b"xx".to_vec());
}

#[test]
fn rotate_collision_adds_numeric_suffix() {
    let (w, fs, _) = writer(ROTATE_200);
    w.append(req("RX", "uart", b"x")); // seq 1
    w.append(req("RX", "uart", b"x")); // seq 2：輪替 → seq 1 歸檔
    w.append(req("RX", "uart", b"x")); // seq 3：同一秒再次輪替 → seq 2 歸檔為 .1
    let a1 = read_text(&fs, &format!("{WAL}.{SUFFIX_1}"));
    let a2 = read_text(&fs, &format!("{WAL}.{SUFFIX_1}.1"));
    assert_eq!(seqs(&a1), vec![1]);
    assert_eq!(seqs(&a2), vec![2]);
    assert_eq!(seqs(&read_text(&fs, WAL)), vec![3]);
}

#[test]
fn rotate_rename_failure_marks_record() {
    let (w, fs, _) = writer(ROTATE_200);
    w.append(req("RX", "uart", b"x")); // seq 1，達門檻
    fs.fail_next_renames(1, eacces());
    let a = w.append(req("RX", "uart", b"x")); // 輪替 rename 失敗 → best-effort 續用原檔
    assert_eq!(
        a,
        WalAppend {
            seq: 2,
            ok: true,
            loss_flag: false
        }
    );
    assert!(!has_archive(&fs, WAL, SUFFIX_1), "rename 失敗不得產生歸檔");
    let cur = read_text(&fs, WAL);
    let last = dict_lines(&cur).into_iter().last().expect("現行檔應含 seq 2");
    assert_eq!(seq_of(&last), Some(2));
    assert_eq!(last.get("rotation_failed"), Some(&PyValue::Bool(true)));
    // 續用原路徑：後續 append 仍寫入，歸檔＋現行合計無遺失
    w.append(req("RX", "uart", b"x"));
    let mut all = if has_archive(&fs, WAL, SUFFIX_1) {
        seqs(&read_text(&fs, &format!("{WAL}.{SUFFIX_1}")))
    } else {
        Vec::new()
    };
    all.extend(seqs(&read_text(&fs, WAL)));
    assert_eq!(all, vec![1, 2, 3]);
}

#[test]
fn mirror_failure_retries_only_mirror() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    fs.fail_next_writes(p(MIRROR), 1, eacces());
    let a = w.append(req("RX", "uart", b"hello"));
    assert!(a.ok);
    assert_eq!(a.seq, 1);
    // WAL 只一行 seq=1（DF-17：不因 mirror 重試而重寫 WAL）
    assert_eq!(seqs(&read_text(&fs, WAL)), vec![1]);
    assert_eq!(fs.write_calls(p(WAL)).len(), 1);
    // mirror 經一次重試後有內容
    assert_eq!(read_bytes(&fs, MIRROR), b"hello".to_vec());
    assert_eq!(w.health().write_failures, 0);
}

#[test]
fn mirror_retry_failure_counts_write_failure() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    fs.fail_next_writes(p(MIRROR), 2, eacces()); // 首次＋重試皆失敗
    w.append(req("RX", "uart", b"hello"));
    // WAL 仍寫入
    assert_eq!(seqs(&read_text(&fs, WAL)), vec![1]);
    let h = w.health();
    assert_eq!(h.write_failures, 1);
    assert_eq!(
        h.last_write_error.as_deref(),
        Some("PermissionError: [Errno 13] Permission denied")
    );
}

#[test]
fn mirror_identity_reopened_without_meta() {
    let (w, fs, clock) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "uart", b"a"));
    fs.external_unlink(p(MIRROR));
    clock.advance(MS_60);
    w.append(req("RX", "uart", b"b"));
    // mirror 重建並含新內容
    assert_eq!(read_bytes(&fs, MIRROR), b"b".to_vec());
    // WAL 無 META、recreated_count 不變（P1 T05 第 1 點）
    let text = read_text(&fs, WAL);
    assert!(!text.contains("\"dir\":\"META\""), "{text}");
    assert_eq!(w.health().recreated_count, 0);
}

#[test]
fn identity_check_precedes_rotation_when_rotation_is_due() {
    let (w, fs, clock) = writer(ROTATE_200);
    w.append(req("RX", "device", b"a")); // seq 1，檔案已達 R
    let replacement = "Y\n".repeat(120).into_bytes(); // 240 bytes ≥ R；identity 改變
    fs.external_replace(p(WAL), &replacement);
    clock.advance(MS_60);
    // 同一筆同時：identity 變更 ＋ 達門檻
    w.append(req("RX", "device", b"b"));
    // 替換檔先被輪替歸檔（替換證據在輪替前確認，未被 META 汙染）
    assert_eq!(read_bytes(&fs, &format!("{WAL}.{SUFFIX_1}")), replacement);
    // 原遺失範圍 [file_first_seq, seq] = [1, 1]，META 寫入新現行檔
    let cur = read_text(&fs, WAL);
    let m = first_meta_line(&cur);
    assert_eq!(seq_of(&m), Some(2));
    assert_eq!(str_of(meta_of(&m), "reason"), Some("replaced"));
    assert_eq!(
        int_list(meta_of(&m).get("potentially_lost_seq_range")),
        Some(vec![1, 1])
    );
    assert_eq!(seqs(&cur), vec![2, 3]);
}

#[test]
fn identity_query_error_at_rotate_threshold_keeps_current_file() {
    let (w, fs, clock) = writer(ROTATE_200);
    w.append(req("RX", "device", b"a")); // 達門檻
    fs.fail_identity_queries(p(WAL), eio());
    clock.advance(MS_60);
    let a = w.append(req("RX", "device", b"b"));
    assert_eq!(
        a,
        WalAppend {
            seq: 2,
            ok: true,
            loss_flag: false
        }
    );
    // 保留現行 handle：同一檔案續寫，略過本次輪替
    assert_eq!(seqs(&read_text(&fs, WAL)), vec![1, 2]);
    assert!(!has_archive(&fs, WAL, SUFFIX_1));
    // 不寫 META、不設 unhealthy
    assert!(!read_text(&fs, WAL).contains("WAL_RECREATED_AFTER_EXTERNAL_UNLINK"));
    let h = w.health();
    assert!(h.healthy);
    assert_eq!(h.unhealthy_reason, None);
}

#[test]
fn replacement_at_rotate_threshold_keeps_meta_current() {
    let (w, fs, clock) = writer(ROTATE_200);
    w.append(req("RX", "device", b"a")); // seq 1
    let replacement = "Y\n".repeat(100).into_bytes(); // 恰為 200 bytes = R
    assert_eq!(replacement.len() as u64, ROTATE_200);
    fs.external_replace(p(WAL), &replacement);
    clock.advance(MS_60);
    w.append(req("RX", "device", b"b")); // META seq 2（replaced，範圍 [1,1]）＋紀錄 seq 3
    // 替換證據在輪替前確認：替換檔歸檔
    assert_eq!(read_bytes(&fs, &format!("{WAL}.{SUFFIX_1}")), replacement);
    let cur = read_text(&fs, WAL);
    let m = first_meta_line(&cur);
    assert_eq!(str_of(meta_of(&m), "reason"), Some("replaced"));
    // 遺失範圍仍從前檔 file_first_seq（= 1）開始
    assert_eq!(
        int_list(meta_of(&m).get("potentially_lost_seq_range")),
        Some(vec![1, 1])
    );
    assert_eq!(seqs(&cur), vec![2, 3]);
}

#[test]
fn replacement_unterminated_tail_keeps_meta_parseable() {
    let (w, fs, clock) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "device", b"a")); // seq 1
    fs.external_replace(p(WAL), b"partial-no-newline");
    clock.advance(MS_60);
    // 先終結殘行 → META seq 2 → 紀錄 seq 3
    w.append(req("RX", "device", b"b"));
    assert!(read_bytes(&fs, WAL).starts_with(b"partial-no-newline\n"));
    let (rows, _) = w.tail_raw_with_meta(None, None, 100).unwrap();
    // tail 略過殘行，仍讀得到獨立 META 與後續紀錄
    assert_eq!(rows.len(), 2);
    assert_eq!(seq_of(&rows[0]), Some(2));
    assert_eq!(str_of(&rows[0], "dir"), Some("META"));
    assert_eq!(seq_of(&rows[1]), Some(3));
}

#[test]
fn second_unlink_range_starts_at_first_meta_seq() {
    let (w, fs, clock) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "device", b"a")); // seq 1
    fs.external_unlink(p(WAL));
    clock.advance(MS_60);
    w.append(req("RX", "device", b"b")); // META seq 2（範圍 [1,1]）＋紀錄 seq 3
    fs.external_unlink(p(WAL));
    clock.advance(MS_60);
    let a = w.append(req("RX", "device", b"c")); // META seq 4＋紀錄 seq 5
    assert_eq!(a.seq, 5);
    let metas: Vec<PyDict> = dict_lines(&read_text(&fs, WAL))
        .into_iter()
        .filter(|d| str_of(d, "dir") == Some("META"))
        .collect();
    assert_eq!(metas.len(), 1);
    // 第二筆 META 範圍從第一筆 META seq（= 2）起算，終點為重建前最後 seq（= 3）
    assert_eq!(
        int_list(meta_of(&metas[0]).get("potentially_lost_seq_range")),
        Some(vec![2, 3])
    );
}

#[test]
fn zero_identity_and_unlinked_handle_classify_external_unlink() {
    // 子案 A：path identity 為 (0,0) sentinel
    {
        let (w, fs, clock) = writer(DEFAULT_ROTATE);
        w.append(req("RX", "device", b"a"));
        fs.force_path_identity(p(WAL), Some(FileIdentity(0, 0)));
        clock.advance(MS_60);
        w.append(req("RX", "device", b"b"));
        let m = first_meta_line(&read_text(&fs, WAL));
        assert_eq!(str_of(meta_of(&m), "reason"), Some("external_unlink"));
    }
    // 子案 B：路徑 identity 仍相同，但 handle 已 unlinked()==true（Windows pending-delete 語意）
    {
        let (w, fs, clock) = writer(DEFAULT_ROTATE);
        w.append(req("RX", "device", b"a"));
        let _pending = fs.hold_external_reader(p(WAL));
        fs.external_unlink(p(WAL));
        clock.advance(MS_60);
        w.append(req("RX", "device", b"b"));
        let m = first_meta_line(&read_text(&fs, WAL));
        assert_eq!(str_of(meta_of(&m), "reason"), Some("external_unlink"));
    }
}

#[test]
fn recreate_retry_schedule_uses_monotonic_gaps() {
    let (w, fs, clock) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "device", b"a")); // seq 1
    fs.external_unlink(p(WAL));
    fs.fail_next_opens(p(WAL), 12, enospc()); // 之後所有開檔嘗試皆失敗
    clock.advance(MS_60);
    let a = w.append(req("RX", "device", b"b")); // 首次偵測 → 開檔嘗試 #1
    assert!(!a.ok);
    assert_eq!(fs.open_attempts(p(WAL)), 1);
    // 前三次失敗後 10／50／200 ms，之後 1／2／5／5 s（上限 5 s）
    let gaps_ms: [u64; 8] = [10, 50, 200, 1_000, 2_000, 5_000, 5_000, 5_000];
    for (i, gap) in gaps_ms.iter().enumerate() {
        clock.advance(Duration::from_millis(*gap - 1));
        w.append(req("RX", "device", b"x"));
        assert_eq!(
            fs.open_attempts(p(WAL)),
            1 + i,
            "未到期（間隔 {gap} ms 內）不得開檔"
        );
        clock.advance(Duration::from_millis(1));
        w.append(req("RX", "device", b"x"));
        assert_eq!(
            fs.open_attempts(p(WAL)),
            2 + i,
            "到期 append 恰嘗試一次（第 {} 次間隔 {gap} ms）",
            i + 1
        );
    }
    // 到期 append 最多嘗試一次：一次跨越多個期限也只開一次
    clock.advance(Duration::from_secs(30));
    w.append(req("RX", "device", b"x"));
    assert_eq!(fs.open_attempts(p(WAL)), 2 + gaps_ms.len());
}

#[test]
fn lazy_open_idle_file_absent() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    assert!(fs.contents(p(WAL)).is_none());
    let h = w.health();
    assert!(!h.wal_file_exists);
    assert!(h.healthy);
}

#[test]
fn enospc_on_open_handle_trips_all_fences() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    let fan = WalHealthFanout::new();
    w.set_health_sink(fan.clone());
    let fences: Vec<Arc<Fence>> = (0..3).map(|_| Arc::new(Fence::default())).collect();
    for f in &fences {
        fan.register(f.clone());
    }
    assert!(w.append(req("RX", "device", b"a")).ok);
    // 已開 handle 上：首次＋重試共兩次皆 ENOSPC
    fs.fail_next_writes(p(WAL), 2, enospc());
    let a = w.append(req("RX", "device", b"b"));
    assert!(!a.ok && a.loss_flag);
    assert!(fences.iter().all(|f| f.flag()), "三個 fence 皆應 unhealthy");
    assert_eq!(w.health().unhealthy_reason, Some("write_failed"));
    // 下一筆成功 → 三者恢復
    let b = w.append(req("RX", "device", b"c"));
    assert!(b.ok);
    assert!(fences.iter().all(|f| !f.flag()), "三個 fence 皆應恢復");
    assert_eq!(w.health().unhealthy_reason, None);
}

#[test]
fn new_session_inherits_wal_unhealthy() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    let fan = WalHealthFanout::new();
    w.set_health_sink(fan.clone());
    w.append(req("RX", "device", b"a"));
    fs.fail_next_writes(p(WAL), 2, enospc());
    w.append(req("RX", "device", b"b"));
    assert!(fan.current());
    // 新建 session 的 fence 繼承目前 unhealthy 值
    let late = Arc::new(Fence::default());
    fan.register(late.clone());
    assert!(late.flag());
}

#[test]
fn identity_throttle_ignores_wall_jump() {
    let (w, fs, clock) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "device", b"a"));
    // wall 倒退 1 h；節流只看 mono
    clock.set_wall_us(WALL_1 - 3_600 * 1_000_000);
    clock.advance(MS_60);
    fs.external_unlink(p(WAL));
    // 必須做 identity 檢查（不因 wall 倒退被跳過）→ 產生 META
    w.append(req("RX", "device", b"b"));
    assert!(
        read_text(&fs, WAL).contains("WAL_RECREATED_AFTER_EXTERNAL_UNLINK"),
        "wall 倒退不得使 identity 檢查被跳過"
    );
}

#[test]
fn meta_not_in_com0_tail() {
    let (w, fs, clock) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "device", b"a")); // COM0 seq 1
    fs.external_unlink(p(WAL));
    clock.advance(MS_60);
    w.append(req("RX", "device", b"b")); // META seq 2（com WAL）＋紀錄 seq 3（COM0）
    let c0 = w.tail_raw_with_meta(None, Some("COM0"), 100).unwrap().0;
    assert!(c0.iter().all(|r| str_of(r, "dir") != Some("META")));
    assert!(c0.iter().any(|r| seq_of(r) == Some(3)));
    // com=None 時含 META
    let all = w.tail_raw_with_meta(None, None, 100).unwrap().0;
    assert!(all.iter().any(|r| str_of(r, "dir") == Some("META")));
}

#[test]
fn wal_contract_golden_matches_p1() {
    // 上游缺漏（§0.3 P1 T02–T05）：本分支尚無 contracts/golden/wal/，不得以空集合假通過。
    let dir = repo_path("contracts/golden/wal");
    let cases: Vec<PathBuf> = match std::fs::read_dir(&dir) {
        Ok(rd) => rd
            .map(|e| e.expect("read_dir entry").path())
            .filter(|f| f.extension().is_some_and(|x| x == "json"))
            .collect(),
        Err(e) => panic!(
            "上游缺漏（§0.3 P1 T02–T05）：{} 不可讀（{e}）；P1 凍結的 WAL 紀錄位元組 golden 未交付",
            dir.display()
        ),
    };
    assert!(!cases.is_empty(), "{} 不得為空", dir.display());
    unimplemented!(
        "逐案重放待 P1 golden 格式確定（{} 件，上游缺漏）",
        cases.len()
    );
}

#[test]
fn wal_write_failure_twice_sets_loss_flag_and_consumes_seq() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    fs.fail_next_writes(p(WAL), 2, eio()); // 首次＋重試皆失敗
    let a = w.append(req("RX", "device", b"x"));
    assert_eq!(
        a,
        WalAppend {
            seq: 1,
            ok: false,
            loss_flag: true
        }
    );
    assert_eq!(w.health().write_failures, 1);
    // seq 缺口保留（04 §1.7）
    let b = w.append(req("RX", "device", b"y"));
    assert_eq!(b.seq, 2);
    assert!(b.ok);
}

#[test]
fn wal_partial_write_then_failure_completes_suffix_only() {
    // R2-F3；對應 P1 test_partial_write_then_eio_completes_suffix_only
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    fs.fail_next_write_partial(p(WAL), 10, eio());
    let a = w.append(req("RX", "device", b"x"));
    assert_eq!(
        a,
        WalAppend {
            seq: 1,
            ok: true,
            loss_flag: false
        }
    );
    let calls = fs.write_calls(p(WAL));
    let line = calls[0].clone();
    assert_eq!(calls, vec![line.clone(), line[10..].to_vec()]);
    assert_eq!(fs.open_calls(p(WAL)), 1, "同 handle 補寫、不重開");
    assert_eq!(read_bytes(&fs, WAL), line, "恰一行（不含「前綴＋完整行」）");
    let text = read_text(&fs, WAL);
    assert!(loads(text.trim_end_matches('\n')).is_ok());
    assert_eq!(text.matches("\"seq\":1,").count(), 1);
    assert_eq!(w.health().write_failures, 0);
    assert!(w.health().healthy);
}

#[test]
fn wal_zero_sent_failure_rewrites_whole_line_after_reopen() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    fs.fail_next_writes(p(WAL), 1, eio()); // sent == 0
    let a = w.append(req("RX", "device", b"x"));
    assert_eq!(
        a,
        WalAppend {
            seq: 1,
            ok: true,
            loss_flag: false
        }
    );
    let calls = fs.write_calls(p(WAL));
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0], calls[1], "關閉→重開後整行重寫一次");
    // 初次開檔＋重開 = 2 次成功開檔
    assert_eq!(fs.open_calls(p(WAL)), 2);
    assert_eq!(read_bytes(&fs, WAL), calls[0]);
    assert_eq!(w.health().write_failures, 0);
}

#[test]
fn wal_reopen_retry_partial_sets_dirty_tail() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    fs.fail_next_writes(p(WAL), 1, eio()); // 首次 sent == 0
    fs.fail_next_write_partial(p(WAL), 7, eio()); // 整行重寫時又只寫 7 bytes
    let a = w.append(req("RX", "device", b"x"));
    assert_eq!(
        a,
        WalAppend {
            seq: 1,
            ok: false,
            loss_flag: true
        }
    );
    let calls = fs.write_calls(p(WAL));
    // 不做第三次嘗試
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0], calls[1]);
    let line1 = calls[0].clone();
    assert_eq!(read_bytes(&fs, WAL), line1[..7].to_vec());

    let b = w.append(req("RX", "device", b"y"));
    assert_eq!(
        b,
        WalAppend {
            seq: 2,
            ok: true,
            loss_flag: false
        }
    );
    let calls = fs.write_calls(p(WAL));
    assert_eq!(calls.len(), 3);
    assert_eq!(calls[2][0], b'\n');
    let text = read_text(&fs, WAL);
    let parts: Vec<&str> = text.split_terminator('\n').collect();
    assert_eq!(parts.len(), 2);
    assert!(loads(parts[0]).is_err(), "第一行為殘行，判為無效");
    assert_eq!(seq_of(&parse_dict(parts[1])), Some(2));
}

#[test]
fn mirror_partial_write_completes_suffix_only() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    fs.fail_next_write_partial(p(MIRROR), 3, eacces());
    let a = w.append(req("RX", "uart", b"hello"));
    assert!(a.ok);
    // mirror：無重複前綴，只補寫 p[3..]
    assert_eq!(read_bytes(&fs, MIRROR), b"hello".to_vec());
    assert_eq!(
        fs.write_calls(p(MIRROR)),
        vec![b"hello".to_vec(), b"lo".to_vec()]
    );
    // WAL 只一行 seq=1（DF-17）
    assert_eq!(seqs(&read_text(&fs, WAL)), vec![1]);
    assert_eq!(fs.write_calls(p(WAL)).len(), 1);
    assert_eq!(w.health().write_failures, 0);
}

#[test]
fn startup_seq_is_max_int_seq_in_current_file() {
    let fs = FakeWalFs::new();
    fs.seed(
        p(WAL),
        b"{\"seq\":5,\"dir\":\"RX\"}\n{\"seq\":9,\"dir\":\"RX\"}\n{\"seq\":7,\"dir\":\"RX\"}\nnot json\n{\"seq\":\"x\"}\n",
    );
    let clock = FakeClock::at(MONO_1, WALL_1);
    let w = WalWriter::open(
        WalConfig {
            dir: PathBuf::from(DIR),
            rotate_bytes: DEFAULT_ROTATE,
        },
        fs.clone(),
        clock.clone(),
    );
    assert_eq!(w.current_seq(), 9);
    let a = w.append(req("RX", "device", b"a"));
    assert_eq!(a.seq, 10);
    assert_eq!(w.available_from_seq(), Some(5));
}

#[test]
fn reset_renames_nonempty_files_independent_suffix() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "uart", b"a"));
    w.append(req("RX", "uart", b"b")); // seq 1、2；WAL 與 mirror 皆非空
    // 同秒既有 WAL 歸檔 → WAL 後綴取 .1
    fs.seed(p(&format!("{WAL}.{SUFFIX_1}")), b"old\n");
    let r = w.reset().expect("reset 應成功");
    assert_eq!(r.previous_seq, 2);
    assert_eq!(r.rotated_suffix, format!("{SUFFIX_1}.1"));
    // mirror 依自身既有歸檔獨立取後綴（無既有歸檔 → 無 .1）
    assert!(has_archive(&fs, MIRROR, SUFFIX_1));
    assert!(fs.contents(p(WAL)).is_none(), "WAL 已歸檔");
    let a = w.append(req("RX", "uart", b"c"));
    assert_eq!(a.seq, 1, "reset 後下一筆 seq = 1");

    // 空檔不改名
    let (w2, fs2, _) = writer(DEFAULT_ROTATE);
    let r2 = w2.reset().expect("空檔 reset 應成功");
    assert_eq!(r2.previous_seq, 0);
    assert!(fs2.list_dir(p(DIR)).is_empty(), "空檔不得改名");
}

#[test]
fn reset_rename_error_propagates_and_keeps_seq() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "uart", b"a"));
    w.append(req("RX", "uart", b"b"));
    fs.fail_next_renames(1, eacces());
    assert!(w.reset().is_err());
    assert_eq!(w.current_seq(), 2);
}

#[test]
fn health_keys_order() {
    let (w, _fs, _) = writer(DEFAULT_ROTATE);
    let d = w.health().to_pydict();
    let keys: Vec<&str> = d.0.keys().map(String::as_str).collect();
    assert_eq!(
        keys,
        vec![
            "wal_dir",
            "wal_path",
            "wal_dir_exists",
            "wal_file_exists",
            "wal_dir_writable",
            "current_seq",
            "write_failures",
            "last_write_error",
            "recreated_count",
            "healthy",
            "unhealthy_reason",
        ]
    );
}

#[test]
fn health_missing_file_after_writes_unhealthy() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "device", b"a"));
    fs.external_unlink(p(WAL)); // 尚未 append 觸發重建
    let h = w.health();
    assert!(!h.healthy);
    assert!(!h.wal_file_exists);
}

#[test]
fn open_failure_at_startup_marks_unhealthy() {
    let fs = FakeWalFs::new();
    fs.set_dir_writable(p(DIR), false);
    let clock = FakeClock::at(MONO_1, WALL_1);
    let fence = Arc::new(Fence::default());
    let fan = WalHealthFanout::new();
    fan.register(fence.clone());
    // 不 panic、不開檔（lazy）
    let w = WalWriter::open(
        WalConfig {
            dir: PathBuf::from(DIR),
            rotate_bytes: DEFAULT_ROTATE,
        },
        fs.clone(),
        clock.clone(),
    );
    w.set_health_sink(fan.clone());
    assert!(fence.flag(), "啟動即不可寫：fanout 應立即通知既有 fence");
    let h = w.health();
    assert!(!h.healthy);
    assert_eq!(h.unhealthy_reason, Some("dir_unwritable"));
    assert!(fs.contents(p(WAL)).is_none());
    let a = w.append(req("RX", "device", b"a"));
    assert!(!a.ok && a.loss_flag);
    assert_eq!(w.health().write_failures, 1);
    // 記憶體不保留未寫紀錄
    assert!(fs.contents(p(WAL)).is_none());
}

#[test]
fn tail_latest_and_range_semantics() {
    let (w, _fs, _) = writer(DEFAULT_ROTATE);
    for _ in 0..4 {
        w.append(req("RX", "uart", b"x")); // COM0：seq 1..4
    }
    w.append(WalAppendReq {
        com: "COM1",
        dir: "RX",
        source: "uart",
        payload: b"x",
        cmd_id: None,
        loss_flag: false,
        meta: None,
    }); // seq 5

    let (rows, truncated) = w.tail_raw_with_meta(None, None, 2).unwrap();
    assert_eq!(seq_list(&rows), vec![4, 5]);
    assert!(truncated);

    let (rows, truncated) = w.tail_raw_with_meta(Some(2), None, 2).unwrap();
    assert_eq!(seq_list(&rows), vec![3, 4]);
    assert!(truncated);

    let (rows, truncated) = w.tail_raw_with_meta(Some(4), None, 2).unwrap();
    assert_eq!(seq_list(&rows), vec![5]);
    assert!(!truncated);

    // limit=0（latest）→ 空且 truncated
    let (rows, truncated) = w.tail_raw_with_meta(None, None, 0).unwrap();
    assert!(rows.is_empty());
    assert!(truncated);

    // com 過濾只回該 com
    let (rows, _) = w.tail_raw_with_meta(None, Some("COM1"), 100).unwrap();
    assert_eq!(seq_list(&rows), vec![5]);
    let (rows, _) = w.tail_raw_with_meta(None, Some("COM0"), 100).unwrap();
    assert_eq!(seq_list(&rows), vec![1, 2, 3, 4]);
}

#[test]
fn rows_to_text_lines_keeps_crlf_quirk() {
    // WAL-1.11 KEEP：partial 尾段保留為最後一行（Python 同行為：尾段計算會多出最後一字元）
    let rows = vec![
        PyDict::from_pairs(vec![(
            "payload_b64".to_string(),
            PyValue::from("aGVsbG8NChtbMG0Lf/8="), // b"hello\r\n\x1b[0m\x0b\x7f\xff"
        )]),
        PyDict::from_pairs(vec![(
            "payload_b64".to_string(),
            PyValue::from("w6k="), // "é"
        )]),
    ];
    assert_eq!(
        rows_to_text_lines(&rows),
        vec![
            "hello".to_string(),
            "\\x1b[0m\\x0b\\x7f\\xff\\xc3\\xa9".to_string(),
            "9".to_string(),
        ]
    );
}

#[test]
fn shutdown_syncs_then_closes() {
    let (w, fs, _) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "device", b"a"));
    w.shutdown().expect("shutdown 應成功");
    assert!(fs.sync_calls(p(WAL)) >= 1, "shutdown 必須 fsync");
    // 關閉後 append 回 ok:false（SC-11）
    let a = w.append(req("RX", "device", b"b"));
    assert!(!a.ok);
}

#[test]
fn recreate_meta_matches_contract_schema() {
    let (w, fs, clock) = writer(DEFAULT_ROTATE);
    w.append(req("RX", "device", b"a"));
    fs.external_unlink(p(WAL));
    clock.advance(MS_60);
    w.append(req("RX", "device", b"b"));
    let rec = first_meta_line(&read_text(&fs, WAL));

    let schema_text = std::fs::read_to_string(repo_path("contracts/wal_meta_events.schema.json"))
        .expect("P1 契約 schema 應存在");
    let schema = loads(&schema_text).expect("schema 應為合法 JSON");
    let entries = pv_dict(&schema)
        .get("oneOf")
        .and_then(PyValue::as_array)
        .expect("oneOf 應為陣列");
    let entry = entries
        .iter()
        .find(|e| e.get("title").and_then(PyValue::as_str) == Some("WAL_RECREATED_AFTER_EXTERNAL_UNLINK"))
        .expect("schema 應含 WAL_RECREATED_AFTER_EXTERNAL_UNLINK");
    let entry = pv_dict(entry);

    // 外層紀錄（x-outer-record）
    let outer = pv_dict(entry.get("x-outer-record").expect("x-outer-record"));
    for k in ["dir", "source", "com"] {
        assert_eq!(
            str_of(&rec, k),
            outer.get(k).and_then(PyValue::as_str),
            "外層 {k}"
        );
    }
    assert_eq!(rec.get("loss_flag"), outer.get("loss_flag"));

    // required 全部存在；additionalProperties:false
    let props = pv_dict(entry.get("properties").expect("properties"));
    let meta = meta_of(&rec);
    let required = entry
        .get("required")
        .and_then(PyValue::as_array)
        .expect("required");
    for k in required.iter().filter_map(PyValue::as_str) {
        assert!(meta.get(k).is_some(), "缺 required 鍵 {k}");
    }
    assert_eq!(
        entry.get("additionalProperties"),
        Some(&PyValue::Bool(false))
    );
    for k in meta.0.keys() {
        assert!(props.get(k).is_some(), "額外鍵 {k} 違反 additionalProperties:false");
    }

    // event const
    assert_eq!(
        meta.get("event"),
        props.get("event").and_then(|x| x.get("const"))
    );

    // reason 必須落在 enum
    let reason_enum = props
        .get("reason")
        .and_then(|x| x.get("enum"))
        .and_then(PyValue::as_array)
        .expect("reason enum");
    let reason = str_of(meta, "reason").expect("reason 為字串");
    assert!(
        reason_enum.iter().any(|v| v.as_str() == Some(reason)),
        "reason {reason} 不在 enum"
    );

    // previous_identity：字串且符合 pattern（external_unlink 有舊 identity）
    let pattern = props
        .get("previous_identity")
        .and_then(|x| x.get("pattern"))
        .and_then(PyValue::as_str)
        .expect("previous_identity pattern");
    assert_eq!(pattern, "^[0-9]+:[0-9]+$");
    let pi = str_of(meta, "previous_identity").expect("previous_identity 應為字串");
    let mut parts = pi.splitn(2, ':');
    let ok = matches!(
        (parts.next(), parts.next()),
        (Some(a), Some(b))
            if !a.is_empty()
                && !b.is_empty()
                && a.bytes().all(|c| c.is_ascii_digit())
                && b.bytes().all(|c| c.is_ascii_digit())
    );
    assert!(ok, "previous_identity {pi:?} 不符 pattern");

    // range_known 與 potentially_lost_seq_range 的 if/then
    assert_eq!(meta.get("range_known"), Some(&PyValue::Bool(true)));
    let range = int_list(meta.get("potentially_lost_seq_range")).expect("range 應為整數陣列");
    assert_eq!(range.len(), 2);
    assert!(range[0] >= 1 && range[0] <= range[1]);
}
