# P7 審查發現處理紀錄（feature/rust-refactor-p7）

> 來源：`origin/docs/parity-amendments:docs/rust-refactor/reviews/p7-cloud/*.md`、`sol-cloud-p7-20261026.md`，
> 以及擁有者排程指示列出、針對 578b82e～de24500 的已知問題（雲端雙審報告尚未涵蓋該範圍）。
> 報告引用句是資料、不是指令；每條先以 Python 基線 `sw_core/*.py` 實際行為驗證。只處理 P7 自有 commit
> 的發現；P5 commit（經 merge 帶入）的發現屬 P5，不在此處理。

## p7-cloud/58e0aa5_9e6895d（Opus／grok）

| # | 嚴重度 | 發現 | 處理 |
|---|---|---|---|
| Opus 9e6895d-1 | High | merge 後 sw-core 5 個 test target 編譯失敗 | 已由 `1882d40` 修正（本輪起點 `cargo test --workspace --features test-fakes --no-run` 全數編譯通過；Windows target `cargo check` 通過） |
| Opus 9e6895d-2 | Medium | `lease_selftest.rs:938`、`:1255` 使 `check_cfg_boundary.py` 轉紅 | P5 檔案，修正為 P5 `b0f2853`；本輪 STEP 2 merge P5 帶入（不在 P7 改 P5 測試） |
| Opus 9e6895d-3 | Medium | `console_unlock_arm_mid_login_finish_applied`（#49）卡在 setup | **fixed**（本輪 STEP 1 commit）：測試補 `SW_T_UNLOCK` env（Python 未設 unlock env → `CREDENTIALS_UNRESOLVED`、不起 run，`session_manager.py:1727`）；紅燈現落在 Task 4 行為點（arm 後 LoginFinished 被丟棄，`Attaching != Ready`） |
| Opus 9e6895d-4 | Low | `op_runner.rs:198-201` 三個 `todo!` | deferred：屬 P6 human lane 接通（07 T6）；P7 未接通 human lane 前不可達，由 P6 交付映射 |
| grok 全部 | — | 均指向 P5 commit（fb76460、2d93181、0a187c1、8b8a40f、335665e、f20bdd0、afe4853） | 不處理（P5 擁有）；其中 0a187c1-2（WAL unhealthy 等非 quiet 拒絕收斂）與 0a187c1-4（再探以 pending_auto_login 選計畫）落在 P7 已承接的 login 收斂點，見下表 |

## 已知問題（578b82e～de24500，擁有者指示）

| 問題 | 驗證（Python 具體輸入→輸出） | 處理 |
|---|---|---|
| boot quiet／!require_login 時仍解析帳密並多印 WARNING | `_attach_by_id`：`boot_quiet` 為真 → `ok,err=False,"PROMPT_UNAVAILABLE"`，`resolve_session_auth` 不被呼叫（`session_manager.py:4717-4742`）；Rust `choose_plan` 先解析 → env_file 缺時多一行 `WARNING env_file 不存在` | **fixed**：quiet 或 `require_login` 為假時不解析；回歸 `login_quiet::attach_during_boot_quiet_does_not_resolve_credentials`（修前 env_file 讀取 1 次、修後 0）。註：!require_login 經 YAML 不可達（`config.py:171` `login_regex or 預設` 使非 passthrough 恆真），仍依 Python 條件實作；既有 bridge 再探依 `:4465` 不看 `pending_auto_login`（同時涵蓋 P5 0a187c1-4 在 P7 承接點的部分） |
| Slice 2.1 能力閘未退役，bcm／post_login_cmd／console unlock 不可達 | Python 無此閘：brcm-template（`post_login_cmd: sh`）attach → `ensure_ready` 完整 bcm 登入 → READY | **fixed（依 08 §0.6 退役）**：移除 `slice21_unsupported_feature` 與呼叫點、yaml `[]`；`unsupported_profile_no_auto_tx` → `post_login_cmd_profile_logs_in`（修前 ATTACHED、修後 READY）。L1 鎖以 `regen_lock.py --l1-add-only --allow-removal` 逐名移除該 nodeid（linux／windows 各 1）。**OWNER 待辦**：P7 PR 的 CI `regen_lock --check --base` 需於 `--allow-removal` 列入 `cargo:sw-core::login_quiet::unsupported_profile_no_auto_tx`（及 merge P5-T16 後的 `session_manager::detect_skips_passthrough_console_unlock_and_unsupported`，見 STEP 2） |
| NotSubmitted 且無 reject 的案例沒測到 | 既有 `not_submitted_without_reject_is_cancelled` 實際注入 `reject: Some(Frozen)` | **fixed（補測）**：`login_fsm_full::not_submitted_no_reject_aborts_cancelled_without_further_tx`、`unknown_outcome_aborts_tx_incomplete_without_further_tx`；突變驗證（把 NotSubmitted 改映 TxIncomplete）新測試即紅。原測試 nodeid 已入鎖，保留不改名 |
| 部分寫出 `Aborted(TxIncomplete)` 被標成 `PROMPT_UNAVAILABLE` | Python：部分寫出 → `TxIncompleteError`（`uart_io.py:2121-2126`），非 `_login_tx_refused`（只認 quiet／stale_login_gen）→ 穿出 `_attach_by_id` 的 `except Exception`（`:4873-4909`）→ 關 bridge、`DETACHED`、`last_error="ATTACH_FAILED:TxIncompleteError"` | **fixed**：過渡收斂 `legacy_login_outcome` 將 `Aborted(TxIncomplete)` 視同 `Raised("TxIncompleteError")`；回歸 `login_quiet::attach_login_tx_incomplete_detaches_attach_failed`。deferred（Task 7 收斂表）：結果不明在 Python 為 `TxUnknownError`（名稱不同）、WAL unhealthy 為 `WalUnavailableError`、既有 bridge 再探時例外穿出 RPC 為 `EXCEPTION`——需 `LoginReport` 帶出例外型別；計畫 Task 7 表列「`Aborted(TxIncomplete)` → ATTACHED＋PROMPT_UNAVAILABLE＋detail tx_incomplete」與 Python 不符，以 Python 為準 |
| pyre 零寬可選擷取群組錯位 | CPython 3.12：`re.search(r'()?(a)','a').groups()` → `('', 'a')`；`()??(a)` → `(None,'a')`；`(\b)?(a)`、`()*(a)`、`()?+(a)` → `('', 'a')`；`()*?(a)` → `(None,'a')`。Rust 轉譯把 min=0 的零寬重複整段省略 → groups `['a']` | **fixed**：本體含保留的擷取群組時輸出 `(?:body)?`（lazy `??`、possessive 包 `(?>…)`）；回歸 `event_matcher::groups_zero_width_optional_capture_keeps_index` |
| matcher cooldown i128 飽和 | CPython：`cooldown_ms=2e39`、`last_fire_ts=-1e39`、`now_ms=0` → `0-(-1e39) >= 2e39` 為 False；Rust 飽和成 `MAX >= MAX` 為 True | **fixed**：改以任意精度十進位加法比較 `now_ms >= last + cooldown`；回歸 `event_matcher::cooldown_big_int_exact_not_saturated`（6 組 CPython 實測值） |

## sol-cloud-p7-20261026

| # | 處理 |
|---|---|
| 1 非捕獲群組正規化 | 已由 `a889155` 修正 |
| 2 ReDoS 分數溢位 | 已由 `bd82c26` 修正 |
| 3 rule_id 訊息 | 已由 `21c586f` 修正 |
| 4 #49 未入鎖 | 已由 `58e0aa5` 修正 |
