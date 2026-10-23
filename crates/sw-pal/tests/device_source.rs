//! P5-T5：PAL 裝置來源與 DeviceWatcher 測試（計畫 Task 5、arch §4.3、SC-19／OD-11、#100 排序前提）。
//!
//! 涵蓋 POSIX by-id／by-path 掃描（realpath 去重、allowlist 完整比對）、Windows SERIALCOMM＋藍牙雙重排除（經
//! `RegistryView` 替身，Linux 上也可跑）、BTHENUM 快取 TTL 5 s，以及 watcher 的差異演算法與「單輪失敗不終止 thread」。
//!
//! 測試 POSIX／Windows 裝置掃描、原生來源工廠接線、watcher 初始快照與停止行為；
//! `exclude_bluetooth`／`read_bt_ports` 沿用 P4 05 的實作。
//!
//! 替身語意約定（計畫 Task 1）：`FakeDeviceScript::set` 之後每輪掃描都回傳該快照，直到下一次 `set`。
#![cfg(feature = "test-fakes")]

use std::collections::{HashMap, HashSet};
#[cfg(unix)]
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::fs::symlink;

use sw_pal::device_source::windows_model::*;
use sw_pal::device_source::{posix_scan, WindowsDeviceSource};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::FakeDeviceSource;
use sw_pal::traits::{DeviceSource, PalError, PalErrorKind, PalResult};
use sw_pal::{DeviceInfo, DeviceWatcher, WatcherEvent};

/// BTHENUM 根鍵（07 §1.6）。
const BTHENUM: &str = "HKLM\\SYSTEM\\CurrentControlSet\\Enum\\BTHENUM";
/// watcher 測試的等待上限；超過即判定為逾時失敗。
const WAIT: Duration = Duration::from_secs(5);
/// watcher 輪詢間隔（真實時鐘，避免假時鐘自動前進造成事件洪流）。
const POLL: Duration = Duration::from_millis(5);

const A: &str = "/dev/serial/by-id/usb-A";
const B: &str = "/dev/serial/by-id/usb-B";
const TTY0: &str = "/dev/ttyUSB0";
const TTY1: &str = "/dev/ttyUSB1";

// ---------------------------------------------------------------------------
// POSIX 掃描
// ---------------------------------------------------------------------------

#[cfg(unix)]
struct PosixFixture {
    _dir: tempfile::TempDir,
    byid: PathBuf,
    bypath: PathBuf,
    tty: PathBuf,
    tty_y: PathBuf,
}

/// by-id：`usb-B`→ttyX、`usb-A`→ttyY、`usb-dangling`（斷鏈）；by-path：`pci-0`→ttyX（與 `usb-B` 同一實體 tty）。
#[cfg(unix)]
fn posix_fixture() -> PosixFixture {
    let d = tempfile::tempdir().unwrap();
    let byid = d.path().join("by-id");
    let bypath = d.path().join("by-path");
    let tty = d.path().join("ttyX");
    let tty_y = d.path().join("ttyY");
    std::fs::create_dir_all(&byid).unwrap();
    std::fs::create_dir_all(&bypath).unwrap();
    std::fs::write(&tty, b"").unwrap();
    std::fs::write(&tty_y, b"").unwrap();
    symlink(&tty, byid.join("usb-B")).unwrap();
    symlink(&tty_y, byid.join("usb-A")).unwrap();
    symlink(d.path().join("missing"), byid.join("usb-dangling")).unwrap();
    symlink(&tty, bypath.join("pci-0")).unwrap();
    PosixFixture {
        _dir: d,
        byid,
        bypath,
        tty,
        tty_y,
    }
}

#[cfg(unix)]
#[test]
fn by_id_wins_over_by_path_for_same_tty_and_sorted_names() {
    let f = posix_fixture();
    let got = posix_scan(&[f.byid.clone(), f.bypath.clone()], None).unwrap();
    let keys: Vec<_> = got.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(
        keys,
        vec![
            f.byid.join("usb-A").display().to_string(),
            f.byid.join("usb-B").display().to_string(),
        ]
    );
    assert_eq!(
        got[0].1.real_path,
        std::fs::canonicalize(&f.tty_y).unwrap().display().to_string()
    );
    assert_eq!(
        got[1].1,
        DeviceInfo {
            by_id: f.byid.join("usb-B").display().to_string(),
            real_path: std::fs::canonicalize(&f.tty).unwrap().display().to_string(),
        }
    );
}

