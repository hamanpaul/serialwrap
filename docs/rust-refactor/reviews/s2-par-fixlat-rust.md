# S2-PAR-fixlat：Rust daemon 低負載對照

## 結論

本項分類為**尚無足夠證據**。相同工具、樣本數、判準及低負載條件下，Python daemon 與 Rust daemon 各跑三輪，六輪都沒有 `command_cancel` 的 accepted timeout，測試均 exit 0。沒有證據顯示 10 秒等待太短，也沒有觀察到 Rust worker 未取走命令；同樣不能據此排除原始 `9d915e84` 失敗與負載或取消後的時序有關。

每輪仍維持 `command_cancel=200`、`stop_request=20`、flood 上限 100 ms；沒有調高上限、改變判定或變更 `event_latency_measure.py`。六輪都是 `quick_check_only`，不代表 OD-59 的 500×3 契約量測。

## 範圍與 artifact 身分

分支 `feature/rust-refactor-p8`，來源 HEAD `2e64264872619f7d33ca107ad2a2726527498711`。本項只新增本報告；未改 Rust 產品、`sw_core/`、量測工具、測試或凍結鎖。

`scripts/rust/mkbundle.sh --dev --profile debug` exit 0，產生來源 commit 可追溯的臨時 bundle。建置器原始 `release-manifest.json` 標示 `components.daemon.implementation=python`，原始 manifest 保存在 `target/s2-par/S2-PAR-fixlat-3/bundle-manifest-python-generated.json`。只在忽略的 `target/` 測試 bundle manifest 將 daemon implementation 設為 `rust`、entry 設為 `bin/serialwrapd`；沒有改建置工具或版本庫中的產品檔案。bundle 的 `SHA256SUMS` 已依測試 manifest 重建並逐檔驗證成功。

| 元件 | 來源 commit | SHA-256 |
|---|---|---|
| Rust CLI `bin/serialwrap` | `2e64264872619f7d33ca107ad2a2726527498711` | `6323e220295b0754a62d79df46340196707db8c08b9ccd8c4af3a3f34bb9f531` |
| Rust daemon `bin/serialwrapd` | `2e64264872619f7d33ca107ad2a2726527498711` | `994978cbce3d8b32d33afa1b058c12bf128eba78daea48516f6c8372f6a1309a` |
| 測試 manifest | 同上 | `4b03ade474cb6defe831f558f5769573aa247db9c6f36a8fc713310ca4cc291b` |

Rust run 的 `SERIALWRAP_CLI_EXE` 與 `SERIALWRAP_DAEMON_EXE` 都指向該 bundle。`bb_harness.py:87-98,101-109` 會由 `AB_ENV` 選出這兩個實際路徑；`tests/ab_env.py` 在 root conftest 清除外層變數前擷取選擇器。此測試透過 `slice21.harness.start_daemon()` 啟動受測 daemon，健康檢查後以 `/proc/<pid>/exe` 核對其 realpath 等於 `daemon_prefix()[0]`（`slice21/harness.py:166-205,335-425`）。三個 Rust run 都通過此身分檢查後才進入量測。`main.rs:11-15` 呼叫 `dispatch_to_rust_daemon()`；該函式在 manifest 的 daemon implementation 為 `rust` 時直接進入 Rust daemon 本體（`launcher.rs:315-330`）。

Rust 與 Python 的 `serialwrapd --help` 輸出刻意對齊 argparse，不能用它區分實作；此處的身分證據是 bundle manifest、binary hash 及 harness 對執行中 `/proc/<pid>/exe` 的檢查，不是 A/B label。

Python run 均 unset `SERIALWRAP_AB_LABEL`、`SERIALWRAP_CLI_EXE`、`SERIALWRAP_DAEMON_EXE`，因此走 Python 基線 CLI／daemon。兩種實作都透過同一 FakeTarget PTY 測試，未使用真實 COM、`/dev/ttyUSB*` 或實體序列埠。

## 低負載三輪比較

順序為 Rust 1、Python 1、Rust 2、Python 2、Rust 3、Python 3。表中 load 範圍是每輪外部逐秒 `/proc/loadavg` 第一欄（1 分鐘 loadavg）；每輪全部低於 8。cancel 的 max 是判準使用的每輪最大值；p99 另列作參考。

