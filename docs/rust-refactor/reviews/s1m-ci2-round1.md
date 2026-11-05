# S1M-ci2 第 1 輪：hosted Windows Cargo 可攜性

分支 `feature/s1m-ci2`，起點 `5cac8744bb28b780b18ffa952cd1d9b199a42f6f`。範圍只有 brief 列出的六支測試；未提供本子項的上一輪 A／B 審查，因此沒有額外承接的 High／Medium。未修改 Rust 產品邏輯、Python daemon、契約、judge 或凍結鎖。

原始失敗證據為 `~/b-log/rust-refactor/exec/s1m-cilogs/j4-rust-windows-hosted.log`。本輪命令輸出保留於 `target/s1m-ci2/`。

## 六支失敗的根因與修法

| 測試 | 根因 | 修法／處理 |
|---|---|---|
| `sw-cli::skill_cmd::skill_default_windows_outputs_skill_windows` | hosted checkout 的 skill 資產含 CRLF。產品 `run_skill` 已按 Python 文字讀取語意做 universal newlines；測試的原始內嵌字串再以 `replace('\n', "\r\n")` 計算期望值，會重複加入 CR。 | `.gitattributes` 固定 `sw_core/assets/**` 為 LF；保留原測試逐位元組斷言。fixed `25b48159`。 |
| `sw-pal::oserror_golden::listdir_enoent_matches_golden` | `FormatMessageW` 訊息隨系統語系改變；runner 為英文，golden 為繁中，兩邊 errno 都是 2。 | golden 的例外類別、errno、winerror 不變；Windows 訊息以同機 Python 3.12 `ctypes.WinError` 取得。仍完整比對 `strerror`，另加 `winerror` 斷言。fixed `25b48159`。 |
| `sw-pal::rpc_client_conformance::tcp_refused_and_gai_and_timeout0_match_python` | 失敗在 `tcp_timeout0`；10035 的系統訊息為英文，golden 為繁中。 | 同一獨立 Python oracle 只替換期望值中的語系文字，完整訊息、errno、winerror 仍相等；TCP refused、DNS 與 timeout 的其餘 golden 不變。fixed `25b48159`。 |
| `sw-pal::sysenv_mgmt::run_windows_acp_decode_golden` | golden 假設 ACP／cmd 輸出都為 cp950。runner 實際輸出為 `ä¸\u{ad}æ–‡\n`，不能假設 OEM／console code page 等於 ACP。 | 同一 `cmd /d /c echo 中文` 分別由 PAL 與原生 Python `subprocess.run(..., text=True, encoding='mbcs')` 執行並逐字比對 stdout，另要求 rc=0、stderr 為空；不改 PAL 解碼語意。fixed `25b48159`。 |
| `sw-proto::pyre::review_regression_tests::unicode_character_names_do_not_guess_and_cover_unicode_15_names` | 內嵌 `unicode_names.tsv` 轉成 CRLF；按 LF 切出的十六進位碼尾端帶 CR，解析失敗，`DIGIT ONE` 被當成未定義。 | `.gitattributes` 固定名稱表為 LF；不改解析器、名稱表內容或原測試。fixed `25b48159`。 |
| `sw-proto::pyre_golden::golden_compile_search_completion` | 與前項同因，具名字元的 compile／search／completion 與字元範圍錯誤優先序一起受影響。 | 共用名稱表的 LF 修正；原始四個不符項目與逐列比較器不變。fixed `25b48159`。 |

六支都適用 Windows，沒有新增平台 skip、移除案例或改變 PASS／FAIL／INSUFFICIENT 判準。擁有者與硬體項目維持原有 `BLOCKED-OWNER`。

## 先紅後綠與 hosted 推論依據

新增 `tests/test_rust_hosted_checkout.py`，以真實版本庫資料及 `.gitattributes` 建立隔離 Git index，再以 `core.autocrlf=true` 實際 checkout。八個參數涵蓋 Unicode 名稱表與全部 Rust 內嵌 skill／profile／shell 資產；同時要求沒有 CRLF 且位元組與來源完全相等。

修前 `checkout-red.log` 為 **8 failed**；加入 LF 規則後，`checkout-green.log` 為 **8 passed**。原六支在未修正起點的紅燈由指定 hosted job log 提供。沒有改寫既有 golden。

本機不能執行 GitHub hosted runner。LF 修法由相同 autocrlf checkout 的實際位元組回歸支持；語系修法使用 Python 自己的 WinError 格式，並先驗證其分類與數字等於凍結 golden，再逐字比較 PAL。ACP 修法依 `SysEnv::run` 的 `GetACP` 路徑，以 Python `mbcs` 作獨立對照，忽略 oracle 外層的 `PYTHONUTF8`。runner 的原始 log 已證明安裝 Python 3.12.3；oracle 使用 `py -3.12 -I -X utf8=0`，缺 Python、非零退出或非空 stderr 均直接 FAIL。

## 驗證

