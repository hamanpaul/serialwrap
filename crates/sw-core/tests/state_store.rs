//! P5-T15：`state.json` 載入／序列化／單一寫入者與 alias registry（Python `_load_state`／`_save_state`／
//! `alias_registry.py` 逐字承接）。斷言值以 Python 3.12 基線實測為準（見各測試註解）。

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use sw_core::alias_registry::AliasRegistry;
use sw_core::state_store::*;
use sw_pal::fakes::file_mode;
use sw_pal::fs_ops::{FileLockGuard, FsOps, LinkKind};
use sw_pal::traits::{FsRead, PalError, PalErrorKind, PalResult};
use sw_proto::pyjson::{PyDict, PyValue};

const PY_BYTES: &str = "{\"aliases\":{\"custom\":{\"session_id\":\"p:COM5\",\"updated_at\":\"2026-10-15T03:07:39.596535+00:00\"},\"zz\":{\"device_by_id\":\"/dev/serial/by-id/Z\",\"profile\":\"prpl-template\",\"updated_at\":\"2026-10-15T03:07:39.596600+00:00\"}},\"bindings\":{},\"profile_detected\":{},\"profile_pins\":{\"/dev/serial/by-id/X\":\"prpl-template\"},\"released\":{\"prpl-template:COM0\":{\"by_id\":\"/dev/serial/by-id/X\",\"reason\":\"r\",\"released_at\":\"t\",\"released_by\":\"cli\"}}}\n";

fn native_fs() -> Arc<dyn FsOps> {
    sw_pal::native().fs
}

fn write(p: &Path, s: &str) {
    std::fs::write(p, s).unwrap();
}

fn load(p: &Path) -> Result<(LoadedState, Option<PathBuf>), StateLoadError> {
    load_state(p, &*native_fs())
}

fn row(by_id: &str) -> ReleasedRow {
    ReleasedRow {
        by_id: PyValue::from(by_id),
        released_by: PyValue::from("cli"),
        released_at: PyValue::from("t"),
        reason: PyValue::from("r"),
    }
}

fn dir_names(d: &Path) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(d)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    v.sort();
    v
}

/// 委派到原生 `FsOps` 的計數替身：atomic_write 次數、一次性注入失敗。
struct CountingFs {
    inner: Arc<dyn FsOps>,
    atomic_writes: AtomicUsize,
    fail_next: Mutex<Option<PalError>>,
}

impl CountingFs {
    fn new() -> Arc<CountingFs> {
        Arc::new(CountingFs {
            inner: native_fs(),
            atomic_writes: AtomicUsize::new(0),
            fail_next: Mutex::new(None),
        })
    }
    fn fail_atomic_write(&self, e: PalError) {
        *self.fail_next.lock().unwrap() = Some(e);
    }
    fn writes(&self) -> usize {
        self.atomic_writes.load(Ordering::SeqCst)
    }
}

impl FsRead for CountingFs {
    fn read(&self, p: &Path) -> PalResult<Vec<u8>> {
        self.inner.read(p)
    }
    fn exists(&self, p: &Path) -> bool {
        self.inner.exists(p)
    }
    fn is_dir(&self, p: &Path) -> bool {
        self.inner.is_dir(p)
    }
    fn is_file(&self, p: &Path) -> bool {
        self.inner.is_file(p)
    }
    fn list_dir(&self, p: &Path) -> PalResult<Vec<String>> {
        self.inner.list_dir(p)
    }
}

impl FsOps for CountingFs {
    fn atomic_write(&self, p: &Path, data: &[u8], mode: Option<u32>) -> PalResult<()> {
        if let Some(e) = self.fail_next.lock().unwrap().take() {
            return Err(e);
        }
        self.atomic_writes.fetch_add(1, Ordering::SeqCst);
        self.inner.atomic_write(p, data, mode)
    }
    fn mkdirs(&self, p: &Path) -> PalResult<()> {
        self.inner.mkdirs(p)
    }
    fn replace(&self, from: &Path, to: &Path) -> PalResult<()> {
        self.inner.replace(from, to)
    }
    fn file_lock_exclusive(&self, p: &Path, blocking: bool) -> PalResult<FileLockGuard> {
        self.inner.file_lock_exclusive(p, blocking)
    }
    fn symlink_or_copy_dir(&self, link: &Path, target: &Path) -> PalResult<LinkKind> {
        self.inner.symlink_or_copy_dir(link, target)
    }
}

