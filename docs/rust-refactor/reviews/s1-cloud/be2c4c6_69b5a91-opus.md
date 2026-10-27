# feature/rust-refactor be2c4c6..69b5a91 非實作者審查（Claude Opus）

## 範圍

- 分支 `origin/feature/rust-refactor`，`be2c4c6..69b5a91`，共 6 個 commit、19 個檔案（+294/-23），沒有 merge commit。
- 依序為：`6c70012`、`ea33090`、`3130f73`、`57fe9a9`、`aed5d02`、`69b5a91`。
- 產品程式的變更只有 `6c70012`：`crates/sw-pal` 新增 `FsOps::remove_dir`，systemd hold release 會一併移除已空的 drop-in 目錄。另外 `ea33090` 改了 `process/windows.rs`，只動註解。其餘變更是測試、凍結鎖、changelog fragment 與文件。

## 依據

- `CLAUDE.md`、`docs/rust-refactor/serialwrap-rust-spec-final.md`
- `docs/rust-refactor/owner-decisions-2026-10-15.md`（OD-14 起，含 OD-24／25／26／29）、`owner-decisions-addenda.md`、`owner-runbook-od29.md`
- docs/parity-amendments 的 `parity-amendments-2026-10-26.md` 與修訂後的計畫
- 前次報告 `reviews/s1-cloud/d2118cd_be2c4c6-opus.md`（`f50f793`）：本範圍有 5 個 commit 是回應它的發現
- Python 基線 `sw_core/*.py`
- policy engine `hamanpaul/paulsha-conventions@2dcf2f8`（v1.0.19）的 `policy_check/rules/r21_secret_scan.py` 與 `_secret_scan_config.py`、`data/secret_scan_defaults.yml`：clone 到 scratch 目錄，以 `PYTHONPATH` 載入，沒有安裝進系統

## 方式

- 主審查直接在 `/home/user/rr`（detached `69b5a91`）進行。
- 反證做法：在同一工作樹暫時還原修正或注入變異，實跑後立即 `git checkout .`。每次反證後都確認 `git status --short` 為空。
- `CARGO_TARGET_DIR=/home/user/target-s1`。
- 本機為 root，沒有 Windows／PowerShell，也沒有可用的 docker daemon。凡是宣稱 Windows 原生或擁有者 host（`~/b-log/...`）的證據，只能核對指標可不可追溯，以及 repo 內 blob 雜湊對不對得上，無法重跑。

## 結論

- **High 0、Medium 1、Low 3。**
- 凍結鎖的變更符合規定：只在 `s1_pytest_full.linux` add-only 新增 12 筆，沒有移除，也沒有改 selector 或 metadata。`regen_lock.py --check --base be2c4c6` 為 OK。
- cfg 邊界檢查通過。`FsOps::remove_dir` 的 posix 與 windows 實作都放在 `crates/sw-pal/src/fs_ops/{posix,windows}.rs`。
- 本範圍的回歸測試都經實跑反證：
  - `6c70012`：還原 `systemd.rs` 後，2 支新測試 FAIL。
  - `3130f73`：舊替身加 0.4 s 真實延遲會 FAIL，新替身會 PASS。
- 沒有發現 Windows 原生項目被假裝完成：OD-29 (5) ctrl_c 仍標為 BLOCKED-OWNER，runbook 有完整 exe sha256（`74f26463e220…a95b`）。
- 唯一的 Medium 在 `57fe9a9`：新增的 R-21 防線在 CI（沒有 engine）上，marker 維度形同空轉。

## 實際執行的驗證命令與結果

