# install.sh bundle 化第 2 輪雲端回報（合入 OD-48＋release 前置、解 Docker 腳本衝突，2026-10-29）

- **依據**：
  - `rulings/s1.md` 的以下各列：OD-50、「release 前置第 2 輪 OWNER-BATCH #2」原列與「承接改派：改由 install.sh 第 2 輪處理」列、編排者追加指示（20:50）指定的「Docker 升級驗證 on-demand 預熱競態」列。
  - `review-convergence-rules.md`。
  - 審查報告：`s1-cloud/62ffec7_b84ee5b-sol.md`（起點時最新），以及本輪期間新出的 `b84ee5b_407ed7e-sol.md`、`407ed7e_ed46551-sol.md`。
- **分支**：`feature/install-sh-bundle`，`b84ee5b..607ea76` 皆以 fast-forward 推送，沒有被拒，未用 `claude/install-sh-bundle-r2`。
  - 未 merge 到 main、未開 PR、未建 tag、未改 `VERSION`。
  - 未接觸 production（本 VM 沒有）。
- **環境**：
  - 雲端 VM：Ubuntu 24.04、4 cores、15 GB。
  - Rust：1.96.1（`rust-toolchain.toml` 釘定，含 rustfmt、`x86_64-pc-windows-gnu` 與 mingw-w64）；另裝 stable 1.99.1 為預設工具鏈，原因見測試數字。
  - Python：`/usr/bin/python3.12`（3.12.3）venv，裝 PyYAML 6.0.3、pytest 9.2.0、jsonschema 4.26.0、setuptools／wheel，以及 policy engine `2dcf2f8`。
  - clone 已 unshallow。
  - 所有測試與 Docker 實跑都以一般使用者 `dev`（uid 30033，docker 群組）執行，前景等待結束。
  - dockerd 29.9.2 在 VM 內啟動。VM 期間重開一次，重開後重啟 dockerd。

## Commit 清單（first-parent）

| commit | 內容 |
|---|---|
| `a9b8a38` merge: 併入 origin/feature/od48-legacy-handoff（2179a83）並解 Docker 升級腳本衝突 | 三檔衝突解法見下；新增 `test_install_sh_scenario_rejects_systemd_system_mode`；`REQUIRED_CHECKS` 斷言由比長度改為逐項比 tuple |
| `e1c5365` merge: 併入 origin/feature/release-0.4.0-prep（2ddf90e） | 唯一衝突是 release 計畫 §2 表格 |
| `407ed7e` fix(docker): 升級驗證以 /usr/bin/python3.12 build venv 建 bundle 並檢查 pyvenv.cfg | OWNER-BATCH #2；7 個先紅回歸；§4.1 文件；`changelog.d/od-50-upgrade-docker-system-python.md` |
| `ed46551` test(docker): 保存 install.sh 第 2 輪三模式與 install-sh 升級 Docker 證據（407ed7e） | `evidence/stage1/upgrade-docker/install-sh-407ed7e/` |
| `a20de1d` fix(docker): 升級驗證在 daemon stop 後等舊行程樹與 socket 消失才續行 | 追加指示：on-demand 預熱競態；13 個先紅回歸；§4.1；`changelog.d/od-50-upgrade-docker-stop-wait.md` |
| `607ea76` test(docker): 保存預熱競態修正後的升級 Docker 證據（a20de1d，on-demand 連跑 3 次） | `evidence/stage1/upgrade-docker/install-sh-a20de1d/` |

每個 commit 都帶三行 trailer：Copilot、`Review-pending: 非實作者增量單審`、Claude。

## 衝突解法

**`a9b8a38`（OD-48）**：兩邊的檢查全部保留，沒有刪除任何檢查 id，也沒有放寬判定。

- `upgrade_checks.py`：
  - 同時保留 OD-48 的 `REQUIRED_CHECKS`（19 項，含 `legacy_handoff_direct_setup`）與 `REQUIRED_CHECKS_SYSTEMD_SYSTEM`（13 項）。
  - 同時保留 OD-50 的 `REQUIRED_INSTALL_SH_CHECKS`（12 項）、`PIPX_UNINSTALL_HINT` 與 `judge_install_sh`。
  - `overall_verdict` 的 `required` 改為 `Iterable[str]`，可用位置或關鍵字傳入，兩邊的呼叫端都相容。
- `run_upgrade_container.py`：
  - `Driver` 同時帶入兩邊的欄位：install-sh 的 `scenario`／`installer`／`bundle_tarball`，以及 OD-48 的模式旗標（含 `--system`）與 `setup_args`。
  - 報告的 `required` 與 `scope` 依情境選定：
    - install-sh：`REQUIRED_INSTALL_SH_CHECKS`，scope 為 `install_sh`。
    - systemd-system：`REQUIRED_CHECKS_SYSTEMD_SYSTEM`。
    - 其餘：`REQUIRED_CHECKS`，scope 為 `full`。