| 實作／輪次 | loadavg 1m 範圍 | command_cancel（200 筆）max / p99 ms | stop_request（20 筆）max ms | pytest |
|---|---:|---:|---:|---:|
| Rust 1 | 3.48–4.27 | 1.389 / 0.676 | 0.294 | exit 0 |
| Python 1 | 2.55–3.18 | 1.161 / 1.006 | 0.329 | exit 0 |
| Rust 2 | 1.92–2.58 | 0.890 / 0.572 | 0.249 | exit 0 |
| Python 2 | 2.42–2.98 | 1.619 / 0.774 | 0.323 | exit 0 |
| Rust 3 | 3.05–3.56 | 0.721 / 0.646 | 0.243 | exit 0 |
| Python 3 | 3.20–4.82 | 1.218 / 0.889 | 0.294 | exit 0 |

六輪的 `command_cancel` 與 `stop_request` verdict 都是 `PASS`、`errors=[]`、`void=false`；最大延遲都遠低於原有 100 ms 上限。flood rule 每輪有比對，`event_dropped` 筆數均在送出行數範圍內且 selector 為 COM0。pytest log 另保留各輪的 internal host monitor 樣本與完整摘要；每輪的 command、JUnit、exit code、逐秒 load 與 pytest 輸出位於 `target/s2-par/S2-PAR-fixlat-3/{rust-run-1,python-run-1,rust-run-2,python-run-2,rust-run-3,python-run-3}/`。

既有 ITEM 2 的 Python 高負載三輪也沒有 accepted timeout，但 loadavg 1m 範圍為 11.24–21.29，不能代替本表低負載對照（詳 `s2-par-fixlat-python.md` 與其 `target/s2-par/S2-PAR-fixlat-2/` 證據）。

## 未修正 Rust 的前置診斷輪

在低負載三輪前，先以同一份未修改的 Rust 產品執行原測試並保存 `target/s2-par/S2-PAR-fixlat-3/rust-baseline-high-1/`。此輪 pytest exit 1，沒有 accepted timeout（`errors=[]`），但 loadavg 1m 外部為 21.53–41.86，內部 cancel/stop peak 分別為 40.84/41.86（24 CPUs）。judge 將兩事件標成 `INSUFFICIENT_EVIDENCE`、`void=true`，故這是保留的高負載紅燈診斷，不算有效 latency 或低負載結果。

## 行為對照與根因判讀

- 量測工具在 `event_latency_measure.py:428-451` submit 後每 5 ms 查 `command.get`，最多等 10 秒見到 `running`；若到期仍是 `accepted`，錯誤表示該命令在觀察窗內沒有進入 running，並非延遲值超標。本項未更動這個 10 秒等待或 100 ms 判準。
- Rust `CommandRegistry::cancel()` 會立刻把紀錄改成 `canceled` 並寫入 `done_at`，它不會中止正在跑的 worker（`crates/sw-core/src/arbiter.rs:342-377`）。`command.cancel` 路由再 best-effort 送出 `CancelQueued`（`crates/sw-daemon/src/service.rs:715-728`）；actor 移除尚在 queue 的項目，若命令已佔用 foreground 則撤銷其 TX permit（`session_core.rs:1809-1829`）。
- `start_next()` 在 foreground slot 忙或 session 不是 READY 時不取 queue；取件後才 `mark_running`（`session_core.rs:4225-4250`）。目前前景 op 結束時 `op_finished()` 清理 foreground，再呼叫 `start_next()`（`session_core.rs:4691-4785`）。因此，accepted 若持續存在，可能是前一個 foreground operation 尚未完成；只看 cancel 回應的 `done_at` 不能證明 worker/actor 已排空。
- Python 基線亦由單一 session worker 從 queue 取件後才設 `running`；`cancel()` 會先寫 `canceled`／`done_at` 再呼叫 cancel callback（`origin/main:sw_core/arbiter.py:364-405`）。先前三次高負載 Python 與本次三次低負載 Python 都沒有重現 accepted timeout。

上述程式碼指出 accepted 的狀態路徑與取消後的同步限制，但六次低負載實跑沒有失敗樣本。它們不能判斷 10 秒是否不足，也不能證明 Rust actor/worker 在原始 host run 真的卡住。取消後以 `done_at` 加 `READY` 作為下一筆樣本的準備條件，也不是 worker drain 確認；這是量測流程可疑的同步假設，尚未由本項的失敗時間線證實為工具缺陷，因此沒有只為變綠而延長等待或修改量測器。

## SPEC-GAPS

提供的 `9d915e84` 證據只有 `command_cancel #41` accepted 錯誤文字，沒有該次 pytest temp、cmd_id 時序、前一命令的 started/done 時間、session foreground/recovering snapshot、FakeTarget TX 記錄或 daemon stderr。當前低負載三輪 Python/Rust 都沒重現；高負載 Rust 診斷輪則因 load 超過 24 CPUs 作廢。因此原始 accepted 的直接根因，以及它是否與負載相關，仍無法確認。


