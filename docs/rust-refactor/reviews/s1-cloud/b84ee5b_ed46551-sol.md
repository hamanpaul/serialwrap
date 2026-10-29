> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

# feature/install-sh-bundle b84ee5b..ed46551 非實作者增量審查

範圍：first-parent 上 2 筆非 merge commit（`407ed7e3` 升級驗證改以 `/usr/bin/python3.12` build venv 建 bundle 並檢查 `pyvenv.cfg`；`ed46551c` 保存 Docker 證據），以及 2 筆 merge（`a9b8a385` 併入 `origin/feature/od48-legacy-handoff`＠`2179a83`；`e1c53657` 併入 `origin/feature/release-0.4.0-prep`＠`2ddf90e`）。兩筆 merge 只看衝突解法，以及合入後是否破壞既有行為；經 merge 帶進的 OD-48／release 前置 commit 不重審。

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `978aa1e` | 0 | 已確認是 `ed46551` 的祖先。從 `b84ee5b` 到 `ed46551`，該 commit 改過的檔案都沒再變動，修正仍然有效。 |
| 2 | Medium | 已修 `51bd8fa` | 0 | 同上：是祖先，相關檔案本輪沒動。 |
| 3 | Medium | 已修 `ddcbdde` | 0 | 同上。後續的 `4e3ba13`、`2fe7750` 也都是祖先，相關檔案本輪沒動。 |
| 4 | High | 已修 `12b3a92` | 0 | 本輪改過 `run-upgrade.sh`、`run_upgrade_container.py`、`upgrade.Dockerfile`，逐項重核：Dockerfile 只加了 `sudo`，沒有 `python3-yaml`；兩處 `pipx install` 都還是 `--no-index --find-links=/opt/sw-old`；全檔沒有 `system-site-packages`；新 bundle 的 runtime 另外檢查 `include-system-site-packages = false`。 |
| 5 | Medium | 已修 `12b3a92` | 0 | 自建 bundle 仍用 `mkbundle.sh --release-bundle --profile release`，dry-run 與實跑兩處都是；`mkbundle.sh` 的 `--dev`／`--release-bundle` 互斥守門沒有變。 |
| 6 | Low | 已修 `12b3a92` | 0 | `release-0.4.0-plan.md` §1 前置條件 #1 仍為「`VERIFIED`，或 `IMPLEMENTED` 且只剩 OD-27～OD-32 允許的 `BLOCKED-OWNER`；沒有 FAIL／INSUFFICIENT」。 |

## B 新發現

本輪沒有 High 或 Medium。以下 3 項 Low 不擋放行，交編排者收進 low-backlog：

- **S1-ed46551-1 [Low]** `scripts/docker/run-upgrade.sh:161-164`
  - 問題：commit 訊息說 dry-run「依實際順序列出」，但 build venv 與 mkbundle 這幾行在 dry-run 不分條件一律印出。
  - 失敗情境：執行 `run-upgrade.sh --dry-run --bundle /x` 會印出 `/usr/bin/python3.12 -m venv …`、`pip install …`、`env PATH=…/build-venv/bin:… mkbundle.sh …`。實跑時 `[[ -z "$BUNDLE" ]]` 為假，這三步都不會執行。mkbundle 那行無條件印出是既有行為，本輪多了兩行同類輸出。
  - 修法：dry-run 的這三行改用 `if [[ -z "$BUNDLE" ]]; then …; fi` 包起來，條件與實跑相同。
- **S1-ed46551-2 [Low]** `scripts/docker/run-upgrade.sh:117-123`、`133-137`
  - 問題：版本比對（必須等於 `contracts/cli_help/_meta.json` 的 `python_version`）與 build venv 的 `sys._base_executable` 必須位於 `/usr/bin` 這兩道 fail-closed，都沒有回歸測試。
  - 失敗情境：刪掉任一段，`tests/test_docker_upgrade.py` 仍然全綠。現有 7 個新回歸只覆蓋缺檔、實體不在 `/usr/bin`，以及 4 種 `pyvenv.cfg`。
  - 修法：補一個 parametrize 案例：讓 `SW_UPGRADE_SYSTEM_PYTHON` 指向 `/usr/bin/python3.*` 中版本不符的直譯器，主機沒有時 `pytest.skip`；或把 `_meta.json` 的比對抽成可注入的路徑。預期 rc=3，且沒有呼叫 docker。
