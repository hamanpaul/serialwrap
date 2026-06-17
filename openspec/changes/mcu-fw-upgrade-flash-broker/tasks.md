## 1. MCU pattern registry

- [ ] 1.1 寫測試：registry 載入/解析 per-family（family/probe/ack/baud/timeout）；預設含 TI CC2674/CC2652（`0x55 0x55`→`0x00 0xCC`）
- [ ] 1.2 寫測試：非破壞不變式——未標記為非破壞審核的 probe 項目被拒絕載入
- [ ] 1.3 實作 `sw_core/mcu_patterns.py`（registry + 載入器 + 非破壞 guard）令 1.1/1.2 轉綠
- [ ] 1.4 寫測試 + 實作：支援家族清單的文字渲染（供 `cat` 與 `mcu patterns`）

## 2. sync-probe 偵測器（TDD）

- [ ] 2.1 寫測試：候選計算正確排除 `command_capable` console
- [ ] 2.2 寫測試：逐候選逐 pattern 送 probe，第一個回正確 ACK 者被鎖定並記 family（假 PTY/loopback 假 MCU）
- [ ] 2.3 寫測試：多候選都 ACK → `FLASH_AMBIGUOUS`（不自動挑，列出命中識別資訊）
- [ ] 2.4 寫測試：無候選 ACK → 不回合成錯誤、不寫端點；週期 re-probe 後遲到 BSL 仍能命中
- [ ] 2.5 實作偵測器令 2.1–2.4 轉綠

## 3. raw/flash bridge（uart_io，TDD）

- [ ] 3.1 寫測試：flash 模式下 endpoint→device TX 原樣（含 `0x08/0x0A/0x0D/0x7F`），不經 `_consume_console_input` 行處理
- [ ] 3.2 寫測試：device→endpoint RX 原樣 1:1；daemon 維持 real device 唯一 reader
- [ ] 3.3 寫測試：RAW WAL 全程記 flash 的 TX/RX
- [ ] 3.4 寫測試：baud/framing 由端點 slave 鏡射到 real device；fallback 用 registry baud
- [ ] 3.5 在 `UARTBridge` 實作 raw/flash 旗標、bridge 路由與 termios 鏡射令 3.1–3.4 轉綠

## 4. `/dev/ttyMCU` 端點與開啟分流（TDD）

- [ ] 4.1 寫測試：daemon 啟動建常駐 PTY + 穩定 symlink（預設 `${RUN_DIR}/dev/ttyMCU`）
- [ ] 4.2 寫測試：只讀（無端點輸入）→ 回支援清單 + 候選狀態文字 + EOF
- [ ] 4.3 寫測試：有端點輸入（flasher 送 bytes）→ 走偵測/flash 路徑
- [ ] 4.4 寫測試：已在 flash 中再開 → `FLASH_IN_PROGRESS`
- [ ] 4.5 實作 `sw_core/flash_endpoint.py`（PTY/symlink 生命週期 + 分流 + 重入擋）令 4.1–4.4 轉綠

## 5. FLASHING 狀態、仲裁與自動恢復（session_manager，TDD）

- [ ] 5.1 寫測試：認線成功 → 目標 session 進 `FLASHING`
- [ ] 5.2 寫測試：`FLASHING` 期間 `cmd submit` → `FLASHING_BUSY`；其他 COM 不受影響
- [ ] 5.3 寫測試：flash 結束（hangup/timeout/顯式）→ `_probe_external_holder` + `_spawn_attach` 自動恢復；失敗停 `ATTACHING` + `last_error`
- [ ] 5.4 寫測試：flash 期間既有 human console 轉唯讀快照（不可注入）
- [ ] 5.5 實作 FLASHING 進出（沿用 release/attach 骨架）令 5.1–5.4 轉綠

## 6. 誤燒防護（TDD）

- [ ] 6.1 寫測試：`--selector/--by-id` 明指 `command_capable` console 未帶 `--force` → 擋下 + 警告
- [ ] 6.2 寫測試：帶 `--force` → 允許
- [ ] 6.3 實作明指防護令 6.1/6.2 轉綠

## 7. CLI / RPC

- [ ] 7.1 寫測試 + 實作：`mcu patterns`（列支援家族）與 `mcu status`（候選/FLASHING/holder probe）RPC + CLI
- [ ] 7.2 寫測試 + 實作：device list 反查（`/dev/ttyUSBx` → st_rdev → 對應 COM；查無→明確回報）
- [ ] 7.3 在 `service.py`/`rpc.py` 接上 `mcu.*` dispatch

## 8. 整合測試（假 PTY/loopback）

- [ ] 8.1 端到端：open ttyMCU → 認到假 MCU（一條 console + 一條回 `55 55→00 CC`）→ byte-perfect 雙向轉送
- [ ] 8.2 FLASHING 期間其他 COM 不受影響；結束自動恢復 console
- [ ] 8.3 回歸：`python3 -m pytest -q tests/` 無新失敗（既有 flaky 不計）

## 9. 文件與真機 gate

- [ ] 9.1 更新 `README.md`（新增 `/dev/ttyMCU` flash 用法、`mcu patterns/status`、與 #54 handoff 區隔）+ `CHANGELOG.md`（R-18）
- [ ] 9.2 **真機 gate（強制，必做）**：OCTOPUS/CC2674 上以 DUT console GPIO BSL-invoke 流程進 BSL，host 改用 `-d <…/dev/ttyMCU>` 實燒 → `Return error code : 0x0`；`led-test.sh -v` 版本回讀正確；double-sync 不干擾；其他 COM 不受影響、daemon 不死、結束自動恢復；RAW WAL 留證
- [ ] 9.3 `python3 -m policy_check --repo .` 通過；四份 agent 檔若有改同步（本變更預期不改）
