# S1M-x6w 第 1 輪：Windows 原生 Cargo L1 夾具修正

分支 `feature/s1m-x6w`，起點 `0a7b4f70bbdc1cc277ce830cb2fd8c5058278066`。修正提交 `0f07bda433a7423e96b910f8a5d752d024df95a0`（`test(sw-cli): 修正 Windows passwd 對照夾具隔離`）。

## 根因與修正

三支案例在進入 Rust CLI 前，先由 `tests/support/mod.rs::assert_passwd_home_sandboxed` panic：`passwd 家目錄不得指向測試行程的真實家目錄`。Windows runner 的原生 TEMP 位於 `%USERPROFILE%/AppData/Local/Temp/sw-rust-test/<run>/tmp`，原先與隔離 HOME 並列的 passwd 夾具也落在 USERPROFILE 下，因此被既有防線正確拒絕。本輪根因是夾具位置，並非已證實的 POSIX 路徑分隔符或 CLI 路徑組裝缺陷。

runner 現在另提供 `<worktree>/target/win-passwd-fixtures/<run>`，經 WSLENV 的 `/p` 傳給 Windows 原生測試。三支案例以 `passwd_home_fixture()` 持有獨立 TempDir，其 passwd 對照檔隨案例結束回收；HOME、config、state、install 等仍在 Windows 原生 TEMP，維持檔案鎖與 symlink 語意。隔離斷言改為驗證 passwd 路徑屬於該持有中的 TempDir，HOME 與 passwd 不同的斷言、所有 unit／hold／快照／還原內容斷言均保留。

`assert_passwd_home_sandboxed` 與起點逐位元組相同，既有拒絕真實家目錄的 `s1mx6e_passwd_home_refuses_process_home` 原生實跑通過。沒有使用平台 cfg、ignore、移除節點、放寬 judge 或覆寫舊 FAIL 報告。Python daemon 與 Rust 產品碼未改；本修正可由原生測試及隔離檔案覆蓋，不需實體 UART regression case。

## High／Medium 與指定案例

本輪未收到額外的上一輪 A／B finding 清單；以下逐項處理 brief 指定案例，未核銷未提供的審查項目。

| 節點（均以 `cargo:sw-cli::` 為前綴） | 起點實測 | 處理 |
|---|---|---|
| `mgmt_supervision_switch::s1mx6_supervision_home_precedes_passwd` | 家目錄防線 panic；FAIL | fixed `0f07bda4`，保留 HOME unit、passwd 無 unit 與真實 unit 快照斷言。 |
| `mgmt_supervision_switch::s1mx6_unit_hold_ignores_xdg` | 同上；FAIL | fixed `0f07bda4`，保留有／無 XDG、hold 計畫與實際路徑、Restart 與復原無殘留斷言。 |
| `mgmt_update::s1mx6_unit_snapshot_restore_ignores_xdg` | 同上；FAIL | fixed `0f07bda4`，保留 HOME 快照與失敗還原、XDG／passwd sentinel 未變、hold 清除斷言。 |
| `run_process::s1m241_version_fingerprint_falls_back_without_manifest_or_field` | 單獨原生測試及完整起點 L1 均 PASS | 起點即通過，本輪未修改。 |
| `mgmt_setup::s1m241_assessment_is_omitted_when_manifest_is_missing` | 單獨原生測試及完整起點 L1 均 PASS | 起點即通過，本輪未修改。 |

兩支指紋測試是在未修改的 `0a7b4f70` 實跑；`embedded_code_fingerprint.bin` 與起點相同，為 18 bytes 佔位檔。並行 mkbundle 是否造成先前失敗的因果，本輪沒有重現或獨立證明；不把該推測當作已驗證根因。

## 先紅後綠與凍結鎖

