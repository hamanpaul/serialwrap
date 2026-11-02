# Slice 2.1 擁有者證據手冊（BLOCKED-OWNER 執行程序）

> 對應計畫 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 22 與結尾「BLOCKED-OWNER 總表」；機器可讀清單為
> `tests/acceptance/slice21_bo.yaml`（同序、同 `case_ids`）。gate 報告 `target/slice21-gate/<run_id>/report.json`
> 的 `blocked_owner[]` 每項恆為 `BLOCKED-OWNER`（schema `const`），只能由本手冊程序取得證據、回填並經審查後解除；
> gate 與執行者不得把任何一項標為 PASS。

## 共通規則

- **artifact 身分**：每項證據都必須記錄受測 artifact 的 sha256，且須等於 gate 報告 `identity.cli_sha256`／`identity.daemon_sha256`
  （Linux：`target/x86_64-unknown-linux-gnu/release/serialwrap{,d}`；Windows：`target/x86_64-pc-windows-gnu/release/serialwrap{,d}.exe`）。
  不同 artifact 的證據不得沿用（規格 §0「不得只因版本字串相同就沿用先前 PASS」）。
- **隔離**：一律使用 throwaway daemon（獨立 `SERIALWRAP_RUN_DIR`／`SERIALWRAP_STATE_DIR`／`SERIALWRAP_WAL_DIR`／`SERIALWRAP_CONFIG_DIR`，
  Windows 另設 `SERIALWRAP_TEST_DEVICE_ALLOWLIST`，OD-11），不得停掉或改寫正式 daemon 與人類 minicom／TeraTerm。
- **證據目錄**：`~/b-log/rust-refactor/p5/<BO-ID 小寫>/<YYMMDD-HHMMSS>/`，內含 `evidence.json`（至少 `bo_id`、`artifact_sha256`、
  `host`、`started_at`、`finished_at`、`commands`、`result`、`notes`）與原始 log／報告。查不到的值寫 `unknown`，不得填 0。
- **回填方式**（各項相同）：把證據目錄相對路徑填入 `report.json` 對應 `blocked_owner[].evidence`，並在 PR 描述的 BLOCKED-OWNER
  清單逐項附上路徑與結論；由審查者核對 artifact sha256、命令逐字一致與結果後，才可在 PR 上記錄「已解除」。未回填前不得宣告 Slice 2.1 出口。
- **失敗也要回填**：結果 FAIL 時照實回填並保留樣本，不得改列觀察值或重跑挑樣本。
- **L1.5 A/B 的 bridge 自測報告**：gate 第 9 段只在 `SERIALWRAP_L15_SELFTEST_REPORT` 指向依 Task 8 正式資格判定合格的
  `selftest-report.json`（`python3 -m tools.uart_stdio_bridge selftest …` 產出，tool tree sha256 等於 `APPROVED_REVISION.json`）時才執行；
  未設或不合格時 `l15_ab` 記為 `EVIDENCE_MISSING`（不是 FAIL）。完整命令例：
  `SERIALWRAP_L15_SELFTEST_REPORT=<路徑>/selftest-report.json bash scripts/rust/slice21_gate.sh --run-id <run_id>`。
- **承接率斷路器**：`case_uptake` 由 judge 以 `slice_breaker.evaluate` 判定（快照檔 `tests/acceptance/breaker/slice_2.1.snapshots.ndjson`、
  OD-02 剩餘預估 `stage2_estimate_weeks` 取自 `tests/acceptance/slice21_gate_expect.yaml`）；tripped 或超過 OD-02 → FAIL，
  證據不足（含 `stage2_estimate_weeks` 未填）→ `EVIDENCE_MISSING`。`stage2_estimate_weeks` 由擁有者填值。

### BO-S21-01

**原生 Windows COM handle gate（OD-08；PR 合併前置）**

- 目的：在真實 Windows COM handle 上驗證 `cap_windows_com__*` 全部（獨占 `ERROR_ACCESS_DENIED`、`CancelIoEx` 收斂、部分寫出、conformance）。
- 前置：OD-15 起於擁有者 host 可執行（com0com pair `COM20`／`COM21`；orchestrator 經 WSL interop 代跑）；`s21_gate:windows_cap_windows_com`
  已以原生 Windows 收集登記於 `tests/acceptance/l1_required.lock.json`。
