# 0.4.0 release 前置準備：雲端回報（2026-10-29）

- 依據：`docs/rust-refactor/release-0.4.0-plan.md`（release 計畫）、`owner-decisions-2026-10-15.md` OD-33。
- 分支：`feature/release-0.4.0-prep`（起點 `origin/feature/rust-refactor` @ `62ffec7`），已 push 到 origin；未開 PR、未建 tag、未改 `VERSION`（仍為 `0.3.2`）、未提交 collate 結果。
- 環境：雲端容器，Rust 1.96.1（`rust-toolchain.toml` 釘版）、Python 3.12.3 venv（PyYAML 6.0.3、pytest 9.2.0、jsonschema 4.26.0、pyserial 3.5、policy engine `2dcf2f8`）；所有測試、cargo、mkbundle 以一般使用者（非 root，uid 1000）執行，git commit／push 以容器預設身分執行。

## Commit 清單

| commit | 內容 |
|---|---|
| `999124c` | `fix(changelog)`：補齊 5 個缺 frontmatter 的 fragment，使 0.4.0 collate 可執行（任務 3） |
| `3185c6d` | `feat(release)`：`release.yml` 新增 `publish-linux-bundle` job、`tests/test_release_workflow.py`、release 計畫 §2／RELEASE-TODO-01／§3 補充、changelog fragment `release-0.4.0-linux-bundle-job.md`（任務 1、2） |

## (1) RELEASE-TODO-01：Linux Rust bundle job

`.github/workflows/release.yml` 新增 job `publish-linux-bundle`（`runs-on: ubuntu-latest`、`needs: publish`、`timeout-minutes: 60`）。既有 `project-policy`、`publish`（wheel）、`publish-windows-exe`（PyInstaller）未改一行。workflow 只由 `v*` tag push 觸發（不變）。

步驟（皆有 `id`，測試以 id 定位）：

1. checkout（`persist-credentials: false`）、`actions/setup-python@v5` Python **3.12.3**（mkbundle 要求 bundle runtime 等於 `contracts/cli_help/_meta.json` 的 `python_version`）、`dtolnay/rust-toolchain@1.96.1`。
2. `verify-version`：tag 必須等於 `v$(VERSION)`，且 `Cargo.toml` `[workspace.package] version` 等於 `VERSION`（release 計畫 §3 第 1 步要求同步）；任一不符 `::error::` 並 exit 1。通過後把 `VERSION` 寫入 `$GITHUB_ENV`。
3. 安裝 `pyyaml==6.0.3 jsonschema==4.10.3`（與 `rust.yml` 同版）。
4. `cargo-build`：`cargo build --release --workspace --locked`。
5. `mkbundle`：`scripts/rust/mkbundle.sh --release-bundle --profile release --out "$RUNNER_TEMP/bundle/serialwrap-${VERSION}-linux-x86_64"`（無 `--version-override`、無 `--dev`）。
6. `package`：`tar` 打包為 `dist/serialwrap-${VERSION}-linux-x86_64.tar.gz`（保留 `bin/` 指向 `versions/<V>/bin/` 的相對 symlink；排除 `.mkbundle-owner`；owner/group 0），`dist/SHA256SUMS` 為 `sha256sum` 標準格式。
7. `smoke`：外層 `sha256sum -c SHA256SUMS` → 解開到 `$RUNNER_TEMP/smoke` → `bin/serialwrap --version` 必須等於 `serialwrap ${VERSION}` → `bin/serialwrap --help` → `versions/${VERSION}/release-manifest.json` 存在，`bundle_version`、`components.cli.version`、`components.daemon.version`、`pairing.cli_version`、`pairing.daemon_version` 全等於 `VERSION`，`stage` 不得為 sentinel 的 `rust-1b-dev` → 內層 `SHA256SUMS` 核對 → `versions/${VERSION}/bin` 不得含 `SW_TEST_UPDATE_PAUSE_AFTER`／`SW_TEST_UPDATE_FORCE_VERIFY_FAIL`（只在 `sw-cli/test-fakes` 下編入的故障注入鉤子字串，`crates/sw-cli/src/rollback.rs`）→ 不得含 `delegate_sentinel*`。
8. `upload`：`gh release upload "$TAG_NAME" "dist/serialwrap-${VERSION}-linux-x86_64.tar.gz" dist/SHA256SUMS --clobber`。

Windows bundle：workflow 不建（`windows-x86_64.zip` 字樣不出現在 workflow，由測試守護）。release 計畫 §2 表格與 RELEASE-TODO-01 段補上擁有者 runner 的附加步驟與命令：等 `publish-linux-bundle` 成功後，`zip` 打包 `serialwrap-<V>-windows-x86_64.zip` → `gh release download` 取回 `SHA256SUMS` → 確認已含 Linux 列 → 追加 zip 雜湊列 → `gh release upload ... --clobber` → 重新下載以 `sha256sum -c` 驗證；並註明重跑 workflow 會以 `--clobber` 覆寫 `SHA256SUMS`，需重補 Windows 列。