| 驗證 | 結果／證據 |
|---|---|
| 指定 `scripts/rust/win-test.sh --l1-set s1b_cargo --package sw-pal --package sw-cli`，PATH 補 Windows PowerShell 與 System32 | **869 passed、0 failed、8 skipped**；58 個執行檔，`finished=true`、exit 0、`overall=pass`、`l1_verdict=PASS`、`l1_reasons=[]`。原四支目標 nodeid 全為 passed。 |
| Windows `sw-proto --lib unicode_character_names_do_not_guess_and_cover_unicode_15_names` | **1 passed、0 failed、54 filtered out**；`windows-proto-names.log`。 |
| Windows `sw-proto --test pyre_golden golden_compile_search_completion` | **1 passed、0 failed、3 filtered out**；`windows-proto-golden.log`。 |
| 沙箱 `cargo test --workspace` | **1041 passed、0 failed、7 ignored**；`cargo-workspace.log`，exit 0。 |
| 沙箱 `cargo test -p sw-pal -p sw-cli -p sw-proto --features test-fakes` | **1028 passed、0 failed、5 ignored**；`cargo-affected-test-fakes.log`，exit 0。 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0；`cargo-windows-check.log`。 |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0；`cfg-boundary.log`。 |
| `python3 -m tools.contracts check` | `ok=true`、`findings=[]`、exit 0；`contracts.log`。 |
| `python3 tests/acceptance/regen_lock.py --check` | 案例鎖、屬性快照、統計及 L1 鎖結構皆 OK，exit 0；`regen-lock.log`。鎖檔零修改。 |
| 新增 checkout pytest | **8 passed**；修前同組 **8 failed**，逐支失敗 ID 已保存在 `checkout-red.log`。 |
| `tests/test_resource_markers.py -m memory_only`，`--noconftest` | **8 passed、1 deselected**；`resource-markers.log`。只驗 AST 稽核；未執行會再載入根 conftest 的 marker 註冊案例。11 個 SyntaxWarning 來自既有資料，未修改。 |
| `python3 -m policy_check --repo .` | **24 pass、1 fail、2 warn**；唯一失敗為起點既有 **R-07**，詳下段。沒有把該 FAIL 改為 PASS。 |

另以 `5cac8744` 與本分支提交作為 base／head 核對 R-09、R-18、R-21，確認實際變更具備 fragment、文件對齊及減敏檢查；這不取代上列完整 policy 的原始 FAIL。

Windows L1 報告為 `target/win-test-report/261106-025631-2174608.json`，對應 `l1-results-261106-025631-2174608.json`。`interactive_desktop`、`supervisor_runtime`、`windows_com` 仍列在 `evidence_insufficient`，不算已取得硬體或監管證據。兩支補驗為 Windows GNU 原生執行，不宣稱已在 hosted MSVC runner 重跑；hosted 的推論依據如上。

**R-07 起點重現**：第一份 policy 執行時 HEAD 仍為 `5cac8744`；`VERSION` 與 tag 完全未改，分別為 `0.4.0` 與最新 `v0.3.2`，記錄於 `policy-baseline-inputs.json`。另以 `git show 5cac8744:VERSION` 及同版 `.project-policy.yml` 建立隔離輸入目錄，只讀既有 Git metadata，以真正 policy engine 的 `--only R-07` 重跑：**0 pass、1 fail**、exit 1，同樣輸出 `VERSION (0.4.0) mismatches latest tag (v0.3.2)`（`policy-baseline-r07.log`）。沒有更動版本、tag、豁免 label 或規則。

Linux 測試只經 `target/s1m-ci2/sbox`：真實根目錄唯讀、僅本 worktree 可寫，另提供私有 `/dev`、`/proc`、`/tmp`、`/run`，遮蔽 Windows 掛載，保留唯讀 DNS resolver；PATH 排除 pipx，清除繼承的 SERIALWRAP／測試覆寫變數。純 checkout pytest 以 `--noconftest` 執行，只使用內建 `tmp_path`，避免載入會探測 production daemon 的根 conftest。所有 gate 都前景等待；私有 PID namespace 內執行後的 daemon 檢查均為 `namespace_test_daemons_remaining=[]`，namespace 退出後也無法留下其子行程。

未執行全套 pytest、slice gate、Docker 或出口證據 driver，遵循本 brief 的明確禁令。

## 不成立主張

無。

## SPEC-GAPS

本 brief 六項無契約缺口，也不需要範圍外的產品修改。GitHub hosted runner 未直接重跑；沒有把本機 Windows GNU 證據當作 hosted MSVC 證據。R-07 版本／tag 收尾屬既有 release 流程，保持 FAIL 並交編排者。

## 既有碼待辦

R-07 版本／tag 不一致，已在起點重現；R-19 advisory 保留，R-22 的 dangling reference advisory 為 3081 筆（本輪報告加入前為 3078 筆），未改規則或豁免。`crates/sw-proto/tests/common/mod.rs`、`crates/sw-cli/tests/support/mod.rs` 的既有 unused／dead_code 警告未修。未處理其他分支的 finding。此項為測試可攜性問題，由 pytest 與 Cargo 覆蓋，不需新增實體 UART regression case。

## Commit

- `25b48159` — `fix(ci): 修正 Windows Cargo 測試的語系與檢出行尾`。
- 本報告另以文件 commit 提交；所有 commit 均附指定三段 trailer。未 push，由 driver 接手。

REMAINING: R-07 起點既有 FAIL，留待 release/tag 流程收尾；本 brief 六項無未修。