#[test]
fn scan_skips_missing_dirs_and_includes_regular_files() {
    let d = tempfile::tempdir().unwrap();
    let missing = d.path().join("no-such-dir");
    let dir = d.path().join("by-id");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("plain"), b"").unwrap();
    let got = posix_scan(&[missing, dir.clone()], None).expect("不存在的目錄應略過而非回錯");
    let keys: Vec<String> = got.into_iter().map(|(k, _)| k).collect();
    assert_eq!(keys, vec![dir.join("plain").display().to_string()]);
}

#[cfg(unix)]
#[test]
fn allowlist_filters_by_id_exactly() {
    let f = posix_fixture();
    let allow = vec![f.byid.join("usb-A").display().to_string()];
    let got = posix_scan(&[f.byid.clone(), f.bypath.clone()], Some(allow.as_slice())).unwrap();
    let keys: Vec<String> = got.into_iter().map(|(k, _)| k).collect();
    assert_eq!(keys, vec![f.byid.join("usb-A").display().to_string()]);

    // by_id 以完整字串比對：片段不得命中。
    let partial = vec!["usb-A".to_string()];
    let none = posix_scan(&[f.byid.clone()], Some(partial.as_slice())).unwrap();
    assert!(none.is_empty(), "allowlist 必須完整比對 by_id，片段不得命中：{none:?}");
}

#[cfg(unix)]
#[test]
fn allowlist_does_not_let_by_path_replace_deduplicated_by_id() {
    let f = posix_fixture();
    let allow = vec![f.bypath.join("pci-0").display().to_string()];
    let got = posix_scan(&[f.byid.clone(), f.bypath.clone()], Some(allow.as_slice())).unwrap();

    assert!(
        got.is_empty(),
        "by-id 先占用同一 realpath 後，即使 allowlist 僅含 by-path 也不得補位：{got:?}"
    );
}

#[cfg(unix)]
#[test]
fn native_device_factory_uses_selection_and_exact_allowlist() {
    let d = tempfile::tempdir().unwrap();
    let by_id = d.path().join("by-id");
    let by_path = d.path().join("by-path");
    std::fs::create_dir_all(&by_id).unwrap();
    std::fs::create_dir_all(&by_path).unwrap();
    std::fs::write(by_id.join("usb-A"), b"").unwrap();
    std::fs::write(by_path.join("pci-0"), b"").unwrap();

    let allowed = by_id.join("usb-A").display().to_string();
    let mut source = (sw_pal::native_devices())(sw_pal::traits::DeviceSelection {
        by_id_dir: by_id.clone(),
        by_path_dir: by_path,
        exclude_coms: Vec::new(),
        test_allowlist: Some(vec![allowed.clone()]),
    });
    let got = source.scan().unwrap();

    assert_eq!(
        got,
        vec![(
            allowed.clone(),
            DeviceInfo {
                by_id: allowed.clone(),
                real_path: std::fs::canonicalize(by_id.join("usb-A"))
                    .unwrap()
                    .display()
                    .to_string(),
            },
        )]
    );
}

#[cfg(unix)]
#[test]
fn native_device_factory_prefers_by_id_for_shared_real_path() {
    let d = tempfile::tempdir().unwrap();
    let by_id = d.path().join("by-id");
    let by_path = d.path().join("by-path");
    let tty = d.path().join("ttyX");
    std::fs::create_dir_all(&by_id).unwrap();
    std::fs::create_dir_all(&by_path).unwrap();
    std::fs::write(&tty, b"").unwrap();
    symlink(&tty, by_id.join("usb-A")).unwrap();
    symlink(&tty, by_path.join("pci-0")).unwrap();

    let mut source = (sw_pal::native_devices())(sw_pal::traits::DeviceSelection {
        by_id_dir: by_id.clone(),
        by_path_dir: by_path,
        exclude_coms: Vec::new(),
        test_allowlist: None,
    });

    let got = source.scan().unwrap();
    assert_eq!(got.len(), 1, "同一實體 tty 只能列舉一次：{got:?}");
    assert_eq!(got[0].0, by_id.join("usb-A").display().to_string());
}

// ---------------------------------------------------------------------------
// Windows 模型（SERIALCOMM／BTHENUM／藍牙排除）
// ---------------------------------------------------------------------------

