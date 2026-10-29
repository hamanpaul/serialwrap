> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

# feature/install-sh-bundle `b84ee5b..407ed7e` 非實作者增量審查

範圍：first-parent 共 3 筆，分別是 `a9b8a38`（merge `origin/feature/od48-legacy-handoff`@`2179a83`）、`e1c5365`（merge `origin/feature/release-0.4.0-prep`@`2ddf90e`）與 `407ed7e`（非 merge）。兩筆 merge 只看衝突解法及合入後是否破壞既有行為；被合入分支的各 commit 已由各自分支審查（LAST-REVIEWED：od48 `2179a83`、release-prep `2ddf90e`）。

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `978aa1e` | 0 | 本輪沒有改動 `tests/contract_blackbox/test_cli_mgmt_daemon.py`，SC-18 的 checkpoint 雜湊比對與延遲重複寫入案例都還在。 |
| 2 | Medium | 已修 `51bd8fa` | 0 | 本輪沒有改動 `crates/sw-pal/tests/exec_delegate.rs` 與 `tests/test_rust_exec_delegate.py`；以一般使用者重跑 `exec_delegate`，結果通過。 |
| 3 | Medium | 已修 `ddcbdde` | 0 | 本輪沒有改動 `scripts/rust/l15_exit_driver.py`、`stage1_exit_driver.sh` 與 `tests/test_rust_stage1_exit_driver.py`；`4e3ba13`、`2fe7750` 的補強也都還在。 |
| 4 | High | 已修 `12b3a92` | 0 | 0.3.2 依賴仍走 wheelhouse，容器內仍以 `--no-index --find-links=/opt/sw-old` 安裝。`407ed7e` 只把 `pip download` 移到 bundle 檢查之後。 |
| 5 | Medium | 已修 `12b3a92` | 0 | 自建 bundle 仍用 `mkbundle.sh --release-bundle --profile release`，現改經 `/usr/bin/python3.12` 建立的 build venv 執行。`mkbundle.sh` 本輪沒有改動。 |
| 6 | Low | 已修 `12b3a92` | 0 | 前置條件文字沒有退回。 |

## B 新發現

- **S1-407ed7e-1** [Low] `README.md:213-215`、`README.md:1790` — 合入 OD-48 後，install.sh 的「從 0.3.x 升級」段仍寫「升級前請先 `serialwrap daemon stop`（TODO(OD-48)：……實作後再更新此步驟）」。這與同一份 README 的 OD-48 段（`README.md:1417`、`README.md:2677`，可在 daemon 執行中直接升級並明確指定監管模式）及 `docs/rust-refactor/release-0.4.0-plan.md:88`（§5.1「不需先停 0.3.2 daemon（OD-48）」）互相矛盾。照 README 先停再升級仍可完成，只是 setup 不會代為啟動 daemon，要手動 start，所以沒有功能失敗。修法：改寫中英兩段為「可直接執行 `install.sh` 並明確帶入目前的監管模式（`--user`／`--system --with-sudo`／`--on-demand`）；先停再升級亦可，但 setup 不代為啟動」，並同步調整 `tests/test_install_docs.py:35` 中釘住 `TODO(OD-48)` 的斷言。

除上列 Low 外，B 段沒有 High／Medium。各項核對如下：

- `a9b8a38` 的衝突解法：
  - `upgrade_checks.py` 同時保留 OD-48 的 `REQUIRED_CHECKS`（19 項）、`REQUIRED_CHECKS_SYSTEMD_SYSTEM`，以及 OD-50 的 `REQUIRED_INSTALL_SH_CHECKS`、`PIPX_UNINSTALL_HINT`、`judge_install_sh`。`overall_verdict` 改為位置或關鍵字皆可傳入 `required`，所有呼叫端都以關鍵字傳入。
  - `run_upgrade_container.py` 等於 OD-48 head 加上 install-sh 的增量（逐行比對 `2179a83..407ed7e`）。OD-48 的 `cfg_files()` 與 `expected_overwrites()` 已帶進抽出的 `_judge_entries_and_config()`；非 system 模式的 `expected_overwrites()` 為 `{}`，語意與原本的 `snapshot_files(CONFIG)` 相同。
  - 新增的 `install-sh × systemd-system` 組合，在 runner 與 driver 兩端都回 rc=2。我在暫存複本分別拿掉兩端的守門，`test_install_sh_scenario_rejects_systemd_system_mode` 都會紅。
  - `REQUIRED_CHECKS` 的斷言由比長度改為逐項比 tuple，比原本更嚴，沒有削弱。
  - 測試函式沒有遺失：install-sh 側 26 個、OD-48 側 30 個，合併後 39 個，兩側都是子集；assert 數為 92／107 → 151。