## 本項驗收

本項指定的 pytest nodeid 六次均在前景完成並 exit 0：Rust 3/3、Python 3/3。另保存先行 Rust 高負載診斷輪，pytest exit 1，因負載超出可用證據門檻而保留為無效 latency 輪。所有命令、逐秒負載、JUnit、exit code 與 gate 輸出位於 `target/s2-par/S2-PAR-fixlat-3/`。

| 檢查 | 結果 |
|---|---|
| `python3 -m tools.contracts check` | exit 0，`ok=true`、findings 0 |
| `python3 tests/acceptance/regen_lock.py --check` | exit 0，鎖檔與 L1 結構兩項 OK |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0 |
| `python3 -m policy_check --repo .` | exit 0，25 pass、0 fail、2 warn（R-19 parser advisory、R-22 既有 dangling references） |

本項僅新增此比較報告；未改 production code、測試、契約或凍結鎖，故未執行全套 pytest、Cargo suite、slice gate 或 Docker。

REMAINING: none

---

## 補記：第 4 項取消樣本收尾修正（2026-11-08）

工具修改的內容、修改理由、為何不算放寬判準，以及新回歸的紅燈證據，都記在 `s2-par-fixlat-python.md` 的同名補記節。本節只補 Rust daemon 這一側的證據。上面第 3 項的內容保留原文。

Rust daemon 一律使用第 3 項建好的 bundle `target/s2-par/S2-PAR-fixlat-3/rust-bundle/`。執行前重新核對 hash：CLI 為 `6323e220…f531`、daemon 為 `994978cb…09a`，和上表一致。`2e642648..HEAD` 期間沒有修改 `crates/`。

| 證據 | 位置 | loadavg 1m | 結果 |
|---|---|---|---|
| 探針（**修改前**工具） | `target/s2-par/S2-PAR-fixlat-4/probe-rust-1/` | 14.96–17.21 | 200／200 有效。其中 32 筆返回時仍是 `READY`＋`foreground_busy=true`，這 32 筆的下一筆 accepted 等待為 0.255–0.274 s；其餘 167 筆的下一筆只等 0.001–0.003 s。200／200 筆在取消後第一次 `command.get` 就已帶 `done_at` |
| 探針（修改後工具） | `probe-rust-fixed/` | 23.15–24.04 | 200／200 有效，`errors=[]`；0 筆於忙碌中返回；下一筆 accepted 等待 0.0014–0.0037 s |
| 原黑箱測試（修改後工具） | `blackbox-rust-fixed/` | 14.69–27.02 | command_cancel 200／200 PASS，max 2.240 ms，`errors=[]`。stop_request 因 host loadavg 峰值 27.02 > 24 CPU，作廢重跑後仍是 `INSUFFICIENT_EVIDENCE`，pytest exit 1。stop 路徑本項沒有修改，這一輪不算有效的 stop 證據 |

**已實測：**Rust 也會在 `CommandRegistry::cancel()` 回覆前寫入 `done_at`（`crates/sw-core/src/arbiter.rs:342-377`），所以原工具的 `done_at` 等待對 Rust 一樣沒有作用。Rust 有 16% 的樣本在 worker 還忙時就結束，Python 則是 100%。**推斷：**比例比 Python 低，可能是 Rust 的 `CancelQueued` 撤銷 TX permit 後，大部分情況下前景 op 很快就結束，只有 TX 已經送出的樣本要等 prompt。這一點本項沒有逐筆核對 TX 時間線。這個差異是取消後的收尾時間，不在 `event_latency` 的契約口徑內，本項不把它列為 Rust 缺陷，也不改 Rust 程式。

---

## 補記：第 5 項啟動條件核對（2026-11-08）

第 5 項的範圍是「Rust 歸因成立時，修正取消後的命令推進」。啟動條件是：第 3 項取得 Rust 特有的失敗，並定位到取消、前景收尾或下一筆出列的責任點。本節只記錄這個條件的核對結果；上面第 3、4 項的內容保留原文。

### 核對結果（已實測：本輪重讀既有 artifact）

本輪重新讀了第 3、4 項保存的全部 Rust 輸出，在其中搜尋 `命令未進入 running`，命中 0 個檔案。

