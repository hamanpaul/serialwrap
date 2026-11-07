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

---

## 補記：第 6 項接線與整體驗收（2026-11-08）

本節記錄第 6 項的接線核對、本輪前景 A/B 六次實跑、逐筆時間線核對與整體驗收。上面第 3～5 項的內容保留原文。執行分支 `feature/rust-refactor-p8`，HEAD `38a93d975cf7fd298d130032c65c61a0287bab64`（本輪起點，工作樹乾淨）；本輪沒有修改任何 production code、測試、契約或凍結鎖。所有實跑都在前景完成，輸出在 `target/s2-par/S2-PAR-fixlat-6/`。

### 接線核對（程式碼確認）

- 原指定測試每次以 `_measure_tool()` 從工作樹載入 `scripts/rust/event_latency_measure.py`（`tests/contract_blackbox/test_p7_latency_noise.py:42-48`），`_cancel_run()` 逐筆直接呼叫該模組的 `tool._sample_cancel(rpc)`（`:85`），沒有第二套取消量測流程。
- 第 4 項的收尾等待 `_wait_cancel_drained()` 位於 `_sample_cancel()` 內部（`event_latency_measure.py:428-453`，呼叫點 `:499`），所以原測試不需改檔就已經走修正後的流程。`:208` 的結果輸出逐輪保留 `errors`，第 1 項的 `stage=wait_running` 與第 4 項的 `stage=wait_drain` 診斷會原樣出現在 `control_latency_summary`。
- 因此 `_cancel_run()` 與結果輸出都沒有修改。沒有新增接線邏輯，所以不適用「先用具名回歸證明舊接線會失敗」。沒有新增測試，鎖檔不變（`regen_lock --check` OK）。沒有動到 `code_paths`，不需要 changelog fragment。

### 本輪前景 A/B 六次（目前負載，未等待負載下降）

依 Python 1、Rust 1、Python 2、Rust 2、Python 3、Rust 3 的順序執行原 nodeid `test_control_latency_under_flood_with_events`。loadavg 是每次外部逐秒 `/proc/loadavg` 第一欄的範圍。表中的 peak 是 `judge_run` 內部監測的 1 分鐘 loadavg 峰值。

| 實作／輪次 | 起訖（Asia/Taipei） | 外部 loadavg 1m | command_cancel（200）max／p99 ms，peak | stop_request（20）max ms，peak | pytest |
|---|---|---:|---:|---:|---:|
| Python 1 | 00:51:06–00:52:40 | 5.51–11.55 | 1.240／1.123，11.55 | 0.394，6.98 | 1 passed，exit 0 |
| Rust 1 | 00:52:52–00:53:48 | 4.81–8.95 | 0.661／0.575，8.95 | 0.285，8.76 | 1 passed，exit 0 |
| Python 2 | 00:53:52–00:55:26 | 5.63–8.46 | 1.644／1.281，7.63 | 0.274，8.46 | 1 passed，exit 0 |
| Rust 2 | 00:55:26–00:56:21 | 4.66–10.06 | 1.647／0.545，6.54 | 3.872，10.06 | 1 passed，exit 0 |
| Python 3 | 00:56:24–00:57:58 | 8.96–16.83 | 1.358／1.284，16.83 | 0.432，15.90 | 1 passed，exit 0 |
| Rust 3 | 00:57:58–00:58:55 | 11.60–20.14 | 4.878／1.460，19.55 | 0.270，20.14 | 1 passed，exit 0 |

六次都是 `verdict=PASS`、`errors=[]`、`void=false`、`load_problems=[]`，沒有作廢重跑；flood 規則每次都有比對，`event_dropped` 只出現在 COM0，原因只有 `per_rule_busy`，筆數都不超過送出的行數。六次都是 `quick_check_only`（200／20×1）。依 OD-59／OD-71，這些只算快速檢查，不是 500×3 契約量測。本輪六次的外部負載都有時段超過 8，不算低負載樣本；低負載三輪對照仍以上面第 3 項的表為準（Python 2.42–4.82、Rust 1.92–4.27）。

每次的 `run.meta`（HEAD、起訖 load、bundle hash）、`command.txt`、`pytest.log`、JUnit、`exit_code`、`load-per-second.tsv` 與 `pytest-tmp/` 都保存在 `target/s2-par/S2-PAR-fixlat-6/{python,rust}-run-{1,2,3}/`。

**環境選擇與 artifact 身分。** Python 輪 unset `SERIALWRAP_AB_LABEL`、`SERIALWRAP_CLI_EXE`、`SERIALWRAP_DAEMON_EXE`，走 `py-serialwrapd`，也就是工作樹的 `serialwrapd.py`。Rust 輪設 `SERIALWRAP_AB_LABEL=rust-fixlat6`，`SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE` 指向第 3 項建好的 bundle `target/s2-par/S2-PAR-fixlat-3/rust-bundle/`。包裝腳本每次開跑前都重算 hash：CLI `6323e220…f531`、daemon（realpath `versions/0.4.0/bin/serialwrapd`）`994978cb…09a`，不符就拒跑；三次都相符。bundle 建自 `2e642648`；`git diff 2e642648..HEAD -- crates Cargo.toml Cargo.lock scripts/rust/mkbundle.sh` 為空，所以 Rust 二進位對應的原始碼和 HEAD 相同。harness 在健康檢查後會以 `/proc/<pid>/exe` 核對受測 daemon，三次 Rust 輪都通過這項檢查後才進入量測。