| # | 命令（於 /home/user/rr @69b5a91，除非另註） | 結果 |
|---|---|---|
| 1 | `python3 scripts/rust/check_cfg_boundary.py` | rc 0 |
| 2 | `cargo check -j4 --target x86_64-pc-windows-gnu --workspace --all-targets` | rc 0（只有 unused import warning） |
| 3 | `cargo test -j4 --workspace --all-features --no-fail-fast` | 81 個 test result：897 passed、2 failed、7 ignored。2 個失敗是已知 root 基準：`sw-pal::oserror_golden::file_eacces_matches_golden`、`sw-pal::rpc_client_conformance::unix_fixture_rows_match_python`，與前次報告相同。比前次的 895 多出 2 支，即本範圍新增的 systemd 測試 |
| 4 | `cargo test -p sw-pal --features test-fakes --test service_ctl` | 44 passed、1 ignored |
| 5 | 反證 6c70012：`git show be2c4c6:crates/sw-pal/src/service_ctl/systemd.rs > …systemd.rs`，保留 `FsOps::remove_dir`，再跑 #4 | 42 passed、2 FAILED：`systemd_user_release_removes_empty_dropin_dir_but_keeps_foreign_dropins`（:1011）與 `systemd_system_release_rmdirs_dropin_dir_ignoring_non_empty`（:1052） |
| 6 | 變異 6c70012：`systemd.rs:421` 的 `let _ = self.run_recorded(rmdir, …)` 改成 `…map_err(release_failure)?`，即 rmdir 失敗會讓 release 失敗 | 44 passed，**沒有測試抓到**（見 6c70012 Low 1） |
| 7 | 探針：在 `service_ctl.rs` 暫加一支測試，讓 hold 驗證不符（`Restart=on-failure`），觸發 `rollback_hold` | `dropin_file_exists=false dir_exists=true`，rollback 後留下空的 `serialwrap.service.d`（見 6c70012 Low 2） |
| 8 | 自寫腳本逐 set／platform 比對 `l1_required.lock.json`（be2c4c6 對 69b5a91） | set／platform 鍵完全相同。只有 `('s1_pytest_full','linux')` add 12、rem 0，metadata 不變；`schema_version` 不變 |
| 9 | `python3 tests/acceptance/regen_lock.py --check --base be2c4c6` | 兩行 OK，rc 0 |
| 10 | `pytest -q <#8 新增的 12 個 nodeid>` | 12 passed（全部可收集） |
| 11 | `PYTHONPATH=<engine@2dcf2f8> python3.12 -m policy_check --repo .` | R-21 pass；R-19、R-22 為 warn；其餘 pass |
| 12 | `pytest -q tests/test_policy_r21_allow_guard.py`，分別在沒有 engine 與有 engine 下執行 | 兩者都是 3 passed |
| 13 | 變異 57fe9a9：在受保護檔尾端加一行 `mutant: BGW720 build`（`bgw720` 是 engine baseline marker） | **沒有 engine（＝CI）：3 passed**；有 engine：1 failed（marker 斷言）（見 57fe9a9 Medium 1） |
| 14 | 變異 57fe9a9：加一行假 `ghp_…` token；另一次改加一行 `/home/someone/x` | 沒有 engine 時兩者都是 1 failed，credential 與 structural 防線有效 |
| 15 | `pytest -q tests/test_stage0_wal_av_sim.py` | 44 passed、2 skipped |
| 16 | 連跑 40 次 `-k records_no_delete_primary_and_oplock_variant_separately`，其中前段與 cargo workspace 測試並行 | 40/40 PASS |
| 17 | 反證 3130f73：在新替身 `vclock.advance_s(0.03)` 後插入 `threading.Event().wait(0.4)` 真實延遲 | PASS |
| 18 | 反證 3130f73：be2c4c6 版替身在 `real_sleep(0.03)` 後插入同樣的 0.4 s 延遲 | FAIL（`INSUFFICIENT_EVIDENCE`）。與 commit 訊息宣稱一致 |
| 19 | 變異 3130f73：新替身的 lease 由 200 ms 改為 10 ms（小於 30 ms 排程延遲） | FAIL（`INSUFFICIENT_EVIDENCE`）。虛擬時鐘沒有讓案例失去鑑別力 |
| 20 | `pytest -q tests/test_docker_l15.py tests/test_l1_judge.py tests/test_resource_report.py tests/test_rust_cargo_l1_results.py tests/test_stage0_wal_av_sim.py tests/test_policy_r21_allow_guard.py` | 145 passed、3 skipped。`test_docker_smoke_two_container` 以 `cap_missing:docker` SKIP |
| 21 | 直接呼叫 `_selftest_diag()`，分別餵 dict 報告與頂層為 list 的 JSON | dict：正確列出非 PASS 項；list：`AttributeError: 'list' object has no attribute 'get'`（見 69b5a91 Low 1） |
| 22 | `git show <c>:<path> \| sha256sum`，對照 addenda 記載的 repo 內 blob 雜湊 | `win-startup-100.ps1`：@fdbb148 為 `59112b76…`，@7f195f7 與 HEAD 為 `5792ee21…`；`tests/test_rust_win_startup_100.py` 為 `eedcce01…`；`s1x-oracle-review-1.md` 在 HEAD、`4689a92`、`origin/docs/parity-amendments` 皆為 `dc1cbfaa…`；`d2118cd_be2c4c6-opus.md`@`f50f793` 為 `0ffb6d35…`。全部與 addenda 的 64 字元值相符 |
| 23 | `git diff --stat 37166b3 63d9bdb -- crates` | 只有 `crates/sw-pal/tests/service_ctl.rs` 2 行，與 addenda「受測 binary 的 crates/ 內容等同、差異僅測試清理」相符 |
| 24 | `pytest -q tests/`（全套） | 32 failed、4685 passed、175 skipped、433 errors（32 分 26 秒）。全部是環境因素，與本範圍無交集：(a) 本機 `python3` 為 3.11.17，golden／mkbundle 釘選 3.12.3：`test_bb_cli_help.py` 的 432 個 error、`test_rust_mkbundle.py` 19 支、bundle 類 contract_blackbox 與 `test_rust_release_manifest_schema` 都因此失敗；(b) 沒有 `pyserial`：`test_stage0_tx_*`、`test_stage0_uart_exclusive`、`test_doctor::test_pyserial_ok_when_importable`；(c) 缺 Windows 原生證據檔：`test_rust_stage1b_registration`、`test_rust_bench_tools::…windows_native`；(d) root／`/proc` 因素：`test_rust_agent_perm_verifier`、`test_bb_l15_holder_observer`、`test_bench_tools_cli`。(c)、(d) 中較不明顯的 3 支外加 1 支 mkbundle，另在 be2c4c6 的暫時 worktree 重跑，同樣 4 failed（用後已 `git worktree remove`），確認不是本範圍造成的回歸。本範圍新增或修改的測試全部 PASS（#10、#12、#15、#20） |