- `run-upgrade.sh`：
  - 用法字串同時列出 `systemd-system` 與 `--scenario`。
  - `--mode all` 仍只含 systemd-user＋on-demand，這是 OD-48 既有測試的約束。
  - 合併後出現新組合 `--scenario install-sh --mode systemd-system`，runner 與 driver 兩端都以 rc=2 拒絕。新測試在任一端拿掉守門時都會紅。
- 語意衝突：install-sh 側測試原本的 `len(REQUIRED_CHECKS) == 18` 在合入 OD-48 後變成 19 項。我改為逐項釘住 19 個 id 的 tuple 相等，比單純比長度更嚴。

**`e1c5365`（release 前置）**：§2 表格的處理如下。

- Windows bundle 列採用 release 前置版。
- wheel 與 install.sh 列保留 OD-50 版。
- 合併後 `changelog.d` 共 225 個 fragment。我在暫存複本試跑 `collate --version 0.4.0`，可以成功收斂；試跑結果未提交。

## 各裁決處理

- **release 前置第 2 輪 OWNER-BATCH #2**：照修（`407ed7e`），裁決內容沒有變。
  - 未給 `--bundle` 時，先確認 `/usr/bin/python3.12`：可執行、realpath 在 `/usr/bin`、版本等於 `_meta.json` 的 3.12.3。
  - 任一不符就以 rc=3 結束，而且在任何建置、下載或 docker 動作之前結束，不回退使用其他 python。
  - build venv 的釘版與 workflow 相同：`pyyaml==6.0.3`、`jsonschema==4.10.3`。
  - 確認 `sys._base_executable` 位於 `/usr/bin`。
  - 自建的 bundle 與 `--bundle` 提供的 bundle，`py/pyvenv.cfg` 都必須含 `home = /usr/bin` 與 `include-system-site-packages = false`。
  - 7 個回歸測試：實作前全紅，實作後全綠。
- **Docker 升級驗證 on-demand 預熱競態**（追加指示）：照修（`a20de1d`），細節見下節。
- **OD-48 系列**（OD-48 本體與修訂、OWNER-BATCH #1～#3、SPEC-GAP #5／#10、S1X-c-1／c-2、OD-51）：皆由 OD-48 分支實作，且已通過非實作者審查。本輪只負責合入。
- **`process_ctl` environ 競態列**：交付點為「下一個 Stage 1 修正輪」，不在追加指示的範圍內，本輪未處理。
- **審查報告**：
  - `62ffec7_b84ee5b`：NO FINDINGS。
  - `b84ee5b_407ed7e`：可放行。
  - `407ed7e_ed46551`：可放行。
  - 結束前 fetch（`93bb8a8`）又看到兩份：`b84ee5b_ed46551`（可放行）、`ed46551_607ea76`（可放行）。
  - 五份都沒有 High／Medium。
  - 以下 Low 依收斂規則不修，請編排者記入 `low-backlog.md`：
    - S1-407ed7e-1：README 殘留 TODO(OD-48)。
    - S1-ed46551-1：dry-run 在有 `--bundle` 時仍印出 build venv 幾行。
    - S1-ed46551-2：版本不符與 `_base_executable` 兩道 fail-closed 沒有回歸。
    - S1-ed46551-3：`require_system_python` 未檢查 venv／ensurepip 是否可用。
    - S1-607ea76-1：降級逾時時，`pipx` 與 0.3.2 setup 仍會執行，只有 start 被代位。判定仍為 FAIL，不會產生假 PASS。另外，§4.1「逾時略過後續的 start／setup」一句對降級段不精確。
    - S1-607ea76-2：§4.1「由 `install-sh-407ed7e` 取代」語意顛倒，原意是 `install-sh-407ed7e` 被本輪取代。
    - S1-607ea76-3：`_start_ticks` 與兩處 cmdline 讀取仍寫死 `/proc`，正式路徑行為不變。
    - S1-607ea76-4：計時案例在 Windows 可能因 rmtree 撞上開檔而 flake（推論，未實跑）。
  - 既有碼待辦（不計入結論，本輪未修）：
    - wheelhouse 的 PyYAML 下載跟隨主機 python3 的標籤。
    - 報告沒有記錄 driver 的 sha256。

## 追加指示：Docker on-demand 預熱競態（`a20de1d`）

**根因**：0.3.2 的 `daemon.stop` 會先回應，之後才依序關 server（刪 socket）、`service.stop()`、放鎖。