- `e1c5365` 唯一的衝突在 release 計畫 §2 表格，解法正確：Windows 列採 release 前置版，wheel／install.sh 列保留 OD-50 版。`release.yml` 產出的 tarball 名稱、頂層目錄 `serialwrap-<V>-linux-x86_64/` 與 `SHA256SUMS` 的 `sha256sum` 格式，都和 `install.sh` 的 `locate_bundle`／`verify_sha256` 相容。
- `407ed7e` 與裁決「release 前置第 2 輪 OWNER-BATCH #2」逐項一致：
  - 以 `/usr/bin/python3.12` 建 build venv，釘版 `pyyaml==6.0.3`、`jsonschema==4.10.3`。
  - 檢查 `sys._base_executable` 位於 `/usr/bin`，並以 PATH 首位執行 `mkbundle --release-bundle`。
  - 版本以 `_meta.json` 判定（3.12.3，與 workflow 和 `mkbundle.sh:347` 同一判準）。
  - 主機缺 `/usr/bin/python3.12` 或其實體不在 `/usr/bin` 時，rc=3，在任何建置、下載或 docker 動作之前結束，不回退其他 python。
  - 自建或 `--bundle` 提供的 bundle 都要求 `pyvenv.cfg` 含 `home = /usr/bin` 與 `include-system-site-packages = false`。
  - 新增 7 個回歸，在暫存複本換回 `e1c5365` 版的 `run-upgrade.sh` 後 7 項全紅。

實跑證據（以一般使用者 `ubuntu` 執行，審查 worktree 全程未被改動，`git status` 乾淨）：

- pytest（`/usr/bin/python3.12` venv，PyYAML 6.0.3、jsonschema 4.10.3）：
  - `tests/test_docker_upgrade.py`、`test_release_workflow.py`、`test_install_sh.py`、`test_contracts_catalog.py`：242 passed、2 skipped。兩個 skip 都是 `cap_missing:docker`，屬能力式 SKIP，不算 PASS。
  - 全套（在一般使用者可寫的 `407ed7e` clone 中執行，不含 Docker 實跑案例）：5332 passed、204 skipped、20 failed、1 error。失敗都是環境造成：一般使用者沒有 pyserial，也沒有 cargo（bundle／`cargo` 類案例無法執行），另有 Windows 原生與 P4 證據案例。同一組 nodeid 在同一環境的 `b84ee5b` 上也是 20 failed、1 error，集合完全相同，不屬本輪引入。
- Rust：`cargo test --workspace --all-features` 共 78 個測試 binary，76 個通過。其餘 2 個都是環境造成：
  - `emitted_codes` 寫 workspace 的 `target/` 時，因 worktree 對一般使用者唯讀而失敗。
  - `sysenv_mgmt` 因 PATH 含一般使用者不可讀的 `/root/.cargo/bin` 而失敗；PATH 修正後重跑 17/17 通過。
- `python3 -m policy_check --repo .`（3.12）：25 pass、0 fail、2 warn（R-19、R-22，屬非 PR 上下文）。PATH 首位若是 3.13，R-16 會因 argparse 排版不同而 FAIL；這是環境差異，以 3.12 重跑即消失。
- 未實跑 Docker 升級（本機沒有 docker daemon）與 Windows 原生測試，不宣稱取得這兩項證據。

## 既有碼待辦（不計入結論）

- `scripts/docker/run-upgrade.sh:226`：0.3.2 wheelhouse 的 PyYAML 以主機 PATH 上的 `python3 -m pip download --only-binary=:all:` 下載，wheel 的直譯器標籤跟隨主機的 python3。若主機的 python3 是 3.13（本審查機 `/usr/bin/python3` → 3.13.17，同時有 `/usr/bin/python3.12` 3.12.3，因此能通過 `407ed7e` 的前置檢查），實測會下載 `pyyaml-6.0.3-cp313-cp313-manylinux…whl`。容器的 python 是 3.12.3，`pipx install --no-index --find-links=/opt/sw-old` 會找不到相容的 PyYAML，`old_install_0_3_2` 因而 FAIL。此行在 `b84ee5b` 以前就存在，`407ed7e` 只移動位置，不屬本輪引入。建議加上 `--python-version 3.12 --implementation cp --abi cp312 --platform manylinux2014_x86_64`，或在自建 bundle 時改用 build venv 的 pip 下載。

## 對裁決的異議（不計入結論）

無。

結論：可放行
