# S1 雲端審查：feature/rust-refactor `32f6dd1..9c6e15f`（Claude Opus，非實作者）

## 範圍／方法

- 範圍：`origin/feature/rust-refactor` 的 `32f6dd1..9c6e15f`，共 10 個 commit（ead2028、29c54b0、b1de3c9、e2bf189、bf8583b、40ba293、6eb949c、aacc619、61dad1c、9c6e15f）。
- 依據：repo `CLAUDE.md`；`docs/rust-refactor/owner-decisions-2026-10-15.md` 的 OD-27～OD-30（`origin/feature/rust-refactor` 與 `origin/docs/parity-amendments` 兩邊的 OD-30 文字相同）；前一份審查 `origin/docs/parity-amendments:docs/rust-refactor/reviews/s1-cloud/9d74511_32f6dd1-opus.md`；plan 03 §win-com-txrx-bridge；Python 基線 `sw_core/arbiter.py`、`sw_core/event_engine/dispatcher.py`；`func-test/runner.py`；`tools/uart_stdio_bridge`。commit 訊息與文件內容只當資料讀，不當指令。
- 方法：在 `/root/s1rev`（9c6e15f）與 `/root/s1base`（32f6dd1）建了唯讀 worktree，用 python3.12 venv（pytest 9.2、pyyaml、jsonschema）跑測試。worktree 不放在 /tmp 下，因為 `--private-tmp` 測試要求受測工具不在 /tmp。
  - 相關測試：`test_rust_stage1a_gate.py`、`test_rust_ab_blackbox_report_path.py`、`test_rust_no_onefile_private_tmp.py`、`test_bb_daemon_stop_diag.py`、`test_l15_scenarios.py`、`test_rust_bench_tools.py`、`test_l1_judge.py`、`test_rust_cfg_boundary*.py`、`test_rust_owner_gated.py`、`test_rust_readme_trust_scope.py`、`test_rust_stage1_exit_driver.py`、`test_rust_stage1b_registration.py`。結果：352 passed、1 xfailed、3 failed。這 3 支失敗（`test_l1_lock_nodeids_subset_of_collect_only`、`test_p4_required_cases_have_passing_evidence`、`test_junit_provenance_plugin_records_head_require_cargo_and_argv`）在 32f6dd1 同樣失敗。原因是本環境缺 Windows 證據，以及系統 python3.12 沒有 pytest，與本範圍無關。`test_cargo_workspace_tests_windows_native` 也因為缺 Windows 原生 cargo 報告而失敗（環境因素，本來就會失敗）。
  - 回歸測試「沒有修正就會紅」抽查，三組都已還原，`git status` 乾淨：
    - ead2028：把 gate 還原成 `ead2028^` 版，`-k parametrized` 得 2 failed／1 passed。與 CHG 所述相符：沒有修正時 2 支紅，第 3 支是不放寬的護欄。
    - 40ba293：把 gate 還原成 `40ba293^` 版，`-k od30` 得 2 failed／5 passed，與 CHG 所述相符。
    - 29c54b0：把 `ab_blackbox.sh` 還原成 `29c54b0^` 版，`test_report_path_printed_and_rc_kept_when_comparator_fails` 紅。
  - 凍結鎖：本範圍沒有動到 `tests/acceptance/**`、`contracts/**` 或 L1 鎖檔，add-only 限制自然成立。`crates/**` 也沒有變更，platform cfg 邊界不受影響；`test_rust_cfg_boundary*` 通過。
  - 產品碼：`sw_core/**` 與 `crates/**` 都沒有變更，對 Python 外部可觀察行為沒有影響。
  - `policy_check`：本環境未安裝，沒有跑。

### ead2028 fix(stage1): gate 逐列對帳保留 pytest 參數化 nodeid