// ---------------------------------------------------------------------------
// Step 0：P4 atomic_write 前置檢查（Python `_save_state` 語意）
// ---------------------------------------------------------------------------

/// tmp 前綴 `state.json.tmp.`（同目錄）、0600、內容完整、無殘留。
///
/// 目錄 fsync 次數無法以黑箱觀測原生實作（`sync_dir` 在 PAL 內部呼叫）；由 sw-pal posix.rs
/// `atomic_write` → `self.sync_dir(parent)`（每次一次）的程式碼閱讀確認，見交付說明。
#[test]
fn p4_atomic_write_semantics() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    let fs = native_fs();
    let big = vec![b'x'; 8 << 20];

    // 觀察者 thread 持續列目錄，捕捉 atomic_write 期間的暫存檔名。
    let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let seen = Arc::new(Mutex::new(std::collections::BTreeSet::<String>::new()));
    let watcher = {
        let (stop, seen, dir) = (stop.clone(), seen.clone(), d.path().to_path_buf());
        std::thread::spawn(move || {
            while !stop.load(Ordering::SeqCst) {
                if let Ok(rd) = std::fs::read_dir(&dir) {
                    for e in rd.flatten() {
                        let n = e.file_name().to_string_lossy().into_owned();
                        if n != "state.json" {
                            seen.lock().unwrap().insert(n);
                        }
                    }
                }
            }
        })
    };
    for _ in 0..200 {
        fs.atomic_write(&p, &big, Some(0o600)).unwrap();
        if !seen.lock().unwrap().is_empty() {
            break;
        }
    }
    stop.store(true, Ordering::SeqCst);
    watcher.join().unwrap();
    let seen = seen.lock().unwrap().clone();
    assert!(!seen.is_empty(), "未觀察到任何暫存檔");
    for n in &seen {
        assert!(
            n.starts_with("state.json.tmp."),
            "暫存檔名 {n:?} 應以 state.json.tmp. 開頭"
        );
    }

    fs.atomic_write(&p, b"{}\n", Some(0o600)).unwrap();
    assert_eq!(std::fs::read(&p).unwrap(), b"{}\n");
    if let Some(mode) = file_mode(&p) {
        assert_eq!(mode, 0o600);
    }
    assert_eq!(dir_names(d.path()), vec!["state.json".to_string()]);
}

// ---------------------------------------------------------------------------
// Step 1：載入／序列化
// ---------------------------------------------------------------------------

#[test]
fn python_bytes_roundtrip_exactly() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    std::fs::write(&p, PY_BYTES).unwrap();
    let (s, backup) = load_state(&p, &*sw_pal::native().fs).unwrap();
    assert!(backup.is_none());
    assert_eq!(String::from_utf8(serialize_state(&s)).unwrap(), PY_BYTES);
    assert_eq!(
        s.released["prpl-template:COM0"].released_by,
        PyValue::from("cli")
    );
}

#[test]
fn unreadable_is_state_load_error_and_file_untouched() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    std::fs::create_dir(&p).unwrap(); // read 失敗（EISDIR）
    let e = load_state(&p, &*sw_pal::native().fs).unwrap_err();
    assert!(
        e.message
            .starts_with(&format!("無法讀取既有 state.json（{}）：", p.display()))
    );
    assert!(e.message.ends_with(
        "；為避免以空狀態覆寫並遺失 RELEASED 交接（two-reader 風險），daemon 拒絕啟動。"
    ));
    assert!(p.is_dir());
    // Python 基線：daemon 以 `serialwrapd: <msg>` 寫 stderr 並 exit 3（daemon.py StateLoadError 分支）。
    assert_eq!(e.kind, StateLoadErrorKind::ReadFailed);
    assert_eq!(e.exit_code(), 3);
    assert_eq!(e.stderr_text(), format!("serialwrapd: {}\n", e.message));
    assert_eq!(e.to_string(), e.message);
}

