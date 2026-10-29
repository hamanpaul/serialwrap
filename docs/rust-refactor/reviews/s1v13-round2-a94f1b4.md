# s1v13 第 2 輪修正與驗證

日期：2026-10-28

起點：`a94f1b4`；程式變更完成點：`b2b0bd0`
範圍：套用雲端 Codex sol 對 `9908f93..a94f1b4` 的發現，執行本輪既有測試與 gate；未執行 Stage 1 出口證據 driver。

## 審查發現處置

審查原文：`origin/docs/parity-amendments:docs/rust-refactor/reviews/s1-cloud/9908f93_a94f1b4-sol.md`。

1. **Medium：多次 readline 折行回顯污染 stdout — fixed `3a46b25`.** `_extract_command_stdout` 原本只移除第一個 `" \r"` 折行標記；兩次以上折行會讓命令回顯殘留並污染 stdout。新增兩次及三次折行的精確輸出回歸，先確認修正前失敗，再逐一移除開頭命令區域內的折行標記。同步修正這次插入程式碼造成的五筆 logger source 行號位移。`tests/test_wrapped_echo_strip.py` 13 passed；logger 與 wrapped-echo 定點集合 15 passed。
2. **High：合法延後列掩蓋 Windows L1.5 其他失敗 — fixed `4e3ba13`.** 先新增「OD-31 `ev-01` 延後列與未授權 `co-01` FAIL 同時出現」及「報告跨 run_id」的拒絕測試，確認舊 helper 會錯誤豁免，再要求 exit driver 驗證 scenarios、basic 原始報告、runner metadata、COM20:COM21 argv、exit status、同一 run_id 與每列 gate `BLOCKED-OWNER` 對應。只有所有非 PASS 列都屬明確授權延後項時才可豁免 producer exit 1；其他失敗仍保留。

完整 pytest 另抓到兩個問題，均已處理：

- **Logger source metadata 暫時失敗：fixed `3a46b25`.** 多次折行修正新增四行，導致後段五筆 `contracts/daemon_logging.yaml` source 行號過期；依 AST 呼叫位置同步行號，定點兩個 logger 契約測試通過。未改 logger 行為或契約分母。
- **release drain 測試的同步競態：fixed `b2b0bd0`.** 全套執行時測試在恰等於 3 秒 drain 上限的 thread join 後讀取尚未寫入的 `out["ok"]`。只將測試等待增為 drain 上限加 2 秒收尾餘裕，並明確檢查兩個 thread 已結束、結果為成功、狀態為 `RELEASED`。產品 drain 門檻及狀態判定未變；定點 `tests/test_stage0_session_drain.py` 28 passed。

沒有拒絕的審查發現。

## 驗證結果

- `python3 -m pytest -q tests/` 最終一次：**5294 passed、157 skipped、1 xfailed、1 failed**，14 warnings、92 subtests passed，耗時 2372.65 秒。唯一失敗：`tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`。報告列出的 P4 記錄仍綁在舊 commit `32f6dd1`，與目前受測 HEAD 不符；此證據過期失敗在本輪起點 `a94f1b4` 已存在（前輪記錄亦見 `s1v12-sol-findings-9908f93.md`）。本輪按指示未跑出口 driver，因此不把它記為 PASS。
- 全套中先後出現的 logger source 行號與 drain thread 同步失敗已修正；最終全套不再出現這兩項。
- `python3 -m pytest -q tests/test_docker_l15.py::test_docker_smoke_two_container`：**1 passed in 63.87s**。
- `tests/contract_blackbox/test_bb_daemon_e2e.py` 同範圍連跑兩次：**39 passed、39 passed**，未重現 stop 後 daemon 存活；這不足以關閉高負載 stop 根因調查，詳見 addenda `CHG-S1X-AB-TEARDOWN-STOP-DIAG-01`。
- 受影響集合：exit-driver／wrapped-echo／WAL AV sim 為 **98 passed、2 skipped**；logger／wrapped-echo 定點集合 **15 passed**；session-drain **28 passed**。
- `cargo test --workspace`：exit 0，81 個 test binary，**905 passed、0 failed、7 ignored**。
- `python3 -m tools.contracts check`：exit 0，**0 findings**。
- `python3 -m policy_check --repo .`：**25 pass、0 fail、2 warn**（既有 R-19、R-22）。
- `python3 tests/acceptance/regen_lock.py --check --base a94f1b4`：通過，鎖結構、案例屬性快照、統計與基準比對一致。
- `git diff --check`：通過。

## 本輪未執行與待辦

- 最終 HEAD 的 Stage 1 出口證據仍待 driver 重產，包括 P4／Stage 1A／Spike 3／Windows cargo 與 release bundle 升級證據；因此本輪 P4 證據失敗維持未解。本輪沒有執行出口 driver。
- Windows 原生 bridge、Windows cargo 收集與 COM20:COM21 原生驗證本輪未執行；此 WSL 環境沒有 `cmd.exe`。ST-12 實體 transport disconnect 依 OD-30 (2) 留待 release 後於真實 USB 序列硬體安裝機驗證。
- 新增及更新的 oracle／gate 變更仍待雲端 Claude Opus 複審，包括 `CHG-S1X-WRAPPED-ECHO-SPACE-01`、`CHG-S1X-L15-EXIT-RC-01`、`CHG-S1X-DAEMON-LOG-LINE-SYNC-01`。本輪未更動 bridge 工具或 `APPROVED_REVISION.json`。
- `CHG-S1X-AB-TEARDOWN-STOP-DIAG-01` 維持 OPEN：兩次 39 案通過且未重現，但尚無故障現場 stoplog 可判定 rig 預算或 daemon 關機路徑根因。
- 待擁有者裁決：`PENDING-OD-S1X-UPGRADE-LEGACY-HANDOFF`、`PENDING-OD-S1X-UPGRADE-ROLLBACK-PIPX`。
- OD-27 真板項目（f1–f11、`pt-quiet-ready`、realhw p0／p1 含 `p1-rst-daemon`、`rm-live-e2e` 及既有正式安裝機升級）維持 `BLOCKED-OWNER`。OD-28 排程工作不在本輪執行；logon-trigger 待擁有者登出／登入，single-instance、restart-on-failure、72h 項目待 P8。OD-31 Windows `ev-01-spawn-handler` 列待 slice 2.3；OD-32 Windows `sm-04-device-unplug` 列依 OD-27 待 release 後實機；OD-29 新版 delegate proof 的 Windows Terminal 重跑仍待擁有者。已完成的 OD-29 六支 cargo 擁有者測試維持既有實證且不回填凍結鎖。
- 升級驗證 Linux Docker 準備與測試已在前輪完成，但需以最終 release bundle 重跑留存出口證據；Windows 升級仍為 `LOCAL-WINDOWS-TODO`。VERSION／正式 changelog 收斂、release workflow 與 PR／tag／release 依 OD-33 留待出口 gate 通過後處理。
- 未 push、未 merge、未建立 tag 或 release。
