//! S1X 出口 G9b：回滾入口 symlink 啟動回歸（sol 72a887e，Low）。
//!
//! POSIX 上穩定入口 `bin/serialwrapd` 是指向 `versions/<v>/bin/serialwrapd` 的 symlink。`spawn_daemon` 必須以該
//! 穩定入口啟動回滾目標版本，不得改用 CLI 自身 bundle 的 launcher（回滾時 CLI 來自目前的新版）。PAL 的 `is_file`
//! 以 `symlink_metadata` 判定、不跟隨連結；若入口判定退回 `is_file`，symlink 入口會被視為不存在，改啟動新版 launcher，
//! `start_and_verify` 因而回報 pairing_mismatch。
//!
//! 既有的版本回報替身（`with_new_daemon_reporting_version`）只回覆 RPC 與 `--version`，無法證明 spawn 選到哪一個入口；
//! 本檔直接以 spawn 的 argv[0]、入口 symlink 的解析結果與入口位元組斷言。
//!
//! 平台範圍：本檔不寫平台謂詞（`scripts/rust/check_cfg_boundary.py` 同樣掃描 `tests/`，禁止 `#[cfg(unix)]` 與
//! `std::os::unix`）。POSIX 限定由 `TestEnv::posix()`（EntryStyle::Symlink 的 POSIX 形狀）與 PAL 測試替身
//! `real_fs().symlink_file`（平台分支僅存在於 sw-pal 內）表達。前提與 `mgmt_layout::posix_symlink_switch_atomic`
//! 相同：主機須能建立 symlink；無法建立時測試失敗，不以一般檔案替代而略過。
//!
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 12（SC-23 回滾）；
//! 產品碼位置：`crates/sw-cli/src/rollback.rs` 之 `spawn_daemon`。
mod support;

use support::*;

/// 把 `root.bin/<name>` 換成指向 `versions/<version>/bin/<name>` 的 symlink（POSIX 穩定入口形狀）。
///
/// `with_current` 寫入的是一般檔案，此處先移除再建立 symlink；建立失敗即 panic，不退為一般檔案。
fn point_entry_at(t: &TestEnv, name: &str, version: &str) {
    let root = t.root();
    let entry = root.bin.join(name);
    let target = root.versions.join(version).join("bin").join(name);
    std::fs::remove_file(&entry).expect("移除既有一般檔入口失敗");
    sw_pal::fakes::real_fs()
        .symlink_file(&entry, &target)
        .unwrap_or_else(|error| {
            panic!(
                "建立入口 symlink 失敗（主機須能建立 symlink）：{}",
                error.py_message
            )
        });
    let meta = std::fs::symlink_metadata(&entry).expect("入口 metadata 讀取失敗");
    assert!(meta.file_type().is_symlink(), "前置條件失敗：入口必須是 symlink");
}

#[test]
fn on_demand_rollback_spawns_through_symlink_entry() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        // CLI 自身的 bundle launcher（新版）：若 spawn 誤用它，argv[0] 會與穩定入口不同。
        .with_bundle("0.4.1")
        .with_supervisor_state("on-demand", false, false)
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.0");
    t.with_previous_backup_mode("on-demand");
    point_entry_at(&t, "serialwrap", "0.4.1");
    point_entry_at(&t, "serialwrapd", "0.4.1");

    let (rc, out, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(json(&out)["rollback"]["to_version"], "0.4.0", "{out}");
    assert_eq!(
        sw_cli::install_layout::read_current(&t.root())
            .unwrap()
            .unwrap()
            .bundle_version,
        "0.4.0"
    );

    let root = t.root();
    let entry = root.bin.join("serialwrapd");
    let spawns = t.process().spawns();
    assert_eq!(spawns.len(), 1, "on-demand rollback 應只啟動一次 daemon：{out}");
    assert_eq!(
        std::path::Path::new(&spawns[0].0[0]),
        entry.as_path(),
        "spawn 必須使用穩定入口 symlink，不得改用 CLI bundle launcher"
    );

    let resolved = std::fs::canonicalize(&entry).expect("穩定入口應可解析");
    let target_launcher = std::fs::canonicalize(
        root.versions.join("0.4.0").join("bin").join("serialwrapd"),
    )
    .expect("回滾目標 launcher 應存在");
    assert_eq!(resolved, target_launcher, "穩定入口切換後必須指向回滾目標版本");
    assert_eq!(
        std::fs::read(&spawns[0].0[0]).expect("spawn 入口應可讀取"),
        t.version_bytes("0.4.0", "bin/serialwrapd"),
        "spawn 所用入口的內容必須是回滾目標版本的 launcher"
    );
}
