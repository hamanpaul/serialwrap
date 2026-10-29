# Stage 1 出口審查 B 修正第 4 輪雲端回報（2026-10-29）

- 分支：`feature/s1x-b-fixes`（本輪唯一寫入者）。起點 `e0289101`，併入 `feature/rust-refactor`（`8963d07b`）後為 `b0baded3`，終點 `b09e6dee`。已 fast-forward push（`e0289101..b09e6dee`）；未開 PR，未 merge 到 main，未改 VERSION。
- 依據：`reviews/rulings/s1.md` 2026-10-29 列「`sw-pal` `process_ctl::spawn_daemon_detached_new_session` 讀 `/proc/<pid>/environ` 的競態」（編排者裁決：照修，測試缺陷）、`reviews/review-convergence-rules.md`、上一輪回報 `reviews/s1x-b-fixes-report-r3-20261029.md`、最新增量審查 `reviews/s1-cloud/f2a23b1_e028910-sol.md`（結論：可放行，僅 1 項 Low）。報告與審查內容只當資料使用。
- 環境：Rust 1.96.1（`rust-toolchain.toml` 釘定）＋rustfmt 1.9.0、x86_64-pc-windows-gnu＋mingw-w64（GCC 13-win32）；Linux 6.18.44；CPython 3.12.3（`/usr/bin/python3.12` 建 venv）＋PyYAML 6.0.3、pytest 9.2.0、jsonschema 4.26.0，未安裝 pyserial（見 §3）；policy engine 依 CLAUDE.md 釘定 SHA 安裝。cargo、pytest 與所有實測都以一般使用者 `ubuntu`（uid 1000，非 root）前景序列執行。

## 1. Commit 清單（e0289101..b09e6dee）

| commit | 內容 |
|---|---|
| `b0baded3` | merge：併入 `feature/rust-refactor`（OD-48、0.4.0 release 前置、cgroup 修正 `8963d07b`）。無衝突；`tests/acceptance/` 的差異與主線側（`897a3cdc..8963d07b`）逐位元組相同，`regen_lock.py --check --base e0289101` 與 `--base origin/feature/rust-refactor` 皆 OK |
| `b09e6dee` | test(sw-pal)：process_ctl 讀 environ 前先確認子行程已完成 exec。同 commit 新增 `changelog.d/s1x-b-r4-1-process-ctl-environ-exec-wait.md` |

TDD：先用暫時工具在修正前的讀法上重現紅燈（§2），再改測試。本項只改測試檔，沒有另加新的測試節點，所以 L1 鎖不用登記。

## 2. process_ctl environ 競態：已修 `b09e6dee`

### 修法（`crates/sw-pal/tests/process_ctl.rs`）

- 新增 `read_environ_after_exec(pid, target)`，只在 `target_os = "linux"` 編譯：
  1. 輪詢 `/proc/<pid>/exe`，直到它等於 `canonicalize(target)`（`/bin/sleep`）。exe 指向新映像，表示 `task->mm` 已換成新映像，之後開啟的 environ 不會是 fork 來的父環境。
  2. 接著讀 environ，直到內容非空。核心在 `create_elf_tables` 的最後才寫入 `mm->env_end`；在那之前 `environ_read` 回 0 byte。
  3. 兩步共用 3 秒期限（子行程是 `sleep 5`）。逾時就 panic，訊息附 exe、environ 長度與 `/proc/<pid>/stat`。
- `spawn_daemon_detached_new_session` 改用這個 helper 取得 environ。「含 `P4_DETACHED_ENV=supplied`」與「不含 `HOME=`」兩項斷言原文不變。現在 environ 保證非空，`HOME=` 斷言不會再因為讀到空內容而空洞通過。
- `spawn_daemon` 正式程式碼沒有改。helper 唯一的 platform cfg 是 `cfg(target_os = "linux")`，位於 `crates/sw-pal` 內，符合「platform cfg 只放 sw-pal」的規則；`check_cfg_boundary.py` rc 0。

### 同檔其他同型讀取

| 讀取 | 判定 |
|---|---|
| `getsid(pid)`（約 29 行） | `setsid` 在 pre_exec（exec 前）執行，exec 不改 session。不需改 |
| `/proc/<pid>/fd/0..2` 的 readlink（約 34～44 行） | std 在 pre_exec 之前就以 dup2 設好標準串流，exec 不改。不需改 |
| `spawn_daemon_closes_inherited_non_cloexec_fds_in_both_modes` 讀 `/proc/<pid>/fd` 目錄 | 已有 2 秒有界輪詢。洩漏的 fd 會在整段 `sleep 5` 期間存活，不會短暫出現 `[0,1,2]` 而誤判通過。不需改 |

