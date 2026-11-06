# S1M-bc3 第 1 輪修正回報

起點：`f59ce805`；分支：`feature/s1m-bc3`。本輪只處理 D3／D4。

## High／Medium 與漏追項目

| 項目 | 處理 | 實際變更 |
|---|---|---|
| D3 | fixed `032e90cd` | 錯誤碼抽取測試依序使用執行期 `CARGO_TARGET_TMPDIR`、`CARGO_TARGET_DIR`、編譯期 `CARGO_TARGET_TMPDIR`，不再對測試資料根目錄寫入；建立輸出目錄失敗也明確報錯。同步計畫的輸出位置說明。 |
| D4 | fixed `b89e90f7` | 新增委派前停住的突變 fixture 與可攜負向自測；Windows Ctrl 事件父測試沿用完全相同的 ready 等待判準。子行程仍活著但沒有孫行程 ready 標記時，必須逾時拒絕；斷言前先終止並回收子行程。 |

來源審查的既有 A 段已修項未重做；其他分支的 finding 未納入本輪。
本輪未修改 Rust 產品程式、Python daemon、`contracts/rpc_schema.json` 或契約行為，Stage 2 對齊基線不變。
兩項屬測試工具修正，回歸由 Cargo 覆蓋，無需真板回歸 case；各附一份 changelog fragment。

## 修前紅／修後綠

- D3：新增 `emitted_codes_output_uses_cargo_directory_instead_of_data_root`，以普通檔案封住資料樹的 `target`，避免 root 權限使唯讀測試失真。舊寫入路徑為 **0 passed、1 failed**，錯誤為 `NotADirectory`。修後整份 `emitted_codes` **14 passed**；覆蓋兩種目錄覆寫、TMPDIR 優先序、空白與中文路徑，以及資料樹未遭改寫。
- D4：先新增 `delegate_ready_rejects_child_stopped_before_exec` 並共用原 Windows 等待函式；尚無停住 fixture 時為 **0 passed、1 failed**，子行程照舊委派並以 37 退出，無法到達停住標記。補上純測試用的停住鉤子後，整份 `exec_delegate` **3 passed、1 ignored**。這個紅燈證明原先缺少可執行的突變 fixture，不宣稱產品原有的 ready 判準錯誤。
- 原始紅／綠紀錄保留於 `target/s1m-bc3/logs/d3-red.log`、`d4-red.log`、`d3-green.log`、`d4-green.log`，不改寫為 PASS。

## 測試與 gate

所有執行皆在前景等待結束。會觸及 Rust CLI 管理路徑的 Cargo 測試一律經 `target/s1m-bc3/sandbox.sh`：根檔案系統唯讀、僅本 worktree 可寫，獨立 HOME／XDG、PID namespace、獨立 `/run` 與合成 `/dev`。namespace 結束即收掉剩餘測試行程；D4 另明確 kill＋wait。

| 驗證 | 結果 | 紀錄（位於 `target/s1m-bc3/logs/`） |
|---|---|---|
| `cargo test --offline --workspace --features test-fakes` | 1028 passed、0 failed、7 ignored | `cargo-workspace-test-fakes-network.log` |
| `cargo test --offline --workspace` | 1028 passed、0 failed、7 ignored | `cargo-workspace.log` |
| `cargo check --offline --workspace --target x86_64-pc-windows-gnu` | rc 0 | `windows-check.log` |
| 受影響兩 crate 的 `cargo check --offline -p sw-cli -p sw-pal --tests --features test-fakes --target x86_64-pc-windows-gnu` | rc 0；既有 unused-import 警告 | `windows-tests-check.log` |
| `python3 scripts/rust/check_cfg_boundary.py` | rc 0 | `cfg-boundary.log` |
| `python3 -m tools.contracts check` | `ok:true`、0 findings | `contracts.log` |
| `python3 tests/acceptance/regen_lock.py --check --base f59ce805` | 案例／屬性／統計與 L1 基準比對皆 OK | `regen-lock-check.log` |
| `python3 -m pytest -q tests/test_rust_exec_delegate.py` | 1 passed；保留根 conftest，隔離 namespace 的 live daemon guard 不可達，未連 production | `pytest-focused.log` |
| `python3 -m policy_check --repo .` | 25 pass、0 fail、2 warn（R-19、R-22） | `policy-check.log` |
| Docker 唯讀原始碼執行兩份受影響整合測試 | 17 passed、0 failed、1 ignored；實際寫入來源目錄被 `Read-only file system` 拒絕 | `docker-readonly.log` |

Linux 實際收集共 1035 個節點。以收集結果限縮至既有凍結節點與本輪兩個新節點，僅經 `regen_lock.py --l1-add-only` 新增 `s1a_cargo.linux` 2 筆、`s1b_cargo.linux` 2 筆、`s1m241_cargo.linux` 1 筆；移除 0、selector 變更 0。Windows 凍結集合未以 Linux 收集結果代填。

Docker 僅建立本輪 `sw-s1m-bc3-*` 前景容器，`--rm --network none --read-only --cap-drop ALL`，來源唯讀掛載，唯一主機可寫掛載為本 worktree 的 `target/s1m-bc3/docker-output`，未掛主機家目錄、真實 UART 或 Docker socket。重跑命令為 `python3 target/s1m-bc3/docker-check.py`；使用既有映像 ID `sha256:555a7008723e82dc8eaa2de54351b646de3e60c39b1ec217c5782d1cf661df9b`，覆寫 entrypoint 直接跑測試，不啟動 systemd／daemon。結束確認本輪容器無殘留，未操作 `thala-*`。

### 中途失敗與處理

首次 workspace 測試額外使用 `--unshare-net`，使下列未修改案例因 DNS 回 `EAI_AGAIN (-3)`、預期為 `EAI_NONAME (-2)` 而失敗：

- `cargo:sw-pal::rpc_client_conformance::resolve_tcp_gai_matches_python`
- `cargo:sw-pal::rpc_client_conformance::tcp_refused_and_gai_and_timeout0_match_python`

移除這層額外網路隔離後，同檔 **7 passed**（`dns-network-restored.log`），再跑完整 workspace 為上表全綠。原失敗保留於 `cargo-workspace-test-fakes.log`；未修改案例或斷言。

policy-check 首次因隔離 HOME 看不到既有 user-site 套件而回 `No module named policy_check`（`policy-check-isolated-home.log`）。改將既有套件目錄以 `PYTHONPATH` 唯讀加入後，使用已安裝的 policy-check 1.0.19，沒有安裝或改寫 worktree 外檔案。

依本 brief 的明確禁令，未跑全套 pytest、slice gate、出口證據 driver；未 push、開 PR、merge 或部署。
既有原生 Windows 互動 Ctrl 事件及硬體項維持 **BLOCKED-OWNER**；交叉編譯、Linux 負向自測與 Docker 均不充當該實機證據。

## 不成立主張

無。

## SPEC-GAPS

無本輪範圍內待裁決缺口。

## 既有碼待辦

- Windows 測試交叉編譯的 unused imports 位於未修改的 `crates/sw-pal/tests/osstr.rs`、`crates/sw-pal/tests/process_ctl.rs` 與 `crates/sw-cli/tests/support/mod.rs`；本輪不修。
- R-19 的既有 CI 安裝步驟解析警告、R-22 的既有文件懸空引用警告維持原狀，不擴張本輪範圍。

## REMAINING:

REMAINING: none
