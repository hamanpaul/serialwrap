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