/// 登錄替身：只實作 P5-T5 所需的三個查詢。`calls_bthenum_root` 記錄 BTHENUM 根鍵被列舉的次數（TTL 快取驗證用）。
#[derive(Default)]
struct FakeView {
    serialcomm: Vec<(String, String)>,
    serialcomm_missing: bool,
    /// 鍵路徑 → 子鍵名稱清單。
    children: HashMap<String, Vec<String>>,
    /// `…\Device Parameters` 完整路徑 → PortName（REG_SZ）。
    port_names: HashMap<String, String>,
    /// PortName 不是字串（非 REG_SZ）的 `…\Device Parameters` 完整路徑；查詢回 `Ok(None)`。
    non_string_port: HashSet<String>,
    /// OpenKey 失敗的鍵（子鍵列舉與查詢皆回 `Err`）。
    failing: HashSet<String>,
    calls_bthenum_root: Arc<AtomicUsize>,
}

impl RegistryView for FakeView {
    fn enum_values(&self, key: &str) -> PalResult<Vec<(String, String)>> {
        if key != SERIALCOMM_KEY {
            return Ok(Vec::new());
        }
        if self.serialcomm_missing {
            return Err(PalError::plain(PalErrorKind::NotFound, "SERIALCOMM 登錄鍵不存在"));
        }
        Ok(self.serialcomm.clone())
    }

    fn enum_subkeys(&self, key: &str) -> PalResult<Vec<String>> {
        if key == BTHENUM {
            self.calls_bthenum_root.fetch_add(1, Ordering::SeqCst);
        }
        if self.failing.contains(key) {
            return Err(PalError::plain(PalErrorKind::AccessDenied, "OpenKey 失敗"));
        }
        Ok(self.children.get(key).cloned().unwrap_or_default())
    }

    fn query_string(&self, key: &str, name: &str) -> PalResult<Option<String>> {
        if self.failing.contains(key) {
            return Err(PalError::plain(PalErrorKind::AccessDenied, "OpenKey 失敗"));
        }
        if name != "PortName" || self.non_string_port.contains(key) {
            return Ok(None);
        }
        Ok(self.port_names.get(key).cloned())
    }
}

#[test]
fn bluetooth_double_exclusion_keeps_enumeration_order() {
    let sc = vec![
        (r"\Device\Serial0".to_string(), "COM3".to_string()),
        (r"\Device\BthModem0".to_string(), "COM7".to_string()),
        (r"\Device\VCP0".to_string(), "COM10".to_string()),
        (r"\Device\VCP1".to_string(), "COM5".to_string()),
    ];
    let bt: HashSet<String> = ["COM5".to_string()].into();
    let ex: HashSet<String> = ["COM10".to_string()].into();
    assert_eq!(exclude_bluetooth(&sc, &bt, &ex), vec!["COM3".to_string()]);
}

#[test]
fn bt_ports_walk_recursive_portname() {
    let x = format!("{BTHENUM}\\X");
    let y = format!("{x}\\y");
    let z = format!("{BTHENUM}\\Z");
    let w = format!("{BTHENUM}\\W");
    let mut v = FakeView::default();
    v.children
        .insert(BTHENUM.to_string(), vec!["X".into(), "Z".into(), "W".into()]);
    v.children.insert(x.clone(), vec!["y".into()]);
    // 第一層與更深一層各有 PortName。
    v.port_names.insert(format!("{x}\\Device Parameters"), "COM9".into());
    v.port_names.insert(format!("{y}\\Device Parameters"), "COM5".into());
    // 非字串 PortName 略過。
    v.non_string_port.insert(format!("{z}\\Device Parameters"));
    // OpenKey 錯誤必須被吞掉，不得中斷掃描。
    v.failing.insert(w.clone());
    v.failing.insert(format!("{w}\\Device Parameters"));

    let got = read_bt_ports(&v);
    let want: HashSet<String> = ["COM5", "COM9"].iter().map(|s| s.to_string()).collect();
    assert_eq!(got, want);
}

#[test]
fn windows_scan_maps_real_path() {
    let view = FakeView {
        serialcomm: vec![(r"\Device\Serial0".into(), "COM3".into())],
        ..Default::default()
    };
    let mut src = WindowsDeviceSource::new(Box::new(view), FakeClock::new(), HashSet::new(), None);
    let got = src.scan().expect("SERIALCOMM 可讀時 scan 應成功");
    assert_eq!(
        got,
        vec![(
            "COM3".to_string(),
            DeviceInfo {
                by_id: "COM3".into(),
                real_path: r"\\.\COM3".into(),
            },
        )]
    );
}

