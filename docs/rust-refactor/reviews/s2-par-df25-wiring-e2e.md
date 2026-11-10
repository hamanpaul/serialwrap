# S2-PAR-df25（DF-25）接線＋端到端驗收報告

- **對應工作包**：S2-PAR-df25（OD-77 B1，DF-25）
- **受測分支**：`feature/rust-refactor-p8`
- **受測基準 commit**：`32219f2b949343951deb2e8da482a3c86be44371`
- **工作樹狀態**：clean
- **審查發現對應**：處理 P8-32219f2-1 [Medium]，提供受測 commit 可追溯之驗收交付、完整命令結果與失敗/待驗收項目交代

---

## 1. 驗收結果彙總與可追溯日誌

本驗收於本地工作樹前景執行，命令、退出碼、結果狀態與持久化日誌雜湊彙整如下：

| 命令 | 特性 / 參數 | 退出碼 | 結果 / 摘要 | 日誌檔名（`target/s2-par/df25/`） | SHA-256 雜湊 |
|---|---|:---:|---|---|---|
| `cargo test -p sw-daemon` | `--features test-fakes --test dispatch df25_` | 0 | **2 passed**; 0 failed | `item2-df25.txt` | `66240d7717a9ae9b974607c289f8ffa8da1d0235a7870a0810174f31ae20bf9c` |
| `cargo test -p sw-pal` | `--features test-fakes` | 0 | **456 passed**; 0 failed; 17 ignored | `item4-cargo-test-sw-pal-fakes.txt` | `801af99b2c14c16e87d8258101f65ea47a86df93957196a463d22a3b051e1463` |
| `cargo test -p sw-pal` | *(無額外 features)* | 101 | **既有測試組織限制**（見下文說明） | `item4-cargo-test-sw-pal.txt` | `a3d61aecf5300bd4865b3ad24020fdbd359a7b3604c49fc8495b142a7636f92a` |
| `cargo test -p sw-core` | `--features test-fakes` | 0 | **738 passed**; 0 failed; 0 ignored | `item4-cargo-test-sw-core-fakes.txt` | `6b452bfd34a01dab597e7cc62dcda4e12cf1b2b2d6eca81aa40c731ebeea3ba8` |
| `cargo test -p sw-core` | *(無額外 features)* | 0 | **144 passed**; 0 failed; 0 ignored | `item4-cargo-test-sw-core.txt` | `3dc3eb46a200832254408c49601807d743a0ef4f60a58e2d1c6d6a571ce3e959` |
| `cargo test -p sw-daemon` | `--features test-fakes` | 0 | **308 passed**; 0 failed; 6 ignored | `item4-cargo-test-sw-daemon-fakes.txt` | `98ddadae4cec974f63a67d4e0efd6a17fcc0bbd9021119ea1f261f1ce081ca3b` |
| `cargo test -p sw-daemon` | *(無額外 features)* | 0 | **28 passed**; 0 failed; 2 ignored | `item4-cargo-test-sw-daemon.txt` | `bb73b719b26b2079cdf175e80eb2a02ef765d57c83dd17c44942984b160bedd8` |
| `cargo build --workspace` | `--all-targets` | 0 | **0 errors**; 無新增編譯警告 | `item4-cargo-build-all.txt` | `eca28e3eeca4b5a662c90c7c0d55d14dd098a483a0589dcd27c9d41b76fbd571` |
| `cargo check --workspace` | `--all-targets --target x86_64-pc-windows-gnu` | 0 | **0 errors**; Windows 交叉檢查通過 | `item4-cargo-check-windows.txt` | `6c1a6e78ba71c8bbcd875be7e5a804266ffb08192c1af1bd20a427adc42d1cca` |
| `cargo clippy --workspace` | `--all-targets` | 101 | **既有 rustc 1.96 clippy deny**（見下文說明） | `item4-cargo-clippy-all.txt` | `365f042e388e9511018b2f7e80fef0906168ab696e4ae02e2c180ca0d0f65559` |
| `cargo clippy --workspace` | `--all-targets --features test-fakes` | 101 | **既有 rustc 1.96 clippy deny**（見下文說明） | `item4-cargo-clippy-fakes.txt` | `a5f75b04a0d906b78880059c8530f0017aa200dc2a3009f9fe1b9165e9ef229a` |
| `cargo clippy --workspace` | `--all-targets --features test-fakes -- --cap-lints warn` | 0 | **0 errors**; 既有 warning capped | `item4-cargo-clippy-caplints.txt` | `b574a2fb59b31c243aab534123cf06e3567ff204da67a9b64f5cfd66e8c5339c` |
| `python3 check_cfg_boundary` | `scripts/rust/check_cfg_boundary.py` | 0 | **OK**; 無違規 cfg 邊界 | `item4-check-cfg-boundary.txt` | `1ca4a13146b2891c0e462123ac0cb848f92e6221f18b870a1b78c232d1956c35` |
| `python3 -m tools.contracts` | `check` | 0 | **ok: true**; 0 findings; 133 error codes | `item4-contracts-check.txt` | `6929f163b215310da664c3b68a88ceadb7b8ca48e8dbd57ea7ddf6af07762b0e` |
| `python3 regen_lock.py` | `--check` | 0 | **OK**; 鎖檔、案例快照與 L1 結構一致 | `item4-regen-lock-check.txt` | `fba64375fa6f21362fedc2fa03d1458de9bf3d2d4e5f7e2f927dccaf4e299804` |
| `pytest (touched)` | `tests/test_s1m_b1_interactive_cancel.py` 等 3 檔 | 0 | **43 passed** in 11.34s | `item4-pytest-touched.txt` | `37d7bcad61b3b5f979022d7e7576c41d9eff98526535c5d93853c37b02f4605a` |
| `pytest (full suite)` | `tests/` | — | **待驗收狀態**（依指示不在此跑，待編排者 host 驗證） | `item4-pytest.txt` | `545f9e2e4bf8f8ac7d3bc83f959523d54d96d48afad6c6448b62323f0bf8e0d6` |
| `python3 -m policy_check` | `--repo .` | 0 | **pass: 25, fail: 0, warn: 2** (advisory) | `item4-policy-check.txt` | `a76b0d4440610afa3602cca7ec6e7c09784276f7e0e592ac4491f7762cc52929` |