#[test]
fn corrupt_json_renamed_and_empty_state() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    let corrupt = d.path().join("state.json.corrupt");
    std::fs::write(&corrupt, "old").unwrap(); // 既有 .corrupt 被覆蓋
    write(&p, "{\"aliases\":");
    let warns = Mutex::new(Vec::<String>::new());
    let (s, backup) = load_state_with_warn(&p, &*native_fs(), &|m| {
        warns.lock().unwrap().push(m.to_string())
    })
    .unwrap();
    assert_eq!(s, LoadedState::default());
    assert_eq!(backup.as_deref(), Some(corrupt.as_path()));
    assert!(!p.exists());
    assert_eq!(std::fs::read_to_string(&corrupt).unwrap(), "{\"aliases\":");
    // Python：json.loads(b'{"aliases":') → "Expecting value: line 1 column 12 (char 11)"
    assert_eq!(
        warns.into_inner().unwrap(),
        vec![format!(
            "state.json 解析失敗（JSON 損毀），略過載入並備份至 {}：Expecting value: line 1 column 12 (char 11)",
            corrupt.display()
        )]
    );
}

#[test]
fn corrupt_non_utf8_renamed() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    std::fs::write(&p, b"\xff{}").unwrap();
    let warns = Mutex::new(Vec::<String>::new());
    let (s, backup) = load_state_with_warn(&p, &*native_fs(), &|m| {
        warns.lock().unwrap().push(m.to_string())
    })
    .unwrap();
    assert_eq!(s, LoadedState::default());
    assert!(backup.is_some());
    // Python：UnicodeDecodeError 字串
    assert!(
        warns.into_inner().unwrap()[0]
            .ends_with("：'utf-8' codec can't decode byte 0xff in position 0: invalid start byte")
    );
}

#[test]
fn non_dict_json_is_empty_without_backup() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    write(&p, "[1,2]");
    let (s, backup) = load(&p).unwrap();
    assert_eq!(s, LoadedState::default());
    assert!(backup.is_none());
}

/// #95：合法 JSON 但非 object → 空狀態、不產生任何備份、原檔不動（`session_manager.py` `_load_state`）。
#[test]
fn non_dict_json_not_backed_up() {
    for body in ["[1,2]", "1", "\"x\"", "null", "true"] {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("state.json");
        write(&p, body);
        let (s, backup) = load(&p).unwrap();
        assert_eq!(s, LoadedState::default(), "{body}");
        assert!(backup.is_none(), "{body}");
        assert_eq!(
            dir_names(d.path()),
            vec!["state.json".to_string()],
            "{body}"
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), body);
    }
}

#[test]
fn missing_file_is_default() {
    let d = tempfile::tempdir().unwrap();
    let (s, backup) = load(&d.path().join("state.json")).unwrap();
    assert_eq!(s, LoadedState::default());
    assert!(backup.is_none());
}

#[test]
fn tolerant_fields() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    write(
        &p,
        r#"{"bindings":{"a":1," b ":" /x ","c":"  ","   ":"y","d":null},
            "released":{"s1":"nope","s2":{"by_id":5,"reason":"r"},"s3":{"by_id":""},"s4":{"by_id":"/dev/B","extra":1}}}"#,
    );
    let (s, _) = load(&p).unwrap();
    assert_eq!(
        s.bindings,
        IndexMap::from([("b".to_string(), "/x".to_string())])
    );
    assert_eq!(
        s.released.keys().cloned().collect::<Vec<_>>(),
        vec!["s2", "s3", "s4"]
    );
    assert_eq!(
        s.released["s2"].by_id,
        PyValue::Int(sw_proto::pyjson::PyInt::Small(5))
    );
    assert_eq!(s.released["s2"].released_by, PyValue::Null);
    let m = StateModel::new(s);
    let ids = m.released_by_ids();
    assert_eq!(ids, std::collections::HashSet::from(["/dev/B".to_string()]));
}

/// #96：profile_pins／profile_detected 鍵值 strip 後空者略過、非字串略過，存 strip 後的值。
#[test]
fn profile_pins_and_detected_strip_tolerance() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    write(
        &p,
        r#"{"profile_pins":{" /k ":" p ","e":" ","  ":"v","n":1,"z":null},
            "profile_detected":{"/d":"t","x":[],"y":"\t"}}"#,
    );
    let (s, _) = load(&p).unwrap();
    assert_eq!(
        s.profile_pins,
        IndexMap::from([("/k".to_string(), "p".to_string())])
    );
    assert_eq!(
        s.profile_detected,
        IndexMap::from([("/d".to_string(), "t".to_string())])
    );
    // 非 dict 的區段整段忽略
    write(
        &p,
        r#"{"profile_pins":["a"],"profile_detected":"x","bindings":1,"released":[]}"#,
    );
    assert_eq!(load(&p).unwrap().0, LoadedState::default());
}

