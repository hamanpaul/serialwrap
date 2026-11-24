# 0.5.1 i306 驗收報告

## 候選與提交

- 起點：`e77c76a708a5a8631f78c238f0db7467f967ecfb`
- 最終程式候選：`590e7bbc1447d5e863f7388701c167daa68a2369`
- 實作提交：`ddf7ef9b305ed94749319d8a1d508fc5c798bbc6` — `fix: 修正 self-test prompt 行界與驗收同步（issue 306）`
- clippy 比較修正：`590e7bbc1447d5e863f7388701c167daa68a2369` — `style: 調整 self-test readiness 比較（issue 306）`
- 驗收命令都以前景執行並留存 stdout／stderr；Cargo 只使用 `/home/paul_chen/.cache/serialwrap-cargo/v051-i306` 及其 `no-fakes/` 子目錄。此報告與完整日誌一併存檔，摘要及逐檔 SHA-256 見 `SHA256SUMS`。

## Finding 對照

| Finding | 處置 | 可追溯證據 |
|---|---|---|
| V051-d32e47c-3 High | fixed `ddf7ef9b`。payload echo、Unicode／trim marker 分段都等 RX snapshot 寫入且 runner 完成 regex snapshot 判定後，才交付下一段；不再依賴固定睡眠。 | `red/` 下的 `red-segment-snapshot-hook-mutation.txt` 在暫停 test-fakes snapshot 觀察寫入時，分段案例以「等待逾時：runner 檢查完整 TX 回顯」轉紅；還原後 `green/` 的 `final-restored-core-self-test-safety.txt` 為 25 passed。完整綠燈見 `acceptance/cargo-test-workspace-final.txt`。 |
| V051-d32e47c-4 High | fixed `ddf7ef9b`。RPC fixture 先用 boot banner 建立 `READY + ready_reconfirm_pending=true`，`BOOT_DONE` 清除 quiet 後仍斷言 pending 為 true；四個 evidence 情境分別斷言失敗保留、完整成功清除。 | `red/red-pending-precondition.txt` 在舊 READY fixture 上以 pending 前置斷言轉紅；`green/green-pending-precondition.txt` 通過。最終 RPC 測試也包含於 workspace 全套。 |
| V051-d32e47c-5 High | 已完成；候選 `590e7bbc` 的整包驗收、紅綠燈索引、日誌、收集檔及 checksum 在本證據提交中交付。 | 本檔、`red-green-index.md`、`acceptance/`、`collect/`。候選 SHA 是 workspace 與兩項 Windows GNU check 的受測 SHA。 |
| V051-e77c76a-1 High | fixed `ddf7ef9b`。marker regex 消耗 LF／CRLF 後，prompt 搜尋回退至 marker 行終止符起點保留自訂 regex 的行界上下文，並要求 prompt match end 大於 marker end。 | `red/red-newline-prompt.txt` 與 `red/red-rpc-newline-prompt.txt` 在原始實作上都得到 `prompt_after_marker=false`；`green/green-core-newline-prompt.txt` 和 `green/green-rpc-newline-prompt.txt` 轉綠。 actor 測試同時涵蓋同 chunk／分 chunk；RPC 測試從 service RPC 入口驗證。 |
| V051-e77c76a-2 Medium | fixed `ddf7ef9b`。未終止 marker 後先等 runner 檢查該 snapshot，並斷言 self-test 尚未完成，再注入同一行 prompt。 | `red/red-old-marker-mutation.txt` 暫時套回 `$` 終止判準時，原本應拒絕的 probe 回 `probe_ok=true`，測試轉紅；恢復正式程式後 `execution_marker_requires_terminated_line_before_prompt` 通過。 |
| V051-e77c76a-3 High | 已完成；前述項目、兩個新回歸節點及最終候選驗收都有可追溯交付。 | `red-green-index.md` 與本報告；完整執行日誌及 nodeid 收集見子目錄。 |

**不成立主張：無。** 六個 High／Medium finding 均已處理，沒有以刪案例、放寬斷言、改 judge 或改 FAIL／INSUFFICIENT 判定取得通過。

## 實作與契約

- `crates/sw-core/src/op_runner.rs::run_self_test_probe`：保留 marker 後的 CRLF／LF 行界，驗證 prompt 確實延伸至 marker 結尾之後。
- `crates/sw-core/src/uart_bridge.rs::RxShared::wait_for_regex_end_from_shutdown_cancellable`：只在 `test-fakes` 提供已檢查 RX snapshot 的觀察值，供分段測試同步。
- `crates/sw-core/tests/self_test_safety.rs`：逐段 snapshot 同步、未終止 marker 同步回歸，以及自訂 newline-prefixed prompt 的同／分 chunk actor 測試。
- `crates/sw-daemon/tests/self_test_safety.rs`：pending 前置 fixture、失敗保留／成功清除斷言，以及 newline-prefixed prompt 的 RPC 回歸。
- 新增 Cargo nodeid：
  - `cargo:sw-core::self_test_safety::newline_prefixed_prompt_after_marker_confirms_ready_same_chunk_and_split_chunk`
  - `cargo:sw-daemon::self_test_safety::rpc_newline_prefixed_prompt_after_marker_confirms_ready`