- **S1-ed46551-3 [Low]** `scripts/docker/run-upgrade.sh:106-124`、`131`
  - 問題：`require_system_python` 沒有確認 `venv`／`ensurepip` 可用。
  - 失敗情境：主機有 `/usr/bin/python3.12` 但沒裝 `python3.12-venv` 時，前置檢查會通過，接著先跑 `git archive`＋`pip wheel` 建 0.3.2 wheel，到 `"$SYSTEM_PYTHON" -m venv` 才以 rc=1 結束。這與「在任何建置之前以 rc=3 結束」的宣稱不符。仍是 fail-closed，沒有回退到其他 python，也不會產生假 PASS。
  - 修法：在 `require_system_python` 末尾加 `"$SYSTEM_PYTHON" -I -c 'import venv, ensurepip'`，失敗時 rc=3 並提示安裝 `python3.12-venv`。

### 已核對、無缺陷的項目

- **`a9b8a385` 衝突解法**（用 `git show --remerge-diff` 逐段核對）：
  - `REQUIRED_CHECKS` 與 `REQUIRED_CHECKS_SYSTEMD_SYSTEM` 和 od48 端（`2179a83`）逐項相同。`REQUIRED_INSTALL_SH_CHECKS`、`PIPX_UNINSTALL_HINT` 和 OD-50 端（`b84ee5b`）相同。
  - `overall_verdict` 本體沒有變，只放寬參數型別。
  - driver 中兩邊都改過的 `__init__`、`execute`、`phase_upgrade_after_stop`、`main` 都保留了兩邊的行為。`_judge_entries_and_config` 改用 od48 的 `cfg_files()`／`expected_overwrites()`，在非 systemd-system 模式下等價於原本的 `snapshot_files(CONFIG)` 加空覆寫集合。
  - 測試由 `len == 18` 改成釘死 19 個 id 的 tuple，比原本更嚴。各測試檔都沒有遺失測試函式（`test_docker_upgrade`／`test_release_workflow`／`test_install_sh`／`test_install_docs`／`test_contracts_catalog` 取兩側聯集比對）。
  - 新增的 `--scenario install-sh --mode systemd-system` 由 runner 與 driver 兩端守門。我分別只拔掉 runner 的守門、只拔掉 driver 的守門，`test_install_sh_scenario_rejects_systemd_system_mode` 都會紅。
  - `crates/` 的內容與 od48 端完全相同，`.github/` 的內容與 release 前置端完全相同。
- **`e1c53657`**：唯一衝突是發佈內容表。Windows 列採 release 前置版，wheel 與 install.sh 列採 OD-50 版；RELEASE-TODO-02 段落與 OD-50 一致。
- **`407ed7e3`**：
  - 7 個新回歸在修正前（`e1c5365` 的腳本加上新測試）全紅，修正後全綠。
  - 建置步驟與 release workflow 的 `publish-linux-bundle` 一致：同樣釘 `pyyaml==6.0.3`、`jsonschema==4.10.3`，同樣以 `_base_executable` 為 `/usr/bin` 作判準，同樣要求 `pyvenv.cfg` 這兩行。
  - `mkbundle.sh` 確實用 PATH 上的 `python3` 執行 `python3 -m venv --copies`，jsonschema 驗證也走同一個 `python3`。
  - 對照編排者裁決：缺 `/usr/bin/python3.12` 時 fail-closed、不回退到其他 python、有 `home = /usr/bin` 檢查，三項都符合。
- **`ed46551c`**：
  - `SHA256SUMS` 的 6 列都通過 `sha256sum -c`。
  - 五份報告我依 scope 選用對應的必要集合，以 `overall_verdict` 重算，結果都是 PASS：full 19 項×2、systemd-system 13 項、install_sh 12 項×2，都沒有缺項或多出的項目。
  - 五份報告的 `bundle_git_revision` 都是 `407ed7e3…`，`bundle_manifest_sha256` 與 `wheel_sha256` 也都一致。`bundle-pyvenv.txt` 為 `home = /usr/bin`。

### 實跑證據

全部以一般使用者（uid 30033）執行。審查 worktree 未修改；需要寫檔的測試在另一份與 `ed46551` 內容相同的暫存複本中跑。