#[test]
fn unknown_keys_dropped_on_save() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    write(&p, r#"{"future":1,"aliases":{"a":{"x":1}}}"#);
    let (s, _) = load(&p).unwrap();
    let out = String::from_utf8(serialize_state(&s)).unwrap();
    assert!(!out.contains("future"));
    assert_eq!(
        out,
        "{\"aliases\":{\"a\":{\"x\":1}},\"bindings\":{},\"profile_detected\":{},\"profile_pins\":{},\"released\":{}}\n"
    );
}

/// #12：`aliases` 列無法 `dict(v)` 時 Python 啟動即拋未捕捉例外（rc 1、stderr 首行
/// `Traceback (most recent call last):`、末行為例外）。golden 取自 Python 3.12 基線（`dict(v)` 實測）。
#[test]
fn aliases_not_dict_fails_startup_like_python() {
    let cases: &[(&str, &str)] = &[
        ("1", "TypeError: 'int' object is not iterable"),
        ("null", "TypeError: 'NoneType' object is not iterable"),
        ("true", "TypeError: 'bool' object is not iterable"),
        ("1.5", "TypeError: 'float' object is not iterable"),
        (
            "\"ab\"",
            "ValueError: dictionary update sequence element #0 has length 1; 2 is required",
        ),
        (
            "[1]",
            "TypeError: cannot convert dictionary update sequence element #0 to a sequence",
        ),
        (
            "[[\"a\",1],null]",
            "TypeError: cannot convert dictionary update sequence element #1 to a sequence",
        ),
        (
            "[\"abc\"]",
            "ValueError: dictionary update sequence element #0 has length 3; 2 is required",
        ),
        (
            "[[\"a\"]]",
            "ValueError: dictionary update sequence element #0 has length 1; 2 is required",
        ),
        ("[[[1],2]]", "TypeError: unhashable type: 'list'"),
        ("[[{},2]]", "TypeError: unhashable type: 'dict'"),
    ];
    for (v, want) in cases {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("state.json");
        let body = format!("{{\"aliases\":{{\"x\":{v}}}}}");
        write(&p, &body);
        let e = load(&p).unwrap_err();
        assert_eq!(e.kind, StateLoadErrorKind::Uncaught, "{v}");
        assert_eq!(e.message, *want, "{v}");
        assert_eq!(e.exit_code(), 1);
        assert_eq!(e.stderr_text().lines().next(), Some(PY_TRACEBACK_HEAD));
        assert_eq!(e.stderr_text().lines().last(), Some(*want));
        // 原檔不動（Python 於 _save_state 之前即崩潰）
        assert_eq!(std::fs::read_to_string(&p).unwrap(), body);
    }
    // `aliases` 本身非 dict → Python 略過（不失敗）；可轉換的列依 `dict(v)` 結果載入。
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    write(&p, r#"{"aliases":[1]}"#);
    assert_eq!(load(&p).unwrap().0, LoadedState::default());
    write(
        &p,
        r#"{"aliases":{"e":"","l":[],"p":[["k",1],"ab",{"m":1,"n":2}],"i":[[1,"x"]]}}"#,
    );
    let s = load(&p).unwrap().0;
    let as_json =
        |k: &str| sw_proto::pyjson::dumps_dict(&s.aliases[k], &sw_proto::pyjson::DISK_STABLE);
    assert_eq!(as_json("e"), "{}");
    assert_eq!(as_json("l"), "{}");
    assert_eq!(as_json("p"), r#"{"a":"b","k":1,"m":"n"}"#);
    assert_eq!(as_json("i"), r#"{"1":"x"}"#);
}

#[test]
fn tmp_residue_ignored() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    write(&p, PY_BYTES);
    write(&d.path().join("state.json.tmp.abc"), "{garbage");
    let (s, backup) = load(&p).unwrap();
    assert!(backup.is_none());
    assert_eq!(s.released.len(), 1);
    let store = StateStore::spawn(p.clone(), native_fs());
    store.commit_sync(s).unwrap();
    assert_eq!(
        dir_names(d.path()),
        vec!["state.json".to_string(), "state.json.tmp.abc".to_string()]
    );
}

// ---------------------------------------------------------------------------
// StateModel
// ---------------------------------------------------------------------------

#[test]
fn dynamic_released_rows_retained_across_snapshots() {
    let mut loaded = LoadedState::default();
    loaded
        .released
        .insert("t+1:COM3".to_string(), row("/dev/serial/by-id/D"));
    let mut m = StateModel::new(loaded);
    assert!(m.snapshot().released.contains_key("t+1:COM3"));
    m.apply(StateDelta::Binding {
        sid: "t:COM0".into(),
        by_id: Some("/b".into()),
    });
    m.apply(StateDelta::Touch);
    let snap = m.snapshot();
    assert_eq!(snap.released["t+1:COM3"], row("/dev/serial/by-id/D"));
    assert_eq!(snap.bindings["t:COM0"], "/b");
    assert!(m.released_by_ids().contains("/dev/serial/by-id/D"));
}

/// #15：實體化失敗（未經 `Released` 實體化）的已載入列持續保留於 released 並阻擋該 tty。
#[test]
fn released_materialize_failure_keeps_block() {
    let mut loaded = LoadedState::default();
    loaded
        .released
        .insert("gone-template:COM2".to_string(), row("/dev/serial/by-id/G"));
    loaded
        .released
        .insert("t:COM0".to_string(), row("/dev/serial/by-id/A"));
    let mut m = StateModel::new(loaded);
    // t:COM0 實體化成功（session 進 RELEASED），gone-template:COM2 失敗（template 不存在）
    m.apply(StateDelta::Released {
        sid: "t:COM0".into(),
        row: row("/dev/serial/by-id/A"),
    });
    assert_eq!(
        m.unmaterialized_released()
            .keys()
            .cloned()
            .collect::<Vec<_>>(),
        vec!["gone-template:COM2"]
    );
    for _ in 0..3 {
        m.apply(StateDelta::Touch);
        let snap = m.snapshot();
        assert!(snap.released.contains_key("gone-template:COM2"));
        assert!(snap.released.contains_key("t:COM0"));
        assert!(m.released_by_ids().contains("/dev/serial/by-id/G"));
    }
    // 寫出再讀回仍在
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    StateStore::spawn(p.clone(), native_fs())
        .commit_sync(m.snapshot())
        .unwrap();
    assert!(
        load(&p)
            .unwrap()
            .0
            .released
            .contains_key("gone-template:COM2")
    );
}

#[test]
fn unreleased_removes_row() {
    let mut loaded = LoadedState::default();
    loaded.released.insert("a:COM0".to_string(), row("/A"));
    let mut m = StateModel::new(loaded);
    m.apply(StateDelta::Released {
        sid: "b:COM1".into(),
        row: row("/B"),
    });
    m.apply(StateDelta::Unreleased {
        sid: "a:COM0".into(),
    });
    m.apply(StateDelta::Unreleased {
        sid: "b:COM1".into(),
    });
    assert!(m.snapshot().released.is_empty());
    assert!(m.released_by_ids().is_empty());
}

#[test]
fn materialized_row_wins_over_loaded() {
    let mut loaded = LoadedState::default();
    loaded.released.insert("a:COM0".to_string(), row("/old"));
    let mut m = StateModel::new(loaded);
    m.apply(StateDelta::Released {
        sid: "a:COM0".into(),
        row: row("/new"),
    });
    assert_eq!(m.snapshot().released["a:COM0"].by_id, PyValue::from("/new"));
    assert!(!m.released_by_ids().contains("/old"));
}

#[test]
fn model_deltas_update_sections() {
    let mut m = StateModel::new(LoadedState::default());
    let mut r = PyDict::new();
    r.insert("session_id", PyValue::from("t:COM0"));
    m.apply(StateDelta::AliasSet {
        alias: "x".into(),
        row: Some(r.clone()),
    });
    m.apply(StateDelta::Pin {
        device_key: "/k".into(),
        profile: Some("t".into()),
    });
    m.apply(StateDelta::Detected {
        by_id: "/d".into(),
        profile: "t".into(),
    });
    let s = m.snapshot();
    assert_eq!(s.aliases["x"], r);
    assert_eq!(s.profile_pins["/k"], "t");
    assert_eq!(s.profile_detected["/d"], "t");
    m.apply(StateDelta::AliasSet {
        alias: "x".into(),
        row: None,
    });
    m.apply(StateDelta::Pin {
        device_key: "/k".into(),
        profile: None,
    });
    m.apply(StateDelta::Binding {
        sid: "nope".into(),
        by_id: None,
    });
    let s = m.snapshot();
    assert!(s.aliases.is_empty() && s.profile_pins.is_empty() && s.bindings.is_empty());
}

// ---------------------------------------------------------------------------
// StateStore
// ---------------------------------------------------------------------------

#[test]
fn commit_file_mode_0600_and_trailing_newline() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("sub").join("state.json"); // 目錄不存在 → 先 mkdirs（os.makedirs exist_ok）
    let store = StateStore::spawn(p.clone(), native_fs());
    store.commit_sync(LoadedState::default()).unwrap();
    let raw = std::fs::read(&p).unwrap();
    assert_eq!(raw.last(), Some(&b'\n'));
    assert_eq!(
        raw,
        b"{\"aliases\":{},\"bindings\":{},\"profile_detected\":{},\"profile_pins\":{},\"released\":{}}\n"
    );
    if let Some(mode) = file_mode(&p) {
        assert_eq!(mode, 0o600);
    }
}

#[test]
fn commit_failure_returns_error() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    write(&p, PY_BYTES);
    let fs = CountingFs::new();
    let store = StateStore::spawn(p.clone(), fs.clone());
    fs.fail_atomic_write(PalError::new(
        PalErrorKind::Io,
        Some(5),
        None,
        "Input/output error",
        "[Errno 5] Input/output error",
    ));
    let err = store.commit_sync(LoadedState::default()).unwrap_err();
    assert_eq!(err.os_code, Some(5));
    assert_eq!(std::fs::read_to_string(&p).unwrap(), PY_BYTES);
    // #23：失敗後同一 store 可再次提交（重試入口）並成功落盤
    store.commit_sync(LoadedState::default()).unwrap();
    assert_ne!(std::fs::read_to_string(&p).unwrap(), PY_BYTES);
}