- driver 在 `phase_seed` 的 on-demand 分支 `daemon stop` 後立即 `start_daemon`。舊行程還沒結束時，0.3.2 的 `daemon start` 會從 `/proc` 掃到它（`_find_conflicting_daemon`），回 `DAEMON_ALREADY_RUNNING_ELSEWHERE`，之後就沒有 daemon。
- 降級段與重新升級段雖然有 `wait_no_daemon()`，但只等實例數歸零，不看 socket，也不看子孫行程。

**修法**（判定與必要檢查集合不變）：

- `upgrade_checks` 新增三個函式：
  - `daemon_process_tree`：列出 daemon 行程與其子孫，記錄各自的啟動時間。
  - `daemon_socket_paths`：從 argv 的 `--socket` 與 config.yaml 的 `socket_path` 取得 socket 路徑。
  - `wait_daemon_gone`：有界輪詢。
    - gone 的條件：停止前快照裡的行程都已結束、沒有任何 serialwrapd 實例、socket 檔都已移除。
    - PID 重用以啟動時間辨識；zombie 視為已結束。
    - 逾時回 `gone=False`，並列出殘留項。
- driver 新增 `stop_daemon`，在 stop 後等待，上限 `STOP_WAIT_S=60` 秒，等待結果寫進 `steps.ndjson`。
  - 套用到五處：基線預熱、停舊後 setup、install.sh 前、手動降級、重新升級。
  - 等待結果寫進該步驟判定列的 `detail.stop_wait`。
  - 逾時時略過後續的 start／setup（以 rc=124 代位），讓既有判定照常判 FAIL。
  - `ok` 條件不變；`old_gone` 的條件由「無實例」改為更嚴的「行程樹＋socket＋無實例」。
- 掃描改用 `Driver.proc_root`，正式路徑仍是 `/proc`，供測試替身注入。
- systemd-system 的前景暖機沒有改。那一步以同步 `timeout -s INT` 跑到行程結束才返回，之後原本就有 `wait_no_daemon()`，不屬於「stop 後立即 start」。

**先紅後綠**：實作前 13 項全紅，driver 層的紅燈就是競態本身。

- 以假 `/proc` 加上「stop 後延遲 0.4 秒才退出」的舊 daemon 替身重現：五處都在舊 daemon 未結束時就呼叫了 start／setup／install.sh。
- 回歸內容：
  - 純函式 5 項。
  - driver 五處各驗「只在舊 daemon 結束後才呼叫，並記錄 stop_wait」。
  - 三處各驗「舊 daemon 永不退出時判 FAIL 並記錄殘留」。
- 修後連跑 5 次，皆 13/13 passed。

**Docker 實跑**（bundle git revision `a20de1d`）：

- on-demand 連跑 3 次皆 **19/19 PASS**：
  - `seed_user_data` 的 `stop_wait.waited_ms` 三次都是 100。0.3.2 `daemon stop` 回應後，舊行程約 0.1 秒才結束，正是原本的競態窗口。
  - 降級等待約 300 ms，重新升級等待約 200～300 ms。
  - 取樣 1438／1448／1447 次，0 次重疊。
- 同一 bundle 下其他情境的結果：
  - systemd-user：19/19 PASS。
  - systemd-system：13/13 PASS。
  - install-sh 兩模式：各 12/12 PASS。
  - 全部 0 次重疊。
- 證據在 `evidence/stage1/upgrade-docker/install-sh-a20de1d/`：7 份報告、`SHA256SUMS`、`bundle-pyvenv.txt`。
- 本機 Docker 的連跑 3 次仍可由編排者覆核，但不再列為 LOCAL 待辦，因為雲端已實跑。

## 測試數字

| 項目 | 起點 `b84ee5b` | 中途 `ed46551` | 最終 `607ea76` |
|---|---|---|---|
| `python3 -m pytest -q tests/`（同一 worktree、同一命令） | 20 failed, 5322 passed, 164 skipped, 1 xfailed（34:48） | 12 failed, 5382 passed, 164 skipped, 1 xfailed（33:08） | **10 failed, 5397 passed, 164 skipped, 1 xfailed（35:25）** |

**起點 20 failed 的組成**：

- 10 項 `test_rust_dep_direction.py`：dev 的 rustup 沒有預設工具鏈，屬環境因素。補裝 stable 後在起點重跑為 11 passed。
- 其餘 10 項與上輪回報相同，也都是環境因素：
  - 缺 pyserial：5 項。
  - docker 網路拓撲：2 項。
  - Windows 原生 cargo：1 項。
  - P4 證據：1 項。
  - L1 lock：1 項。

