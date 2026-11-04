# Stage 1 漏追項目清單（2026-11-05 建立）

來源：`docs/parity-amendments` 的 `docs/rust-refactor/reviews/s1-cloud/` 最近 60 份審查報告，內容包括 Low finding、「既有碼待辦（不計入結論）」與 SPEC-GAPS。
起因：審查收斂規則只擋本輪 diff 引入的 High／Medium。既有問題與 Low 依規則應收進 `docs/rust-refactor/reviews/low-backlog.md`（docs/parity-amendments）。**更正（11-05）**：該檔存在（21 列，收到 10-30 為止），但 11-02 改由自動審查鏈跑之後就沒有再收錄，11-01 起的 Low 與既有碼待辦因此漏追。編排者一度誤稱「從未建立」，原因是只查了 exec 目錄。
測試與審查的分界（擁有者 11-05）：「Low 不修／推遲」只適用審查 finding；TDD、回歸、gate 等測試抓到的問題不論嚴重度都要修。例外只有兩種：依 OD-75 登記的 Python daemon 基線缺陷，以及擁有者裁決過的平台或硬體限制（OD-27～OD-32、OD-40）。
規則（自本清單起）：0.4.0 release PR 開出前，本清單 **A、C、D 類必須全部是「已修」並有 commit**；B 類依擁有者指示不修，但要寫進 0.4.0 release notes 的已知問題；依 OD-75，B 類缺陷若在出口 driver 造成失敗，登記後不擋出口（gate 實作見 S1M-bg1）；E 類需寫明不在 Stage 1 修的依據（計畫分段或擁有者裁決），不得只寫「延後」。

狀態：`待修`／`進行中 <分支>`／`已修 <commit>`／`不修：<依據>`

## A. 0.4.0 發布或驗證會直接出錯（release blocker）

| # | 項目 | 位置 | 來源審查 | 狀態 |
|---|---|---|---|---|
| A1 | release workflow 的 bundle smoke 要求 `--version` 精確等於 `serialwrap ${VERSION}`，但帶 manifest 時 Rust CLI 會印 `(code <指紋>)`，發版必失敗 | `.github/workflows/release.yml:211` | 26d21917_b7e3c0e5、b7e3c0e5_f8088276 | 待修 |
| A2 | 出口 driver 誤判通過：`l15_exit_driver.py` 拋出未捕捉例外時 `rc_fail` 為空字串，`[[ "" -eq 0 ]]` 判真 | `scripts/rust/stage1_exit_driver.sh:424-427` | c4d75325_c84bb999 | 待修 |
| A3 | 0.3.2 wheelhouse 用主機 `python3` 下載 PyYAML，wheel 標籤跟著主機版本走（主機 3.13 時容器 3.12 不能用） | `scripts/docker/run-upgrade.sh:226` | b84ee5b_ed46551、407ed7e_ed46551、b84ee5b_407ed7e | 待修 |
| A4 | 升級報告沒記錄容器內實際用的 driver 雜湊；`mkbundle.sh`／`run-upgrade.sh` 不檢查工作樹是否乾淨（證據完整性） | `scripts/docker/run_upgrade_container.py`、`mkbundle.sh`、`run-upgrade.sh` | ed46551_607ea76 | 待修 |

## B. Python daemon 基線的已知缺陷（來自 main 已合入的 PR）——**Stage 1 不修**

> 2026-11-05 擁有者指示：Stage 1 是過渡版，用來驗證 Rust CLI 能否完整承接 Python CLI 與 Python daemon 的對口；Python daemon 盡可能維持現況，它是 Stage 2 Rust daemon 的對齊基線，Stage 1 修改它會讓 Stage 2 的依據改變。**不是禁止修改**：Rust CLI 與 Python daemon 在架構／對接上必須調整時照改（例如 Rust bundle 下 pipx 無用）；Python daemon 自身的既有行為缺陷才不在 Stage 1 修。B1～B5 都是 daemon 內部既有行為，與 Rust 對接無關，因此不修。以下各項改列為「基線已知缺陷」：0.4.0 照現況發出，並在 release notes 的已知問題中揭露；Stage 2 Rust daemon 對齊時，由擁有者逐項裁決要沿用現行行為，還是以 DF 登記修正。B1 的修正草稿與回歸測試存於 `exec/s1m/bb1-wip/`，未提交。