/// #23：非同步 commit 的失敗經 done channel 回報，之後的 commit 依序執行（單一 persister）。
#[test]
fn async_commit_reports_failure_then_retry_succeeds() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    let fs = CountingFs::new();
    let store = StateStore::spawn(p.clone(), fs.clone());
    fs.fail_atomic_write(PalError::plain(PalErrorKind::Io, "boom"));
    let (tx, rx) = crossbeam_channel::bounded(4);
    let mut s1 = LoadedState::default();
    s1.bindings.insert("a".into(), "/1".into());
    let mut s2 = LoadedState::default();
    s2.bindings.insert("a".into(), "/2".into());
    store.commit(s1, tx.clone());
    store.commit(s2.clone(), tx);
    assert!(rx.recv().unwrap().is_err());
    assert!(rx.recv().unwrap().is_ok());
    assert_eq!(load(&p).unwrap().0, s2);
}

/// #13：每次 commit 一律實際寫入（即使內容相同；啟動完成 init 後的 `Touch` 落盤）。
#[test]
fn startup_always_rewrites_state_json() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    write(&p, PY_BYTES);
    let fs = CountingFs::new();
    let (s, _) = load_state(&p, &*fs).unwrap();
    let mut m = StateModel::new(s);
    m.apply(StateDelta::Touch);
    let store = StateStore::spawn(p.clone(), fs.clone());
    let ino_before = std::fs::metadata(&p).unwrap().modified().unwrap();
    std::thread::sleep(std::time::Duration::from_millis(20));
    store.commit_sync(m.snapshot()).unwrap();
    store.commit_sync(m.snapshot()).unwrap();
    assert_eq!(fs.writes(), 2);
    assert_eq!(std::fs::read_to_string(&p).unwrap(), PY_BYTES);
    assert!(std::fs::metadata(&p).unwrap().modified().unwrap() > ino_before);
}