### 1.1 失敗與待驗收項目交代

1. **`cargo test -p sw-pal`（未帶 `--features test-fakes`）exit 101**：
   - **原因**：`crates/sw-pal/tests/` 底下數個整合測試（`legacy_supervision.rs`、`service_ctl.rs`、`registry_view.rs`、`lock.rs`）在檔頭未加 `#[cfg(feature = "test-fakes")]` 守衛，直接引用了僅於 `test-fakes` feature 下導出的模組（例如 `sw_pal::fakes` 與 `read_bt_ports`）。
   - **歸屬**：此為既有測試模組組織限制，非本包新增；`sw-pal` 單元測試 `cargo test -p sw-pal --lib`（61 passed）以及完整測試 `cargo test -p sw-pal --features test-fakes`（456 passed）均全數通過。
2. **`cargo clippy --workspace --all-targets` exit 101**：
   - **原因**：rustc 1.96.0 新增預設 deny lint `clippy::never_loop`，命中既有 `crates/sw-proto/src/pyre/parse.rs:587`：
     ```rust
     while self.peek() == Some('|') {
         return Err(self.error(
             "conditional backref with more than two branches",
             Some(self.pos),
         ));
     }
     ```
   - **歸屬**：該程式碼於 commit `9f309290` 即已存在，非本輪引入。在加上 `--cap-lints warn` 下 exit 0，無任何產品新增錯誤。本輪依規則不順手修改無關既有程式。
3. **全套 host pytest**：
   - **狀態**：**待編排者以 host 環境執行驗收**。
   - **原因**：依本輪 brief 明確指令「不要跑全套 pytest、slice gate、Docker（另派）」，全套 pytest 測試（含 PTY/live guard 等長時間與 host 相關項目）由編排者統一於 host 環境排跑。本輪完成範圍內之 Python 測試（`tests/test_s1m_b1_interactive_cancel.py`、`tests/test_acceptance_cases.py`、`tests/test_changelog_fragments.py`）43 passed 通過。

---

## 2. 跨模組實際接線核對

### 鏈路一：Interactive 命令送出與執行
- **RPC 入口**：`crates/sw-daemon/src/service.rs:1108`
  - 處理 `command.submit`，解析 `mode: "interactive"`、`source`、`selector` 與命令參數。
  - 經 `self.manager.route(&sid, SessionRequest::Submit(req), ROUTE_TIMEOUT)` 轉發至對應 session actor。
