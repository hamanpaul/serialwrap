# S2-PAR-fixlat：Python accepted 命令等待診斷

## 範圍與基線

本報告只完成 S2-PAR-fixlat 第 2 項：在目前主機負載下，以 Python daemon 重跑指定黑箱測試三次，保存逐秒 loadavg 與 pytest 證據，並對照取消／worker 的來源程式。沒有修改 `sw_core/`、測試或量測工具；沒有啟動 Rust daemon、production daemon、Docker、實體序列埠或其他 worktree。

執行分支為 `feature/rust-refactor-p8`，HEAD=`8cd0e55549a41f87b5052ebec721a9d8252de02d`，`origin/main`=`9fdd847238fd3230fe94e2ff724af3a3ae640768`。HEAD 的 `sw_core/arbiter.py`、`sw_core/session_manager.py` 與 `origin/main` 逐位元組相同；整體 `sw_core/` 有兩個既有差異：`event_engine/counter.py` 與 `event_engine/registry.py` 各把暫存檔覆寫由 `os.rename` 改成 `os.replace`。此處實跑標示為 HEAD `8cd0e555`，不稱為純 `origin/main` 基線。這兩處不是本項修改。

每次均 unset `SERIALWRAP_AB_LABEL`、`SERIALWRAP_CLI_EXE`、`SERIALWRAP_DAEMON_EXE`；`bb_harness.daemon_prefix()` 因此選 `tests/contract_blackbox/bin/py-serialwrapd`，它執行工作樹的 `serialwrapd.py`（`tests/contract_blackbox/bb_harness.py:101-109`）。pytest 隔離各 run 的 `SERIALWRAP_*` state/WAL/config 路徑，測試 DUT 是 PTY FakeTarget（`test_p7_latency_noise.py` 模組標記 `posix_pty`）。沒有使用真實 COM 或 `/dev/ttyUSB*`。命令只增加 `--junitxml` 與 `--basetemp` 以保存證據，nodeid 與預設樣本數未變：每次 200 筆 command_cancel、20 筆 stop_request。

## 三次前景實跑

以下實跑日期為 2026-11-07（Asia/Taipei）。所有逐秒 loadavg 均來自 pytest 啟動前至結束後的 `/proc/loadavg` 採樣。每次的 `pytest.log`、JUnit、`command.txt`、`exit-code`、`loadavg.log` 與 `pytest-tmp/` 都保存在 `target/s2-par/S2-PAR-fixlat-2/run-N/`。pytest 暫存目錄含隔離 state、WAL、事件檔與 `daemon.stderr`。

| Run | 起訖（Asia/Taipei） | 逐秒 1 分鐘 loadavg 範圍（開始／結束） | command_cancel | pytest / exit |
|---|---|---|---|---|
| 1 | 22:36:44–22:38:16 | 11.53–15.80（15.80／12.06） | 200/200，errors=[]，max 1.081781 ms；取消階段內部 peak 15.80 | 1 passed，0 |
| 2 | 22:38:29–22:40:03 | 12.35–13.66（12.63／13.17） | 200/200，errors=[]，max 1.046505 ms；取消階段內部 peak 13.32 | 1 passed，0 |
| 3 | 22:40:15–22:41:51 | 11.24–21.29（12.99／21.29） | 200/200，errors=[]，max 1.331260 ms；取消階段內部 peak 12.99 | 1 passed，0 |

三次 command_cancel 與 stop_request 的量測 verdict 都是 PASS，事件 flood 規則均有匹配與 `event_dropped`。每次外部 loadavg 的最低值仍高於 8，故三次都不是低負載樣本。Run 3 後段的 21.29 出現在整個 pytest 區間；該 run 的 command_cancel 量測窗口內部 peak 為 12.99。三次皆為 `quick_check_only`，不是 OD-59 全契約量測。採樣器以 `sleep 1` 間隔取樣；每份 loadavg.log 的 wall-clock 時戳各有三次倒退約 0.43–0.55 秒，因此起訖使用牆鐘時間，pytest/JUnit elapsed 則記錄測試計時值，不以兩者相減推算執行秒數。

