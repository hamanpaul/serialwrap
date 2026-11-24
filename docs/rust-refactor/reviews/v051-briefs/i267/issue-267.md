# issue 267：fix(session): 終態 last_error（CREDENTIALS_UNRESOLVED）下 USB 讀取端點 STALL 不被偵測，self-test 誤回 ATTACHED_NOT_READY／console_attach

## 現象

COM0 的 session 從 daemon 啟動起就停在 `ATTACHED` + `last_error=CREDENTIALS_UNRESOLVED`（profile 宣告的帳密來源解析為空，#140 終態）。這個狀態下 RX 原本正常流了約五天。之後 FTDI 讀取端點 STALL（`urb stopped: -32`），COM0 連續約 16.7 小時零 RX，serialwrap 沒有判成 `TRANSPORT_STALL`：

- `session list`：`last_error` 仍是 `CREDENTIALS_UNRESOLVED`，`last_error_detail=null`，沒有 #150 的 host 層復原指令。
- `session self-test`：`classification=ATTACHED_NOT_READY`、`recommended_action=console_attach`。當時 human console 已經接著（`human_attached=true`、`human_active=true`），照這個建議做不會有任何改善。

和 #233 的差別：#233 處理的是 `TRANSPORT_STALL` **已經判定之後**的通知與復原。這次則是因為終態 last_error 擋掉 probe，**連判定都沒有發生**。

## 時間軸（UTC，host 為 UTC+8）

| 時間 | 事件 |
|---|---|
| 11-10 05:33:27 | daemon 啟動；COM0 進入 `ATTACHED` + `CREDENTIALS_UNRESOLVED`，`last_probe_at` 一直是 `null` |
| 11-10 ～ 11-15 | RX 正常，DUT 每 10 秒、每 30 秒各有一筆週期性輸出 |
| 11-15 09:09:31.44 | COM0 最後一筆 RX（週期性輸出就此中斷，前面沒有 panic 或重開機訊息） |
| 11-15 09:09:32 | dmesg：`ftdi_sio ttyUSB2: usb_serial_generic_read_bulk_callback - urb stopped: -32`（vhci_hcd 1-3） |
| 11-16 01:50 ～ 01:53 | human console（3 個 console id）多次送 `\r`，WAL 只有 TX，零 RX |
| 11-16 約 01:51:38 | DUT 開機（由恢復後 kernel timestamp `[380.18]` @ 01:57:58 回推）。整段 boot log 都沒收到 |
| 11-16 01:57:18 | `/sys/devices/platform/vhci_hcd.0/usb1/1-3/authorized` 0→1，FT232R 重新列舉 |
| 11-16 01:57:58 | RX 恢復，serialwrap 自動 re-attach（`bridge_generation` 1→2），`last_error` 仍為 `CREDENTIALS_UNRESOLVED`（符合預期） |

同一時段 COM1、COM2 的 RX 都正常，只有 COM0 這支 FT232R 出問題。

## 根因（程式碼，`47097715`）

1. stall 判定只走 probe 失敗後的精煉路徑：`_refine_probe_failure` → `classify_probe_failure`。`is_transport_refinable_error` 的 docstring 寫明 `CREDENTIALS_UNRESOLVED` 在 probe 前就早退，所以不會進到這條路。
   https://github.com/hamanpaul/serialwrap/blob/47097715183698b138f1fa8bacd9d0f25c6edae7/sw_core/transport_stall.py#L27-L38
2. reprobe 排程對 `CREDENTIALS_UNRESOLVED` 直接 `return None`，所以 probe 永遠不會跑，stall 也就永遠判不到。終態不自動重探本身是 #140 刻意的設計，問題在 stall 偵測被它連帶關掉。
   https://github.com/hamanpaul/serialwrap/blob/47097715183698b138f1fa8bacd9d0f25c6edae7/sw_core/session_manager.py#L2196-L2200
3. self-test 的 `ATTACHED` 分支依序檢查 passthrough、`LOGIN_REQUIRED`、`REBOOTING`、BOOTLOADER、`RX_FLOOD`，都不符合就落到 `ATTACHED_NOT_READY` + `console_attach`。整個分支沒有任何 RX 凍結檢查，也沒有把 `CREDENTIALS_UNRESOLVED` 獨立分類出來。
   https://github.com/hamanpaul/serialwrap/blob/47097715183698b138f1fa8bacd9d0f25c6edae7/sw_core/session_manager.py#L7732-L7803

推論：任何「不自動重探」的終態 last_error（例如 `LOGIN_REQUIRED`、`PASS_ENV_MISSING`、`POST_LOGIN_CMD_TIMEOUT`、`REBOOTING`）都會以同樣方式遮蔽 stall。這一點只從程式碼推得，實機只驗證過 `CREDENTIALS_UNRESOLVED`。

## 預期行為

- stall 判定不依賴 probe 路徑，也不受 last_error 是否為終態影響。session 只要曾經有 RX、之後 RX 凍結超過閾值，就要能被標記出來。不送任何 probe bytes 也做得到，例如用下列被動訊號：
  - 最後一次 RX 之後出現過 TX（human console、agent 或 interactive lease 皆可），但之後零 RX，連 echo 都沒有。
  - 參考 #233 第 2 項：讀 kernel 的 `urb stopped: -32` 或 sysfs 狀態佐證。
- 終態 last_error 本身保留不覆寫（`CREDENTIALS_UNRESOLVED` 仍需要人補帳密），stall 改以獨立欄位或 detail 並存，避免一個錯誤蓋掉另一個。
- self-test 在上述情況回報 transport stall 分類與 host 層復原指令，不再回 `console_attach`。

以上屬提案方向，欄位名稱與實作方式待定。

## 驗收條件

- [ ] session 處於 `CREDENTIALS_UNRESOLVED`（以及其他不自動重探的終態）時發生 RX 凍結：`session list` 能看出 transport stall，並附 busid 的復原指令。
- [ ] 同上情境，`session self-test` 不回 `ATTACHED_NOT_READY` + `console_attach`，改回 transport stall 分類與對應的 `recommended_action`。
- [ ] 偵測過程不對 DUT 送任何 bytes，不違反 #140 不自動重探、不送空帳密的約束。
- [ ] RX 恢復後 stall 標記自動清除，原本的終態 last_error 不受影響。
- [ ] 單元測試覆蓋：終態 last_error 加 RX 凍結、終態 last_error 加 RX 正常（不得誤判）、從未有過 RX 的 session（不得誤判）。

## 非目標

- 自動 USB reset、一鍵 reset、主動通知（屬 #233）。
- 改變 `CREDENTIALS_UNRESOLVED` 的終態語意或自動補帳密。

## 適用範圍

- Python 0.4.1（`code 469f55aa5ccf`）實機重現。
- Rust：`crates/` 內搜不到 `ATTACHED_NOT_READY` 分類字串，Rust 端的 self-test 行為未驗證；移植時需要一併納入。

## 關聯

- #150：`TRANSPORT_STALL` 判定（probe 路徑）
- #233：STALL 判定後的通知與 reset
- #140：`CREDENTIALS_UNRESOLVED` 終態
- #153：`RX_FLOOD` 分類（self-test 同一分支）


## 留言

