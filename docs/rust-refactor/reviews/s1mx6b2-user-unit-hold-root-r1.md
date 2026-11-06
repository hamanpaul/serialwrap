# S1M-x6b2 第 1 輪：統一真實 SystemdCtl 的使用者 hold 根目錄

分支 `feature/s1m-x6`，起點 `9a0b91d12aa3b2e464ce7aecde5ffb0e4b374ff9`。本輪只處理 S1-6f67a0c-1；產品修正 commit 為 `e56c54759cffb0ec504dd90a87ff4ade1331dec0`（`fix(sw-pal): 統一使用者 hold 與 unit 根目錄`）。

## High／Medium 處理

| 項目 | 嚴重度 | 處理 |
|---|---|---|
| S1-6f67a0c-1 | Medium | **fixed `e56c5475`**：真實 SystemdCtl 的 user hold 改用 HOME/.config，與 unit 及預存計畫一致；三個新回歸先紅後綠。 |
| 前輪 A-1 DUT TX checkpoint | Medium | already fixed `978aa1e1`，沿用前輪非實作者結論；本輪確認 commit 已在 HEAD 歷史中。 |
| 前輪 A-2 Ctrl 事件孫行程同步 | Medium | already fixed `51bd8fa5`；同上。 |
| 前輪 A-3 出口 driver 保留原始失敗 | Medium | already fixed `ddcbdde0`；同上。 |
| 前輪 A-4 升級環境 PyYAML 隔離 | High | already fixed `12b3a929`；同上。 |
| 前輪 A-5 發佈 bundle 不含 test-fakes | Medium | already fixed `12b3a929`；同上。 |

前輪 A 段沒有標為「未修」的 High／Medium。Low 不處理；沒有承接其他分支或其他子項。

## 修正依據與範圍

選用 **HOME/.config**，依據如下：

- Python `sw_core/setup_cmd.py:296` 的 `_USER_UNIT_REL` 固定為 `.config/systemd/user/serialwrap.service`；第 543、715 行分別在安裝與同模式刷新時使用 `home / _USER_UNIT_REL`。
- Python 監管 hold 驗證實作 `scripts/docker/systemd_container.py:222` 的 `apply_hold` 也在 `USER_HOME/.config/systemd/user/serialwrap.service.d` 寫 drop-in；第 240 行的 `release_hold` 移除該次傳入路徑。這是既有 Python harness，本輪僅讀原始碼，未執行 Docker。
- Rust `crates/sw-cli/src/setup_cmd.rs:1369` 的 unit helper 與 `crates/sw-cli/src/rollback.rs:4384` 的預存計畫已使用同一 HOME 根目錄。
- 修正 `crates/sw-pal/src/service_ctl/systemd.rs:55` 的 `hold_path`，改由既有 `home()` resolver 組 `.config`：非空 HOME 優先，缺省／空值才回退 passwd 家目錄。一般 SERIALWRAP／XDG config/state 的優先序沒有改動。

產品差異只有上述控制器的路徑選擇，另新增三個 Rust 回歸、changelog fragment 與 L1 鎖登記。沒有修改 Python 產品碼、契約、比較器、judge 或既有案例斷言。此問題可由隔離檔案系統與真實控制器覆蓋，不需新增實體 UART regression case。

## 先紅後綠與中斷恢復