- 命令（WSL，repo 根目錄）：
  ```bash
  SW_TEST_COM_PAIR=COM20:COM21 bash scripts/rust/slice21_gate.sh --with-com-pair --run-id bo01-$(date +%y%m%d-%H%M%S)
  ```
  或只跑本段：`SW_TEST_COM_PAIR=COM20:COM21 bash scripts/rust/win-test.sh --caps windows_com --package sw-pal -- --features test-fakes`
- 預期證據：`target/slice21-gate/<run_id>/windows_cap_windows_com/l1-results.json`（每個凍結 nodeid 恰一列 `passed`）與 `target/win-test-report/<run_id>.json`。
- 回填：gate 報告中 `windows_cap_windows_com.status` 必須為實際 `PASS`／`FAIL`；`EVIDENCE_MISSING` 只在 host 不可用時允許，並在 `evidence` 註明原因。

### BO-S21-02

**Windows 外部 TeraTerm／PuTTY 先開 COM 時的 `DEVICE_STILL_HELD`**

- 目的：確認外部程式先占用 COM 時，`device attach` 回 `{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[],"holder_unknown":true,...}`。
- 前置：Windows 11 擁有者機台；TeraTerm 或 PuTTY；隔離 daemon（`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`）。
- 命令（PowerShell）：
  ```powershell
  # 1. 以 TeraTerm／PuTTY 開啟 COM20 並保持連線
  $env:SERIALWRAP_TEST_DEVICE_ALLOWLIST = "COM20"
  .\serialwrapd.exe            # 另一視窗，隔離目錄依共通規則設定
  .\serialwrap.exe device attach --selector COM20 | Tee-Object attach.json
  .\serialwrap.exe session list | Tee-Object sessions.json
  ```
- 預期證據：`attach.json`（`DEVICE_STILL_HELD`、`pids: []`、`holder_unknown: true`）、`sessions.json`、daemon log。

### BO-S21-03

**Windows USB 拔除時在途 overlapped I/O 完成碼與 drain 3000 ms 收斂**

- 目的：實測 USB-UART 在讀寫進行中被拔除時的完成碼（`ERROR_OPERATION_ABORTED`／`ERROR_DEVICE_NOT_CONNECTED` 等），並確認 drain 於 3000 ms 內收斂、session 轉 `DETACHED`／`RECOVERING`。
- 前置：實體 USB-UART（CH340 或 FTDI）接 DUT；隔離 daemon；可手動拔插。
- 命令：`.\serialwrap.exe cmd submit --selector COM20 --cmd "cat /dev/urandom | head -c 200000 | base64"` 執行中拔除 USB；之後
  `.\serialwrap.exe session list`、`.\serialwrap.exe daemon status`，並保存 daemon log 與 `raw.wal.ndjson`。
- 預期證據：daemon log 中的完成碼、drain 起訖時間（≤3000 ms）、WAL META 與最終 session 狀態。

### BO-S21-04

**實體 USB-UART 的 TIOCEXCL＋flock 與 RTS/CTS 卡住時可取消寫入（`cap_physical_uart__*`）**

- 目的：在 Linux 實體 USB-UART（CH340／FTDI）上驗證 TIOCEXCL＋flock 獨占與 RTS/CTS 卡住時寫入可取消、回報部分寫出。
- 前置：Linux bench；USB-UART 開啟硬體流控並使 CTS 維持未就緒（不接對端或以跳線拉住）。
  **注意**：截至本手冊建立（P5-T22），`crates/sw-pal` 尚無 `cap_physical_uart__*` 測試；須先由 P5 Task 2 後續補齊該能力測試
  （以環境變數指定實體埠、`#[ignore]` 能力標記），再以下列命令執行。
- 命令（測試補齊後）：
  ```bash
  python3 scripts/rust/cargo_l1_results.py --platform linux --caps physical_uart \
    --out ~/b-log/rust-refactor/p5/bo-s21-04/<ts>/l1-results.json -- -p sw-pal --features test-fakes
  ```
