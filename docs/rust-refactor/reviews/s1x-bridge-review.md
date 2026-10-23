# S1X uart_stdio_bridge 工具 revision 審查（PENDING-OD-S1X-BRIDGE-REVISION）

- 審查者：Claude Sonnet max（非實作者；codex gpt-6.1-sol 雙審待額度恢復後補審）
- 日期：2026-10-22
- 審查對象：工具樹 `c4c22c0e369e4c593e3b4a6a9a0ebb21c37259cc63b0394d295948568e60b030`（`uart_stdio_bridge.identity.tool_tree_sha256()`）
- 核准前之舊值：`9eaed6de5abd3d75d18929b6fdb3bdac7e1929fe32f666c3193c9983005ecd1c`（commit 2960e86）
- 以下 BRIDGE 審查段落逐字收錄自審查報告。

## 5. BRIDGE 審查（PENDING-OD-S1X-BRIDGE-REVISION）

**BRIDGE VERDICT: APPROVE** 對象為 tool tree `c4c22c0e369e4c593e3b4a6a9a0ebb21c37259cc63b0394d295948568e60b030`。

**理由**
1. **差異範圍乾淨。** 我重算 2960e86 的 tree 得 `9eaed6de…`，與核准值相符。到 HEAD 之間只有 `win_serial.py` 與 `selftest/checks.py` 兩個被雜湊的檔案不同（ca0106f），`tools/uart_stdio_bridge` 無未提交變更。
2. **`win_serial.py` 的併發重構正確。**
   - 它實作 spec §15.4.3「每個方向獨立讀寫工作」，並解掉已實測的 ST-03(a) 雙向死鎖。
   - 鎖序固定為 `_lock → _read_io_lock → _write_io_lock`（`:239-249`）。I/O 路徑在持有串行鎖時不取 `_lock`，所以不會死鎖。
   - handle 在串行鎖內重驗（`:79`、`:120`），關閉要等進行中的 I/O 結束，所以不會對已關閉的 handle 讀寫。
   - 我寫了拋棄式壓力腳本（只放在 scratchpad）：reader、writer 與 control 三條執行緒跑 8 秒，含 171 次 disconnect 與 reconnect。結果 0 deadlock、0 非預期例外、0 use-after-close 或關閉時仍有 I/O。
3. **未決疑慮 (1)：timeout 的 setter 在 I/O 進行中變更，風險可忽略。**
   - bridge 的 reader 固定用 0.05 s（`pumps.py:31`），writer 固定用 5.0 s（`core.py:34`）。唯一會在另一方向 I/O 進行時執行的 setter，是每個 handle 第一次寫入時的一次 `write_timeout=5.0`。我用 fake 實測只有 1 次，且沒有同方向競態。
   - pySerial 3.5 的 `_reconfigure_port` 只做 `SetCommTimeouts`、`SetCommMask`、`SetCommState`，沒有 purge 或 abort，並設 `fAbortOnError=0`。
   - 最終 tree 的 Windows 實測為 PASS，作者要求的「Windows 實測」已滿足。不阻擋。
4. **未決疑慮 (2)：selftest 測試端寫入無 `write_timeout`，只會卡住、不會變 PASS。** 位置是 `checks.py:395,493,824,901,964,1179,1506`（ST-01/02/04/05/06/08/11）。bridge 出錯時這些檢查會卡住、不產生報告，gate 維持 INSUFFICIENT。不阻擋。
5. **selftest 沒有被放寬。**
   - ST-03 保留位元組相等、短讀筆數大於 1、`dut_stdin_write` 總和三項斷言，只新增有界期限與 4 MiB 驅動佇列，未讀取逾時以 FAIL 呈現。
   - 要注意 Windows 的期限是 120 s，POSIX 是 30 s。
   - ST-07 的包裝只影響 win-serial。
   - ST-10 加 `write_timeout=1.0`，例外由外層 `except Exception: pass` 吞掉，與 POSIX 分支同語意。
6. **報告屬實。**
   - 兩份都在 `c4c22c0e`、`complete=true`、`test_overrides={}`。sha256 與 addenda 所載一致：Linux `fba1ec09…`，Windows `e2566f15…`。
   - Linux 14 項全 PASS。Windows 13 項 PASS，ST-12 為 NOT_RUN（`cap_missing:transport_disconnect`），`is_qualified` 本就允許。
   - 我模擬 `is_qualified`：核准值改為現 tree 後，兩份報告都為 True。負向對照（ST-12 改 FAIL、ST-03 改 NOT_RUN）都為 False。
7. **測試結果。**
   - `tools/uart_stdio_bridge/tests`（排除 docker 測試）247 passed、2 skipped（windows_com 能力）、1 deselected。
   - 唯一紅燈是預期的 `test_approved_revision_matches_tool_tree`。

**核准後的套用程序**
- 以獨立 commit `chore(bridge): 核准工具 revision c4c22c0e369e` 更新 `tools/uart_stdio_bridge/APPROVED_REVISION.json`：
  - `tree_sha256` 填 `c4c22c0e369e4c593e3b4a6a9a0ebb21c37259cc63b0394d295948568e60b030`。
  - `reviewer` 與 `review_ref` 填本審查，並註明單審（Codex 雙審待額度恢復補審）。
  - `date` 填 2026-10-22。
- `src/**`、`pyproject.toml`、`docker/Dockerfile.dut` 在此之前不得再改，否則 tree 變動、需重審並重跑兩平台 selftest。
- 套用後依序重跑：
  1. `tools/uart_stdio_bridge/tests/test_selftest.py::test_approved_revision_matches_tool_tree`。
  2. `tests/test_docker_l15.py::test_docker_smoke_two_container`。
  3. windows-com qualification。
  4. Linux func-test 的 17 列 `gate_eligible`。

**非阻擋的後續項目（下一次修改工具時）**
- B-1：為上述 7 個 `serial.Serial(...)` 補 `write_timeout`。
- B-2：把 timeout 的 setter 移進各自方向的 I/O 鎖內，或在 open 時固定。
- B-3：下一輪 Windows selftest 帶 `--com-driver-name`、`--com-driver-version`、`--com-driver-sha256`、`--com-driver-install-method`。目前報告的 `platform.evidence_insufficient=["com_driver"]`，驅動身分為 unknown，`is_qualified` 不檢查，但屬溯源缺口。
- B-4：歸檔 Windows selftest 的原始 log。

BRIDGE VERDICT: APPROVE（tree c4c22c0e369e4c593e3b4a6a9a0ebb21c37259cc63b0394d295948568e60b030）

## 2026-10-23 延後紀錄（Stage 1 出口修正輪）

- 本段為記錄，不是新的審查結論，不改變上方 `BRIDGE VERDICT`（該結論僅適用 tree `c4c22c0e…`）。
- 目前工具 tree（`tool_tree_sha256()`）為 `8643bd00…`，與核准值 `c4c22c0e…` 不同；`APPROVED_REVISION.json` 本輪未變更，新 tree 尚未核准。
- **B-1、B-2 本輪延後，未實作。** 理由：工具 src 已因 `endpoints/win_serial.py` 的 `peer_state` 宣告修正而變動，需重審與兩平台 selftest；於此時併入未經實機驗證的 `write_timeout` 與 I/O 鎖內 setter 變更，風險高於收益。B-1、B-2 併入下一次工具修改，與 B-3、B-4 一併處理。
- `bridge/core.py` 的註解變更保留（描述 `peer_state` 宣告條件，與 `win_serial.py` 修正同一判準）。
- 登記對應：`owner-decisions-addenda.md` 之 `CHG-S1X-BRIDGE-PEER-STATE-01`。