**觀察（已實測）：**Python 輪每次約 98 s，Rust 輪每次約 57 s；Python 的 flood 送出約 4,970 行，Rust 約 2,344 行。和第 4 項的探針一致：Python 每筆取消都要等已送出的 `sleep 0.3` 收尾，Rust 大部分樣本在 TX 前就已撤銷。

### 逐筆時間線核對（回應 `2e5f1d9f..38a93d97` 審查的既有碼待辦建議）

第 5 項只從單輪探針的彙總數字推斷「舊前景收尾後才讓下一筆進 running」。本輪改用第 4 項**修改前工具**探針已保存的 WAL 與 RPC 紀錄，逐筆核對（只讀，沒有另起 daemon）：`target/s2-par/S2-PAR-fixlat-6/timeline/timeline.py` 對每筆「舊命令已送出 TX」的樣本，從 WAL 取出舊命令的 TX，以及其後第一筆含 `root@t:/# ` 的 device RX（FakeTarget 執行 `sleep N` 會在 N 秒後送 prompt），再拿下一筆命令首次 `running` 時 `command.get` 帶的 `started_at` 來比對。輸出在 `timeline/{rust,python}-old-tool.json`。

| 探針（修改前工具） | 忙碌中返回 | 舊命令已送 TX 的樣本 | TX→prompt（mono） | prompt→下一筆 started_at（wall） | prompt→下一筆 TX（mono） |
|---|---:|---:|---:|---:|---:|
| Rust `probe-rust-1` | 32／200 | 32（全部是忙碌中返回；其餘 168 筆的 WAL 沒有 TX） | 0.3004–0.3027 s | 32／32 為正，7.6–16.3 ms | 21 筆有 TX，全為正，9.3–18.8 ms |
| Python `probe-python-1` | 200／200 | 199（第 200 筆沒有下一筆可比） | 0.2969–0.3260 s | 199／199 為正，0.5–52.9 ms | 199 筆全為正，0.8–53.3 ms |

探針期間 WAL 的牆鐘曾倒退約 1.7 s（Rust 1 次、Python 2 次，以 wall−mono 偏移偵測），所以 TX→prompt 改用 `mono_ts_ns` 計算。`started_at` 只有牆鐘，因此 prompt→started_at 用牆鐘比較；所有區間都是正值且 ≤ 53 ms，如果區間跨過那次跳動，會出現約 −1.7 s 的值。

**已實測：**在兩種 daemon 中，只要前一筆被取消的命令已經送出 TX，下一筆一定等到前一筆的完成 prompt 回來之後才進入 running，逐筆都沒有例外。也就是說，下一筆停在 `accepted`，是 worker／actor 依單寫入者規則等待前景 op 結束，不是 worker 沒有取件。下一筆的 accepted 等待就是前一筆剩下的收尾時間。修改後的工具把這段收尾歸到前一筆自己的期限內（第 4 項）。

### 三個問題的回答

1. **命令為什麼停在 accepted。**已實測：取消回覆與 `done_at` 都早於 worker 收尾（Python、Rust 皆然，第 4 項），修改前的工具因此在前一筆尚未收尾時就提交下一筆；下一筆會留在 accepted，直到前一筆的前景 op 結束（上節逐筆核對）。這是量測工具的同步假設錯誤，不是 flood 下 worker 不取件；已由第 4 項修正工具，判準不變。推斷，未證實：`9d915e84` 的 `command_cancel #41` 需要前一筆收尾超過 10 s 才會出現，例如 flood 中漏認完成 prompt、等到 `timeout_s=10` 後再進 recovery。第 2～6 項保存的所有實跑都沒有重現，原始失敗也沒有留下時間線，所以這個成因仍是推斷。
2. **Python 與 Rust 的行為。**已實測：兩者都在取消回覆前寫入 `done_at`；兩者都要等前景 op 結束才讓下一筆 running。差別在取消後的收尾比例：Python 200／200 筆都要等已送出的 `sleep 0.3`；Rust 只有 TX 已送出的 32／200 筆需要等，其餘在 TX 前就撤銷了 permit。本輪修改後工具的 A/B 各三次全部 PASS。Rust 沒有特有缺陷，第 5 項未啟動的結論不變。
3. **與負載的關係。**已實測：低於 8 的三輪（第 3 項）、本輪 4.66–20.14 的六輪、第 2 項 11.24–21.29 的三輪，都沒有 accepted timeout。峰值超過 24 CPU 的輪次會被負載閘門判 `INSUFFICIENT_EVIDENCE`（第 3、4 項），失敗原因是閘門，不是 accepted。沒有證據顯示 accepted timeout 和負載有關，但也無法排除原始主機在負載峰值 22 時，因收尾變長而觸發。

### 整體驗收