- 修前以 `win-test.sh` 相同的 Windows GNU target 與 `win-runner.sh`，用 libtest `--nocapture` 取得三支真實 panic；`windows-red.log`、`windows-update-red.log`。紅燈發生於原有案例，未加入替代的弱斷言。
- 完整起點 run `261105-154240-1992660`：866 passed、3 failed、8 skipped，rc 1、`overall=fail`、`l1_verdict=FAIL`；失敗恰為表中三支。provenance 起訖都是 `0a7b4f70`、`clean=true`。
- 修後原生聚焦 `s1mx6`：7 passed、0 failed、0 ignored，含上述三支、拒絕真實家目錄與既有真實控制器 hold 回歸；`windows-fixed-focused.log`。
- 起點 `s1b_cargo.windows` 實際為 736 個必要節點，未包含表中五支。依原生 collector 的實際 collected，僅以 `regen_lock.py --l1-add-only` 補入這五支至 741；移除 0，其他集合與所有 selector 不變。`lock-add.log`、`lock-collect.json`、`change-proof.json` 留存證明。

## 驗收結果

Linux 測試均以前景 `bash target/s1mx6w/sbox` 執行並等待結束。bwrap 將真實根目錄與家目錄唯讀掛載，只綁本 worktree 可寫；私有 PID、dev、proc、tmp、run，遮蔽 Windows mount 並補回唯讀 DNS resolver。PATH 排除 pipx 入口。未操作 production daemon、真實 UART、其他 worktree 或 Docker。

| 檢查 | 結果 |
|---|---|
| `cargo test -p sw-cli --features test-fakes` | 698 passed、0 failed、3 ignored；rc 0。 |
| `cargo test --workspace` | 1041 passed、0 failed、7 ignored；rc 0。 |
| `cargo test --workspace --features test-fakes` | 1041 passed、0 failed、7 ignored；rc 0。 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | rc 0；此列僅為交叉編譯證據。 |
| `python3 scripts/rust/check_cfg_boundary.py` | rc 0。 |
| `python3 -m tools.contracts check` | `ok=true`、`findings=[]`；rc 0。 |
| `python3 tests/acceptance/regen_lock.py --check --base 0a7b4f70` | 案例鎖、屬性、統計與 L1 結構／基準比對皆 OK；rc 0。 |
| `python3 -m policy_check --repo .` | 25 pass、0 fail、2 warn（R-19、R-22）、0 exempt；rc 0。 |
| 指定完整 Windows 原生 L1 | 869 passed、0 failed、8 skipped；rc 0、`overall=pass`、`l1_verdict=PASS`。 |

原生完整命令：

```bash
export PATH="/mnt/c/Windows/System32/WindowsPowerShell/v1.0:/mnt/c/Windows/System32:$PATH"
scripts/rust/win-test.sh --l1-set s1b_cargo --package sw-pal --package sw-cli
```

最終原生 run `261105-161953-2956899`，正式報告為 `target/win-test-report/261105-161953-2956899.json`，逐案結果為同目錄的 `l1-results-261105-161953-2956899.json`。provenance 起訖均為修正提交 `0f07bda4`、`clean=true`，`finished=true`、`exit_code=0`；741 個必要節點全部符合，表中五支全部 `passed`。修前與修後收集集合完全相同（877 個）、deselected 均為 0，保留原有 8 個 skipped。報告的 `evidence_insufficient` 仍保留 `interactive_desktop`、`supervisor_runtime`、`windows_com`，未視為硬體、監管實跑或互動桌面通過。

本輪未修改 Python 檔案，因此無觸及的 Python 測試。遵循 brief 的明確禁令，未跑全套 pytest、slice gate、Docker 或出口證據 driver。Windows COM、實體 UART 與擁有者項目沒有新增實證，維持原有 BLOCKED-OWNER／證據不足狀態，不計為 PASS。

本輪原始證據均位於 `<worktree>/target/s1mx6w/`，原生 JSON 位於 `target/win-test-report/`。Linux 各 suite 的 PID namespace 均已結束；本項 Windows 案例沒有啟動 serialwrap daemon。修正已 commit；push、PR、merge 與部署由編排者接手。

## 不成立主張

無。

## SPEC-GAPS

無本項待裁決項。凍結鎖與 brief 描述的落差已以限定五支的 add-only 補齊；本輪沒有處理其他分支的 finding。

## 既有碼待辦

既有 `tests/support/mod.rs` 的 unused-import 警告屬 Low，本輪不修；沒有新增範圍外修正。

## REMAINING:

REMAINING: none