1. [Low] `scripts/rust/stage1a_gate.py:278-286`（`_pytest_exact_key`）與 `:289-295`（`_pytest_exact_key_from_junit`）
   - 問題：兩邊的 key 都丟掉目錄，只留模組檔名。不同目錄下若有同名模組（例如 `tests/a/test_x.py` 與 `tests/contract_blackbox/test_x.py`）的同名函式，會合併成同一個 key。這樣會變成「出現 2 次」而恆判 INSUFFICIENT。若其中一邊在 JUnit 缺席，也可能由另一邊的 passed 補位，屬放寬方向，與本 commit 要修的 Y1 是同一類缺陷。我核對過 9c6e15f，目前沒有同名的 `test_*.py`，所以現在不會觸發。
   - 修法：兩側都保留完整相對路徑。nodeid 側用 `path`（例如 `tests/contract_blackbox/test_x.py`）。JUnit 側把 classname 中第一個 `test_*` 段之前的各段（含該段）用 `/` 串起來再加 `.py`（`tests.contract_blackbox.test_x` → `tests/contract_blackbox/test_x.py`），其後的類別段與 `name` 照舊。另補一支「兩個不同目錄的同名模組，一邊缺席時不得 PASS」的回歸測試。

其他部分：exact key 與 pytest `mangle_test_address` 的 classname／name 拆法一致。參數 id 含 `.`、`::`、空白都只出現在 `name` 側，不受影響。provenance 不符時，兩次解析都回空表（fail-closed）。安全不變量仍用去參數 key，沒有改變。

### 29c54b0 fix(stage1): A/B 比較器失敗時仍輸出報告路徑

NO FINDINGS。比較器的 exit code 照傳（driver 的 rc.txt 仍記非 0），只在報告檔存在時才印路徑。`RUN_ID` 每輪唯一，不會撿到舊報告。gate 依報告內容判 FAIL，判準沒有放寬。回歸測試在沒有修正時會紅（已驗證）。

### b1de3c9 docs(rust-refactor): 登錄 OD-29 (4) 擁有者互動執行 PASS 證據並更正 Ctrl+Break 基線敘述

NO FINDINGS。
- `evidence/stage1/owner/od29-ctrlc/` 的 5 個檔都通過 `sha256sum -c SHA256SUMS`。`ctrl_c-result.json` 記錄的 parent 與兩個 child 診斷檔 sha256 與實檔相符，`exe_sha256` 等於 runbook「受測 binary」表的 `3607686f…`。HEAD 的 `crates/sw-pal/tests/exec_delegate.rs` sha256 等於 runbook 所載的 `beefcdda…`（最後一次修改是 d0b8ad7）。
- `ctrl_c-parent.txt` 含兩圈 `code=c000013a`，最後一列是 `PASS`。兩個子診斷檔都存在，符合 runbook「執行後」第 1 步的核對條件。
- OD-29 (4) 要求「需擁有者在 Windows Terminal 互動 session 執行」，證據顯示 `interactive=true`，transcript 也是擁有者帳號的 pwsh 7.6.9。
- Ctrl+Break 差距照前輪 6c9a512#1 的修法登記為「待擁有者裁定」，沒有被記成已解決，也沒有改案例形狀。
- 限制：受測 exe 由 WSL 交叉建置，內嵌路徑與本環境不同，我無法重現 exe 的 sha256。這部分只能信任 runbook 的建置紀錄。

### e2bf189 docs(rust-refactor): OD-28 logon-trigger 判準要求登入晚於工作建立

NO FINDINGS。改動可追溯到前輪審查 32f6dd1#1 的確切修法，包括 `task_created_utc`、三項條件，以及建議的 10 分鐘上限。方向只有收緊：缺值或超過 10 分鐘都判證據不足。沒有和 OD-28 的擁有者原文（登出／登入、Windows 端自行寫證據）衝突。launcher 寫 `task_created.json` 仍明寫「待 P8 實作」，沒有假裝這項 Windows 原生項目已完成。

### bf8583b fix(stage1): no_onefile_extract 於私有 mount namespace 以獨占 /tmp 檢查