#[test]
fn windows_allowlist_matches_complete_com_name() {
    let view = FakeView {
        serialcomm: vec![
            (r"\Device\Serial0".into(), "COM3".into()),
            (r"\Device\Serial1".into(), "COM30".into()),
        ],
        ..Default::default()
    };
    let mut src = WindowsDeviceSource::new(
        Box::new(view),
        FakeClock::new(),
        HashSet::new(),
        Some(vec!["COM3".into()]),
    );

    let got = src.scan().unwrap();
    assert_eq!(
        got.iter().map(|(key, _)| key.as_str()).collect::<Vec<_>>(),
        vec!["COM3"],
        "OD-11 allowlist 必須完整比對 COM 名稱"
    );
}

fn windows_filter_fixture() -> FakeView {
    let bt_device = format!("{BTHENUM}\\PairedDevice");
    let mut view = FakeView {
        serialcomm: vec![
            (r"\Device\Serial0".into(), "COM3".into()),
            (r"\Device\VCP0".into(), "COM5".into()),
            (r"\Device\VCP1".into(), "COM10".into()),
            (r"\Device\bThMoDeM0".into(), "COM7".into()),
            (r"\Device\Serial1".into(), "COM4".into()),
        ],
        ..Default::default()
    };
    view.children
        .insert(BTHENUM.to_string(), vec!["PairedDevice".into()]);
    view.port_names
        .insert(format!("{bt_device}\\Device Parameters"), "COM5".into());
    view
}

#[test]
fn windows_scan_applies_bthenum_exclude_coms_and_bthmodem() {
    let exclude: HashSet<String> = ["COM10".to_string()].into();
    let mut source = WindowsDeviceSource::new(
        Box::new(windows_filter_fixture()),
        FakeClock::new(),
        exclude,
        None,
    );

    let got = source.scan().unwrap();
    assert_eq!(
        got.iter().map(|(key, _)| key.as_str()).collect::<Vec<_>>(),
        vec!["COM3", "COM4"],
        "BTHENUM、exclude_coms 與 bthmodem 排除後應保留 SERIALCOMM 順序"
    );
}

#[test]
fn windows_allowlist_cannot_resurrect_excluded_com() {
    let exclude: HashSet<String> = ["COM10".to_string()].into();
    let allowlist = vec!["COM3", "COM5", "COM7", "COM10"]
        .into_iter()
        .map(str::to_string)
        .collect();
    let mut source = WindowsDeviceSource::new(
        Box::new(windows_filter_fixture()),
        FakeClock::new(),
        exclude,
        Some(allowlist),
    );

    let got = source.scan().unwrap();
    assert_eq!(
        got.iter().map(|(key, _)| key.as_str()).collect::<Vec<_>>(),
        vec!["COM3"],
        "OD-11 allowlist 不得重新加入已排除的藍牙或 exclude_coms 埠"
    );
}

#[cfg(windows)]
#[test]
fn native_windows_factory_applies_empty_allowlist() {
    let mut source = (sw_pal::native_devices())(sw_pal::traits::DeviceSelection {
        by_id_dir: std::path::PathBuf::new(),
        by_path_dir: std::path::PathBuf::new(),
        exclude_coms: Vec::new(),
        test_allowlist: Some(Vec::new()),
    });

    assert!(
        source.scan().unwrap().is_empty(),
        "原生 Windows 工廠也必須套用空 allowlist"
    );
}

#[test]
fn bt_cache_ttl_5s() {
    let view = FakeView::default();
    let calls = Arc::clone(&view.calls_bthenum_root);
    let clock = FakeClock::new();
    let mut src = WindowsDeviceSource::new(Box::new(view), clock.clone(), HashSet::new(), None);

    src.scan().unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1, "第一次 scan 應讀取 BTHENUM");

    clock.advance(Duration::from_millis(4_900));
    src.scan().unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1, "4.9 s 內應命中快取、不讀 BTHENUM");

    clock.advance(Duration::from_millis(200));
    src.scan().unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 2, "5.1 s 後快取過期，應重讀 BTHENUM");
}

