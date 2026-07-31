---
status: accepted
work_item: issue-161-echo-ack-push
target_branch: feature/161-echo-ack-push
issue: 161
---

# #161 file.push echo-ACK 節流實作計畫

**Goal:** 讓 `file.push` 在無流控（`flow_control: none`）真機 console 上不再節流掉字——把 chunk 命令行拆成短 slice，逐 slice 送出後等板端 echo 回讀確認再送下一段（echo 即天然的應用層流控），最後才送換行讓命令執行。#157 的 chunk_size／chunk_timeout_s 參數面完整保留。

**Architecture:** 節流發生在**單行內部**（chunk 仍是 `base64 -d >>` 命令單位、`DEFAULT_CHUNK_SIZE=512` 不動）。`UARTBridge` 新增 `send_command_echo_paced()` 原語（逐 slice send＋`_await_echo_progress()` 比對回顯，全段確認後才送 `\n`）＋`cancel_input_line()`（Ctrl-U 恢復半行）；`file_transfer.push_file()` 以 `ack_mode`（`auto|echo|none`）選路；RPC/CLI 打通 `--ack-mode`。關鍵不變量：**echo 停滯時換行尚未送出＝命令未執行＝可安全重試**。

## Decisions（本計畫已裁決，實作不得再開放）

1. **不**在 `sw_core/assets/profiles/default.yaml` 為 prpl-template 加 `chunk_timeout_s` 保險值（echo-ACK 後不需要；真機驗收後若仍需要另案）。
2. slice_size 預設 **64**、`echo_timeout_s` 預設 **2.0**，維持固定保守值；**不**做「探測板端極限自適應」（YAGNI，先讓正確性落地）。
3. `cancel_input_line()` 一律送 `\x15`＋`\n`，**不**做 per-platform 分歧；bcm 原生 CLI 若不吃 Ctrl-U，後果僅是多一個無效字元＋一次換行（既有 prompt 重取），無破壞性。
4. F7 的 1MB 案（`f7-larger-file-not-truncated`）**維持 echo-paced 預設路徑**、timeout 調升到 1500s；不用 `--ack-mode none` 保留舊語意（回歸防線要驗的是新機制）。
5. 新錯誤碼 `TRANSFER_ECHO_STALL` 在 F7 分類器歸**獨立 reason_code `transfer_echo_stall`**（不併進 `_TIMEOUT_CODES`），使新機制失效可辨識。
6. `execute_command` 超長命令行走 paced 送：**本次不做**（原語就緒，另案）。
7. pull 側 RX 視窗 128KiB 上限（1MB pull 必 `PULL_PARSE_FAILED`）**不併入本案**——實作時於 `docs/regression-plugin.md` 記為已知限制並在 PR 描述點名，後續另開 issue。

## Task 1: UARTBridge echo-paced 原語

**Files:** `sw_core/uart_io.py`、`tests/test_uart_io.py`

- [ ] 1.1 `send_command_echo_paced(cmd, *, source, cmd_id=None, slice_size=64, echo_timeout_s=2.0) -> dict`：payload＝本文切段逐送、每段後 `_await_echo_progress()`；全段確認才送 `b"\n"`；WAL 維持一命令一筆 TX；回 `{"ok", "acked_chars", "sent_chars"}`
- [ ] 1.2 `_await_echo_progress(expected_cumulative, from_offset, timeout_s)`：輪詢 `rx_text_from()`，`strip_ansi()`＋去 CR/LF 正規化後以移動起點 `find` 逐 slice 比對（吸收 slice 間 printk 噪音）；先檢查再 sleep、poll 0.01s
- [ ] 1.3 `cancel_input_line(*, source)`：送 `\x15`＋`\n`
- [ ] 1.4 單測（fake serial port）：正常逐段確認、噪音插入仍比對成功、echo 停滯回 `ok=False` 且**未送換行**、WAL 單筆、`cancel_input_line` 位元組正確

## Task 2: file_transfer 選路與錯誤碼

**Files:** `sw_core/file_transfer.py`、`sw_core/session_manager.py`、`tests/test_file_transfer.py`

- [ ] 2.1 `push_file()` 新增 keyword-only `ack_mode="auto"`／`echo_slice_size=64`／`echo_timeout_s=2.0`；`auto`＝bridge 有 paced 方法即用（無則 legacy，第三方 fake bridge 不破）
- [ ] 2.2 echo 停滯：`cancel_input_line()` 復原後回 `TRANSFER_ECHO_STALL`（含已送 chunk index／acked_chars 診斷欄）
- [ ] 2.3 `session_manager.file_push` 傳遞新參數；`chunk_timeout_s` 來源優先序（顯式參數 > profile `timeout_s` 推導 > 下限）維持 #157 不回退
- [ ] 2.4 單測：auto/echo/none 三路選路、停滯回 `TRANSFER_ECHO_STALL`、legacy bridge fallback、參數優先序

## Task 3: RPC 與 CLI 打通

**Files:** `sw_core/service.py`、`sw_core/cli.py`、`tests/test_file_transfer_chunk_config.py`

- [ ] 3.1 `file.push`／`file.pull` 解析 `ack_mode`，非白名單回 `INVALID_ARGS`（比照既有 chunk 轉型防呆的 try/except 風格）
- [ ] 3.2 CLI 兩子命令新增 `--ack-mode {auto,echo,none}`（default auto）與 params 組裝
- [ ] 3.3 單測：CLI 解析、RPC 白名單拒絕、params 透傳

## Task 4: 哨兵 case、文件與 changelog

**Files:** `regression/serialwrap_regression/cases/f07_file_transfer.py`、`docs/regression-plugin.md`、`README.md`、`changelog.d/161-file-push-echo-ack.md`

- [ ] 4.1 F7 兩案：`transfer_echo_stall` 獨立 reason_code 分類；64KB 案 docstring 的 timeout 估算改為 echo-paced 模型；1MB 案 timeout 調升 1500s（決策 4）
- [ ] 4.2 `docs/regression-plugin.md`：F7 列改記「#161 echo-ACK 後預期 COM0 轉綠」；補記 pull 側 128KiB 視窗已知限制（決策 7）
- [ ] 4.3 README file transfer 段落補 `--ack-mode` 與吞吐取捨說明（1MB 約 10–17 分鐘、急件可 `none`）
- [ ] 4.4 `changelog.d/161-file-push-echo-ack.md`（`type: fix`、`scope: file_transfer`）

## Task 5: 驗證閘

- [ ] 5.1 `python3 -m pytest -q tests/` 無新失敗
- [ ] 5.2 `python3 -m policy_check --repo .` 通過
- [ ] 5.3 實機（人工閘，PR 後）：`testpilot run serialwrap_regression --case f7-binary-roundtrip-md5` 於 COM0 轉綠

## Open Questions

- 無