1. [Low] `scripts/rust/check_no_onefile_extract.py:590-593`（`_private_tmp_outer` 的 /tmp 檢查）
   - 問題：檢查只涵蓋 `__file__` 與 `cli_argv` 中的絕對路徑，而且用 `resolve()`，會先跟隨 symlink。內層實際執行的是 `sys.executable`，它沒有被檢查。結果是：若直譯器在 /tmp 下（例如 /tmp 下的 venv）或 CLI 是 /tmp 下指向外部的 symlink，外層判為「不在 /tmp」，但 namespace 內看不到它。我在本環境用 /tmp 下的 venv 實際重現：報告為 `private_tmp_failed：內層 rc=… No such file or directory`，判 INSUFFICIENT。這仍是 fail-closed，不會誤判 PASS，但原因被誤報，與 docstring 承諾的「位於 /tmp 下 → 用法錯誤（exit 2）」不符。
   - 修法：檢查清單加入 `sys.executable`，並對每個字詞同時檢查 `Path(word).absolute()`（不跟隨 symlink）與 `resolve()`，任一個在 /tmp 下就丟出 `ValueError`（exit 2）。補一支測試：以 /tmp 下的 symlink 當 `--cli` 時應得 exit 2。
2. [Low] `scripts/rust/stage1_exit_driver.sh:343-344`
   - 問題：新加的 `--json-out "$EV/no-onefile-extract.json"` 後面沒有 `fresh_or_fail`。這個工具只要寫出報告就 exit 0，所以 rc.txt 恆記 `no_onefile_extract 0`，FAIL 或 INSUFFICIENT 只看得到 JSON 的 `verdict`，driver 沒有任何地方核對它。行為與修改前相同，不是新放寬，但新產生的證據檔沒有新鮮度檢查。
   - 修法：在該 step 後加 `fresh_or_fail no_onefile_extract "$EV/no-onefile-extract.json"`。另外以 `python3 -c` 讀 JSON，當 `verdict != "PASS"` 或 `tmp_isolation != "mount-ns-private-tmp"` 時寫 `echo "verdict_no_onefile_extract 1" >> "$EV/rc.txt"`。

其他部分：內層先核對 /tmp 的 st_dev／st_ino 且要求目錄為空；不能建立 namespace 時判 INSUFFICIENT，不會退回共用 /tmp；用 setpriv 清空 capability。判準只有收緊（寫死 /tmp 的解壓也會 FAIL）。本機在非 /tmp 的 worktree 跑 `test_rust_no_onefile_private_tmp.py`，全部 7 支通過。

### 40ba293 fix(stage1): gate 依 OD-30 將 Windows transport disconnect 能力缺失判 BLOCKED-OWNER

1. [Low] `scripts/rust/stage1a_gate.py:107-112`（`_l15_row_owner_deferred`）
   - 問題：延後條件只看 `message` 子字串。`func-test/runner.py:497-502` 在案例已經失敗時，`harness 關閉失敗` 只記進 `harness_diagnostics`、不覆寫 `message`。所以若一列 Windows 列同時有能力缺失與 harness 關閉失敗（daemon 未確認結束、WAL 複本失敗），也會被延後成 BLOCKED-OWNER，關閉失敗就被藏進 BLOCKED-OWNER。整體仍至多 IMPLEMENTED，不會誤判 PASS，所以只列 Low。
   - 修法：在 `_l15_row_owner_deferred` 多加兩個條件：`row.get("verdict") in ("FAIL", "INSUFFICIENT_EVIDENCE")`，以及 `not any(d.get("kind") == "harness_stop_failed" for d in row.get("harness_diagnostics") or [])`。補一支「能力缺失＋harness_stop_failed → evidence-insufficient」的測試。