| Rust 證據 | pytest／探針 exit | command_cancel errors | 說明 |
|---|---:|---|---|
| `S2-PAR-fixlat-3/rust-run-{1,2,3}/` | 0／0／0 | `[]` | 低負載三輪，全部 PASS |
| `S2-PAR-fixlat-3/rust-baseline-high-1/` | 1 | `[]` | loadavg 峰值 41.86 > 24 CPU；judge 判 `INSUFFICIENT_EVIDENCE`，`void=true`。失敗原因是負載閘門，不是 accepted timeout |
| `S2-PAR-fixlat-4/probe-rust-1/`（修改前工具） | 0 | `[]` | 200／200 有效；其中 32 筆返回時 `foreground_busy=true`，下一筆的 accepted 等待為 0.255–0.274 s，遠低於 10 s |
| `S2-PAR-fixlat-4/probe-rust-fixed/` | 0 | `[]` | 200／200 有效；0 筆在忙碌中返回 |
| `S2-PAR-fixlat-4/blackbox-rust-fixed/` | 1 | `[]` | command_cancel 200／200 PASS；只有 stop_request 因 loadavg 峰值 27.02 > 24 CPU 判 `INSUFFICIENT_EVIDENCE` |

Rust 在每一份證據裡都沒有出現命令停在 accepted 的情況。「取消回覆早於 worker 收尾」這個行為 Python 也有（Python 100%，Rust 16%），不是 Rust 特有。探針量到，忙碌中返回之後，下一筆的 accepted 等待是 0.255–0.274 s，最大值低於 0.3 s。（推斷）這段時間符合「舊前景收尾後才讓下一筆進 running」的單寫入者行為；不過探針沒有逐筆核對 running 與 `foreground_busy` 清除的先後。所以，第 3 項沒有取得 Rust 特有的失敗，也沒有定位出 Rust 的責任點。

### 處理

依 brief「Rust 沒有重現缺陷時保留比較結論，不製造產品修改」，**第 5 項未啟動**：

- `crates/sw-core/src/session_core.rs`（`CancelQueued` 處理、`start_next()`、`op_finished()`）與 `crates/sw-daemon/src/service.rs` 的 `command.cancel` 路由都沒有修改。
- 沒有新增回歸 `fixlat_cancelled_foreground_allows_next_command`。它要重播的「第 3 項實際競態」並不存在，這時寫出的測試在現行 Rust 上必然通過。依 brief，舊 Rust 上就通過的回歸不能當成根因證據；為了湊出紅燈去另造情境，等於製造缺陷。
- 沒有新增 brief 為產品修改預留的 changelog fragment：本項沒有修改 `code_paths`，R-09 不會觸發。
- 既有回歸 `command_exec.rs::command_cancel_running_revokes_tx_op`（撤銷後 TX 尾段不再交給 OS）在本輪實跑通過，見下表。

**推斷，未證實：**`9d915e84` 那次 `command_cancel #41` 失敗跑的是 Python daemon，比較可能的成因仍是第 4 項記錄的工具同步缺陷：前一筆的收尾時間被算進下一筆的 accepted 等待。原始失敗沒有留下時間線，所以這一點仍然無法證實。

### 第 5 項驗收

所有命令都在前景執行。command、stdout、stderr 與 exit code 存在 `target/s2-par/S2-PAR-fixlat-5/gates/`。開跑前 loadavg 1m 為 2.84。

| 檢查 | 結果 |
|---|---|
| `cargo test -p sw-core --features test-fakes --test command_exec -- --test-threads=1` | exit 0，69 passed |
| `cargo test -p sw-core --test arbiter -- --test-threads=1` | exit 0，但 **0 tests**：`tests/arbiter.rs` 整檔受 `#![cfg(feature = "test-fakes")]` 管制，不開 feature 時這條命令什麼都沒驗 |
| `cargo test -p sw-core --features test-fakes --test arbiter -- --test-threads=1` | exit 0，26 passed（補跑，用來取代上一列的空跑） |
| `cargo test --workspace` | exit 0，1542 passed、0 failed、45 ignored（loadavg 1m 4.74→4.60） |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0 |
| `python3 -m tools.contracts check` | exit 0，`ok=true`、findings 0 |
| `python3 tests/acceptance/regen_lock.py --check` | exit 0，鎖檔與 L1 結構兩項都是 OK |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0 |
| `python3 -m policy_check --repo .` | exit 0，25 pass、0 fail、2 warn（R-19 parser advisory；R-22 3243 筆既有懸空引用，與第 4 項記錄相同，本節沒有新增） |

本項只修改文件；沒有執行全套 pytest、slice gate、Docker 或出口證據 driver。
