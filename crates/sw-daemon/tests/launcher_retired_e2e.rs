//! P9-T16 審查 F3：竄改的退役 manifest（退役標記＋daemon implementation=python）經真實 `serialwrapd` 二進位的端到端行為。
//!
//! 預期：`parse_manifest` 先以 `RetirementInconsistent` 拒絕，launcher 不組出任何 Python 子行程，
//! stderr 恰一行且以 `serialwrapd: ` 起頭、exit 2、stdout 為空。
//! 夾具的 `components.daemon.entry` 指向不存在的 Python 入口；若守門失效，錯誤會變成啟動失敗而非退役標記不一致，
//! 因此同時斷言訊息含「退役標記不一致」以鎖定守門位置。
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const DAEMON_BIN: &str = env!("CARGO_BIN_EXE_serialwrapd");
const RETIRED_PYTHON_IMPL: &str = include_str!("fixtures/stage3_retired_but_python_impl.json");

/// 暫存目錄（Drop 時刪除）；不新增 dev-dependency。
struct TempTree(PathBuf);

impl TempTree {
    fn new(tag: &str) -> TempTree {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir = std::env::temp_dir().join(format!(
            "swd-retired-e2e-{tag}-{}-{nanos}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).expect("建立暫存目錄");
        TempTree(dir)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempTree {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// 與 `tests/launcher.rs::stage_bundle` 相同的佈局：`<root>/bin/serialwrapd` 與 `<root>/release-manifest.json`。
fn stage_bundle(root: &Path, manifest: &str) -> PathBuf {
    let bin_dir = root.join("bin");
    std::fs::create_dir_all(&bin_dir).expect("建立 bin 目錄");
    let exe = bin_dir.join(format!("serialwrapd{}", std::env::consts::EXE_SUFFIX));
    std::fs::copy(DAEMON_BIN, &exe).expect("複製 serialwrapd 至暫存 bundle");
    std::fs::write(root.join("release-manifest.json"), manifest).expect("寫入 manifest");
    exe
}

/// Windows 上剛複製的可執行檔可能短暫被占用，沿用 launcher 測試的重試策略。
fn retry_busy<T>(mut operation: impl FnMut() -> io::Result<T>) -> io::Result<T> {
    for attempt in 0..=200 {
        match operation() {
            Err(error) if error.kind() == io::ErrorKind::ExecutableFileBusy && attempt < 200 => {
                std::thread::sleep(Duration::from_millis(10));
            }
            result => return result,
        }
    }
    unreachable!("重試迴圈必定回傳結果")
}

#[test]
fn tampered_retired_python_manifest_is_refused_before_any_child() {
    let root = TempTree::new("python-impl");
    let exe = stage_bundle(root.path(), RETIRED_PYTHON_IMPL);
    let out =
        retry_busy(|| Command::new(&exe).stdin(Stdio::null()).output()).expect("執行 launcher");
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();

    assert_eq!(
        out.status.code(),
        Some(2),
        "exit code 必須為 2；stderr={stderr:?}"
    );
    let lines: Vec<&str> = stderr.lines().collect();
    assert_eq!(lines.len(), 1, "stderr 必須恰一行：{stderr:?}");
    assert!(
        lines[0].starts_with("serialwrapd: "),
        "stderr 須以 `serialwrapd: ` 起頭：{stderr:?}"
    );
    assert!(
        lines[0].contains("退役標記不一致"),
        "拒絕必須來自退役守門（而非啟動失敗）：{stderr:?}"
    );
    assert!(
        out.stdout.is_empty(),
        "launcher 失敗不得在 stdout 印出任何內容：{:?}",
        String::from_utf8_lossy(&out.stdout)
    );
}