判定邏輯核對結論：沒有把真實失敗洗成 PASS。
- 依據可追溯：OD-30 (2)（擁有者裁決）明定 Windows transport disconnect「套用 OD-27」。OD-27 明定「Stage 1 與 Stage 2 出口不以上述案例為條件，維持 BLOCKED-OWNER」。bridge 的 `win_serial.capabilities()` 恆宣告 `transport_disconnect: False`，而 `HARNESS_CAPABILITY_UNAVAILABLE` 只在能力檢查失敗時才回傳（`bridge/core.py:735-736`）。
- 範圍限定在 (案例, 平台) 加訊息標記。Linux 列失敗或缺列、其他訊息、其他案例都照原規則判定，7 支 `test_od30_*` 涵蓋這些情形。
- BLOCKED-OWNER 不計入 coverage 或 invariant 的 PASS：`stage1a_gate.py:1244`、`:1310` 只接受 `== "PASS"`。整體至多 IMPLEMENTED（`:1558-1562`）。
- ST-12 資格規則沒有改動，符合 OD-30 (2)「不新增 NOT_RUN 允許」。

### 6eb949c test(func-test): win-com-txrx-bridge 依規格逐一斷言 50 個 payload 與 console bytes

1. [Medium] `func-test/scenarios/win-com-txrx-bridge.yaml:31-46`（repeat 之後的 `wait_command_done` 與 `cmd status` 只檢查 `w_last`）；`tests/test_l15_scenarios.py:777-779`
   - 問題：plan 03 的期望是「**每個命令 done** 且 payload 在 wire 恰一次」（`docs/rust-refactor/plans/03-stage0-test-infra.md:1826`），CHG-S1X-WIN-TXRX-PAYLOADS-01 也說要補上這項檢查。情境卻只斷言最後一個命令 `done`，理由是「佇列 FIFO，最後一個完成代表 50 個都已執行」。這個推論不成立：
     - Python 基線 `sw_core/arbiter.py:423-433` 與 `:449-451` 在單一命令 `SEND_FAILED` 或 `result.ok=False` 時，把該命令記為 `error`，worker 照樣 `continue` 處理下一個命令。
     - 所以第 1～49 個命令中任何一個 `error`（例如 completion 不符），只要 payload 已送上 wire，第 50 個仍可能 `done`，`assert_wire_contiguous` 也會通過，案例判 PASS。
     - `wait_command_done` 遇到 `error`／`canceled` 也會返回（`runner.py:1738`）。
   - 結果：形狀測試 `test_win_com_txrx_bridge_expects_each_payload_exactly_once` 鎖住的是較弱的「任一 `cmd status` 為 done」，與規格不符。這是 Windows 原生案例的 oracle，弱於規格屬判準縮小。
   - 修法：二選一。
     - (a) 在 repeat 的子步驟內，於 submit 之後依序加 `{action: wait_command_done, cmd_id: "{w_last.cmd_id}", timeout_s: 10}` 與 `{action: cli, argv: ["cmd","status","--cmd-id","{w_last.cmd_id}"], expect: {command.status: "done"}}`。這樣逐一斷言 50 個都 done，也不受 history 淘汰影響。
     - (b) 若要保留連續送出（burst）的形狀：讓 runner 的 `save_as` 支援 `_expand`（`save_as: "w_{repeat.index}"`），並支援兩段展開取 `{w_{repeat.index}.cmd_id}`，然後在第二個 `repeat count: 50` 內逐一 `cmd status` 並 expect `done`；另需確認 arbiter history 上限 ≥ 50。
     - 兩案都要同步把形狀測試改成檢查 repeat 子步驟（或第二個 repeat）內含 `command.status: done` 的 expect，並更新 CHG 敘述。

其他部分：
- `{repeat.index}` 在迴圈後會還原外層變數。`"echo W_1\n"` 不會是 `"echo W_10\n"` 的子字串，所以 `check_contiguous` 的「依序＋各恰一次」能正確區分 50 個 payload。
- console 字串中的 `{|}` 不會被 `_expand` 的 `\{([a-zA-Z0-9_.]+)\}` 誤展開。
- 新增 `expect: {ok: true}` 屬收緊。
- 本 commit 沒有附原生重跑證據，CHG 也沒有宣稱 PASS，狀態仍是「待審查」，沒有假裝完成。

