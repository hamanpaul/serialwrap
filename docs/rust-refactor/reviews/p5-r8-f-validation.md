# P5-r8-f4 整體驗收

> 驗收狀態：局部 Rust 測試與靜態 gate 通過；裁決要求的連續 200 輪 workspace selector 未執行，本項尚未完成。未執行出口證據 driver，也未以局部測試替代 200 輪驗收。

## 起點、範圍與裁決

- 起點：`5d1747c60fd7e141823005298c15b1287010b1e6`，分支 `feature/rust-refactor-p5`，開始時工作樹乾淨。
- 本輪僅驗收既有 self-test drain 競態修正，不改產品碼、回歸、golden、契約、selector 或凍結鎖。
- `origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/p5.md:44` 要求修正後同條件完整 workspace selector 連續至少 200 次零失敗；`:46` 要求 OD-69 在送出 `WorkerStopped` 前有界等待該 epoch 已入列 RX 完成 dispatch。
- 最新審查為 `origin/docs/parity-amendments` commit `be56cf64` 的 `p5-cloud/49f57281_5d1747c6-sol.md`：A 段 8 項均已修於 `25ad3900`，B 段為 `NO FINDINGS`，沒有本輪需修的 High／Medium。

## 根因與現有接線

原始失敗記錄在 `docs/rust-refactor/reviews/p5-fix-responses.md:50`：第 863 輪回報 `tx incomplete: completed 0/23 [Errno 125] Operation canceled`，但 Python golden 預期 `not_submitted 0/23 draining`。`crates/sw-core/src/tx_writer.rs:476-495,574-578` 已在零位元組取消後重驗 fence；若 drain 已拒絕該提交，便分類為 `NotSubmitted` 並保留 fence 原因。對應回歸 `crates/sw-core/tests/tx_writer.rs:1111` 鎖住此分類。

另有測試時鐘次序風險：`bedc8deb` 將真實 release 回覆等待移到 FakeClock 推進之前。現行 `crates/sw-core/tests/lease_selftest.rs:1286-1297` 先等 worker drain／release 完成；`:1299-1308` 只在 release 完成後推進 nonce 等待時鐘，避免測試時鐘先於 worker receipt 觸發 3 秒 drain timeout。改動可由 `git show bedc8deb -- crates/sw-core/tests/lease_selftest.rs` 核對。

原競態判準仍在：control FIFO 與 0～1.9 ms 錯開（`:1275-1285`）、環境未設定 `SW_T14_RACE_ITERS` 時預設 1000 次（`:1265-1272`）、golden 錯誤比對及 OS submit 計數（`:1309-1327`）、release／reattach（`:1329-1338`），結尾強制兩分支皆出現（`:1340-1345`）。OD-69 worker receipt barrier 仍在 `crates/sw-core/src/uart_bridge.rs:687-716`。

## 紅綠證據與條件對照

- 已保存的原始紅燈是上述 F92 第 863 輪錯誤分類。`bedc8deb` 前後的程式差異證明 FakeClock 推進順序已調整；但本 worktree 沒有該時鐘次序修正的獨立 pre-fix 重現輸出，因此不宣稱已保存一份確定性時鐘回歸紅燈。
- 本輪以未改名原測試、預設 1000 次完成實跑：`committed=861`、`refused=139`，exit 0。golden 分支、零 TX 拒絕、OS submit 計數、release／reattach 斷言皆由原測試保留並執行。
- 本輪主機條件：Linux 9900X WSL2、UID 1000、Rust/Cargo 1.96.1、24 個 CPU；`SW_T14_RACE_ITERS` 未設定。ITEM 1 的逐輪 host/toolchain metadata 不在此 worktree，故無法聲稱本次與 ITEM 1 完全同條件。
- 這次單測的 1000 次內部迭代及一次 workspace 測試均不等於裁決要求的 200 次完整 selector。

## 驗收結果

執行皆以前景等待完成。輸出保存在忽略目錄 `target/p5-r8-f-validation/`。