- 預期證據：`l1-results.json`（`cap_physical_uart__*` 全部 `passed`、無 skip）。

### BO-S21-05

**外部 minicom 先開埠時 daemon 得 `Held`（Linux 實機）**

- 目的：外部 minicom（普通 open，不 flock、不 TIOCEXCL）先開實體埠時，Rust daemon attach 失敗、`last_error` 為 `ATTACH_FAILED:DeviceHeldError`、
  `device attach` 回 `DEVICE_STILL_HELD` 且 `pids` 含 minicom pid。
- 前置：Linux bench 實體 USB-UART；隔離 daemon，`SERIALWRAP_BY_ID_DIR` 只放該埠 symlink。
- 命令：`minicom -D /dev/serial/by-id/<id>`（另一終端保持）→ 啟動隔離 `serialwrapd` →
  `serialwrap session list > sessions.json`、`serialwrap device attach --selector COM0 > attach.json`。
- 預期證據：`sessions.json`、`attach.json`、`ls -l /proc/<minicom pid>/fd` 輸出。

### BO-S21-06

**Windows `CTRL_BREAK`／`CTRL_CLOSE` 停止與 `windows-task` 監管（Spike 2）**

- 目的：互動 console 下 `CTRL_BREAK`、關閉 console 視窗時 daemon 有序停機（exit 0、WAL close、lock 檔保留）；`windows-task` 監管下啟停。
- 前置：OD-15 先以 P2 Task 18 throwaway 排程工作在擁有者 host 跑（可執行）；正式工作的 Spike 2 證據仍待擁有者。
- 命令：throwaway 排程工作以 `pwsh -File scripts/rust/win/spike2_windows_task.ps1 -Phase <phase> -InstallRoot <Rust artifact 目錄> -OutDir <證據目錄>`
  逐 phase 執行（參數見腳本；P2 Task 18 程序），證據以 `scripts/rust/win/collect_task_evidence.ps1` 收集；互動 console 下以 `Ctrl+Break` 與關閉視窗各停機一次。
- 預期證據：每步 stdout／exit code、daemon log 的停機序、`schtasks /query /tn <task> /v` 輸出；判定以 `python3 scripts/rust/judge_spike2.py` 執行。

### BO-S21-07

**L2：Rust daemon 執行 `serialwrap_regression` F1／F2／F3／F4／F12 的 18 個 case**

- 目的：以 Rust daemon 跑 17 個 `slice_2.1` 分母 case（F12 用 OD-13 修訂 oracle），`f4-interactive-line-cmd-defined` 為提前證據。
- 前置：交 bench 前先以 `scripts/docker/run-l15.sh` 跑 `L1.5-docker`（不計 L2）；TestPilot venv 已安裝 `regression/`；部署 CLI／daemon 為受測 artifact。
- 命令：
  ```bash
  bash scripts/docker/run-l15.sh
  for c in f1-cmdid-survives-timeout f1-limits-queryable f1-near-limit-no-logout f1-newline-rejected f1-too-long-rejected \
           f2-history-bounded-rss f2-queue-full-backpressure f2-recovery-flushes-queue f2-rx-window-crossing-prompt \
           f3-cmd-fail-observable f3-device-error-names-selector f3-fail-error-code f3-log-tail-latest \
           f4-activity-classification f4-background-result-tail-consistent f4-interactive-line-cmd-defined \
           f12-version-reported-consistent f12-wal-path-live; do
    testpilot run serialwrap_regression --case "$c" || true
  done
  ```
- 預期證據：`~/b-log/regression-reports/tp-<ts>/` 每 case 報告；逐案結果彙整為 `case_verdicts.json`（`{case_id: "PASS"|"FAIL"|...}`）供 gate `case_uptake` 使用。

### BO-S21-08

**L3：`realhw` P0 核心集合＋WAL／裝置衝突／Recovery 相關 P1（Linux 與 Windows 各一輪）**