工作樹中原已有兩份本項資料，仍原樣保留，未計入上表三次：`high-load-diagnostic/` 為先前 `1 passed` 的對照，量測 loadavg peak 16.89；`low-run-1/` 的名稱不代表符合低負載，該次起始 11.02、結束 20.83，三個 attempt（含兩次 retry）的最終 command_cancel peak 34.97（24 CPU），`errors=[]`，judge 為 `INSUFFICIENT_EVIDENCE`，pytest exit 1。它沒有重現 accepted timeout，也不能作為 FAIL→PASS 或根因證據。

## 來源對照與判讀

### 已由程式碼確認

- `_sample_cancel()` 在 `scripts/rust/event_latency_measure.py:428-453` submit 命令後，每 5 ms 輪詢 `command.get`，最多等 10 秒進入 `running`；逾時時若狀態仍是 `accepted` 就報錯。這表示「提交已接受但 worker 尚未將它設為 running」，不是延遲值超標。10 秒是量測工具的等待上限；目前沒有實跑證據證明單純把它調長就合理。
- 同函式在取消成功後等待 session state 為 `READY`，再以 `done_at` 有值判定被取消命令已結束（`event_latency_measure.py:454-470`）。然而 `origin/main:sw_core/arbiter.py:364-380` 在呼叫 cancel callback 之前就把狀態設為 `canceled` 並寫入 `done_at`。因此 `done_at` 只證明取消記錄已終結，不證明 worker callback 已返回；`READY` 也不是該 worker 已排空的確認。
- `origin/main:sw_core/arbiter.py:382-405` 由每個 session 的單一 worker 從 priority queue 取件後才設 `running`／`started_at`；`:407-478` 同步執行 `_send_cb`，待 callback 返回或拋例外後才清除 `_running` 並完成 worker 收尾。若舊 callback 尚未返回，同 session 下一筆仍可保持 `accepted`。
- `origin/main:sw_core/session_manager.py:4595-4616` 的 `revoke_tx_op()` 撤銷該命令的 bridge TX permit，不等待 arbiter worker 結束。`execute_command()` 直到 `finally` 才清除 foreground operation（`:6611-6692`）；`_execute_command_inner()` 送出後仍可能等 completion prompt，逾時／靜默時進 recovery（`:6901-6948`）。所以若取消發生在 send 後，worker 仍可能留在 prompt/recovery 路徑；單看 canceled 與 done_at 無法分辨。

### 本輪實測與推斷

**實測：**三次都沒有 accepted timeout；每次 command_cancel 200 筆無錯誤，pytest exit 0。`pytest-tmp` 保留 state、WAL、FakeTarget 測試目錄與 daemon stderr；stderr 有 `/dev/pts/N` 外部 holder 權限不足的既有警告，沒有 worker exception traceback。因沒有失敗樣本，這些資料不能構成「前一筆 canceled 尚在執行」或「下一筆沒有 started_at/TX」的失敗時間線。FakeTarget 的 `rx_log` 是行程內記憶體資料，本測試不把它另存檔；沒有失敗樣本時也沒有對應 TX 可交叉核對。

**推斷：**若未來重現同一錯誤，`accepted` 逾時可證明該 command 在 10 秒觀察窗內沒有被 worker 取件；它本身不能證明原因。工具以取消記錄的 `done_at` 當完成訊號，確實沒有等待 worker drain，因而可能在前一筆 callback 尚未返回時提交下一筆；這是量測流程的可疑時序假設，但本輪未實測出該因果鏈。現有證據也不能排除 worker 異常退出、鎖／permit 等待或單純排程延後；不能判定取消落在送出前、部分 TX、prompt 等待或 recovery。不能只以 CPU 數或本輪 loadavg 推論根因。

## SPEC-GAPS