#[test]
fn serialcomm_missing_key_is_empty() {
    let view = FakeView {
        serialcomm_missing: true,
        ..Default::default()
    };
    let mut src = WindowsDeviceSource::new(Box::new(view), FakeClock::new(), HashSet::new(), None);
    let got = src
        .scan()
        .expect("SERIALCOMM 鍵不存在（NotFound）應回 Ok(空)，不得回錯");
    assert!(got.is_empty(), "應為空清單：{got:?}");
}

#[cfg(windows)]
#[test]
fn windows_live_serialcomm_read_ok() {
    use sw_pal::device_source::windows_registry::WinRegistryView;
    let view = WinRegistryView;
    let got = view.enum_values(SERIALCOMM_KEY);
    assert!(got.is_ok(), "真實 HKLM SERIALCOMM 唯讀列舉應回 Ok：{got:?}");
}

// ---------------------------------------------------------------------------
// DeviceWatcher：差異演算法與單輪失敗隔離
// ---------------------------------------------------------------------------

#[test]
fn watcher_diff_appends_changed_after_added_and_removed() {
    let a = |k: &str, r: &str| {
        (
            k.to_string(),
            DeviceInfo {
                by_id: k.into(),
                real_path: r.into(),
            },
        )
    };
    let prev = vec![a("/b/x", "/dev/ttyUSB0"), a("/b/y", "/dev/ttyUSB1")];
    let cur = vec![a("/b/x", "/dev/ttyUSB2"), a("/b/z", "/dev/ttyUSB3")];
    let (added, removed) = DeviceWatcher::diff(&prev, &cur);
    assert_eq!(
        added
            .iter()
            .map(|d| (&*d.by_id, &*d.real_path))
            .collect::<Vec<_>>(),
        vec![("/b/z", "/dev/ttyUSB3"), ("/b/x", "/dev/ttyUSB2")]
    );
    assert_eq!(
        removed
            .iter()
            .map(|d| (&*d.by_id, &*d.real_path))
            .collect::<Vec<_>>(),
        vec![("/b/y", "/dev/ttyUSB1"), ("/b/x", "/dev/ttyUSB0")]
    );
}

fn dev(by_id: &str, real_path: &str) -> (String, DeviceInfo) {
    (
        by_id.to_string(),
        DeviceInfo {
            by_id: by_id.to_string(),
            real_path: real_path.to_string(),
        },
    )
}

#[derive(Clone, Copy)]
enum Injected {
    Fail,
    Panic,
}

/// 依「第幾輪」注入失敗或 panic 的 DeviceSource 包裝；其餘輪委派給 `FakeDeviceSource`。
/// `threads` 記錄每次 scan 被呼叫時的 thread 名稱。
struct RoundGate {
    inner: FakeDeviceSource,
    rounds: Arc<AtomicUsize>,
    inject: Option<(usize, Injected)>,
    threads: Arc<Mutex<Vec<String>>>,
}

struct CountingSource(Arc<AtomicUsize>);

impl DeviceSource for CountingSource {
    fn scan(&mut self) -> PalResult<Vec<(String, DeviceInfo)>> {
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(Vec::new())
    }
}

impl DeviceSource for RoundGate {
    fn scan(&mut self) -> PalResult<Vec<(String, DeviceInfo)>> {
        let round = self.rounds.fetch_add(1, Ordering::SeqCst) + 1;
        let name = std::thread::current().name().unwrap_or_default().to_string();
        self.threads.lock().unwrap().push(name);
        match self.inject {
            Some((r, Injected::Fail)) if r == round => {
                Err(PalError::plain(PalErrorKind::Busy, format!("注入：第 {round} 輪掃描失敗")))
            }
            Some((r, Injected::Panic)) if r == round => {
                panic!("注入：第 {round} 輪 scan panic")
            }
            _ => self.inner.scan(),
        }
    }
}

/// 以真實時鐘啟動 watcher，回傳控制柄與事件接收端。
fn start_watcher(
    src: Box<dyn DeviceSource>,
) -> (sw_pal::WatcherHandle, crossbeam_channel::Receiver<WatcherEvent>) {
    let (tx, rx) = crossbeam_channel::unbounded();
    let clock = sw_pal::native().clock;
    (DeviceWatcher::start(src, POLL, tx, clock), rx)
}