| 新回歸 | 未修產品碼結果 | 修後結果與驗證內容 |
|---|---|---|
| `sw-pal::service_ctl::s1mx6b2_systemd_user_hold_root_matches_home_unit` | 0 passed、1 failed；非空 XDG 下實際回傳 XDG drop-in 路徑，而預期為 HOME 路徑。 | 1 passed；含 XDG 缺省／空值／非空 × HOME 有值／空值，共 6 組。直接呼叫真實 SystemdCtl，核對實際檔案內容、hold_state 與 release 清除。 |
| `sw-cli::mgmt_supervision_switch::s1mx6b2_real_systemd_hold_plan_matches_written_path` | 0 passed、1 failed；XDG 有值時 hold 記錄與 HOME 計畫分歧。 | 1 passed；XDG 有值／缺省共 2 組，在 `supervision_set:after_hold` 中斷，核對記錄、計畫及磁碟檔案；下次 `setup` 成功回收 hold，unit 位元組維持不變。 |
| `sw-cli::mgmt_update::s1mx6b2_real_systemd_update_hold_recovers_only_owned_dropin` | 0 passed、1 failed；`recover_journal` 後，實際寫入的 XDG hold 仍存在。 | 1 passed；XDG 有值／缺省共 2 組，在 `stop_supervisor:after_hold` 中斷，從磁碟辨識實際寫入檔案；呼叫 `recover_journal` 後該檔移除且 journal 清除。HOME 的其他 drop-in、unit 與另一根目錄的同名 XDG sentinel 都逐位元組保留。 |

CLI 回歸明確使用 `with_real_service`，沒有使用優先選擇 FakeServiceCtl 的 `with_supervisor_state`。更新 fixture 在 `with_bundle` **之前**選用真實控制器，避免 bundle helper 自動注入 FakeServiceCtl。

證據邊界：SystemdCtl、原子寫檔、release 移除與 journal recovery 都是真實 Rust 實作及隔離 tmp 檔案；systemctl 命令回應透過 FakeSysEnv 腳本化，包含 hold 的 `Restart=no`／`RefuseManualStart=yes` 與 release 的 `Restart=on-failure`／`RefuseManualStart=no`。因此本輪證明控制器的路徑、命令驗證流程與中斷後檔案回收，**不宣稱取得真實 systemd manager 的 runtime 屬性證據**；Docker／硬體實跑由其他工作包承擔。

修前產品碼與 `9a0b91d1` 的逐位元組相等證明存於 `target/s1mx6b2-red-source-proof.json`；行為紅燈日誌為 `target/s1mx6b2-red-{pal,cli}.log`，修後為 `target/s1mx6b2-green-{pal,cli}.log`。測試 fixture 的編譯與初始化調整不計為先紅證據；表內只計產品缺陷造成的斷言失敗。

## 凍結鎖

僅經 `tests/acceptance/regen_lock.py --l1-add-only` 更新 `s1a_cargo.linux`（948 → 951）與 `s1b_cargo.linux`（953 → 956），各新增上述 3 個 nodeid、移除 0 個；selector 與 Windows 集合不變。

使用 `cargo_l1_results.py --list-only` 只收集名稱，再從實際 collected 中選取各集合既有 nodeid 與本輪三案；未順帶登記其他子項尚未鎖定的案例。登記日誌為 `target/s1mx6b2-lock-add.log`，`--check --base 9a0b91d1` 已驗證沒有移除或 selector 改變。沒有執行出口證據 driver。

## 測試與 gates

所有測試均以既有 `bash target/sbox` 前景執行並等候結束。bwrap 將 `/` 唯讀掛載，僅本 worktree 可寫；獨立 PID／網路／IPC namespace，隔離 `/dev`、`/proc`、`/tmp`、`/run`，空目錄遮蔽真實 `.config`、`.local/state`、`.local/bin`，PATH 排除 pipx 入口。真實家目錄內容不可寫，未呼叫 production daemon／RPC，未開真實序列埠或操作其他 worktree。

| 指令（皆在沙箱內） | 結果 |
|---|---|
| `cargo test -p sw-pal --features test-fakes --no-fail-fast` | **195 passed、2 failed、2 ignored**；exit 101；兩個 DNS 失敗見下表。 |
| `cargo test -p sw-cli --features test-fakes` | **692 passed、0 failed、3 ignored**；exit 0。 |
| `cargo test --workspace --no-fail-fast` | **1030 passed、3 failed、7 ignored**；exit 101；所有 target 執行完成，未將 FAIL 算成 PASS。 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0；僅跨平台編譯證據。 |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0。 |
| `python3 -m tools.contracts check` | `ok: true`、`findings: []`；exit 0。 |
| `python3 tests/acceptance/regen_lock.py --check --base 9a0b91d1` | 案例鎖／屬性快照／統計檔與 L1 結構及基準比對 OK；exit 0。 |
| `python3 -m policy_check --repo .` | **25 pass、0 fail、2 warn**（R-19、R-22）、0 exempt；exit 0。 |