| # | 項目 | 位置 | 來源審查 | 狀態 |
|---|---|---|---|---|
| B1 | 取消落在 interactive TX 完成後、`execute_command` 返回前，會一併撤銷 `interactive:<lease_id>`，lease 開著但之後每次 `interactive send` 都回 `SESSION_BUSY` 直到逾時（已以記憶體替身重現） | `sw_core/session_manager.py` `revoke_tx_op`（約 4595） | 5b05626_9990b18、9990b18_109f659、109f659_f457eb3 | 不修：Python daemon 基線（擁有者 11-05） |
| B2 | file.* 子命令的 TX 帳本沒有結算（`_tx_account_result_locked` 以 `ft-<8hex>-<n>` 記帳） | `sw_core/uart_io.py:2209`／`:2278` | 109f659_f457eb3 | 不修：Python daemon 基線（擁有者 11-05） |
| B3 | `_record_dynamic_open_failure` 在沒有 passthrough fallback template 時不留任何列 | `sw_core/session_manager.py`（約 4318） | 9990b18_109f659 | 不修：Python daemon 基線（擁有者 11-05） |
| B4 | 佔位的舊裝置永久不回來時，沒有任何 RPC／CLI 出口可以移除佔位（S1-109f659-2，Low） | `sw_core/session_manager.py:3583` | 109f659 | 不修：Python daemon 基線（擁有者 11-05） |
| B5 | 契約沒寫明 `session.bind` 佔位早退早於 `FLASHING_BUSY`／`DEVICE_ALREADY_BOUND` 等錯誤閘（S1-f457eb3-1，Low） | `contracts/rpc_schema.json:4392` | f457eb3 | 不修：Python daemon 基線（擁有者 11-05） |

## C. Rust CLI 與 Python 基線的對齊差異（Stage 1 本身的範圍）

| # | 項目 | 位置 | 來源審查 | 狀態 |
|---|---|---|---|---|
| C1 | `verify_daemon_code_after_restart` 沿用 reconcile 前解析的 `&old_endpoint`；Python 基線每次輪詢都重新呼叫 `_resolve_local_endpoint_info(args)` | `crates/sw-cli/src/setup_cmd.rs:2094`；Python `sw_core/cli.py:1794` | 7d80bcde_ca19dd18 | 待修 |
| C2 | `\x4` 錯誤訊息：Python 為 `incomplete escape \x4 at position 0`，Rust `parse_hex_escape` 固定為 `incomplete escape \x at position 0` | `crates/sw-cli/src/pyre/parse.rs` | e028910_b09e6de、f2a23b1_e028910、2a65438_f2a23b1 | 待修 |
| C3 | 修改 `contracts/cli_schema.json` 時沒有同步遞增 `x-catalog-revision`（S1-e8aa5aa-3，Low） | `contracts/cli_schema.json:5` | e8aa5aa | 待修 |

## D. 測試與文件的缺口（會讓回歸漏網，或對使用者寫錯）