fn start_watcher_with_baseline(
    src: Box<dyn DeviceSource>,
    baseline: Vec<(String, DeviceInfo)>,
) -> (sw_pal::WatcherHandle, crossbeam_channel::Receiver<WatcherEvent>) {
    let (tx, rx) = crossbeam_channel::unbounded();
    let clock = sw_pal::native().clock;
    (
        DeviceWatcher::start_with_baseline(src, POLL, tx, clock, baseline),
        rx,
    )
}

#[test]
fn watcher_initial_scan_returns_snapshot_or_source_error() {
    let (mut source, script) = FakeDeviceSource::new(vec![dev(A, TTY0)]);
    assert_eq!(
        DeviceWatcher::initial_scan(&mut source).unwrap(),
        vec![dev(A, TTY0)]
    );

    script.fail_next(PalError::plain(PalErrorKind::Busy, "初始化掃描失敗"));
    let error = DeviceWatcher::initial_scan(&mut source).unwrap_err();
    assert_eq!(error.kind, PalErrorKind::Busy);
    assert_eq!(error.py_message, "初始化掃描失敗");
}

#[test]
fn watcher_uses_initial_snapshot_as_baseline() {
    let (mut source, _script) = FakeDeviceSource::new(vec![dev(A, TTY0)]);
    let initial = DeviceWatcher::initial_scan(&mut source).unwrap();
    let (handle, rx) = start_watcher_with_baseline(Box::new(source), initial);

    let first = rx.recv_timeout(WAIT).expect("初始掃描後應收到 Tick");
    assert!(
        matches!(first, WatcherEvent::Tick),
        "未變化的初始快照不可重複發送 Delta：{first:?}"
    );
    handle.stop();
}

#[test]
fn watcher_reports_device_removed_after_initial_scan() {
    let (mut source, script) = FakeDeviceSource::new(vec![dev(A, TTY0)]);
    let initial = DeviceWatcher::initial_scan(&mut source).unwrap();
    script.set(Vec::new());
    let (handle, rx) = start_watcher_with_baseline(Box::new(source), initial);

    match rx.recv_timeout(WAIT).expect("初始掃描後拔除裝置應送出 Delta") {
        WatcherEvent::Delta {
            added,
            removed,
            snapshot,
        } => {
            assert!(added.is_empty());
            assert_eq!(removed, vec![DeviceInfo { by_id: A.into(), real_path: TTY0.into() }]);
            assert!(snapshot.is_empty());
        }
        other => panic!("預期首輪送出移除 Delta，實際：{other:?}"),
    }
    assert!(matches!(rx.recv_timeout(WAIT).unwrap(), WatcherEvent::Tick));
    handle.stop();
}

/// 讀取事件直到符合 `pred`，其餘事件略過；逾時即 panic。
fn recv_until(
    rx: &crossbeam_channel::Receiver<WatcherEvent>,
    pred: impl Fn(&WatcherEvent) -> bool,
) -> WatcherEvent {
    let deadline = Instant::now() + WAIT;
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        let ev = rx
            .recv_timeout(left)
            .expect("逾時仍未收到符合條件的 WatcherEvent");
        if pred(&ev) {
            return ev;
        }
    }
}

fn wait_until(what: &str, pred: impl Fn() -> bool) {
    let deadline = Instant::now() + WAIT;
    while !pred() {
        assert!(Instant::now() < deadline, "逾時：{what}");
        std::thread::sleep(Duration::from_millis(1));
    }
}

#[test]
fn watcher_scan_failure_does_not_stop_thread() {
    let (inner, _script) = FakeDeviceSource::new(vec![dev(A, TTY0)]);
    let threads = Arc::new(Mutex::new(Vec::new()));
    let gate = RoundGate {
        inner,
        rounds: Arc::new(AtomicUsize::new(0)),
        inject: Some((2, Injected::Fail)),
        threads: Arc::clone(&threads),
    };
    let (handle, rx) = start_watcher(Box::new(gate));

    // 第 2 輪失敗：必須送出 ScanFailed（其餘輪事件略過）。
    recv_until(&rx, |e| matches!(e, WatcherEvent::ScanFailed(_)));

    // 第 3 輪快照未變：thread 仍存活，ScanFailed 之後的下一個事件必為 Tick。
    let next = rx
        .recv_timeout(WAIT)
        .expect("ScanFailed 之後 thread 應續行並送出第 3 輪事件");
    assert!(
        matches!(next, WatcherEvent::Tick),
        "第 3 輪快照未變應為 Tick，實際：{next:?}"
    );

    handle.stop();
    let names = threads.lock().unwrap();
    assert!(names.len() >= 3, "應至少完成 3 輪掃描，實際 {} 輪", names.len());
    assert!(
        names.iter().all(|n| n.as_str() == "serialwrap-device-watcher"),
        "watcher thread 名稱應為 serialwrap-device-watcher：{names:?}"
    );
}