### 6c70012 fix(sw-pal): systemd hold release 一併移除已空的 drop-in 目錄

與 Python 1:1 的核對：

- Python 基線沒有 autostart hold 機制。`sw_core/*.py` 找不到 `update-hold`、`RefuseManualStart`、`autostart` 或 `.service.d`。`sw_core/service_ctl.py` 與 `setup_cmd.py` 只包裝 `systemctl stop/disable/enable/start/daemon-reload`。因此這是 Rust 專有的 update 流程，沒有可對照的 Python 外部行為。前次報告已把同一問題定為「不涉及 1:1、Low」，本修正方向一致。

具體輸入與預期輸出（實測 #4、#5）：

| 情境 | release 前 | release 後（69b5a91） | release 後（be2c4c6） |
|---|---|---|---|
| systemd-user，目錄內只有 hold 檔 | `~/.config/systemd/user/serialwrap.service.d/90-serialwrap-update-hold.conf` | 檔案與目錄都不存在 | 留下空目錄 |
| systemd-user，目錄內另有 `10-owner.conf` | hold 檔與 `10-owner.conf` | hold 檔移除，`10-owner.conf` 與目錄保留（`remove_dir` 的 ENOTEMPTY 被忽略） | 同左 |
| systemd-system | `/etc/systemd/system/serialwrap.service.d/90-…conf` | argv 依序為 `sudo rm -f <conf>`、`sudo rmdir --ignore-fail-on-non-empty <dir>`、`sudo systemctl daemon-reload`… | 沒有 rmdir |

其他核對結果：

- release 回傳的 `Vec<CmdRecord>`，所有 sw-cli 呼叫端（`rollback.rs:745/849/860/889/1668/1682/2249/3046/3059/3644/3654/3995`）都只取 `Result`，不檢查 `ran` 的 rc。所以 rmdir 回 rc≠0（例如目錄不存在）時只會留在 `ran`，不會改變 release 判定。
- `FsOps::remove_dir` 的預設實作回 `Unsupported`（不是 panic）。`FaultFs`／`Unconfigured`／`RealFs`／`IsolatedSystemdFs` 都已補上委派。

1. [Low] `crates/sw-pal/tests/service_ctl.rs:1027`（`systemd_system_release_rmdirs_dropin_dir_ignoring_non_empty`）
   - 問題：測試名稱與 commit 都寫「best-effort、失敗不影響 release」，但測試沒有替 `rmdir` 腳本化非零 rc。`FakeSysEnv` 對未登記的命令一律回 rc 0（`fakes/sysenv.rs:399`），所以只驗了 argv 順序。
   - 變異 #6 把 `systemd.rs:421` 改成失敗即回傳錯誤，44 支仍全綠，「忽略失敗」的語意沒有防線。
   - 修法：在該測試的 `FakeSysEnv::builder()` 加 `.command(&["sudo","rmdir","--ignore-fail-on-non-empty","/etc/systemd/system/serialwrap.service.d"], 1, "", "rmdir: failed to remove …: No such file or directory")`，並保留 `expect("system release")`，讓 #6 的變異轉紅。