- **針對性 pytest**（`test_docker_upgrade`、`test_release_workflow`、`test_contracts_catalog`）：211 passed、2 skipped。兩個 skip 都是本機沒有 docker daemon 的能力式 SKIP。
- **`cargo test --workspace --locked`**：952 passed、2 failed，兩個失敗都是環境造成，在可寫複本與乾淨 PATH 下重跑皆通過：
  - `emitted_codes` 會寫入 repo 根目錄的 `target/`，而唯讀 worktree 無法寫入。
  - `sysenv_mgmt::run_missing_exe_is_notfound_with_py_message` 失敗，是因為 PATH 含 uid 無權讀取的 `/root/.bun/bin`，回的是 EACCES。
- **全套 `python3 -m pytest -q tests/`**（pytest 9.2.0，起初沒有裝 pyserial）：5372 passed、169 skipped、1 xfailed、17 failed。17 個失敗逐一分類，都與本輪變更無關：
  - 5 個要求 pyserial：`test_doctor`、`test_stage0_tx_outcome`、`test_stage0_tx_permit`×2、`test_stage0_uart_exclusive`。補裝 pyserial 後重跑皆通過。
  - 9 個是 `test_rust_no_onefile_private_tmp`：工作目錄位於 `/tmp` 下時，`--private-tmp` 檢查會拒絕。改在 `/tmp` 以外的複本重跑，11 passed。
  - 2 個缺少本機沒有產生的證據檔：`test_rust_bench_tools::test_cargo_workspace_tests_windows_native` 缺 Windows 原生 cargo L1 報告；`test_rust_stage1b_registration::test_p4_required_cases_have_passing_evidence` 缺 `target/p4-evidence/*.json`，共 23 案，大多與本輪無關。
  - 1 個是 `test_l1_judge::test_l1_lock_nodeids_subset_of_collect_only`：在 pytest 9.2.0 下，parametrize id 收成 `msg2-expected2`；改用 pytest 8.4.2 時，收集到的 id 與鎖檔相同。相關檔案本輪沒動，見「既有碼待辦」。
- **未實跑**：Docker 升級（本機沒有 docker daemon，只核對已保存的報告），以及 Windows 原生測試。Policy check 沒有跑，因為 policy engine 所在的 repo 不在本 session 的 GitHub 存取範圍內。

## 既有碼待辦（不計入結論）

- `scripts/docker/run-upgrade.sh:226`（源自 `12b3a92`，本輪只移動了位置）：0.3.2 wheelhouse 的 PyYAML 是用 host 的 `python3 -m pip download --only-binary=:all:` 下載，沒有指定 `--python-version`。
  - 實測：host 的 `python3` 是 3.13 時，下載到的只有 `pyyaml-6.0.3-cp313-…whl`。容器是 ubuntu:24.04 的 Python 3.12，以 `pipx install --pip-args "--no-index --find-links=/opt/sw-old"` 安裝 0.3.2 時找不到可用的 PyYAML，`old_install_0_3_2` 因此 FAIL。
  - 影響：這是 fail-closed，不會產生假 PASS，但在本雲端主機這類 `python3` 不是 3.12 的環境，無法產生證據。
  - 建議：改用 `$SYSTEM_PYTHON -m pip download`，或補上 `--python-version 3.12 --implementation cp --abi cp312 --platform manylinux_2_17_x86_64`。
- `tests/acceptance/l1_required.lock.json`（`s0_bc.linux`）與 `tests/test_stage0_uart_exclusive.py` 的 `test_classify_pyserial_open_error`：
  - 鎖檔裡釘的是 pytest 8 產生的字串型 parametrize id（`could not open port 'COM9': FileNotFoundError(2, …)-expected2`），pytest 9.2.0 收集時改成 `msg2-expected2`，`test_l1_lock_nodeids_subset_of_collect_only` 因此 FAIL。
  - `.github/workflows/tests.yml:30` 安裝的 `pytest` 沒有釘版，CI 一旦取到 pytest 9 就會紅。
  - 建議：該 parametrize 以 `ids=` 明確指定與鎖檔現有字串完全相同的 id，鎖檔不用動；或在 CI 釘 `pytest<9`。
- `crates/sw-cli/tests/emitted_codes.rs:392-401`：會寫入 `<repo>/target/`，沒有依循 `CARGO_TARGET_DIR`，`create_dir_all` 的錯誤也被忽略；在唯讀 checkout 下會以 NotFound 失敗。`crates/sw-pal/tests/sysenv_mgmt.rs:301` 假設 PATH 中沒有無權讀取的目錄。兩者都是測試對環境的假設，不是產品行為。

## 對裁決的異議（不計入結論）

無。

結論：可放行