- 目的：`p0-doctor`、`p0-cmd-async`、`p0-clear-reattach`、`p0-selftest`、`p0-wal-live`、`p0-multiopen`、`p1-cmd-serial`、`p1-rst-daemon`、
  `p1-rst-recover`、`p1-hp-cycle`、`p1-hp-reorder` 以 Rust daemon 通過。
- 前置：先以 docker 兩容器拓樸跑 `L1.5-docker`（不計 L3）；實機 bench（Linux 與 Windows 各一）。
- 命令：
  ```bash
  python3 -m realhw --tier p0,p1 --only p0-doctor,p0-cmd-async,p0-clear-reattach,p0-selftest,p0-wal-live,p0-multiopen,p1-cmd-serial,p1-rst-daemon,p1-rst-recover,p1-hp-cycle,p1-hp-reorder \
    --report-dir ~/b-log/rust-refactor/p5/bo-s21-08/<ts>/linux
  ```
  Windows 以同一 `--only` 清單在 Windows 原生 Python 執行，`--report-dir` 改為 `...\windows`。
- 預期證據：兩平台 `realhw` 報告目錄（逐案結果與 deployed_content）。

### BO-S21-09

**雙平台實機「外部燒錄工具接手 → `device attach` 收回」**

- 目的：`device release` 後外部燒錄工具獨占開啟並完成燒錄，再 `device attach` 收回且 session 回到 READY／ATTACHED。
- 前置：Linux 與 Windows 實機各一，具備對應燒錄工具（如 `ocp-mcu-upgrade`）。
- 命令：`serialwrap device release --selector COM0` → 執行燒錄工具（保存其 stdout 與 return code）→ `serialwrap device attach --selector COM0` →
  `serialwrap session list`；Windows 以 `.exe` 同序執行。
- 預期證據：每步輸出、燒錄工具 `Return error code : 0x0`、daemon log、WAL。

### BO-S21-10

**實機 shell／prplOS 板登入→READY 與 reboot quiet 最小防護**

- 目的：L3 P0 reboot 相關核心：登入至 READY；`reboot` 後 quiet 期間 agent 命令回 `AUTOBOOT_QUIET`／`READY_UNCONFIRMED`、wire 零 TX；重新登入後命令可執行。
- 前置：實機 shell 與 prplOS 板各一；帳密以 `env_file` 提供（不得寫入證據）。
- 命令：`python3 -m realhw --tier p0 --only <reboot 相關 P0 case> --report-dir ~/b-log/rust-refactor/p5/bo-s21-10/<ts>`（case 清單以 `python3 -m realhw --list` 篩選 reboot 類）。
- 預期證據：`realhw` 報告、WAL（quiet 期間無 TX 紀錄）。

### BO-S21-11

**同款 CH340（by-id 相同）以 by-path 區分的 COM 指派**

- 目的：兩片 by-id 相同的 CH340 以 `/dev/serial/by-path/` 綁定時 COM 指派確定、restart 後不對調（#100）。
- 前置：兩片同款 CH340；profile 以 by-path 綁定。
- 命令：啟動隔離 daemon → `serialwrap session list > before.json` → `serialwrap daemon stop`、再啟動 → `serialwrap session list > after.json`（重複 5 次）。
- 預期證據：每輪 `session list` 的 COM↔by-path 對應完全一致。

### BO-S21-12

**systemd-user／systemd-system 監管下 Rust `serialwrapd` 啟停（L3 P0）**

- 目的：Rust daemon 在 systemd 監管下 start／stop／restart 與異常結束重啟行為與 Python 一致。
- 前置：OD-15 先在 systemd container 驗證並附 `L1.5-docker` 報告；實機 L3 仍待擁有者。
- 命令：`bash scripts/docker/run-systemd.sh`（container）；實機：`serialwrap setup --user`（systemd-system 為 `serialwrap setup --system --with-sudo`）後
  `serialwrap service status`、`serialwrap service restart`、`serialwrap service stop`、`serialwrap service start`，並 `kill -9 <daemon pid>` 觀察監管重啟（逐步保存）。
