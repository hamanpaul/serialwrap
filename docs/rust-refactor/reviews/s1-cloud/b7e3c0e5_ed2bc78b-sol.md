> 審查者：grok-4.7（代理 sol 單審位，擁有者 2026-11-02 指定）

我先核對前輪六項 finding 的現況，並只讀 `b7e3c0e5..ed2bc78b` 的修正範圍，不改任何檔案。前輪六項的相關檔在這兩個 commit 之外還有後續修改。我改讀目前程式，確認是真的修好，還是問題仍在。工作樹的 HEAD 已不在指定的 `ed2bc78b`。我改以該 commit 的 git 物件核對，不改工作樹。前輪六項在 `ed2bc78b` 都已對上修法；本輪 `b7e3c0e5..ed2bc78b` 的兩個 commit 沒有新的 High／Medium。審查對象是 `ed2bc78b` 的 git 物件。審查途中工作樹 HEAD 被移到同父 commit `f8088276`，該 commit 不在本次範圍，未納入結論。

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `978aa1e1` | 0 | `_wait_dut_tx` 的 1 秒提前返回已不在。SC-18 先 `daemon stop`，再以 bridge barrier（佇列清空、pump 閒置、靜默後 `flush`）與 checkpoint 的長度／SHA-256 封住 TX 視窗；未 `settled` 或雜湊不符即斷言失敗。`test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 在 barrier 返回前寫入第二筆 `echo X\n`，要求窗口為 `b"echo X\necho X\n"`。 |
| 2 | Medium | 已修 `51bd8fa5` | 0 | ready 改由 `exec_or_wait` 拉起的孫行程寫入 `delegated grandchild pid=<pid>`。父行程 10 秒內等不到、或該 PID 已不在，就 `panic`，之後才送 Ctrl 事件，並要求事件後該 PID 已退出。子行程在建立孫行程前停住時，本測試不能以 `0xC000013A` 通過。 |
| 3 | Medium | 已修 `4e3ba13f` | 0 | `stage1_exit_driver.sh` 保留 `rc.txt` 原始碼，改交 `l15_exit_driver.py` 做第二層判定。只有 gate 為 `IMPLEMENTED`／`VERIFIED`、errors 為空、唯一非零步驟是 `win_l15_func_test 1`，且全部非 PASS 列都精確符合核准延後規則時才不擋出口。同檔回歸含未授權 `co-01` FAIL、跨 `run_id`、缺報告；這些仍算未解決步驟。 |
| 4 | High | 已修 `12b3a929` | 0 | 升級映像不再裝 `python3-yaml`，容器不再用 `--system-site-packages`。0.3.2 以 `pipx install --no-index --find-links=/opt/sw-old` 從離線 wheelhouse 安裝。`--release-bundle` 把 PyYAML 複製進隔離 runtime，並以 `python -I` 確認由該 runtime 載入。`test_upgrade_environment_does_not_hide_missing_pyyaml` 鎖住上述條件。 |
| 5 | Medium | 已修 `ac6c4e4e` | 0 | 正式配方是 `mkbundle.sh --release-bundle --profile release`。`--dev` 才加 `sw-cli/test-fakes`，且兩者不可並用。`test_release_profile_binary_excludes_test_hooks` 檢查 release 執行檔不含 `SERIALWRAP_TEST_` 與 `SW_TEST_UPDATE_*`；smoke 對含 `SW_TEST_UPDATE_PAUSE_AFTER` 的產物失敗。 |
| 6 | Low | 已修 `12b3a929` | 0 | 前置條件改為 Stage 1A overall 為 `VERIFIED`，或為 `IMPLEMENTED` 且只剩 OD-27～OD-32 的 `BLOCKED-OWNER`，並要求沒有 FAIL／INSUFFICIENT。`test_release_plan_accepts_only_real_stage1_gate_outcomes` 拒絕 `overall PASS` 這個字。 |

## B 新發現

NO FINDINGS

`8b16bc3b` 只把 Windows `help_root_col80` 的 `--version` 說明從「顯示版本後離開」改成「顯示版本與程式碼指紋後離開」，與同 commit 的 Linux golden 以及目前 `COLUMNS=80 python3 serialwrap.py --help` 逐字相同。父版 Windows golden 與 Linux golden 的唯一差異就是這句。`ed2bc78b` 讓 B 輪改跑 `test-fakes` 執行檔（對齊 Linux `mkbundle.sh --dev`），再以不含該 feature 的 release 覆寫標準路徑，並在腳本內拒絕 release 執行檔含 `SERIALWRAP_TEST_`／`SW_TEST_UPDATE_`、或 fakes 執行檔缺少 `SERIALWRAP_TEST_`。

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：可放行