**發現（照實回報）**：目前沒有正式 Windows bundle 的建置腳本——`mkbundle.sh` 對 `*-windows-*` target 直接拒絕（exit 2，訊息「由 P3-T17 承接」），`win-blackbox.ps1` 的 `New-WindowsDevBundle` 只產生測試用 dev bundle。計畫中已寫明不得以 dev bundle 當 release 產物；Windows 正式 bundle 的建置本身仍是擁有者項目（BO-P9-17 同一條路徑），本輪只登記了附加／上傳程序。

## (2) workflow 結構 pytest（先紅後綠）

沿用 `tests/test_rust_ci_workflow.py` 的慣例（PyYAML 解析 workflow、`continue-on-error` 禁止檢查），新增 `tests/test_release_workflow.py`（模組 `posix_only`，各測試 `memory_only` 或 `process_rpc`，通過 `tests/test_resource_markers.py` 稽核）：

- 結構：tag push 觸發；既有三個 job 與其 wheel／PyInstaller 關鍵命令仍在；新 job `needs == ["publish"]`、toolchain 1.96.1、Python 3.12.3；步驟順序 verify→build→mkbundle→package→smoke→upload；mkbundle 參數（含禁止 `--version-override`／`--dev`）；上傳產物名稱 `dist/serialwrap-${VERSION}-linux-x86_64.tar.gz` 與 `dist/SHA256SUMS`；smoke 涵蓋 `--version`、`--help`、manifest、`sha256sum -c`、兩個鉤子字串；workflow 不建 Windows bundle、無 `continue-on-error`。
- 行為（取出步驟 `run` 以 `bash -e -o pipefail` 實跑）：`verify-version` 四組參數（相符通過；tag 版號不符、tag 缺 `v`、Cargo 版號不符皆失敗且不寫 `$GITHUB_ENV`）；以仿 mkbundle 佈局的假 bundle 跑 `package`＋`smoke`：完好者通過且 tarball 內容／symlink／`SHA256SUMS` 格式正確、不含 owner marker；10 種壞 bundle（`--version` 不符、`--help` 失敗、manifest 缺檔、bundle／cli／daemon 版本不符、sentinel stage、含鉤子字串、內層雜湊過期、外層 tarball 被改）全部非零結束，且逐一確認失敗訊息為預期原因。
- 紅燈：job 加入前 `20 failed, 3 passed`（3 個通過的是既有 job 保留類斷言）；加入後 `23 passed`。

## (3) changelog collate 試跑

在暫存 clone（不提交）執行 `python3 -m policy_check.changelog collate --version 0.4.0 --date 2026-11-01`：

- 起點：217 個 fragment，collate rc=1，`FragmentError: fragment must start with a YAML frontmatter block`，於第一個壞檔即中止、不改任何檔。以 `parse_fragment` 逐檔掃描找出全部 5 個壞檔（皆為缺 frontmatter）：`229-late-unlock-prompt.md`、`rust-p4-win-test-l1-set.md`（缺開頭 `---`）、`st06-command-completion.md`、`st06-smart4-prompt-window.md`、`st06-smart4r-console-unlock.md`。
- 修正（`999124c`，只動 frontmatter／格式、條目文字不變）：`### Fixed`／`### Added` 標題改為 `type: fix`／`type: feat`，單一條目去掉 `- `（collate 自行加）；`st06-smart4r-console-unlock.md` 同檔含 Added 三條與 Fixed 一條，依原條目拆成 `-a-profile`、`-b-recovery`、`-c-sysrq-reboot`（feat）與 `-d-prompt-boundary`（fix）。有引用 #229 的兩檔標 `issue: 229`。
- 重跑：rc=0，`collated 220 fragment(s) into [0.4.0] - 2026-11-01`，`changelog.d/` 清空；`[0.4.0]` 段 Added 73、Changed 39、Fixed 106、Security 2（合計 220，與 fragment 數一致）。（含本分支新增的 `release-0.4.0-linux-bundle-job.md` 共 221 個，均可解析。）
- 注意：`st06-command-completion.md` 內有相對連結 `../docs/229-command-completion-validation.md`，在 `changelog.d/` 內正確，但收斂進根目錄 `CHANGELOG.md` 後會失效（應為 `docs/...`）。屬內容層，本輪未改；release PR collate 後須順手修正該連結。
- 結果未提交；release 計畫 §3 第 2 步已記錄本次試跑。

## (4) 容器內實跑等效命令

