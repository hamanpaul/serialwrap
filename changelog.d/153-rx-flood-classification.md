---
type: fix
scope: session_manager
---
RX 洪水分類（#153）：console 被大量輸出灌爆時，probe 失敗過去被折疊進 `PROMPT_UNAVAILABLE`／`TARGET_UNRESPONSIVE`（「灌爆」與「死了」擠同一碼），上層被誤導去重建 session 而非等排空。新增：(1) `UARTBridge` RX 速率統計視窗（`rx_stats()`，最近 10s raw bytes，含 ANSI）；(2) `login_fsm` 兩個公開出口（`probe_ready`／`ensure_ready`）統一反分類——probe 失敗且 `rx_bytes_last_10s >= 20000`（`RX_FLOOD_BYTES_PER_10S`）時回新錯誤碼 **`RX_FLOOD`**（`LOGIN_REQUIRED`／`CREDENTIALS_*` 永不被遮蔽）；(3) self-test 於洪水中回 `classification=RX_FLOOD`＋`recommended_action=wait`（ATTACHED 與 READY nonce probe 兩分支，BOOTLOADER 優先序不變）；(4) `RX_FLOOD` 保留自動重探資格——洪水中 RX-idle 閘天然退避、排空後 3s 內自動接手升 `READY`（自癒）；(5) session 公開 dict 新增 `rx_bytes_last_10s`／`rx_rate_bps` 欄位；`minicom_router.sh`／SKILL.md／README 契約同步。回歸 plugin 新增 F11 family 與 `f11-flood-probe-classified`（destructive）實機 case。
