# Design: realhw-stability-suite-122

> 完整設計（含 case 目錄全文、三路彙整出處、風險）見
> `docs/superpowers/specs/2026-07-02-realhw-stability-suite-design.md`。
> 本檔為 OpenSpec 摘要版，聚焦技術決策。

## Context

- 歷來實機驗證程序散落：agent 記憶（mcu-flash/uboot/attach-reprobe/usbipd-com-rank 等驗證法）、repo 文件（README 真機驗證手法章、各 design spec 的實機 gate 段）、coexist E2E（T1-T8 的 tmux+console 模式）。
- 本機環境：systemd-system daemon、兩塊 prplOS 板（FTDI by-id AC01QZT0=COM0/dut-prpl、AQ00OAQ7=COM1/sta-prpl）、WSL＋usbipd-win、NOPASSWD sudo、tmux。
- 使用情境：全套手動觸發；長跑放假/下班前放下去、無人看護、事後看報告。

## Goals / Non-Goals

**Goals:**
- 部署後一鍵驗證：P0 煙霧（~15 分）＋P1 核心穩定性全自動；長跑獨立 tier（預設 32h）無人看護＋事後分析報告。
- 失敗自帶診斷（歷來坑寫進 hints）與 evidence（命令輸出/capture-pane/檔案引用），不需重新考古。

**Non-Goals:**
- MCU 燒錄自動化（P2 手動）、CI/自動排程、web dashboard、打包進 wheel、跨機器泛化、長跑自動重啟分段統計（v1 停止並保留現場）。

## Decisions

1. **獨立 Python harness（stdlib-only）而非 pytest/bash**：continue-on-failure＋evidence 收集＋客製報告＋tier 選擇是 bespoke harness 的自然形狀；pytest 的報告機制與此打架且與 tests/ 的 conftest 語意易混淆；bash 撐不起 tmux 編排與長跑分析。
2. **測部署後系統**：drivers 走已安裝 `serialwrap` CLI（subprocess），不 import sw_core——避免 repo 程式碼與部署版本混淆；這也是與歷來「throwaway daemon 驗新碼」的本質差異。
3. **與 #120 live guard 的關係**：`realhw/` 不在 `tests/` 下、不載入其 conftest；對 live 的操作是目的。反向互斥：跑套件期間不得同時跑 `pytest tests/`（preflight 檢查無其他 pytest）。
4. **usbipd 當 hotplug 模擬器**：busid 換線會變 → 每輪 `list` 重解析，config 只存 serial；插拔 case 驗 DETACHED-rebind（runtime）與 startup rank（restart 後）兩層。
5. **破壞性動作治理**：case 帶 `destructive` 標記，preflight 彙整預告；破壞性 case 排各檔尾端；case 自身 `finally` 還原，harness 於 case 間驗兩板 READY、不 READY 先嘗試恢復、仍失敗則後續依賴 case 記 SKIP。
6. **長跑無人看護語意**：case 級異常記錄後續跑；daemon 死亡/兩板長時間非 READY＝重大事件 → 停止負載、保留現場（不自動重啟）；SIGINT 與時間到皆走報告產出。
7. **報告落 `~/b-log/realhw-reports/<ts>/`**（與既有 b-log 習慣一致，不進 repo）。

## Risks / Trade-offs

- [開機時間波動使 reboot/hotplug case 誤報] → per-case timeout 進 config、上限寬鬆；失敗訊息附「還在開機」判別（看 `last_rx_at`）。
- [`p1-rst-bootwindow` 在 live profile（timeout_s=10s）卡不住開機窗] → 降級斷言（最終自動 READY 即過）並記錄實況。
- [suite 自身 bug 弄髒 live 環境] → case `finally` 還原＋harness case 間恢復檢查＋報告記錄殘留；套件只用公開 CLI（無直接改 state 檔）。
- [usbipd/tmux/sudo 缺失] → preflight fail-fast，不進入任何 case。

## Migration Plan

純新增（sw_core 零變更），無部署遷移；merge 後在本機直接 `python3 -m realhw --tier p0` 驗收。checklist 文件與 suite 同 PR 交付。