#[cfg(feature = "test-fakes")]
#[test]
fn hooks_run_on_store_thread_around_write() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    let log = Arc::new(Mutex::new(Vec::<String>::new()));
    let (l1, l2, p1, p2) = (log.clone(), log.clone(), p.clone(), p.clone());
    let store = StateStore::spawn_with_hooks(
        p.clone(),
        native_fs(),
        Some(Box::new(move || {
            l1.lock().unwrap().push(format!(
                "before:{}:{}",
                std::thread::current().name().unwrap_or(""),
                p1.exists()
            ))
        })),
        Some(Box::new(move || {
            l2.lock().unwrap().push(format!("after:{}", p2.exists()))
        })),
    );
    store.commit_sync(LoadedState::default()).unwrap();
    assert_eq!(
        *log.lock().unwrap(),
        vec![
            "before:serialwrap-state-store:false".to_string(),
            "after:true".to_string()
        ]
    );
}

// ---------------------------------------------------------------------------
// AliasRegistry（alias_registry.py 逐字承接）
// ---------------------------------------------------------------------------

#[test]
fn alias_list_sorted_with_alias_key() {
    let mut r = AliasRegistry::load(IndexMap::new());
    r.assign_by_id("/dev/serial/by-id/Z", "zz", None, "T1");
    r.set_for_session("p:COM5", "custom", "T2");
    r.assign_by_id("/dev/serial/by-id/Y", "mm", Some("prpl-template"), "T3");
    r.assign_by_id("/dev/serial/by-id/W", "ee", Some(""), "T4"); // 空字串 profile 不加鍵（Python `if profile:`）
    let rows = r.list_alias();
    let names: Vec<_> = rows
        .iter()
        .map(|d| d.get("alias").and_then(|v| v.as_str()).unwrap().to_string())
        .collect();
    assert_eq!(names, vec!["custom", "ee", "mm", "zz"]);
    let dump = |d: &PyDict| sw_proto::pyjson::dumps_dict(d, &sw_proto::pyjson::RPC_WIRE);
    assert_eq!(
        dump(&rows[0]),
        r#"{"session_id":"p:COM5","updated_at":"T2","alias":"custom"}"#
    );
    assert_eq!(
        dump(&rows[1]),
        r#"{"device_by_id":"/dev/serial/by-id/W","updated_at":"T4","alias":"ee"}"#
    );
    assert_eq!(
        dump(&rows[2]),
        r#"{"device_by_id":"/dev/serial/by-id/Y","updated_at":"T3","profile":"prpl-template","alias":"mm"}"#
    );
    assert_eq!(
        dump(&rows[3]),
        r#"{"device_by_id":"/dev/serial/by-id/Z","updated_at":"T1","alias":"zz"}"#
    );
    // list_alias 為複本：dump 不含 "alias"、保留插入序
    let d = r.dump();
    assert_eq!(
        d.keys().cloned().collect::<Vec<_>>(),
        vec!["zz", "custom", "mm", "ee"]
    );
    assert!(d["zz"].get("alias").is_none());
}