2. [Low] `crates/sw-pal/src/service_ctl/systemd.rs:139-163`（`rollback_hold`）
   - 問題：hold 失敗時的回復路徑（驗證不符、`systemctl` 失敗）仍只移除 drop-in 檔，與本 commit 修好的 release 不一致。探針 #7 實測：systemd-user 下 hold 驗證不符後，`~/.config/systemd/user/serialwrap.service.d` 會留下空目錄。屬同一缺陷類別，但只在 hold 失敗時出現，對 systemd 無害。
   - 修法：在 `rollback_hold` 移除檔案後套用與 `release_named` 相同的 best-effort 清理（user：`let _ = self.fs.remove_dir(parent)`；system：`sudo rmdir --ignore-fail-on-non-empty <parent>`），並加一支回歸測試，條件同 #7：hold 驗證回 `Restart=on-failure`，斷言父目錄不存在。

另有一點不列發現：使用者原本就有的「空」`serialwrap.service.d` 也會在 release 後被移除。這個目錄空著時對 systemd 沒有語意，可接受。

### ea33090 docs(rust-refactor): 補 OD-25／OD-29 原生證據完整指標並勘誤 OD-26 執行緒前提

NO FINDINGS。

- 處理前次報告的 7f195f7 Medium 1、37166b3 Medium 1、6f7de84 Low 1、80514c5 Low 1、80514c5 Low 2：
  - OD-25 與 OD-29 每一項都改為完整 64 字元 sha256，並指向不會被 `cargo clean` 清掉的 `~/b-log/rust-refactor/od25/261027-213904/`、`od29/<run>/`（附 `SHA256SUMS`）。
  - TX-BASELINE 列刪除了殘留的「仍待非實作者審查」。
  - oracle 審查原文補上來源 commit `4689a92` 與 sha256。
  - `windows.rs:42` 註解與 OD-26 (c) 更正為「沒有並行建立行程的執行緒」。OD 原文以「勘誤 2026-10-27：原記…」保留原字樣，結論不變，不是改寫擁有者裁決。
- 能在 repo 內核對的雜湊全部相符（驗證 #22、#23）。
- 誠實揭露：OD-25 原生證據是 21:3x 重跑後保存的，並註明「原 7f195f7 當次輸出未保存」，沒有把重跑包裝成原始結果。
- 本機無法核對的部分：`~/b-log/...` 在擁有者 host，Windows 原生 `6F1P／7P`、`ok:true` 與 exe sha256 無法重跑。指標本身完整，本報告登記為「指標齊備、原生實跑仍取信於擁有者 host 證據」。

### 3130f73 test(stage1): Spike 3 分開記錄案例的替身改用虛擬 perf_counter_ns

NO FINDINGS。

- 依前次報告 63d9bdb Low 1 的建議，對 `module.time.perf_counter_ns` 注入虛擬時鐘：每次讀取前進 1 µs，30 ms 排程延遲與 break→lease 到期的等待都改用 `advance_s`。judge（`scripts/stage0/spike3_win_wal.py`）沒有改。失敗訊息補上 `lease_acquired_ns` 與 `append_latency_ms`。
- 覆蓋範圍核對：
  - spike3 的 lease、break、rotation、append 延遲都用 `perf_counter_ns`（`spike3_win_wal.py:111/123/156/165/603/740-752`），都被虛擬時鐘涵蓋。
  - `time.monotonic` 的 drain／ready 逾時迴圈仍走真實時間，但 `sleep` 已改成 no-op，不影響判定。
  - pytest 的計時用 `perf_counter`（不是 `_ns`），不受影響。
  - monkeypatch 在測試結束時還原。
- 反證：舊替身加 0.4 s 真實延遲會紅，新替身會綠（#17、#18）。lease 縮到 10 ms 仍會紅（#19），沒有削弱斷言。連跑 40/40 PASS（#16）。

### 57fe9a9 test(policy): 以 R-21 防線約束整檔豁免的審查原文並將防線測試登記入 L1 鎖

- 凍結鎖只有 add-only，新增 12 筆（#8、#9、#10）。addenda `CHG-S1X-L1-DEFENSE-LOCK-01` 逐名列出的清單與鎖的差集完全相同。前次報告 fdbb148 Low 1 已處理。
- 釘選的 4 支正規式與 engine@2dcf2f8 `r21_secret_scan.py:14-17` 逐字相同，structural／credential 的分組也與 engine 的 `check()` 相同。