- 編排者指定本機目前不會出現 loadavg < 8，要求不等待、不輪詢且不停止他人工作。三次逐秒採樣最低值為 11.24；因此「低負載下是否重現」及負載與 accepted timeout 是否相關仍未判定，需留待有低負載時段再取證。
- 起始背景提供的是另一主機／提交 `9d915e84` 的 `command_cancel #41` 錯誤文字，未提供該失敗的 pytest temp、command id 時序、session snapshot、WAL、FakeTarget RX 或 daemon stderr。當前三次沒有失敗樣本，故無法回答該樣本的前一筆 canceled callback 是否結束、下一筆 `started_at`／TX、取消階段或 worker 健康狀況。
- 來源顯示量測工具把 canceled `done_at` 當成命令結束的依據，但 Python worker 行為是否構成產品契約缺陷，必須由實際失敗時間線判定。本項不改工具或 Python 基線；不把未重現解釋為修復，也不把 `INSUFFICIENT_EVIDENCE` 改判 PASS。

本項沒有進行 Rust daemon 對照；本輪的三次結果全為上述 Python wrapper／Python daemon。


## 驗收結果

| Gate | 結果 |
|---|---|
| `python3 -m tools.contracts check` | exit 0，`findings=[]`、`ok=true` |
| `python3 tests/acceptance/regen_lock.py --check` | exit 0，鎖檔／案例屬性快照／統計與 cases.yaml 一致，L1 必要集合結構正確 |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0 |
| `python3 -m policy_check --repo .` | exit 0，25 pass、0 fail、2 warn；R-19 為 workflow test-gate 辨識警告，R-22 回報 3236 個既有懸空文件引用 |

四項 gate 的 command、stdout、stderr 與 exit code 保存在 `target/s2-par/S2-PAR-fixlat-2/gates-item2-final/`。本項只有文件變更；沒有執行 Rust crate 測試或全套 pytest。

REMAINING: none

---

## 補記：第 4 項取消樣本收尾修正（2026-11-08）

本節記錄第 4 項對量測工具的修改，以及 Python daemon 的相關證據。上面第 2 項的內容與結論保留原文，不改寫。第 4 項自己的 gate 結果記在本節末尾。

### 啟動依據（已實測）

診斷探針 `target/s2-par/S2-PAR-fixlat-4/probe/probe_cancel_drain.py` 只放在被忽略的 `target/` 下，沒有進版本庫。它照原測試 `_cancel_run` 的流程執行：使用同一個 harness、同一條 ALL 規則、同樣 20000 B/s 的 flood，並呼叫**未修改**的 `_sample_cancel()` 200 次。探針只多做被動觀察：記錄每次 RPC 的送出時刻與回應，並在每次樣本返回後立刻呼叫一次 `session.get_state`。Python daemon 的結果在 `probe-python-1/`，期間 loadavg 1m 為 17.03–19.87（24 CPU）：

- 200／200 筆樣本返回時，session 是 `READY`，但 `foreground_busy=true`。
- 200／200 筆在取消後第一次 `command.get` 就已帶 `done_at`。所以原本「等 `done_at`」的 15 秒迴圈從來沒有真正等過；錯誤訊息「被取消的在途命令未結束」描述的後置條件實際上沒有檢查。
- 199 筆後續樣本的 accepted 等待落在 0.289–0.356 s，大致等於前一筆 `sleep 0.3` 剩下的時間。也就是說，下一筆的 accepted 等待把前一筆被取消命令的 worker 收尾時間也算進去了。

程式碼依據：`origin/main:sw_core/arbiter.py:364-380` 在呼叫 cancel callback 之前就寫入 `canceled` 與 `done_at`；`foreground_busy` 要到 `execute_command()` 的 finally 呼叫 `_finish_foreground_operation()` 才會清成 false（`origin/main:sw_core/session_manager.py:1782-1793`）。這屬於 Python 基線既有的行為，取消紀錄本來就可以早於 worker 結束；有缺陷的是量測工具把 `done_at` 當成 worker 已收尾的依據，所以修工具，不改 `sw_core`。

### 修改內容