本輪未觸及 Python 程式或測試，因此沒有需執行的受影響 Python 測試。依 brief 明確的「不得跑全套 pytest」限制，未執行後段通用步驟中的全套 pytest；亦未執行 slice gate、Docker 或出口證據 driver。擁有者／硬體項目維持 **BLOCKED-OWNER**，沒有補造原生 Windows、COM 或實體 UART 證據。

### 失敗 ID 與起點證明

| 失敗 ID | 本輪及起點結果 |
|---|---|
| `sw-pal::rpc_client_conformance::resolve_tcp_gai_matches_python` | PAL／workspace 皆失敗；實際 `[Errno -3] Temporary failure in name resolution`／`Some(-3)`，golden 要求 `[Errno -2] Name or service not known`／`Some(-2)`。 |
| `sw-pal::rpc_client_conformance::tcp_refused_and_gai_and_timeout0_match_python` | PAL／workspace 皆在 `tcp_gai` 子情境失敗；同上。 |
| `sw-pal::fs_lock::file_lock_nonblocking_contention_wouldblock` | workspace 失敗於 `fs_lock.rs:50`：「第一個 guard drop 後應可再次取得鎖」。PAL 單 crate 當次通過；未宣稱已查明根因。 |

透過 `git archive 9a0b91d1` 在**本 worktree 的 `target/s1mx6b2-baseline`** 展開原始碼，不建立或操作其他 worktree。在同一沙箱中：

- 起點 `cargo test -p sw-pal --test rpc_client_conformance` 為 **5 passed、2 failed**，兩個 ID 與錯誤訊息逐字相同；`target/s1mx6b2-baseline-rpc.log`。
- 起點完整 `cargo test -p sw-pal --test fs_lock` 逐次前景執行；前 10 次各 12 passed，第 11 次 **11 passed、1 failed**，同一 ID、同一行及訊息；`target/s1mx6b2-baseline-fs-lock-11.log`。這證明該失敗在起點可重現，沒有以「flaky」作為放行或改判 PASS 的理由。
- 快照來源比對及 SHA256 存於 `target/s1mx6b2-baseline-proof.json`。產品修改與這三個既有失敗的測試／實作路徑分離；本輪沒有修改它們、golden 或失敗判準。

全量原始日誌為 `target/s1mx6b2-pal.log`、`target/s1mx6b2-cli.log`、`target/s1mx6b2-workspace.log`；其餘 gates 為同前綴的 `windows-check`、`cfg`、`contracts`、`lock-check`、`policy` 日誌。`target/` 證據不提交。

## 不成立主張

無；S1-6f67a0c-1 成立並已修正。

## SPEC-GAPS

無新增。前輪 SPEC-GAPS-1 所缺的 sw-pal 檔案範圍已由本輪編排者明示授權，並在 `e56c5475` 完成修正。

## 既有碼待辦

- 上表三個起點既有失敗保留 FAIL；本輪不擴權修改 RPC DNS 對照、檔案鎖或其斷言。
- 延續前輪紀錄：`crates/sw-cli/tests/support/mod.rs::write_history_backup` 仍從 config_dir 組歷史 user unit 路徑，本輪未修改。

## REMAINING:

REMAINING: none（本 brief 修復與允許的起點失敗證明已完成；PAL／workspace 的三個既有失敗、另派的 Docker／出口及 BLOCKED-OWNER 項目，均未改判或宣稱完成）。