| 命令 | 結果 |
|---|---|
| `cargo test -p sw-core --features test-fakes --test lease_selftest selftest_probe_drain_race_golden -- --exact --nocapture` | exit 0；1 passed、0 failed、33 filtered；1000 次內部競態，`committed=861 refused=139`。`race-exact.log` |
| `cargo test -p sw-core --features test-fakes` | exit 0；560 passed、0 failed、0 ignored。`sw-core-test-fakes.log` |
| `cargo test --workspace` | exit 0；1162 passed、0 failed、31 ignored。`workspace-default.log`；ignored 不計為通過。 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0；完成 cross-check，sw-pal 有 3 個 unused/dead-code warnings；不是原生 Windows 執行證據。`windows-check.log` |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0，無違規輸出。`cfg-boundary.log` |
| `python3 -m tools.contracts check` | exit 0；`ok=true`、`findings=[]`。`contracts-final.log` |
| `python3 tests/acceptance/regen_lock.py --check` | exit 0；鎖檔與必要集合結構通過。`lock-check-final.log` |
| `python3 tests/acceptance/regen_lock.py --check --base 5d1747c6` | exit 0；與本輪起點一致。`lock-base-final.log`；本輪未新增 nodeid 或改 selector。 |
| `python3 -m policy_check --repo .` | exit 0；25 pass、0 fail、2 warn（R-19 workflow 解析、R-22 既有懸空文件引用）。`policy-check-final.log` |
| `python3 -m pytest -q tests/` | 未執行。`tests/conftest.py:32-41` 在 collection 前快照 live state/WAL/config 並探測 daemon；`tests/liveguard.py:71-115,133-145` 會查 systemd 並嘗試 live `session.list` RPC。這會觸及本 brief 禁止存取的 production daemon/config/state。未停用 guard，也未把未執行記成 PASS。 |
| `python3 scripts/rust/cargo_l1_results.py ...` 連續 200 輪 | 未執行，依本輪指示不跑出口證據 driver；沒有逐輪 JSON、stdout/stderr、exit code 或時間可供索引。 |

本次驗收沒有 Python 程式或測試變更；因此沒有單獨執行 Python 測試。未操作 pipx production daemon、其設定／狀態、任何序列埠、com0com、Docker、其他 checkout 或 worktree。

## High／Medium 處理與不成立主張

- 前審 A-1～A-8：依最新 `be56cf64` 審查報告均為 **already fixed `25ad3900`**，本輪未重修。
- 前審 B：**NO FINDINGS**；本輪沒有新 High／Medium。
- 不成立主張：無。
- Low finding：無新增；依 brief 不處理。

## SPEC-GAPS

1. 裁決要求的完整 workspace selector 連續 200/200 零失敗尚未取得。本輪遵守「不要跑出口證據 driver」；指定的 200 輪需由後續獲派的驗收步驟完成。本輪 1 次 workspace 與單測內 1000 次不能替代。
2. 完整 pytest 未執行，因其 collection hook 會讀取禁區 live 資源；需要可在不接觸 production daemon/config/state 的核准執行環境。未以環境變數或 mock 繞過 live guard。
3. ITEM 2 時鐘次序修正的獨立 pre-fix 紅燈輸出及 ITEM 1 逐輪環境紀錄未在本 worktree 找到；本報告只記可追溯的 F92 原始紅燈與本輪綠燈，不推定未保存的證據。
4. Windows 原生、com0com DUT/console 與硬體測試未執行；Windows GNU 編譯不構成該等實跑證據。需要擁有者／硬體條件的項目維持 `BLOCKED-OWNER`。

## 既有碼待辦

- 最新審查 `p5-cloud/49f57281_5d1747c6-sol.md:20-23` 指出 `tests/test_l1_judge.py:932` 的 `moved == 22` 常數與起點前新增的兩筆 POSIX PTY 能力列不一致；本輪未跑 pytest、未修改 judge 或鎖。
- 同一審查指出 `tests/acceptance/l1_required.lock.json` 的 `s21_core.windows` 含 ignored 能力列，涉及 `test_l1_lock_frozen_set_excludes_capability_gated` 與 `test_l1_lock_cargo_sets_exclude_ignored_tests`。此為既有鎖／測試治理問題，不屬 self-test drain 競態範圍，本輪未處理。

## 逐輪報告索引

| 執行 | 證據 |
|---|---|
| 本輪原始競態單測（預設 1000 次） | `target/p5-r8-f-validation/race-exact.log`；唯一一次，exit 0。 |
| sw-core `test-fakes` 測試 | `target/p5-r8-f-validation/sw-core-test-fakes.log`；exit 0。 |
| workspace 預設測試 | `target/p5-r8-f-validation/workspace-default.log`；exit 0。 |
| workspace selector 200 輪 | 未執行；無輪次報告檔。 |

REMAINING: 依裁決透過獲派的出口證據流程完成完整 workspace selector 200/200；在不觸及 production live 資源的環境取得 pytest 證據；補齊 ITEM 1/2 原始條件與紅燈輸出索引。Windows 原生及硬體項維持 BLOCKED-OWNER。