命令、stdout、stderr、exit code 與起訖 loadavg 存在 `target/s2-par/S2-PAR-fixlat-6/gates/`。

| 檢查 | 結果 |
|---|---|
| 原 nodeid，Python 三次／Rust 三次 | 6／6 exit 0（上表） |
| `python3 -m pytest -q tests/test_event_latency_measure.py` | exit 0，24 passed |
| `python3 -m pytest -q tests/contract_blackbox/test_p7_latency_noise.py`（第 1 次） | **exit 1**：1 failed、1 passed，279.67 s。失敗的是 `test_login_with_delay_and_noise`，見下節；本項目標測試在同一次中通過 |
| 同上（第 2 次，保留 basetemp） | exit 0，2 passed，180.04 s |
| `python3 -m pytest -q tests/` | **未執行**：本輪 brief 規定全套 pytest 另派，不記為通過 |
| `python3 -m tools.contracts check` | exit 0，`ok=true`、findings 0 |
| `python3 tests/acceptance/regen_lock.py --check` | exit 0，鎖檔與 L1 結構兩項 OK |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0 |
| `python3 -m policy_check --repo .` | exit 0，25 pass、0 fail、2 warn（R-19 parser advisory；R-22 3244 筆懸空引用，本節只新增 1 筆，即被忽略的 `target/` 時間線腳本路徑） |
| `cargo test --workspace` | exit 0，1542 passed、0 failed、45 ignored（173 個 result 行） |
| `cargo test -p sw-core --features test-fakes` | exit 0，1182 passed、0 failed |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0 |

第 5 項沒有啟動，沒有它的 Rust 修改要驗收；第 5 項節中已實跑的 `command_exec`、`arbiter`（test-fakes）結果仍有效，本輪 `sw-core --features test-fakes` 全部通過，已涵蓋這兩支。

### `test_login_with_delay_and_noise` 的失敗（與本項無關，不修）

**已實測：**第 1 次整檔實跑在本輪起點 HEAD（尚無任何修改）、Python daemon 下失敗。該測試期間的外部 loadavg 1m 為 5.39–22.43（開始時 17.52，24 CPU）。session 在 pytest 開始後約 32 s 進入 `DETACHED`，`last_error=POST_LOGIN_CMD_TIMEOUT`、`reprobe_attempts=0`、`next_reprobe_at=None`，`last_error_detail` 的尾段全是 noise 行；之後一直停在 DETACHED，到 180 s 期限為止。重跑前以 `ps` 確認沒有洩漏的拋棄式 daemon。同範圍第 2 次 `2 passed`（loadavg 6.83–24.68）。該次 login 測試在 WAL 中的 system TX 依序是 `\n`（0 s）、`root`（15.0 s）、`\n`（34.1 s）、`sh` 與 ready probe（34.9 s），最後到達 READY；密碼不寫入 WAL，所以無法只靠 WAL 判斷中間有沒有重新 attach。第 1 次的 pytest 暫存在 `/tmp`，已被清除；第 2 次的暫存保存在 `gates/pytest-p7-latency-noise-file-2.tmp/`。

**程式碼確認：**Python `_is_reprobe_prompt_error()`（`sw_core/session_manager.py:2112-2136`）在 DETACHED 時只對 `PROMPT_TIMEOUT`、`*_PROMPT_TIMEOUT`、`RX_FLOOD`、`TRANSPORT_STALL` 自動重探，`POST_LOGIN_CMD_TIMEOUT` 不在其中。測試註解（`test_p7_latency_noise.py:159-160`）假設失敗後會在 DETACHED↔ATTACHING 之間重試，和這個錯誤碼的實際行為不符。先前 P7 全套也記錄過同一支測試出現 `POST_LOGIN_CMD_TIMEOUT`（`docs/rust-refactor/reviews/p7-fix-responses.md:439-440`）。

**推斷：**`sh` 的回應 prompt `# ` 後面每 50 ms 就接一行 noise，高負載下比對可能晚於下一行 noise 到達，因而漏認 prompt。兩次同範圍實跑的失敗集合不同，也已排除洩漏 daemon，但兩次峰值都接近或超過 24 CPU，系統飽和不能排除，所以不主張這是環境問題。這支測試走登入狀態機，和取消／延遲量測無關。依 OD-75 不改 `sw_core`，本項也不改這支測試；列為既有碼待辦，交編排者決定。

### SPEC-GAPS 與待裁決事項

- 取消路徑沒有確認出 Python 基線缺陷，因此不提 `sw_core` 的 SPEC-GAP。原指定測試修正後維持判準不變，並在 A/B 兩邊都通過。
- `9d915e84` 原始失敗的直接成因仍只是推斷（見問題 1），原因是缺少該次時間線。
- 審查 Low `P8-2e5f1d9-1`（drain 期限起算點與 `elapsed` 起點不同）、`P8-2e5f1d9-2`（drain 期限 15 s 或 10 s 的取捨）依規定不修，交編排者收進 low-backlog。
- `test_login_with_delay_and_noise`：Python 基線在 `POST_LOGIN_CMD_TIMEOUT` 後不會自動重探，測試假設與實際行為不符，偶發失敗（見上節）。需由編排者裁決歸屬。