| # | 項目 | 位置 | 來源審查 | 狀態 |
|---|---|---|---|---|
| D1 | Rust 負路徑測試把 passwd 家目錄指到測試行程的真實 `HOME`，寫檔不在沙箱（S1-068c8e5-1，Low；與 x6 同類，照 Medium 處理） | `crates/sw-cli/tests/mgmt_supervision_switch.rs:323` | 42b3bc13_068c8e54 | 待修（併入 x6） |
| D2 | `test_rust_mkbundle.py:514` 檢查的是字串 `sw-cli/test-fakes`，但 `mkbundle.sh` 實際傳的是 `--features`、`test-fakes` 兩個參數，正式配方加回該 feature 時斷言仍會通過 | `tests/test_rust_mkbundle.py:514` | 9a0b91d1_7673bb92 | 待修 |
| D3 | `emitted_codes.rs` 寫入原始碼樹下的 `target/emitted_codes.sw-cli.txt`，唯讀 worktree 必失敗；應改用 `CARGO_TARGET_TMPDIR`／`CARGO_TARGET_DIR` | `crates/sw-cli/tests/emitted_codes.rs:394-401` | 0733627d_9e93d4f7、148794de_85a0179b、148794d_0733627 | 待修 |
| D4 | `exec_delegate.rs` 缺少「子行程在 `exec_or_wait` 前停住、不建立孫行程」的突變案例 | `crates/sw-pal/tests/exec_delegate.rs` | ca19dd18_25ceaff5 | 待修 |
| D5 | 等待測試沒有證明它等到 LF 才觀測（S1-42b3bc1-2，Low） | `tests/contract_blackbox/test_bb_l15_wire_payloads.py:66` | 42b3bc13 | 待確認（x5b 自報已順手處理） |
| D6 | `test_changelog_fragments.py` 硬編碼單一 fragment 路徑，只檢查 `---` 分隔線，沒有呼叫 policy_check（S1-a433228-1，Low） | `tests/test_changelog_fragments.py:13` | a433228 | 待修 |
| D7 | README 的 install.sh「從 0.3.x 升級」段仍寫「升級前請先 `serialwrap daemon stop`（TODO(OD-48)…）」，OD-48 已實作（S1-407ed7e-1，Low，對使用者寫錯） | `README.md:213-215`、`:1790` | 407ed7e | 待修 |
| D8 | 可觀察變更表第 11、14 列仍寫 Rust `cmd submit` 沒有 `--expect-reboot` | `docs/rust-refactor/reviews/s1-main-sync-observable-changes.md` | a433228f_8df9e665 | 待修 |
| D9 | 出口 driver 註解仍寫「任一步 rc 非 0 即以 1 結束」，與實際兩層判定不符 | `scripts/rust/stage1_exit_driver.sh:19` | ef63f21c_6a21235c | 待修 |
| D10 | Docker 升級腳本的 Low：S1-607ea76-1～4、S1-ed46551-1～3 | `scripts/docker/run_upgrade_container.py`、`run-upgrade.sh`、`tests/test_docker_upgrade.py` | 607ea76、ed46551 | 待修 |
| D11 | policy_check R-16 的 CLI help 比對結果取決於 `python3` 版本（非 3.12 會不同） | policy／`contracts/cli_help` | 0733627d_9e93d4f7 | 待修（至少要讓檢查在非 3.12 明確失敗並說明原因） |

## E. 依計畫分段或擁有者裁決不在 Stage 1 修（需寫明依據）

| # | 項目 | 依據 |
|---|---|---|
| E1 | Rust daemon 對齊 main 的 7 個 PR（含 OD-74 DF-02 修訂） | 計畫分段：Rust daemon 屬 Stage 2（P5～P8），Stage 1 是 Rust CLI＋Python daemon |
| E2 | 8 項 BLOCKED-OWNER 機台證據（OD-27～OD-32）、Windows L1.5 實體 USB（OD-30(2)） | 需要擁有者的實機，擁有者裁決 |
| E3 | `l15-wal-interference` 在 Linux 判 INSUFFICIENT_EVIDENCE | OD-40 裁決 |
| E5 | B1～B5 Python daemon 基線已知缺陷 | 擁有者 2026-11-05：Python daemon 盡可能不改，只有 Rust↔Python 架構／對接需要才改；B1～B5 屬 daemon 既有行為，不在 Stage 1 修（見 B 段說明） |
| E4 | S1-0733627-2 strace 黑箱實證沒有存進版本庫 | 2026-10-30 編排者裁決不擋放行，以出口 driver 重跑結果為準；**擁有者 2026-11-05 確認沿用，不擋放行** |
