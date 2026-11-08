# s1v13 第 3 輪修正與驗證

日期：2026-10-29
起點：`aff96ac`
程式修正 commit：`842c014`
範圍：處理 Codex sol 對 `d87f51f..aff96ac` 的增量審查 finding，執行指定驗證；未執行 Stage 1 出口證據 driver。

## 審查發現處置

- 前輪 finding A-1（前置空行後的 readline 折行殘留）：已由 `91ed98a` 修正。
- 前輪 finding A-2（合法五次 L1.5 重複案例證據被拒）：已由 `2fe7750` 修正。
- `S1-aff96ac-1` **[Medium]**（折行標記緊貼命令原有空白時，matcher 先消耗標記空白，命令回顯污染 stdout）：已修正 `842c014`。新增回歸涵蓋前置 `\n`／`\r\n`、第 79 字元前的原有空白與兩次折行；修正前兩種前綴都留下命令回顯，修正後精確回傳 `OUT`。matcher 先識別完整 `" \r"` 標記並驗證既有 20 字元距離，再比對命令字元，未改門檻或輸出判準。
- 本輪 High：0；未處理 Medium：0。新增修正待雲端 Claude Opus 增量複審。

## 驗證

- RED：新增案例在 `aff96ac` 實作下，`\n` 與 `\r\n` 兩種前綴皆失敗，輸出包含命令回顯。
- `python3 -m pytest -q tests/test_wrapped_echo_strip.py`：**17 passed、6 subtests passed**。
- `python3 -m pytest -q tests/`：**5302 passed、157 skipped、1 xfailed、1 failed、98 subtests passed、14 warnings**。唯一失敗：`tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`。此同一失敗已存在於起點 `aff96ac` 的上一輪全套結果（5301 passed、1 failed）。目前 `target/p4-evidence/summary.json` 綁定 `32f6dd1`；缺少該舊證據未涵蓋的四筆 `boot_done_regex` cargo 案例，P4 必要列也缺少受測 HEAD 證據。依本輪指示未跑出口 driver，因此保留 FAIL；不把證據不足改成 PASS／SKIP。
- `cargo test --workspace`：exit 0；**905 passed、0 failed、7 ignored**（81 個 test result 摘要）。
- `python3 -m tools.contracts check`：exit 0，**0 findings**。
- `python3 -m policy_check --repo .`：**25 pass、0 fail、2 warn**（R-19 workflow test gate 辨識警告、R-22 既有懸空文件引用）。
- `python3 tests/acceptance/regen_lock.py --check --base aff96ac`：通過；凍結鎖未修改。
- `git diff --check`：通過。

## SPEC-GAPS

無。

## 不成立主張

無；本輪未拒絕審查 finding。

## 既有碼待辦

- `CHG-S1X-AB-TEARDOWN-STOP-DIAG-01` 維持 OPEN：兩次同範圍 39/39 通過，尚未重現 stop 後程序存活，也沒有足以判定 rig 或 daemon 根因的新故障現場。
- Bridge `write_control_info` 重複 close 是既有 Low backlog，依審查收斂規則留待 Stage 3 收尾，不在本輪修正。

## REMAINING:

- 雲端 Claude Opus 增量複審：本輪 wrapped-echo 修正，以及既有 `CHG-S1X-L15-EXIT-RC-01`、`CHG-S1X-DAEMON-LOG-LINE-SYNC-01`；`CHG-S1X-OD29-DELEGATE-PROOF-01` 亦待審查。
- 最終 HEAD 的 P4、Stage 1A、Spike 3、Windows cargo 與 release bundle 升級證據待出口 driver 重新產生；本輪明確未執行 driver。P4 證據測試因此維持既有 FAIL。
- `PENDING-OD-S1X-UPGRADE-LEGACY-HANDOFF` 與 `PENDING-OD-S1X-UPGRADE-ROLLBACK-PIPX` 待擁有者裁決；Windows 升級仍為 `LOCAL-WINDOWS-TODO`。
- OD-27 真板與安裝機項目、OD-28 排程工作、OD-30 (2) 實體 transport disconnect、OD-31 Windows `ev-01`（Slice 2.3）、OD-32 Windows `sm-04-device-unplug` 依裁決維持 `BLOCKED-OWNER`／延後。OD-29 六支 cargo 測試維持已實證；新版 delegate proof 仍待擁有者 Windows Terminal 重跑。
- 0.4.0 release workflow、`install.sh` 定位、正式版號與 changelog 收斂，以及 PR／tag／release 均待出口 gate 通過後處理；未 push、merge、建 tag 或發 release。