1. [Medium] `tests/test_policy_r21_allow_guard.py:54-63`（`_marker_re`）與 `:71-74`（`test_pinned_patterns_match_engine_when_available`）
   - 問題：沒有 engine 時（CI 的 `tests.yml` 不裝 `policy_check`），marker regex 只取 `.project-policy.yml` 的 `secret_scan.markers`。本 repo 沒有設這個鍵，所以 `marker_re` 為 `None`，整個 marker 斷言被跳過。
   - engine 實際使用 `resolve_markers()`，等於 baseline `policy_check/data/secret_scan_defaults.yml` 的 `markers: [bgw720, build20]`，聯集 repo markers，再扣掉 public_names。fallback 漏了 baseline。
   - 同時，engine 端的 R-21 對 allow 檔是整檔豁免（`_is_exempt`），所以這支測試是該檔 marker 外洩的唯一防線。
   - 變異 #13：在受保護檔加入 `BGW720`，沒有 engine（等同 CI）時 3/3 PASS，有 engine 時才 FAIL。
   - 這與模組 docstring 宣稱的「不可匯入時改用釘選副本（仍執行，不 SKIP）」、commit 與 addenda 所說的「credential／marker 命中須為 0」不符。漂移檢查在沒有 engine 時直接 `return`；有 engine 時也只比 4 支正規式，不比 marker 集合。
   - 修法：
     - 在測試加入 `_PINNED_BASELINE = {"markers": ["bgw720", "build20"], "public_names": ["brcm","broadcom","airoha","mtk","mediatek","prplos","prplog","marvell"]}`（取自 engine@2dcf2f8）。
     - fallback 改為 `(baseline.markers ∪ repo.markers) − (baseline.public_names ∪ repo.public_names)`，與 `resolve_markers` 同式；集合為空時 `pytest.fail`，不要回 `None`。
     - `test_pinned_patterns_match_engine_when_available` 在有 engine 時加一行 `assert resolve_markers(_config()) == <釘選式解析結果>`。
     - 修好後，以 #13 的變異確認沒有 engine 時也會轉紅。

### aed5d02 docs(rust-refactor): LOCAL-EVIDENCE 列補記雲端非實作者審查結論

NO FINDINGS。

- 只在 `CHG-S1X-WIN-COM-BRIDGE-LOCAL-EVIDENCE-01` 列尾附加一句話。逐項與前次兩份報告核對：
  - `0e97195_d2118cd-opus.md` 第 48 行確實是 7c94c31 的 2 個 Low。
  - `d2118cd_be2c4c6-opus.md` 對 9a710b6 確實判 NO FINDINGS，並含修正前會紅的反證。
- 保留「9P 根因仍屬推論」，沒有誇大。

### 69b5a91 test(docker): docker smoke 失敗訊息附上 selftest 非 PASS 項

- 只在 `assert r.returncode == 0` 的訊息附加 `_selftest_diag(tmp_path)`，判定與斷言沒有改。
- 欄位 `verdict`、`complete`、`checks[].id/verdict/reason` 與寫入端 `tools/uart_stdio_bridge/src/uart_stdio_bridge/selftest/runner.py:69-91` 一致。
- 本機沒有 docker daemon，這支測試 SKIP（#20），診斷函式以 #21 直接驗證。

1. [Low] `tests/test_docker_l15.py:479-485`（`_selftest_diag`）
   - 問題：只攔 `OSError`／`ValueError`。報告是合法 JSON 但頂層不是 dict 時（#21 實測為 list），`report.get` 會拋 `AttributeError`。這發生在 assert 訊息求值期間，會讓測試以 `AttributeError` 結束，連原本的 `_tail(r)` 輸出都被吞掉，與「只增加診斷、不影響判定」的目的相反。寫入端正常只寫 dict，所以列 Low。
   - 修法：解析後加 `if not isinstance(report, dict): lines.append(f"{rel}: 頂層非物件（{type(report).__name__}）"); continue`，或把整個函式包在 `try/except Exception as exc: return f"\n--- selftest ---\n診斷失敗：{exc!r}"`。

## High／Medium 摘要

| 嚴重度 | commit | 位置 | 摘要 | 重現證據 |
|---|---|---|---|---|
| Medium | 57fe9a9 | `tests/test_policy_r21_allow_guard.py:54-63`、`:71-74` | 沒有 engine（CI）時，marker 防線因漏了 engine baseline markers（`bgw720`、`build20`）而為 `None`，整段被跳過。受保護檔又在 engine 端整檔豁免，所以 marker 外洩在 CI 上沒有任何防線 | 驗證 #13：受保護檔加 `mutant: BGW720 build` 後，沒有 engine 時 `3 passed`，`PYTHONPATH=<engine@2dcf2f8>` 時 `1 failed` |

High：無。
