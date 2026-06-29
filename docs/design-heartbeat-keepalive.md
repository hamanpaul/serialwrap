# Design: Long-running Command Heartbeat / Keepalive

> 📌 **歷史快照**：#24 heartbeat/keepalive 已交付（功能仍在用）。本檔為當時的設計記錄；現行對外契約（含 `expected_duration_s`、output-based keepalive、`foreground_busy` 等）以 `README.md` 與 `docs/serialwrap-spec.md` 為準，本檔僅留作歷史。

**Issue**: #24  
**Status**: 已實作（Phase 1 於 `bugfix/open-issues-phase1`，Phase 2 於 `feat/open-issues-phase2`）  

## 背景問題

長時間命令（如 `apt upgrade`、`python -m unittest`）執行期間不會產生 prompt 輸出。broker 的 prompt probe 會因超時而將 session 從 READY → ATTACHED + PROMPT_TIMEOUT，即使命令仍在正常執行。

## 解決方案

### 1. Foreground Command Awareness（Phase 1 已實作）

當 `command.submit` 送出前景命令時，broker 會追蹤該命令正在執行，並在 `foreground_busy == True` 期間**暫停 prompt timeout**。

```python
# session_manager.py execute_command:
session.foreground_busy = True   # 標記前景命令進行中
session.fg_cmd_started_at = now_iso()
session.fg_cmd_timeout_s = timeout_s
```

prompt health probe 會跳過 `foreground_busy == True` 且 `elapsed < fg_cmd_timeout_s` 的 session。

### 2. Expected Duration Hint（Phase 2 已實作）

`command.submit` 支援選填的 `expected_duration_s` 參數：

```json
{"tool":"serialwrap_submit_command","params":{
  "selector":"COM0",
  "cmd":"python3 -m unittest discover",
  "timeout_s":120,
  "expected_duration_s":60
}}
```

當提供 `expected_duration_s` 時：
- prompt timeout 在該期間內暫停
- 過期後恢復正常 prompt 探測

### 3. Output-based Keepalive Detection（Phase 2 已實作）

命令執行期間監控 UART RX 活動。若收到任何 bytes（即使不是 prompt，例如測試進度輸出），重置靜默計時器，延長等待：

```python
# _wait_for_prompt keepalive 迴圈:
while elapsed < timeout_s:
    if bridge.rx_snapshot_len() > last_rx_len:
        last_rx_len = bridge.rx_snapshot_len()
        silence_start = time.monotonic()  # 重置靜默計時器
    if time.monotonic() - silence_start > silence_timeout:
        break  # 真正靜默，結束等待
```

### 4. 靜默類型區分

| 條件 | 意義 | 動作 |
|------|------|------|
| foreground_busy + rx 有輸出 | 命令正在執行且有輸出 | 繼續等待 |
| foreground_busy + rx 靜默 + elapsed < expected | 命令靜默執行中 | 繼續等待 |
| foreground_busy + rx 靜默 + elapsed > expected | 可能卡住 | 發出警告 |
| 非 foreground_busy + rx 靜默 | session 可能已失聯 | 執行 probe |

## 實作階段

1. **Phase 1**（已完成）：`foreground_busy == True` 時跳過 prompt timeout
2. **Phase 2**（已完成）：新增 `expected_duration_s` 參數與 output-based keepalive 迴圈

## 已知限制

- 仍需硬上限（如 10x expected_duration 或 30 分鐘）作為安全網，避免無限等待
- Phase 1 單獨使用時，若命令 crash 可能會遮蔽真正的 session 失聯