`_sample_cancel()` 確認取消成功、命令記錄已有 `done_at` 且狀態為 `canceled` 之後，新增 `_wait_cancel_drained()`：必須在同一份 `session.get_state` 快照看到 `state == "READY"` 且 `foreground_busy is False`，樣本才算結束。收尾等待和 `done_at` 迴圈共用原本的 15 秒期限，沒有新增期限。逾時會以 `stage=wait_drain`、cmd_id、經過時間及最後一次的 state／foreground_busy／recovering 記錯。以下情況都會記錯，不算已收尾：`session.get_state` 非 ok 或沒有 session（立即記錯）、缺 `foreground_busy` 欄位、session 已不在 READY。

以下項目都沒有改：計時區間（`command.cancel` 的 RPC 送出→回應）、submit 後 10 秒的 accepted 等待、`judge_run()`、每輪樣本數、flood 有效性判準、100 ms 上限，以及 `contracts/resource_limits.yaml` 的凍結紀錄（`method_revision`、`external_method`）。錯誤仍照舊逐筆計入 errors；只要有錯，整輪就不能 PASS。

### 為何不是放寬判準

修改前，第 N+1 筆實際可用的 accepted 等待只有「10 s 減去第 N 筆的收尾時間」，而它的錯誤又會掛在第 N+1 筆名下。修改後，第 N 筆的收尾在第 N 筆自己的 15 秒期限內完成，超過就算第 N 筆的錯誤；第 N+1 筆則從 worker 閒置時開始計算 10 秒。要說明的是：如果某筆收尾時間落在 10–15 s，修改前會讓下一筆出現 accepted timeout，修改後兩筆都算有效。這是把錯誤歸到正確的樣本，不是延長 accepted 等待：10 s 期限本身沒變，凍結口徑也不包含收尾時間（`external_method` 只定義 RPC 送出→回應）。收尾超過 15 s 一樣會記錯。

### 修改後實測

| 證據 | 位置 | loadavg 1m | 結果 |
|---|---|---|---|
| 探針（修改後工具，Python） | `probe-python-fixed/` | 18.26–23.51 | 200／200 有效，`errors=[]`；0 筆於忙碌中返回；下一筆 accepted 等待 0.0004–0.0073 s |
| 原黑箱測試（Python，修改後工具） | `blackbox-python-fixed/` | 14.49–21.54 | `1 passed`、exit 0；command_cancel 200／200 PASS，max 8.132 ms／p99 1.703 ms |

cancel 計時的最大值在修改前後的探針分別為 1.505 ms 與 6.773 ms。修改後那一輪的 loadavg 比較高，目前的資料不足以把差異歸因於修改；兩者都遠低於 100 ms 上限。

### 已實測與推斷

**已實測：**工具原本沒有等 worker 收尾就進入下一筆；修改後不再發生；新回歸在舊工具上失敗（下節）。**推斷，未證實：**`9d915e84` 的 `command_cancel #41` accepted timeout 可能是因為前一筆被取消命令的 worker 收尾超過 10 s，例如 flood 下沒偵測到 prompt，等到 `timeout_s=10` 後又進入 recovery，結果算到下一筆頭上。但那次失敗沒有留下時間線，本輪的探針也沒有觀察到 0.36 s 以上的收尾，所以不能把這個推斷當成該次失敗的根因。另外，accepted timeout 發生後，工具不會取消那筆留在佇列中的命令，它會讓後續樣本繼續受影響。這一點沒有實測證據證明修正有必要，因此本項沒有處理。

### 新回歸與紅燈

`tests/test_event_latency_measure.py` 新增 5 個測試：`test_cancel_sample_does_not_finish_while_foreground_busy`、`..._errors_when_foreground_busy_outlives_deadline`、`..._drain_requires_reported_idle_ready_session`、`..._drain_rpc_failure_is_distinct`、`..._refused_or_not_canceled_fails_before_drain`。在未修改的工具上跑，前 4 個失敗（`target/s2-par/S2-PAR-fixlat-4/red/`，`4 failed, 20 passed`）；第 5 個在修改前後都通過，用來覆蓋取消被拒與最終非 canceled 的分支。修改後為 `24 passed`（`green/`）。5 個 nodeid 都以 `regen_lock.py --l1-add-only` 登記到 `p7_gate:python3-m-pytest-q-tests` 與 `s21_gate:pytest_full`（linux），兩個集合各新增 5 筆、移除 0 筆。