以腳本解析 `release.yml` 的 `publish-linux-bundle`，依序以 `bash -e -o pipefail` 執行各 `run` 步驟（跳過 checkout／setup／pip／upload；`TAG_NAME=v0.3.2`，因 `VERSION` 仍為 0.3.2、依指示不改），`GITHUB_ENV` 於步驟間傳遞，git revision `3185c6d`：

| 步驟 | 結果 |
|---|---|
| verify-version | rc=0 |
| cargo build --release --workspace --locked | rc=0，43.0 s（有既有 target 快取） |
| mkbundle --release-bundle --profile release | rc=0，38.4 s |
| package | rc=0 |
| smoke | rc=0：`serialwrap-0.3.2-linux-x86_64.tar.gz: OK`、`smoke passed` |

- tarball：`serialwrap-0.3.2-linux-x86_64.tar.gz`，24,789,279 bytes（約 23.6 MiB），1,343 個項目；sha256 `b92fa68393c496065352f7738d4331dc37be002f9655366bde4b96a8e8dd85e7`。
- 解開後 80,432,817 bytes（約 80 MB）；`versions/0.3.2/bin/serialwrap` 31,427,232 bytes、`serialwrapd` 5,563,616 bytes；內層 `SHA256SUMS` 1,192 列全數核對通過。
- manifest：`bundle_version 0.3.2`、`stage 1A`、`target x86_64-unknown-linux-gnu`、`git_revision 3185c6d…`、runtime Python 3.12.3；`bin/serialwrap --version` → `serialwrap 0.3.2`。
- 負向對照：同一 HEAD 以 `mkbundle.sh --dev` 建 dev bundle，兩個鉤子字串在其 `serialwrap` 中各出現 2 次、正式候選為 0 次，證明 smoke 的 test-fakes 檢查有鑑別力；`TAG_NAME=v0.4.0` 對 `VERSION 0.3.2` 時 `verify-version` 輸出 `::error::release tag v0.4.0 does not match v0.3.2` 並 rc=1、後續步驟不執行。
- 限制：容器 jsonschema 為 4.26.0（CI 釘 4.10.3）；本機 bundle 版號為 0.3.2，0.4.0 的實際產出要等 release PR 改 `VERSION`／`Cargo.toml` 後由 CI 產生。上傳步驟未執行（不建 tag、不發 release）。

## 每個 commit 的驗證

- 起點基準（`62ffec7`，一般使用者、全套）：`3 failed, 5297 passed, 160 skipped, 1 xfailed`（31 分 32 秒）。3 個失敗皆屬環境證據缺口：
  - `tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`、`tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`：缺 Windows 原生／P4 證據檔（`target/p4-evidence/...`）。
  - `tests/test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only`：鎖檔 nodeid 內嵌 Windows 版 pyserial 的錯誤訊息字串，與 Linux 上 pyserial 3.5 產生的參數 id 不同。
  - 前兩次基準作廢：第一次一般使用者讀不到 root 目錄下的 proxy CA bundle，cargo 無法抓 crate（34 個失敗）；第二次在 `/tmp` 下的 worktree 跑，git safe.directory 與 `/tmp` 相關測試誤判（52 個失敗）。修正環境（CA 複製到可讀位置、安裝 CI 同樣會裝的 pyserial、改在主 checkout 跑）後取第三次為基準。
- `999124c`：`policy_check` rc=0（僅既有 R-19／R-22 warn，與基準相同）、`tools.contracts check` `ok: true`、0 findings；只動 `changelog.d/`，無測試讀取該目錄。
- `3185c6d`：受影響測試 `tests/test_release_workflow.py tests/test_resource_markers.py tests/test_rust_ci_workflow.py tests/test_docker_upgrade.py::test_release_plan_accepts_only_real_stage1_gate_outcomes` 40 passed；`policy_check` rc=0（同上 warn）；`tools.contracts check` `ok: true`。
- HEAD（`3185c6d`）全套：`3 failed, 5320 passed, 160 skipped, 1 xfailed`（32 分 13 秒）；失敗集合與起點基準逐項相同（同 3 個 nodeid），passed 增加 23 即新增的 `tests/test_release_workflow.py`。未引入新失敗。

## 未完成項／交給 release PR

1. Windows 正式 bundle 的建置腳本（`mkbundle.sh` 拒絕 Windows target；BO-P9-17）——擁有者 runner 項目，附加程序已寫入計畫。
2. release PR 內：`VERSION`／`Cargo.toml` 改 0.4.0、`cargo update -w`、正式 collate（日期用實際發佈日）、collate 後修正 `CHANGELOG.md` 內 `../docs/229-command-completion-validation.md` 相對連結、以 release bundle 重跑 §4 升級驗證。
3. `publish-linux-bundle` 尚未在真正的 GitHub Actions tag push 上執行過（不能建 tag）；第一次實跑會在 v0.4.0 tag。
4. RELEASE-TODO-02（install.sh）不在本輪範圍，未動。
