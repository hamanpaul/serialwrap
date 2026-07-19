# Tasks: realhw-stability-suite-122

## 1. Harness 骨架（TDD：純邏輯全在 tests/）

- [x] 1.1 RED＋GREEN：`realhw/harness.py` 的 Case/CaseResult dataclass、registry 與 tier/only/skip 過濾、duration 解析（`32h`/`45m`/`3600s`）——`tests/test_realhw_harness.py`
- [x] 1.2 RED＋GREEN：報告產生器（CaseResult 清單 → report.json/report.md，含診斷提示與 evidence 相對路徑）——同測試檔
- [x] 1.3 `realhw/__main__.py` CLI（argparse：--tier/--only/--skip/--list/--report-dir/--duration）＋`realhw/config.yaml`（本機兩板 serial、usbipd 路徑、tmux 前綴、per-case timeout）；`--list` 輸出驗證

## 2. Drivers（TDD：解析邏輯單測、subprocess 薄層實機驗）

- [x] 2.1 RED＋GREEN：usbipd `list` 輸出解析（busid↔serial 映射）、tmux capture 斷言 helper（marker 尋找、ANSI 剝除）——`tests/test_realhw_drivers.py`
- [x] 2.2 `swcli`/`tmuxctl`/`usbipd`/`systemd` 四個薄包裝實作（subprocess、JSON 解析、錯誤傳播）

## 3. Preflight

- [x] 3.1 preflight 六項檢查實作（新鮮度/doctor/兩板/工具/環境乾淨/破壞性預告）＋fail-fast 與缺項輸出；純判定邏輯（吃注入的檢查結果）單測

## 4. P0 cases（8 條）

- [x] 4.1 `p0-doctor`/`p0-cmd-async`/`p0-clear-reattach`/`p0-selftest`/`p0-wal-live`/`p0-multiopen`（純 CLI 類）
- [x] 4.2 `p0-console-raw`/`p0-blog-clean`（tmux+minicom 類）
- [x] 4.3 本機實跑 `--tier p0` 全綠（實機驗收）

## 5. P1 cases（20 條）

- [x] 5.1 `p1_console.py`（7 條：fanout/defer/busy/softpreempt/liveness/orphan/second）
- [x] 5.2 `p1_cmd.py`（3 條：modes/serial/file）＋`p1_wal.py`（2 條：reset/fullrun）
- [x] 5.3 `p1_restart.py`（4 條：daemon/reboot/bootwindow/recover，destructive 排尾）
- [x] 5.4 `p1_handoff.py`（2 條）＋`p1_hotplug.py`（2 條，usbipd）
- [x] 5.5 本機實跑 `--tier p1` 並修穩（等待節奏/timeout 調參；實機驗收）

## 6. 長跑

- [x] 6.1 RED＋GREEN：長跑分析器（吃合成快照/事件 log 產 longrun-analysis 統計）——`tests/test_realhw_longrun.py`
- [x] 6.2 `longrun.py`：worker 編排（4 agent＋1 human）、5 分鐘快照、重大事件停止保留、SIGINT 收尾產報告
- [x] 6.3 本機短跑驗收（`--tier longrun --duration 15m`）確認負載/快照/報告全鏈路

## 7. 文件與收尾

- [x] 7.1 `docs/func-test/realhw-stability-checklist.md`（P0/P1 對照＋P2 手動程序＋前置＋坑一覽；與 `--list` id 一致）
- [x] 7.2 README 補「實機穩定性測試」小節指向 checklist 與 `python3 -m realhw`
- [x] 7.3 `changelog.d/122-realhw-stability-suite.md` fragment（R-09）
- [x] 7.4 `python3 -m pytest -q tests/` 無新失敗＋`python3 -m policy_check --repo .`（含 PR 參數）通過