### 第 4 項驗收

所有命令都在前景執行。command、stdout、stderr 與 exit code 存在 `target/s2-par/S2-PAR-fixlat-4/gates/`。

| 檢查 | 結果 |
|---|---|
| `python3 -m pytest -q tests/test_event_latency_measure.py` | exit 0，24 passed |
| `python3 -m tools.contracts check` | exit 0，`ok=true`、findings 0 |
| `python3 tests/acceptance/regen_lock.py --check` | exit 0，鎖檔與 L1 結構兩項都是 OK |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0 |
| `python3 -m policy_check --repo .` | exit 0，25 pass、0 fail、2 warn。R-19 是 parser advisory；R-22 共 3243 筆懸空引用，本節只新增 1 筆，即上面提到的 `target/` 探針路徑 |
| `cargo test --workspace` | exit 0，1542 passed、0 failed、45 ignored。本項沒有改 Rust，這只是確認 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0 |

本項沒有執行全套 pytest、slice gate、Docker 或出口證據 driver。

---

## 補記：第 6 項接線與整體驗收（2026-11-08）

完整的接線核對、A/B 六次實跑表、逐筆時間線、整體驗收與 SPEC-GAPS，記在 `s2-par-fixlat-rust.md` 的同名補記節。本節只摘錄 Python daemon 這一側。上面第 2、4 項的內容保留原文。

- **接線（程式碼確認）：**原測試直接呼叫工作樹 `scripts/rust/event_latency_measure.py` 的 `_sample_cancel()`，第 4 項的收尾等待已在其中，所以沒有修改測試檔，也沒有新增鎖檔登記。
- **本輪三次（已實測，HEAD `38a93d97`，unset 三個 A/B 選擇變數）：**外部 loadavg 1m 分別為 5.51–11.55、5.63–8.46、8.96–16.83；command_cancel 200／200 PASS，max 分別為 1.240、1.644、1.358 ms，`errors=[]`；stop_request 20／20 PASS；三次 pytest 都是 exit 0。證據在 `target/s2-par/S2-PAR-fixlat-6/python-run-{1,2,3}/`，全部是 `quick_check_only`。
- **逐筆時間線（已實測，第 4 項修改前工具的探針 `probe-python-1`）：**199 組相鄰樣本中，前一筆被取消的 `sleep 0.3` 都已送出 TX，prompt 在 TX 後 0.297–0.326 s 回來（mono）。下一筆的 `started_at` 全部晚於這個 prompt，差距 0.5–52.9 ms；下一筆的 TX 也全部晚於它，差距 0.8–53.3 ms（mono）。Python worker 會等前景 op 結束才取下一筆，下一筆停在 accepted 的時間就是前一筆剩下的收尾時間，不是 worker 沒有取件。
- **基線判讀：**取消回覆早於 worker 收尾是 Python 基線既有的行為（`origin/main:sw_core/arbiter.py:364-380`）。第 4 項已在量測工具中補上收尾等待，判準沒有改變。取消路徑沒有確認出 Python 基線缺陷，因此不提 SPEC-GAP，也不需要讓測試維持失敗。`9d915e84` 原始失敗的成因（前一筆收尾超過 10 s）仍是推斷。
- **同檔另一支測試：**本輪第 1 次整檔實跑時，`test_login_with_delay_and_noise` 在 Python daemon 上以 `POST_LOGIN_CMD_TIMEOUT` 失敗，第 2 次通過。Python 在這個錯誤碼之後不會自動重探（`sw_core/session_manager.py:2112-2136`）。細節記在 `s2-par-fixlat-rust.md`，列為既有碼待辦；不改 `sw_core`，也不改該測試。