- **Session 核心排程**：`crates/sw-core/src/session_core.rs:2447` (`submit`) → `crates/sw-core/src/session_core.rs:2544` (`start_next`)
  - `start_next` 於 `crates/sw-core/src/session_core.rs:5170` 命中 `mode == "interactive"` 分支：
    1. 呼叫 `open_lease` 建立 interactive lease 並指派 `lease_id`。
    2. 發送 `Effect::Notify(ManagerNote::InteractiveOpened { sid, interactive_id })` 通知管理器索引該 lease。
    3. 對非空命令生成前景 `grant`，向 `pending_ops` 插入 `PendingOp::InteractiveCmd { cmd_id, result, lease_id }`。
    4. 產出 `Effect::StartOp`，其規格為 `OpSpec::InteractiveOpenCmd { command, owner, interactive_id: None }`。
- **Op 執行與 TX 發送**：`crates/sw-core/src/op_runner.rs:405`
  - `OpRunner` 處理 `OpSpec::InteractiveOpenCmd`，封裝行載荷後呼叫 `lease_send(ctx, grant, payload, TxClass::InteractiveOpenCmd, &owner, grant.cmd_id.as_deref())`。
  - 經 `TxWriter` 寫入 UART 與 WAL（WAL 帶原命令 `cmd_id`，若成功交 OS 則標記送達）。

### 鏈路二：命令取消與 Permit 撤銷
- **RPC 入口**：`crates/sw-daemon/src/service.rs:1216`
  - 處理 `command.cancel`，呼叫 `self.registry.cancel(&cmd_id, &self.manager.now_iso())`。
- **登錄表狀態更新**：`crates/sw-core/src/arbiter.rs:429`
  - `CommandRegistry::cancel` 取得內部鎖，若紀錄非 terminal（`status` 不是 `done` 或 `error`），將其狀態標記為 `canceled`，並保留於 `in_flight`（canceled-running）。
- **Session 佇列與前景 Op 撤銷**：`crates/sw-daemon/src/service.rs:1220` → `crates/sw-core/src/session_core.rs:2223`
  - 經 session 路由發送 `SessionRequest::CancelQueued { cmd_id }`。
  - `session_core.rs:2223` 自待執行佇列移除該命令；若該命令為當前前景 op（`grant.cmd_id == cmd_id`），呼叫 `self.deps.fence.revoke_ops(&[grant.op_id])` 撤銷其 TX permit（之後以該 op 提交的 TX 一律 Cancelled、零 TX）。
  - **重要判定**：取消操作只撤銷前景 op 的 permit，不碰 session 的 `lease`。存活的 interactive lease 依然有效。

### 鏈路三：後續 Interactive Send 與原命令收尾
- **RPC 入口**：`crates/sw-daemon/src/service.rs:1037`
  - 處理 `session.interactive_send`，以 `interactive_id` 經 `self.manager.route_interactive` 路由至持有該 lease 的 session actor。
- **Lease 驗證與新 Grant 生成**：`crates/sw-core/src/session_core.rs:4929` (`interactive_send`)
  - 檢驗 `lease_matches(&interactive_id)` 與逾期狀態；原命令取消不影響 lease 存活。
  - 若原命令執行器尚未返回，`fg_slot_busy()` 為 true，暫回 `SESSION_BUSY`。
  - 待原命令執行器返回收尾：
    - 進入 `crates/sw-core/src/session_core.rs:5583` (`op_finished`)。
    - 命中 `PendingOp::InteractiveCmd`：因為 TX 已完整送出（`OpOutcome::Sent`），不關閉 lease；呼叫 `self.deps.registry.complete(&cmd_id, outcome, &self.wall_iso())`。
    - 在 `crates/sw-core/src/arbiter.rs:368` (`complete`)：因狀態已為 `canceled`，不覆寫已取消紀錄，僅清除 `in_flight` 指標。
  - 原命令前景槽清空後，後續 `session.interactive_send` 即分配新 grant（`new_grant(None)`，`OpSpec::InteractiveSend`），經 `lease_send` 成功寫入 UART 與 WAL。

---

## 3. ServiceRig 黑箱驗證與 WAL 比對證據

依據 `crates/sw-daemon/tests/dispatch.rs::df25_interactive_cancel_preserves_lease` 實測：