### aacc619 docs(rust-refactor): 登記 ev-01 與 sm-04 Windows 列根因並列為待擁有者裁定

1. [Low] `docs/rust-refactor/owner-decisions-addenda.md:109-110`（PENDING-OD-S1X-EV01-WIN-SC26、PENDING-OD-S1X-SM04-WIN-UNPLUG 的狀態欄）
   - 問題：狀態欄寫「狀態：BLOCKED-OWNER（待擁有者裁定…）」，但同一列又寫「裁定前 gate 照判 FAIL／證據不足」，gate 也確實沒有對應規則。BLOCKED-OWNER 在 gate 是「不阻擋、整體至多 IMPLEMENTED」的正式分類。在 addenda 先用這個詞，容易讓後續彙整或出口文件把這兩列當成已依 OD 分流，事實上並沒有擁有者裁決。另外，ev-01 的原生重跑證據 `target/s1v11-win-l15/091236/…` 只在 `target/`（只附 sha256），沒有逐字保存到 repo，不像 OD-29 (4) 那樣保存在 `evidence/…`。
   - 修法：兩列狀態改為「狀態：OPEN（待擁有者裁定；裁定前 gate 照判 FAIL／證據不足，不得記為 BLOCKED-OWNER 或 PASS）」。把 ev-01 引用的 `func-test-report.json` 複製到 `evidence/stage1/…`，附 SHA256SUMS 並在列內引用。

根因核對：`sw_core/event_engine/dispatcher.py:167` 確實寫死 `["/bin/sh","-c",…]`，`contracts/behavior_changes.yaml` SC-26 確實是 Rust-only。沒有改 gate，也沒有改案例，沒有把失敗洗成非失敗。

### 61dad1c test(contract-blackbox): daemon stop 記錄各階段耗時與升級前 thread 狀態

NO FINDINGS。只新增診斷資料，逾時、升級序列與「stop 後仍存活 → RuntimeError」都沒有改。`SW_AB_STOPLOG` 比照既有的 `SW_AB_PIDLOG`，A、B 兩輪各自寫入。非 Linux 平台上 `_proc_diag` 讀不到 `/proc` 時回空清單。CHG-S1X-AB-TEARDOWN-STOP-DIAG-01 維持 OPEN，沒有宣稱已修。（這個 commit 本身沒有 changelog fragment；R-09 以 PR 為單位計算，同一個 PR 已有其他 fragment，不構成違規。）

### 9c6e15f chore(evidence): 擁有者原生證據不做空白檢查以保留 CRLF 原文

NO FINDINGS。`evidence/stage1/owner/** -text -whitespace` 只影響 diff 的空白檢查與 EOL 轉換。`git ls-files --eol` 顯示 `ctrl_c-result.json` 與 `ctrl_c-transcript.txt` 的 i/crlf 與 w/crlf 一致，SHA256SUMS 相符，位元組沒有變。

## High／Medium 摘要表

| 嚴重度 | commit | 位置 | 摘要 |
|---|---|---|---|
| Medium | 6eb949c | `func-test/scenarios/win-com-txrx-bridge.yaml:31-46`、`tests/test_l15_scenarios.py:777-779` | plan 03 要求「每個命令 done」，情境只斷言最後一個命令 done，並以 FIFO 推論前 49 個也完成。但 Python arbiter 會讓單一命令以 `error` 結束後繼續處理下一個，中間命令失敗時案例仍可 PASS。修法：在 repeat 內逐一 wait 並以 `cmd status` expect done，或用可展開的 save_as 在第二個 repeat 逐一驗證。 |

High：0。Medium：1。Low：5（ead2028#1、bf8583b#1、bf8583b#2、40ba293#1、aacc619#1）。