#[test]
fn watcher_emits_delta_only_when_changed() {
    let (inner, script) = FakeDeviceSource::new(vec![dev(A, TTY0)]);
    let gate = RoundGate {
        inner,
        rounds: Arc::new(AtomicUsize::new(0)),
        inject: None,
        threads: Arc::new(Mutex::new(Vec::new())),
    };
    let (handle, rx) = start_watcher(Box::new(gate));

    // 基準：先收到一個 Tick（若實作在第 1 輪以空基準送出 Delta，此處一併略過）。
    recv_until(&rx, |e| matches!(e, WatcherEvent::Tick));

    // 無變化：連續 5 個事件都必須是 Tick。
    for i in 0..5 {
        let ev = rx
            .recv_timeout(WAIT)
            .expect("無變化時 watcher 仍應每輪送出事件");
        assert!(
            matches!(ev, WatcherEvent::Tick),
            "無變化時第 {i} 個事件應為 Tick，實際：{ev:?}"
        );
    }

    // 有變化：A 換成 B → Delta{added:[B], removed:[A], snapshot:[B]}，之後才接 Tick。
    script.set(vec![dev(B, TTY1)]);
    match recv_until(&rx, |e| matches!(e, WatcherEvent::Delta { .. })) {
        WatcherEvent::Delta {
            added,
            removed,
            snapshot,
        } => {
            assert_eq!(
                added,
                vec![DeviceInfo {
                    by_id: B.into(),
                    real_path: TTY1.into(),
                }]
            );
            assert_eq!(
                removed,
                vec![DeviceInfo {
                    by_id: A.into(),
                    real_path: TTY0.into(),
                }]
            );
            assert_eq!(snapshot, vec![dev(B, TTY1)]);
        }
        other => panic!("預期 Delta，實際：{other:?}"),
    }
    let after = rx
        .recv_timeout(WAIT)
        .expect("Delta 之後 watcher 仍應送出 Tick");
    assert!(
        matches!(after, WatcherEvent::Tick),
        "Delta 之後應為 Tick，實際：{after:?}"
    );

    // 若 previous 未更新成 B，後續每輪都會重複送出相同 Delta。
    for i in 0..5 {
        let ev = rx
            .recv_timeout(WAIT)
            .expect("Delta 後無變化時仍應持續送出 Tick");
        assert!(
            matches!(ev, WatcherEvent::Tick),
            "Delta 後第 {i} 輪應只有 Tick，實際：{ev:?}"
        );
    }

    handle.stop();
}

#[test]
fn watcher_tick_panic_isolated() {
    let (inner, _script) = FakeDeviceSource::new(vec![dev(A, TTY0)]);
    let rounds = Arc::new(AtomicUsize::new(0));
    let gate = RoundGate {
        inner,
        rounds: Arc::clone(&rounds),
        inject: Some((2, Injected::Panic)),
        threads: Arc::new(Mutex::new(Vec::new())),
    };
    let (handle, rx) = start_watcher(Box::new(gate));

    // 第 2 輪 panic 後 thread 必須續行：掃描計數需走到第 4 輪。
    wait_until("第 2 輪 panic 後 watcher 應續行到第 4 輪", || {
        rounds.load(Ordering::SeqCst) >= 4
    });

    // 第 1 輪與第 3 輪各應送出一個事件（第 2 輪 panic 不送事件）。
    let got: Vec<WatcherEvent> = rx.try_iter().collect();
    assert!(
        got.len() >= 2,
        "第 1 輪與第 3 輪各應送出事件，實際：{got:?}"
    );
    assert!(
        got.iter().any(|e| matches!(e, WatcherEvent::Tick)),
        "第 3 輪之後應能收到 Tick：{got:?}"
    );

    handle.stop();
}