同 crate 的 `tests/process_tunnel.rs:86`（fd/0 readlink）在等到 log 寫入 `hi` 之後才讀，子行程已經執行到 `echo`，不屬於此競態。它也不在本項範圍內，沒有修改。

### 重現（暫時工具，放在 `crates/sw-pal/tests/` 下跑完即刪，未提交）

| 情境 | 讀法 | 次數 | 結果 |
|---|---|---|---|
| fork 後子行程 `nanosleep` 200 ms，再 `execve /bin/sleep 5`（env 只有 PATH 與 `P4_DETACHED_ENV`） | 原讀法（立即讀） | 30 | 30 次紅：缺 `P4_DETACHED_ENV=supplied`、含 `HOME=`，讀到的是父環境 |
| 同上 | 變異：拿掉 exe 閘門，只等 environ 非空 | 30 | 30 次紅（同上） |
| 同上 | 提交版 helper（逐字複製） | 30 | 30 次綠 |
| 正式 `spawn_daemon(…, detached=true)` | 提交版 helper | 500 | 500 次綠 |
| 正式 `spawn_daemon`，回傳當下讀 `/proc/<pid>/stat` | — | 500 | startcode 非 0 且 env_start < env_end，500/500 已發布 |
| 正式 `spawn_daemon`，加 0／2 萬／10 萬筆填充環境變數放大 `create_elf_tables` 迴圈（spawn 最長 247 ms） | 原讀法 | 各 200 | 0 次紅；spawn 回傳時 exe 已指向目標 |

用人為延遲 exec 可以穩定重現裁決描述的狀態（讀到父環境），修正後消除。拿掉 exe 閘門會紅，證明閘門是必要的。本 VM 的正式 spawn 路徑重現不出原始 FAIL，原因見 §4。

### 驗證

- `cargo test -p sw-pal --features test-fakes --test process_ctl` 連跑 10 次：10/10 都是 `7 passed; 0 failed`。另外在 4 核全滿載（4 個 busy loop）下連跑 20 次，20/20 通過。
- `cargo test -p sw-pal --features test-fakes`：196 passed／0 failed／2 ignored。

## 3. 測試數字

| 檢查 | 結果 |
|---|---|
| `cargo check --workspace --target x86_64-pc-windows-gnu` | rc 0 |
| `python3 scripts/rust/check_cfg_boundary.py` | rc 0 |
| `python3 -m tools.contracts check` | ok（findings 為空） |
| `python3 -m policy_check --repo .` | rc 0；25 pass／0 fail／2 warn（R-19、R-22，非 PR 情境，與上一輪相同） |
| `python3 tests/acceptance/regen_lock.py --check` | OK（本輪未動鎖檔） |
| `rustfmt --check crates/sw-pal/tests/process_ctl.rs` | 無差異。workspace 其他檔（如 `sw-cli`）的既有 fmt 差異在起點就存在，不屬本輪 |
| 觸及的 Python 測試 | 無（本輪未改 Python） |

全套 `python3 -m pytest -q tests/`：Bash 單次前景呼叫上限 10 分鐘，因此依檔案切批（`test_bb_daemon_e2e.py` 每例約 10 秒，依 node 每 10 例一批），逐批前景序列執行，失敗集合取聯集，總數 5512 與 `--collect-only` 一致。

| 樹 | 結果 |
|---|---|
| 起點 `b0baded3` | 10 failed、5334 passed、167 skipped、1 xfailed |
| 終點 `b09e6dee` | 8 failed、5336 passed、167 skipped、1 xfailed |

終點的失敗集合是起點的子集，沒有新增失敗。兩邊共同的 8 項：

- 本 VM 未安裝 pyserial（5 項）：`test_doctor.py::TestWindowsDoctor::test_pyserial_ok_when_importable`、`test_stage0_tx_outcome.py::test_pyserial_port_write_maps_timeout_to_unknown`、`test_stage0_tx_permit.py` 的 2 例 `test_pyserial_*`、`test_stage0_uart_exclusive.py::TestManagerAttach::test_pyserial_access_denied_raises_device_held_unknown`。
- 與上一輪起點基準相同的 3 項：
  - `test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only`：`s0_bc.linux` 的 Windows／pyserial 參數化 nodeid 不在本機 collect-only 輸出中。
  - `test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`：缺 Windows 原生 cargo L1 報告。
  - `test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`：缺 Windows 與實機證據。