#[test]
fn alias_unassign_missing_false() {
    let mut r = AliasRegistry::load(IndexMap::new());
    assert!(!r.unassign("nope"));
    r.set_for_session("t:COM0", "a", "T");
    r.set_for_session("t:COM1", "b", "T");
    assert!(r.unassign("a"));
    assert!(!r.unassign("a"));
    assert_eq!(r.dump().keys().cloned().collect::<Vec<_>>(), vec!["b"]);
}

#[test]
fn alias_set_overwrites_in_place() {
    let mut rows = IndexMap::new();
    let mut old = PyDict::new();
    old.insert("device_by_id", PyValue::from("/x"));
    old.insert("profile", PyValue::from("p"));
    rows.insert("a".to_string(), old);
    rows.insert("b".to_string(), PyDict::new());
    let mut r = AliasRegistry::load(rows);
    r.set_for_session("t:COM0", "a", "T9");
    let d = r.dump();
    assert_eq!(d.keys().cloned().collect::<Vec<_>>(), vec!["a", "b"]);
    assert_eq!(
        sw_proto::pyjson::dumps_dict(&d["a"], &sw_proto::pyjson::RPC_WIRE),
        r#"{"session_id":"t:COM0","updated_at":"T9"}"#
    );
}

#[test]
fn utf16_and_utf32_state_json_load_like_python() {
    // 審查 F54（Medium，c96a91c）：Python `json.loads(bytes)` 以 `json.detect_encoding` 判斷 UTF-8／16／32
    // （含 BOM 與無 BOM 的 NUL 樣式），UTF-16 改寫的 state.json 正常載入、RELEASED 保留、不產生 `.corrupt`。
    let text = r#"{"released":{"t:COM0":{"by_id":"/dev/serial/by-id/X","released_by":"agent","released_at":"2026-10-21T00:00:00","reason":null}}}"#;
    let utf16le_bom: Vec<u8> = [0xFFu8, 0xFE]
        .into_iter()
        .chain(text.encode_utf16().flat_map(u16::to_le_bytes))
        .collect();
    let utf16be: Vec<u8> = text.encode_utf16().flat_map(u16::to_be_bytes).collect();
    let utf32le: Vec<u8> = text.chars().flat_map(|c| (c as u32).to_le_bytes()).collect();
    let utf32be_bom: Vec<u8> = [0x00u8, 0x00, 0xFE, 0xFF]
        .into_iter()
        .chain(text.chars().flat_map(|c| (c as u32).to_be_bytes()))
        .collect();
    for (name, bytes) in [
        ("utf-16 BOM", utf16le_bom),
        ("utf-16-be", utf16be),
        ("utf-32-le", utf32le),
        ("utf-32 BOM", utf32be_bom),
    ] {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("state.json");
        std::fs::write(&p, &bytes).unwrap();
        let (s, backup) = load(&p).unwrap_or_else(|e| panic!("{name}：{e:?}"));
        assert!(backup.is_none(), "{name}：不得視為損毀");
        assert!(s.released.contains_key("t:COM0"), "{name}：RELEASED 應保留");
        assert!(!dir_names(d.path()).iter().any(|n| n.contains("corrupt")), "{name}");
    }
}