#[test]
fn watcher_stop_interrupts_full_event_queue() {
    let rounds = Arc::new(AtomicUsize::new(0));
    let (tx, rx) = crossbeam_channel::bounded(1);
    let handle = DeviceWatcher::start(
        Box::new(CountingSource(Arc::clone(&rounds))),
        Duration::from_millis(1),
        tx,
        sw_pal::native().clock,
    );

    wait_until("watcher 在滿佇列上進入第二輪", || {
        rounds.load(Ordering::SeqCst) >= 2
    });
    let (done_tx, done_rx) = crossbeam_channel::bounded(1);
    let stopper = std::thread::spawn(move || {
        handle.stop();
        let _ = done_tx.send(());
    });

    if done_rx.recv_timeout(Duration::from_secs(1)).is_err() {
        // 釋放接收端可讓舊版阻塞 send 退出，避免失敗測試遺留 watcher thread。
        drop(rx);
        stopper.join().unwrap();
        panic!("停止必須中斷滿佇列上的事件傳送並有限時間內返回");
    }
    drop(rx);
    stopper.join().unwrap();
}

#[test]
fn watcher_poll_wait_uses_fake_clock_and_stop_interrupts_it() {
    let rounds = Arc::new(AtomicUsize::new(0));
    let clock = FakeClock::at(0, 0);
    let (tx, rx) = crossbeam_channel::unbounded();
    let poll = Duration::from_secs(60);
    let handle = DeviceWatcher::start(
        Box::new(CountingSource(Arc::clone(&rounds))),
        poll,
        tx,
        clock.clone(),
    );

    assert!(matches!(rx.recv_timeout(WAIT).unwrap(), WatcherEvent::Tick));
    wait_until("FakeClock 註冊第一個 watcher 等待", || {
        clock.sleeps.lock().unwrap().len() >= 1
    });
    std::thread::sleep(Duration::from_millis(30));
    assert_eq!(rounds.load(Ordering::SeqCst), 1, "假時鐘未前進時不得再次掃描");

    clock.advance(poll - Duration::from_millis(1));
    wait_until("FakeClock 註冊剩餘 1 ms 等待", || {
        clock.sleeps.lock().unwrap().len() >= 2
    });
    assert_eq!(rounds.load(Ordering::SeqCst), 1, "期限前不得再次掃描");

    clock.advance(Duration::from_millis(1));
    assert!(matches!(rx.recv_timeout(WAIT).unwrap(), WatcherEvent::Tick));
    assert_eq!(rounds.load(Ordering::SeqCst), 2, "期限到達後應再掃描一次");
    wait_until("FakeClock 註冊下一輪 watcher 等待", || {
        clock.sleeps.lock().unwrap().len() >= 3
    });

    let started = Instant::now();
    handle.stop();
    assert!(
        started.elapsed() < Duration::from_secs(1),
        "stop 應能在假時鐘未前進時取消輪詢等待"
    );
}

struct SlowSource {
    started: Arc<std::sync::atomic::AtomicBool>,
    finished: Arc<std::sync::atomic::AtomicBool>,
}

impl DeviceSource for SlowSource {
    fn scan(&mut self) -> PalResult<Vec<(String, DeviceInfo)>> {
        self.started.store(true, Ordering::SeqCst);
        std::thread::sleep(Duration::from_millis(2_300));
        self.finished.store(true, Ordering::SeqCst);
        Ok(Vec::new())
    }
}

#[test]
fn watcher_stop_is_bounded_when_scan_is_slow() {
    use std::sync::atomic::AtomicBool;

    let started = Arc::new(AtomicBool::new(false));
    let finished = Arc::new(AtomicBool::new(false));
    let (tx, _rx) = crossbeam_channel::unbounded();
    let handle = DeviceWatcher::start(
        Box::new(SlowSource {
            started: Arc::clone(&started),
            finished: Arc::clone(&finished),
        }),
        Duration::from_secs(60),
        tx,
        sw_pal::native().clock,
    );
    wait_until("慢速掃描開始", || started.load(Ordering::SeqCst));

    let before = Instant::now();
    handle.stop();
    let elapsed = before.elapsed();
    assert!(
        elapsed < Duration::from_millis(2_500),
        "scan 受阻時 stop 應在 2 s 上限後返回，實際耗時 {elapsed:?}"
    );
    wait_until("逾時後 detached watcher 完成目前掃描", || {
        finished.load(Ordering::SeqCst)
    });
}
