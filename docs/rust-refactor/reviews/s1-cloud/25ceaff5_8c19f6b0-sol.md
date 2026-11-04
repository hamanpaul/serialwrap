> 審查者：grok-4.7（代理 sol 單審位，擁有者 2026-11-02 指定）

我以唯讀方式核對前輪 finding，並只審 `25ceaff5..8c19f6b0` 是否引入新缺陷。先看這段提交與前輪所指的程式位置。這段只有一筆非 merge commit `8c19f6b0`。我接著對前輪六項與這筆 diff 逐一讀碼。前輪六項的落點與這筆 `sw-cli` 依賴修正要分開核對。我先讀現行實作與相關裁決。## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | `_wait_dut_tx` 已不在。`test_autostart_l15_tx_once` 先 `daemon stop`（成功路徑要求 rc 0），再經 barrier `settled` 與 checkpoint 的長度／SHA-256 封住 wire 前綴。`test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 在 barrier 回傳前寫入第二筆 `echo X\n`，漏掉就失敗。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | ready 檔改由 `exec_or_wait` 拉起的孫行程寫入 `delegated grandchild pid=`。父行程須在 10 秒內讀到該檔、解析出仍存活的 PID，才送 Ctrl 事件。孫行程未建立時逾時 panic，不會得到 `0xC000013A` 的 PASS。 |
| 3 | Medium | 已修 4e3ba13f | 0 | `stage1_exit_driver.sh` 以 `l15_exit_driver.py` 對 rc.txt、gate 與兩份 Windows 報告做第二層判定。只有唯一非零步驟為 `win_l15_func_test 1`，且全部非 PASS 列都對上 OD-30／31／32 的 gate `BLOCKED-OWNER` 時才豁免。缺報告、harness 關閉失敗與其他失敗仍留下非零步驟。`tests/test_rust_stage1_exit_driver.py` 覆蓋這條路徑。 |
| 4 | High | 已修 12b3a929 | 0 | 升級 Dockerfile 未裝 `python3-yaml`，容器腳本沒有 `--system-site-packages`，0.3.2 以 `pipx --no-index --find-links=/opt/sw-old` 安裝。`--release-bundle` 把 PyYAML 複製進隔離 runtime，並以 `python -I` 確認由 bundle site 載入。`test_upgrade_environment_does_not_hide_missing_pyyaml` 鎖住這三項。 |
| 5 | Medium | 已修 12b3a929 | 0 | `mkbundle.sh` 只在 `--dev` 加上 `--features test-fakes`；`--release-bundle` 與 `--dev` 互斥。`SW_TEST_UPDATE_PAUSE_AFTER`／`SW_TEST_UPDATE_FORCE_VERIFY_FAIL` 包在 `feature = "test-fakes"`。發佈配方為 `mkbundle.sh --release-bundle --profile release`。`test_release_bundle_build_does_not_enable_test_fakes` 檢查該次 cargo 參數沒有 `sw-cli/test-fakes`。 |
| 6 | Low | 已修 12b3a929 | 0 | `release-0.4.0-plan.md` 第 1 項改為 Stage 1A overall 為 `VERIFIED`，或為 `IMPLEMENTED` 且只剩 OD-27～OD-32 允許的 `BLOCKED-OWNER`，並要求沒有 FAIL／INSUFFICIENT。`test_release_plan_accepts_only_real_stage1_gate_outcomes` 對齊這段文字。 |

本輪範圍 `25ceaff5..8c19f6b0` 只有一個非 merge commit `8c19f6b0`，沒有 merge commit。上表六項的關閉提交都在 `25ceaff5` 之前。

## B 新發現

1. **[Medium] [crates/sw-cli/tests/delegate_resolve.rs:202](crates/sw-cli/tests/delegate_resolve.rs:202) — 拿掉自我 dev-dependency 後，Stage 1B 凍結 selector 不再編譯兩支必要測試。**

   `8c19f6b0` 刪除 `sw-cli = { path = ".", features = ["test-fakes"] }`，並把 `run_delegate_increments_depth_and_executes_once_with_three_environment_values` 與 `delegate_mechanism_still_guards_recursion`（`mgmt_terminal.rs:99`）改成 `#[cfg(feature = "test-fakes")]`。這兩支仍在 `l1_required.lock.json` 的 `s1b_cargo` linux／windows。linux selector 是 `--workspace`（`stage1_exit_driver.sh:332` 與 `:349`），windows selector 是 `--package sw-pal --package sw-cli`。兩者都不帶 `--features test-fakes`。`cargo metadata` 對目前 resolve 顯示 `sw-cli` 的 features 為 `[]`（`sw-pal` 仍為 `test-fakes`）。

   輸入：`CARGO_TARGET_DIR=/tmp/solrev-s1-target cargo test -p sw-cli --test delegate_resolve --test mgmt_terminal -- --list`。預期：上述兩支出現在清單。實際：清單只有其餘 5＋4 支，這兩支不存在；`cargo test --workspace` 仍會以 exit 0 結束。`l1_judge` 對缺席的必要 nodeid 回 `INSUFFICIENT`（`missing`／`required case not satisfied`）。同一兩支在 `--features test-fakes` 下為 `ok`（各 1 passed）。

   **確切修法：**不要恢復 sw-cli 對自身的依賴邊。讓 s1b 正式收集在編譯 sw-cli 測試時打開 `test-fakes`：linux 的 `cargo test --workspace` 與 `cargo_l1_linux_1b` 的 `-- --workspace`，以及 windows `win-test.sh --l1-set s1b_cargo --package sw-pal --package sw-cli`，都加上 `--features test-fakes`，並把鎖內 `s1b_cargo` 兩個平台的 selector argv 改成與報告 argv 相同。修完用該 selector `--list` 確認這兩支都在，且與修前收集的差集只有這兩支、沒有新增失敗。若 workspace 根目錄的 `--features` 讓其他成員失敗，就只對 sw-cli 那次編譯打開 feature 再合併同一份 L1 報告。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正