- 預期證據：container 報告、實機每步輸出與 `journalctl --user -u serialwrapd` 擷取。

### BO-S21-13

**Windows L1.5（原生 daemon＋com0com 或實體 USB-UART）與實機 A/B（§15.9.2）**

- 目的：`win-com-open-exclusive`、`win-com-txrx-bridge`、`win-com-cancel-close` 三個凍結案例，及 Windows 端以 Rust daemon 跑
  `tests/contract_blackbox/slice21` 全套（含 `test_windows_nocom.py`）。
- 前置：OD-15 起 com0com 部分在擁有者 host 以 `COM20`／`COM21` 可執行；實機 A/B 仍 BLOCKED-OWNER。
- 命令（WSL）：
  ```bash
  pwsh.exe -File scripts/rust/win-blackbox.ps1 -Suite slice21-nocom -Daemon target/x86_64-pc-windows-gnu/release/serialwrapd.exe
  pwsh.exe -File scripts/rust/win-blackbox.ps1 -Suite slice21 -Daemon target/x86_64-pc-windows-gnu/release/serialwrapd.exe
  ```
  三個 `win-com-*` 案例依 P2 `func-test/runner.py --mode bridge --case <id>` 於 Windows 原生執行。
  **注意**：截至 P5-T22，`scripts/rust/win-blackbox.ps1` 仍是 P3 介面（`-RustExe -RepoSnapshot -OutDir`），尚未支援 `-Suite slice21-nocom`／`-Daemon`；
  在該參數補齊前，可於 Windows 原生 Python 3.12 直接執行
  `py -3.12 -m pytest -q tests\contract_blackbox\slice21\test_windows_nocom.py`（`SERIALWRAP_DAEMON_EXE` 指向 Rust `serialwrapd.exe`）。
- 預期證據：win-blackbox 報告（逐案 passed）、三個案例的 `comparison.json`。

### BO-S21-14

**Spike 3：第三方 EDR 開啟下 Rust `WalFs` 的寫入／rotate／unlink／外部 reader，與 Stage 0 Python 並列；Windows `wal_s1w` 正式量測**

- 目的：`win-wal-av-rotate`、`win-wal-av-unlink`、`win-wal-av-reader-handle` 以 Rust `WalFs` 重跑並與 Stage 0 Python 結果並列；
  在 Windows 原生、EDR 開啟下量測 §1.2 WAL 指標（Windows 門檻 p50 ≤0.03 ms、p95 ≤0.08 ms、p99 ≤0.20 ms、rotate 最大停頓 ≤15 ms）。
- 前置：擁有者 Windows 11 代表機台（HVCI 開、第三方企業 EDR 開、Defender 關）；記錄 EDR 產品與狀態。
- 命令：
  ```powershell
  # Python 基線（Stage 0）
  py -3.12 scripts\stage0\spike3_win_wal.py --case win-wal-av-rotate --evidence-dir <dir>\python --edr-product "<產品>" --edr-status on
  # Rust WAL 效能（§1.2；只量測、不改 WAL 行為）
  cargo run --release -p sw-core --example wal_s1w -- --out <dir>\rust\wal_s1w.json --wal-root <dir>\rust\walroot
  ```
  （`win-wal-av-unlink`／`win-wal-av-reader-handle` 同樣各跑一次。）Rust `WalFs` 版的 Spike 3 情境工具尚未提供，需另行補齊後依同一 case 清單執行並列。
- 預期證據：Python `spike3` 證據、Rust `wal_s1w.json`（`verdict`、全部 `rotate_pauses_ns` 樣本、`env.endpoint_av` 手動補記）。超標即 FAIL，不得改列觀察值。

### BO-S21-15

**藍牙 SPP 虛擬 COM 實際存在時的 SERIALCOMM 排除**

- 目的：配對藍牙 SPP 裝置產生的虛擬 COM（BTHENUM／`bthmodem`）不出現在 daemon 的裝置清單。
- 前置：已配對藍牙 SPP 裝置的 Windows 機台。
- 命令（PowerShell）：`reg query HKLM\HARDWARE\DEVICEMAP\SERIALCOMM > serialcomm.txt`；啟動隔離 daemon 後 `.\serialwrap.exe session list > sessions.json`、
  `.\serialwrap.exe daemon status > status.json`。