#[test]
fn double_bom_state_json_warning_matches_python() {
    // 審查 F58（Low）：CPython `json.loads(bytes)` 解碼後不做 str 的 BOM 檢查，雙重 BOM → decoder
    // `Expecting value: line 1 column 1 (char 0)`（實測），非 `Unexpected UTF-8 BOM`。
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    std::fs::write(&p, b"\xef\xbb\xbf\xef\xbb\xbf{}").unwrap();
    let warnings = std::cell::RefCell::new(Vec::new());
    let (_s, backup) = sw_core::state_store::load_state_with_warn(&p, &*native_fs(), &|m| {
        warnings.borrow_mut().push(m.to_string())
    })
    .unwrap();
    assert!(backup.is_some());
    let w = warnings.borrow();
    assert!(
        w.iter().any(|m| m.ends_with("：Expecting value: line 1 column 1 (char 0)")),
        "{w:?}"
    );
}

#[test]
fn state_json_line_ending_follows_python_text_mode() {
    // 審查 F55（Medium，c96a91c）：Python `_save_state` 以文字模式 `os.fdopen(fd, "w", encoding="utf-8")` 寫檔，
    // Windows 上唯一的結構換行（尾端）為 `\r\n`、POSIX 為 `\n`（JSON 字串內換行一律跳脫，不受影響）。
    let s = LoadedState::default();
    let crlf = sw_core::state_store::serialize_state_with(&s, b"\r\n");
    assert!(crlf.ends_with(b"}\r\n"), "{:?}", String::from_utf8_lossy(&crlf));
    assert_eq!(crlf.iter().filter(|b| **b == b'\n').count(), 1);
    let native = sw_core::state_store::serialize_state(&s);
    assert!(native.ends_with(sw_pal::fs_ops::TEXT_NEWLINE));
    let expected: &[u8] = if std::path::MAIN_SEPARATOR == '\\' { b"\r\n" } else { b"\n" };
    assert_eq!(sw_pal::fs_ops::TEXT_NEWLINE, expected);
    // 讀取端兩種皆接受。
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("state.json");
    std::fs::write(&p, &crlf).unwrap();
    assert!(load(&p).unwrap().1.is_none());
}