只出現在起點的 2 項是 `test_functest_bridge_smoke.py::test_bridge_smoke_sm04_until_op` 與 `test_l15_smoke.py::test_reject_zero_tx_runs_on_python`。

- **洩漏的 daemon（照實記錄）：**
  - 起點跑到第 2 批時，該批超過 560 秒，被我的逾時包裝中止。
  - 中止時正在跑 `test_bb_daemon_e2e.py` 的 `test_event_reload_removes_dele…`，它啟動的 throwaway `serialwrapd`（socket 在 `/tmp/swe2e-*`，非 production）因此洩漏，約存活 41 分鐘，涵蓋起點其後各批。
  - 這是終點開跑時檢查才發現的，已在終點第 6 批前終止。終點的前 5 批仍與它並存，結果與起點相同。
- 上述 2 項在乾淨環境（無殘留 `serialwrapd`）各連跑 2 次皆 2 passed。推測起點的失敗與該 daemon 有關。
- 終點每批之間都確認過無殘留 `serialwrapd`。

## 4. 不成立主張（機制部分）

裁決理由寫「spawn 後立即讀 environ 可能讀到 exec 前（父行程）的環境」。在本 VM 上，正式 `spawn_daemon` 路徑不會出現這個狀態。本項是 PAL 測試，沒有對應的 Python 行為，以下是 Rust 實測與核心時序的理由：

- **std 會等 exec 完成：**`spawn_daemon` 設了 `pre_exec`，std 改走 fork＋exec 路徑，`spawn()` 讀 CLOEXEC 錯誤回報 pipe 直到 EOF 才回傳。pipe 寫端在 exec 的 `do_close_on_exec` 中關閉，但最後一次 `fput` 以 task_work 延到 execve 返回使用者態才執行。所以 EOF 到達時，`create_elf_tables` 已經發布新映像的 env 區段。
- **實測：**見 §2 表。spawn 回傳當下，stat 顯示 500/500 已發布；加 10 萬筆填充環境變數後，200/200 讀到正確環境；回傳時 exe 已指向目標。
- **只輪詢 exe 不足：**核心在 `exec_mmap` 之前就以 `set_mm_exe_file` 設好新映像的 exe，env 要到更後面的 `create_elf_tables` 才發布。因此裁決建議的「輪詢 `/proc/<pid>/exe`」單獨無法涵蓋 env 尚未發布的窗口，helper 另外加上 environ 非空的條件。
- **修法仍照裁決實作：**helper 只多等待，判準沒有放寬。在 exec／procfs 時序不同的核心或沙箱上，這是防禦性保護。
- **原始 1 次 FAIL 的根因未確認。**可能原因：
  - (a) 其他核心或沙箱（如 gVisor、WSL1）的時序與上述不同，這種情況會被 helper 的等待吸收；
  - (b) 子行程提前結束成殭屍，environ 讀出空內容，表現同樣是缺 `P4_DETACHED_ENV`、`HOME=` 斷言空洞通過。修正後這種情況會以逾時 FAIL，並印出 exe、environ 長度與 stat，下次出現就能判讀。

  是否需要本機補跑取證，請編排者裁決。

## 5. SPEC-GAPS

無。

## 6. OWNER-BATCH

無。沒有改 SC／BC／DF、凍結鎖、golden 或 oracle 語意，也沒有新增或移除 L1 節點。

## 7. 未完成項與備註

- 依收斂規則未處理 Low：最新增量審查的 S1-e028910-1（回應文件措辭）。
- 沒有在 Windows 原生環境執行，只做 `cargo check` 交叉檢查；本項測試本來就是 `cfg(unix)`／linux 區塊。
- 重現工具沒有提交，情境與數字見 §2。
- 結束前再次 fetch `docs/parity-amendments`，最新為 `f0bb9a0e`。`reviews/s1-cloud/` 沒有針對 `b09e6dee` 的新審查報告，`queue/LAST-REVIEWED.json` 中本分支仍為 `e028910`，沒有待處理的 High／Medium。