- 預期證據：`serialcomm.txt`（含藍牙 COM）、`sessions.json`（不含該 COM）、`status.json`。

### BO-S21-16

**Windows L2：EDR 開啟下以實體 UART 驗證 F12 WAL 進展 oracle（`win-wal-f12-progress`）**

- 目的：EDR 開啟時實體 UART 流量下 WAL 持續前進（OD-13 修訂 oracle：CRC／len 正確、seq 單調、無長時間停滯）。
- 前置：擁有者 Windows 機台＋實體 USB-UART＋DUT；EDR 開啟。
- 命令：`py -3.12 scripts\stage0\spike3_win_wal.py --case win-wal-f12-progress --evidence-dir <dir> --edr-product "<產品>" --edr-status on`，
  並以 Rust daemon 執行 `testpilot run serialwrap_regression --case f12-wal-path-live`。
- 預期證據：spike3 證據、TestPilot 報告與 WAL 檔。

## 能力集合 section（P5 第 6 輪 SPEC-GAP #2，編排者裁決；P5 第 7 輪）

gate 為 OD-55 的 linux 能力集合各設一個 section（`linux_cap_<cap>`）。gate 先探測能力，具備才實跑；否則記 EVIDENCE_MISSING 並附
探測證據、所需能力與處置，**不得記 PASS**（judge `CAPABILITY_SECTIONS`；即使 l1 結果全 passed 也一樣，例如
`cap_sys_admin__tiocexcl_bypass_documented` 在無權限時 early return 仍回 ok）。

| section | 集合 | 所需能力 | 處置 | 可行的實跑方式 |
|---|---|---|---|---|
| `linux_cap_sys_admin` | `s21_gate:cap_sys_admin` | root（euid 0）且 `CapEff` 含 CAP_SYS_ADMIN | BLOCKED-OWNER | 以 root 於可丟棄容器（例：`docker run --cap-add SYS_ADMIN`，掛載 repo）跑 gate（須設 `SW_GATE_L1_USER` 讓 linux_l1／linux_cap_posix_pty 降權為非 root，F108），或單獨執行 `python3 scripts/rust/cargo_l1_results.py --platform linux --out <run>/linux_cap_sys_admin/l1-results.json --caps-only --caps sys_admin -- --workspace --features test-fakes -- --ignored cap_sys_admin__`（`--caps-only`：只跑能力列；root 下 sw-pal 預設測試數支必紅，不得一併執行。gate 以 `slice21_gate_judge.py caps-plan` 由鎖 selector 推導，OD-53） |
| `linux_cap_supervisor_runtime` | `s21_gate:cap_supervisor_runtime` | `systemctl --user is-system-running` 為 running／degraded | BLOCKED-OWNER | 於隔離的 systemd 容器（`tools/docker/systemd.Dockerfile`，OD-15）以一般使用者登入 session 跑 gate，或單獨執行 `... --out <run>/linux_cap_supervisor_runtime/l1-results.json --caps-only --caps supervisor_runtime -- --workspace --features test-fakes -- --ignored cap_supervisor_runtime__`；測試會建立並清除暫時 user unit |
| `linux_cap_windows_native` | `s21_gate:cap_windows_native` | 原生 Windows | LOCAL-WINDOWS-TODO | linux 列的測試本體需 PowerShell／Job Object，linux 主機無法滿足；待擁有者裁決改由 Windows 原生集合承擔 |
| `linux_cap_interactive_desktop` | `s21_gate:cap_interactive_desktop` | Windows 互動 console | LOCAL-WINDOWS-TODO | 非 Windows 版刻意 panic；同上 |

單獨執行後若要併入 gate 判定，以 `slice21_gate_judge.py record-section --name linux_cap_<cap> --required-set s21_gate:cap_<cap>
--l1-results <cap>/l1-results.json --exit <rc> --capability-probe "<cap>=available:<證據>"` 寫入該 run 的 section 片段後重新 `assemble`。