- `contracts/coverage_map.yaml` 登記本項修改與新增的 BC-13 測試覆蓋。`contracts/behavior_changes.yaml` 的 BC-13 已依 OD-111 為 `IMPLEMENTED`；該檔未改，revision 仍為 17（`contract_revision` 未改）。Python baseline、golden 與診斷碼未改。
- 更新既有 `changelog.d/306-self-test-probe-safety.md` fragment，記錄自訂 prompt 行界相容性。

## 驗收結果

以下結果對應最終候選 `590e7bbc1447d5e863f7388701c167daa68a2369`；crate 專項命令另在實作提交 `ddf7ef9b305ed94749319d8a1d508fc5c798bbc6` 執行，最終候選的 workspace 全套再次覆蓋所有 crate。

| 命令 | 結果 |
|---|---|
| `cargo test -p sw-core --features test-fakes --no-fail-fast` | 1,837 passed，0 failed，1 ignored；68 個 test result。 |
| `cargo test -p sw-daemon --features test-fakes --no-fail-fast`（設定隔離 no-fakes CLI） | 450 passed，0 failed，8 ignored；23 個 test result。 |
| `cargo test --workspace --features test-fakes --no-fail-fast`（最終候選） | 3,680 passed，0 failed，53 ignored；199 個 test result。無失敗 test id。 |
| core `self_test_safety` focused（最終候選、mutation 還原後） | 25 passed，0 failed。 |
| `cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu` | exit 0。 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0。 |
| `cargo clippy -p sw-core --tests --features test-fakes -- --cap-lints warn` | exit 0。 |
| `cargo clippy -p sw-daemon --tests --features test-fakes -- --cap-lints warn` | exit 0。 |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0。 |
| `python3 -m tools.contracts check` | exit 0，`findings=[]`。 |
| `python3 tests/acceptance/regen_lock.py --check` | exit 0；案例鎖與 L1 結構正確。 |
| `python3 -m policy_check --repo . --pr-base-ref origin/main`、`python3 -m policy_check --repo .` | 各 25 pass、0 fail、2 warn；R-19 與 R-22 為既有 advisory。 |
| rustfmt `--check`（四個修改的 Rust 檔） | exit 0。 |

加入本證據目錄後再次執行兩種 policy 命令，仍各為 25 pass、0 fail、2 warn；R-21 保密檢查通過。對應輸出為 `acceptance/policy-evidence-base-ref.txt` 與 `acceptance/policy-evidence-default.txt`。

L1 add-only 僅在 `p7_gate:cargo-test-workspace`、`s21_core`、`s22_gate:cargo_workspace` 的 Linux 集合各新增上述兩個 nodeid，均為 0 removal。Windows 鎖未改；新節點待 Windows 原生收集，未宣稱 Windows 原生執行通過。

sw-daemon 首次 crate 測試因未設定 `SW_TEST_SERIALWRAP_NO_FAKES` 在 `handoff_stop::default_cli_update_scope_cannot_bypass_foreign_daemon` 的測試前置檢查失敗，尚未進入被測流程。依測試支援程式要求，在指定 cache 根目錄下獨立建置 no-fakes CLI，設定該變數後完整 crate 測試 450 passed；最終 workspace 測試也以相同設定通過。兩份原始輸出都保留，未連接 pipx CLI／daemon。

未跑 Python 全套 pytest、exit-evidence driver、Docker、Windows 原生測試、COM 或實體 UART；本項沒有 Python 檔案變更，所有測試使用 test-fakes。`/proc` 檢查未找到本次測試殘留的 `serialwrapd` 行程；未使用 `kill`／`pkill`。

## SPEC-GAPS

1. 完整 Linux collect 有 3,729 個 nodeid，既有三個鎖集合有 3,674 個；本項兩個新節點加入後為 3,676。另有 53 個未登錄 nodeid 與 i306 無關，為遵守單項範圍沒有加入鎖；清單在 `collect/unregistered-out-of-scope-nodeids.txt`。
2. 新增的兩個節點尚待 Windows 原生收集：`cargo:sw-core::self_test_safety::newline_prefixed_prompt_after_marker_confirms_ready_same_chunk_and_split_chunk`、`cargo:sw-daemon::self_test_safety::rpc_newline_prefixed_prompt_after_marker_confirms_ready`。本輪依限制未執行 Windows 原生測試。

## 既有碼待辦與 owner 決定

- clippy 在 `crates/sw-core/src/op_runner.rs:123`、`:144` 顯示原有警告；本項改動位於該檔約 1,099 行之後，未修改這兩處。新增測試的 `cmp_owned` 警告已在 `590e7bbc` 消除。
- 沒有需要本輪新增的 owner 裁決或硬體判定；BC-13 依既有 OD-111。

## REMAINING

Windows 原生收集上列兩個 nodeid；另有 53 個完整 collect 發現的範圍外 nodeid 未登錄，留待其所屬工作包處理。