**中途與最終的對照**：

- 中途比起點多出 2 項時序失敗：
  - `test_rpc_blocking_offload` 的 0.62s>0.5s 門檻。
  - `test_force_spawn_does_not_bypass_uart_lock`。
- 這兩項的處理：起點到中途之間 `sw_core` 與這兩個測試檔都沒有變動。我同範圍序列重跑兩次，兩項都通過，失敗集合也和全套時不同。
- **最終失敗集合與起點那 10 項完全相同，沒有新增。**

**其他驗證**：

- 兩個 Docker 實跑 pytest 包裝，在 `ed46551` 以 `-rs` 單獨跑：2 passed，不是 skip。
- Cargo：只有 `a9b8a38` 含 Rust 變動。
  - `cargo test -p sw-cli -p sw-pal --features test-fakes`：59 個 binary，814 passed、0 failed、5 ignored。
  - `cargo check --workspace --target x86_64-pc-windows-gnu` 通過。
  - 其餘 commit 都沒有動 Rust。
- 每組 commit 都跑以下檢查，全部 rc=0：
  - `check_cfg_boundary.py`
  - `tools.contracts check`
  - `regen_lock.py --check`
  - `policy_check --repo .`（R-19、R-22 為 warn，與起點相同）
  - `git diff --check`
  - `a20de1d` 起初觸發 R-21 structural（替身裡有 home 路徑樣式），改用 `/opt/u/…` 後才提交。
- 觸及的 Python 測試：`test_docker_upgrade.py`、`test_install_sh.py`、`test_install_docs.py`、`test_release_workflow.py`，不含兩個 Docker 實跑，最終 136 passed。
- 證據守門測試：31 passed。
- 凍結鎖：沒有改動。新增的都是 pytest 節點，不屬任何 L1 set。

## Docker 實跑（`407ed7e`，修正前）

- 環境：VM 內 dockerd、`--network none`、apt 原 HTTP 來源，沒有用 scratch 複本。
- 結果：
  - systemd-user：19/19 PASS。
  - on-demand：19/19 PASS。
  - systemd-system：13/13 PASS。
  - install-sh 兩模式：各 12/12 PASS。
- 證據在 `install-sh-407ed7e/`；在預熱競態修正後，由 `install-sh-a20de1d/` 取代。
- 兩輪觀察項與前輪相同：
  - OBS-0.3.2-ONDEMAND-START
  - OBS-UPGRADE-ROLLBACK-PIPX／STALE-CURRENT（OD-49 已知限制）
  - OBS-0.3.2-SYSTEM-UMASK
  - OBS-SYSTEM-PROFILES-MANAGED
  - install-sh 的 OBS-UPGRADE-NOT-STARTED

## 不成立主張

無。三份審查報告都沒有 High／Medium。

## SPEC-GAPS

1. **`--mode all` 涵蓋範圍與裁決文字不一致**：
   - `rulings/s1.md`「OD-48 OWNER-BATCH #3 裁決修正」列與本輪指示都寫「`--mode all` 三模式」。
   - 但 OD-48 的設計（含既有測試）是 `all` 只涵蓋 systemd-user＋on-demand。
   - 本輪沒有改 `all` 的語意，以 `--mode all` 加 `--mode systemd-system` 兩次執行取得三模式 PASS。
   - 若要改 `all` 的涵蓋範圍，需要編排者裁決，並同步修改該測試。

## OWNER-BATCH

無。

## LOCAL 驗證項

- **Windows 原生**：只做了交叉 `cargo check`。下列項目未實跑：
  - 原生 cargo test
  - `win-pytest.sh -m core_portable／windows_native`
  - OD-48 的 Windows 父行程判定（LOCAL-WINDOWS-TODO）
- **install.sh 預設下載路徑**：要等 0.4.0 release 資產上架後，才能對真實 GitHub 驗證。
- **release 前置條件 #3**：需以 `VERSION=0.4.0` 的正式 bundle 重跑 §4。
- **實體 UART／DUT**：本環境沒有。

## 未完成項

- **S1-407ed7e-1（Low，不修）**：
  - README 兩段仍寫「先 `daemon stop`」並標 TODO(OD-48)，`tests/test_install_docs.py` 也釘住這個標記。
  - 改寫時要一併調整該斷言。
  - 建議同時新增「0.3.2 daemon 執行中直接 `install.sh --bundle`」的 Docker 檢查，使用新的檢查 id。
  - 這兩項請編排者排入後續工作。
- **wheelhouse PyYAML 標籤**：既有碼待辦，本輪未修。
- **`process_ctl` environ 競態**：裁決交付點不在本輪。