1. **原命令提交**：
   - 呼叫 `command.submit {"selector":"COM0","cmd":"top","mode":"interactive"}`。
   - 回應：`{"ok":true,"cmd_id":"...","status":"running","execution_mode":"interactive","session_id":"COM0"}`。
   - 取得原命令分配之 `lease_id`。
2. **確定性暫停與 Cancel 前檢查**：
   - 使用 `FakeWalFs::pause_after_next_write`，在 TX WAL 寫入成功後、執行器返回前暫停背景執行緒。
   - UART TX 已輸出 `top\n`，WAL 已寫入首筆帶 `cmd_id` 的 TX 紀錄。
   - 此時呼叫 `session.interactive_send`，因原命令前景 op 仍佔用執行槽，正確回傳：
     `{"ok":false,"error_code":"SESSION_BUSY","interactive_id":"<lease_id>","version":"0.4.1"}`。
3. **原命令取消**：
   - 呼叫 `command.cancel {"cmd_id":"<cmd_id>"}`。
   - 回應：
     `{"ok":true,"cmd_id":"<cmd_id>","status":"canceled","version":"0.4.1"}`。
   - 登錄表紀錄狀態確認為 `canceled`，且此時執行器未返回，在途計數維持 1（canceled-running）。
4. **放行執行器與收尾**：
   - 釋放暫停點，執行器收尾完成，在途計數歸 0。
   - `wp58_g12_get` 確認紀錄仍為 `status: canceled`，未被收尾覆寫。
   - 斷言 session 當前 lease ID 依然等於原 `lease_id`（lease 未被撤銷）。
5. **後續 Interactive Send 兩次送達**：
   - 發送第一筆 `session.interactive_send {"interactive_id":"<lease_id>","data":"q","encoding":"plain"}`：
     回應：`{"ok":true,"interactive_id":"<lease_id>","bytes":1,"version":"0.4.1"}`。
   - 發送第二筆 `session.interactive_send {"interactive_id":"<lease_id>","data":"x","encoding":"plain"}`：
     回應：`{"ok":true,"interactive_id":"<lease_id>","bytes":1,"version":"0.4.1"}`。
6. **UART 與 WAL 精確比對**：
   - UART 累計 TX：恰為 `top\nqx`。
   - WAL 累計 TX 紀錄（恰 3 筆）：
     - 第 1 筆：`cmd_id: "<cmd_id>"`, `payload_b64: "dG9wCg=="` (`top\n`)
     - 第 2 筆：`cmd_id: null`, `payload_b64: "cQ=="` (`q`)
     - 第 3 筆：`cmd_id: null`, `payload_b64: "eA=="` (`x`)
7. **排隊中取消零 TX 回歸**（`df25_cancel_queued_interactive_zero_tx`）：
   - 在前面有長命令占用時提交 interactive 命令（處於佇列中）。
   - 於排隊中呼叫 `command.cancel`，命令被自佇列移除。
   - 斷言原命令零 TX、不開 lease、不佔用後續資源。

---

## 4. 案例與 L1 鎖登記

1. **案例登記**（`tests/acceptance/cases.yaml`）：
   ```yaml
   df25-interactive-cancel-preserves-lease:
     case_ids:
     - df25-interactive-cancel-preserves-lease
     daemon_slice: '2.2'
     description: interactive 命令 TX 完成後、執行器返回前取消，不得撤銷存活的 lease（OD-77 B1；DF-25）
     kind: blackbox
     necessity: conditional
     required_capabilities:
     - process_rpc
     - memory_only
     source_ref: crates/sw-daemon/tests/dispatch.rs::df25_interactive_cancel_preserves_lease
   ```
   - 修訂號前進至 `2026-11-10.2`。
   - 凍結切片 2.1～2.4 分母維持不變（48 / 93 / 154 / 187），追加 OD-76 修訂。
2. **L1 必要集合登記**（`tests/acceptance/l1_required.lock.json`）：
   - `s22_gate:cargo_workspace.linux` 新增 2 個 Rust 回歸節點：
     - `cargo:sw-daemon::dispatch::df25_interactive_cancel_preserves_lease`
     - `cargo:sw-daemon::dispatch::df25_cancel_queued_interactive_zero_tx`
   - `python3 tests/acceptance/regen_lock.py --check` 驗證通過。
