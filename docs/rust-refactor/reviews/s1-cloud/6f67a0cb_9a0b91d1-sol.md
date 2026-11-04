> 審查者：grok-4.7（代理 sol 單審位，擁有者 2026-11-02 指定）

我以唯讀方式核對前輪 6 項 finding，並只審 `6f67a0cb..9a0b91d1` 是否引入新缺陷。先看這段歷史與對應檔案。這段只有文件 commit `9a0b91d1`。我接著用目前程式核對前輪 6 項是否仍能重現。## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `978aa1e1` | 0 | `_wait_dut_tx` 已改為停掉本案 daemon 後的 barrier＋checkpoint。barrier 等佇列清空後 `_flush_writer()`（FIFO，flush 前的紀錄先落盤再 fsync），checkpoint 再 flush，並以完整行前綴的長度與 SHA-256 封口。`test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 在 barrier 回傳前寫入第二筆 `echo X\n`，要求比對結果為兩筆。 |
| 2 | Medium | 已修 `51bd8fa5` | 0 | ready 只由 `exec_or_wait` 拉起的孫行程寫入，內容含其 pid。父行程等到該檔且 `process_is_running` 為真才送 Ctrl 事件；10 秒內沒有標記即 panic。固定 `sleep(2)` 已不在。 |
| 3 | Medium | 已修 `ddcbdde0` | 0 | `stage1_exit_driver.sh` 仍把各步原始 rc 寫入 `rc.txt`，結束前把 gate 與兩份 Windows 報告交給 `l15_exit_driver.py`。只有唯一非零步是 `win_l15_func_test 1`、gate 為 `IMPLEMENTED`／`VERIFIED` 且 `errors` 為空、非 PASS 列都精確符合 OD-30／31／32 時，`failed_steps` 才歸零。缺報告、rc 不是 1、`harness_stop_failed`、其他失敗維持阻擋。回歸在 `tests/test_rust_stage1_exit_driver.py`，直接呼叫 `unresolved_steps` 與 driver 參數契約。 |
| 4 | High | 已修 `12b3a929` | 0 | `upgrade.Dockerfile` 的套件清單沒有 `python3-yaml`。0.3.2 以 `pipx install --no-index --find-links=/opt/sw-old` 安裝，wheelhouse 另下載 `PyYAML>=6`。bundle runtime 用 `python3 -m venv --copies`，把 PyYAML 複製進該 venv，並以 `python -I` 確認載入路徑在 bundle site。升級 runner 要求 `pyvenv.cfg` 含 `include-system-site-packages = false`，否則 rc=3。 |
| 5 | Medium | 已修 `12b3a929` | 0 | 正式配方是 `mkbundle.sh --release-bundle --profile release`。該模式不傳 `--features test-fakes`，且 `test-fakes` 不是 default feature。`SW_TEST_UPDATE_PAUSE_AFTER` 與 `SW_TEST_UPDATE_FORCE_VERIFY_FAIL` 只在 `feature = "test-fakes"` 下編譯；未啟用時 `test_pause_after` 是空函式。 |
| 6 | Low | 已修 `12b3a929` | 0 | `release-0.4.0-plan.md` 前置條件改為 Stage 1A overall 為 `VERIFIED`，或為 `IMPLEMENTED` 且只剩 OD-27～OD-32 允許的 `BLOCKED-OWNER`、沒有 FAIL／INSUFFICIENT。 |

前輪六項都是該輪首次列出。本輪對照目前樹，上述失敗情境已不成立。鎖定測試實跑 `51 passed`（checkpoint、Ctrl ready 原始碼守衛、exit driver、升級映像靜態檢查、release bundle 不帶 `test-fakes`、workflow 建置命令）。未重跑完整 Docker 升級或 Windows 原生 Ctrl 案例。

## B 新發現

NO FINDINGS。

`6f67a0cb..9a0b91d1` 只有 `9a0b91d1`，沒有 merge。該 commit 只新增 `docs/rust-refactor/reviews/s1mx6b-user-unit-paths-r2.md`，產品、測試、契約、凍結鎖與判準都沒有改。報告裡 A-1～A-5 的「已修」與目前程式一致。

## 既有碼待辦（不計入結論）

`9a0b91d1` 記錄的 S1-6f67a0c-1 仍在，且不在本輪 diff 內。`systemd_user_unit_path`（`crates/sw-cli/src/setup_cmd.rs`）把 user unit 放在已解析 HOME 的 `.config/systemd/user/serialwrap.service`。`SystemdCtl::hold_path`（`crates/sw-pal/src/service_ctl/systemd.rs`）在 `XDG_CONFIG_HOME` 非空時，把 drop-in 寫到該目錄下的 `systemd/user/<name>.service.d/90-serialwrap-update-hold.conf`。`stop_supervisor:after_hold`（`rollback.rs` 的 `crash_if`）發生在把實際 hold 寫回 journal 之前。因此 HOME=`/home/swtest`、`XDG_CONFIG_HOME=/tmp/xdg-config` 時，中斷恢復若只跟 journal 裡的 HOME 計畫路徑，會留著 XDG 目錄上的 `Restart=no`／`RefuseManualStart=yes`。非 strict 的 FakeServiceCtl 回 `dropin_path: None`，`merge_hold_plan` 會補回計畫路徑，這條分歧不會被該假控制器測到。

## 對裁決的異議（不計入結論）

無。

結論：可放行