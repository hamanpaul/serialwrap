0.5.1 工作包 p279（一包一項）：承接外部 PR 279，修正 issue 275。Windows TCP console 的 raw 擁有權，要在 session 層補記鏡像 lease，讓 agent interactive lease 的仲裁和 POSIX PTY console 一致。起點：分支 `fix/279-tcp-console-lease`。編排者已從 origin/main 分出這條分支，並 merge 了作者的 commit（origin 分支 `fix/275-tcp-console-lease`，head 17d673c1），保留作者的歷史與署名。本包以新 commit 疊上修正，不得改寫作者的 commit。

=== 必讀 ===
- 設計稿：`/home/paul_chen/b-log/rust-refactor/exec/v051/p279/design-p279.md`，先讀完。
- 裁決：`git -C /home/paul_chen/prj_pri/serialwrap-worktrees/parity-amendments show origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/v051.md`。先讀以下各節：
  - 「OD-111」（外部 PR 的 Python 改動不採用）
  - 「0.5.1 行為差異登錄規則」（含第 8 點補充）
  - 「OD-114」
  - 「其餘工作包的 BC 編號」
  - 「編排者定案（2026-11-24）：p279 設計」
- 本包登錄 **BC-23**：`scope: rust_only`、`ab_participation: absolute_only`、`decision_ref: OD-111`，格式照 BC-06。
- PR 全文：`/home/paul_chen/b-log/rust-refactor/exec/v051/p279/pr-279.md`；issue 全文：`/home/paul_chen/b-log/rust-refactor/exec/v051/p279/issue-275.md`。
- 獨立稽核：`/home/paul_chen/b-log/rust-refactor/exec/s2/open-pr-audit-20261122.md` §3.2。

=== 編排者定案（依 OD-112，擁有者可推翻）===
設計稿第 3 節標【需編排者定案】的 10 項，全部採用建議值。
- **「第二 console 與既有測試」的 fixture 調整核准**：幾支既有節點測的是 line submission、timeout、reboot。這些節點改成先用公開的 close 路徑解除 human lease，再驗原有斷言。
  - 不得改名、刪除節點，也不得放寬成功斷言。
  - 另新增「鏡像存在 → 精確 BUSY 加零 TX」的反例。
  - 回報要逐名列出調整過的節點。
- **README、SKILL、changelog fragment**：要修正作者原本的錯誤敘述。`file.*` 仍視為 agent，bare human 的 `command.submit` 遇到 human lease 回 BUSY。
- **Windows 原生與 TeraTerm smoke**：由編排者另外安排，本包只做 Linux 上的真 TCP 加 fake 驗證。

=== 工作包說明（設計稿草擬，編排者已核）===
0.5.1 工作包 p279：承接外部 PR 279（issue 275），將 Windows TCP console raw ownership 的 session 鏡像 lease 修正交付到 Rust。

起點：分支 ``fix/279-tcp-console-lease``，HEAD ``fix/279-tcp-console-lease``。

作者為 `weisheng-lin-227`／`weisheng_lin`；作者 head 為 `17d673c110d09a4f85b6e78ced0520729c61f744`，包含功能 commit `e56ed46a39bd7ca2dba3f89162d1f145aa3c88d5`。以 merge 承接作者歷史，再以新 commit 撤除 Python 部分，不改寫作者 commit。

=== 背景 ===

- Rust `console_connected()` 已在 accept 時授 raw ownership，卻沒有建立 session lease；self-test、human-active reprobe 與 agent interactive 仲裁因此看不到 TCP 人類。
- 現有 refresh／self-heal 補不上「已有 owner、沒有 lease」。
- 直接套一般 human lease close，會清掉 A 斷線後 B 的 raw owner；agent soft preempt 的 stash close／expiry 也有相同清理風險。
- Linux 已有 native TCP listener 與 `ServiceRig::with_native_tcp_console()`，可以用真 socket＋fake UART 驗證。
- 修正登錄 **BC-23**，只適用 Rust。設計細節採編排者對上方 DESIGN 第 3 節的定案。
- i227 處理登入失敗是否保留 bridge；i294 處理完成判定，均不納入本包。

=== 本包要做的事 ===

1. **保留來源、撤除 Python 修正。**
   - 保留 merge 承接的兩筆作者 commit。
   - 以承接前起點的內容，撤除作者對下列路徑的變更：
     - `sw_core/session_manager.py`
     - `sw_core/uart_io.py`
     - `tests/test_serial_port.py`
     - `tests/test_tcp_console_lease.py`（起點不存在時移除作者新增檔）
     - `contracts/daemon_logging.yaml`
     - `tests/test_contracts_keep_baseline.py`
     - `contracts/cli_help/linux/action_version.json`
     - `contracts/cli_help/linux/action_version_session.json`
     - `crates/sw-cli/tests/argparse_golden.rs`
   - 只撤作者差分，保留起點已有變更；不拿作者舊 base 覆蓋現行 main。
   - README、Windows SKILL 與 fragment 保留可用內容，改成正確的 Rust 0.5.1 說明。

2. **共用 lease 模型並補 actor 接線。**
   - `InteractiveLease` 增加不透出 RPC 的 TCP mirror lifetime 標記。
   - actor 內查詢 active／suspended／pending 的存活 TCP owner，建立及同步鏡像。
   - 接入 accept、refresh、reprobe、自癒及 readiness 落定；不覆蓋 agent／一般 human lease。
   - 新建鏡像遵守 bridge、狀態、stop、flash、handoff guard。
   - 鏡像簿記不得重授 raw owner、額外增加 `owner_gen` 或新增 admission lease flag。

3. **完成 EOF、重連與 agent 仲裁。**
   - EOF 沿既有 data lane／lifetime 防線，立即清該 client 的鏡像與失效 stash。
   - A→B 重連只撤 A，不能清 B；涵蓋 agent soft preempt 的 close／expiry 收尾。
   - READY 下 human active 回既有 BUSY、idle soft preempt，close／expiry 還原；一般 agent 命令沿用 suspend／resume。
   - agent 命令在途查 self-test／refresh 不得拆鏡像；deferred 只回放一次。
   - 不新增第二 console 的立即接班機制，沿用現有 self-heal。

4. **先紅後綠及既有回歸。**
   - 新增 DESIGN 第 5 節兩個 `tcp_console_lease.rs` 測試檔與具名節點。
   - 至少取得真 socket→Hub→actor→RPC→fake UART／WAL 的接線證據。
   - 既有 line／timeout／reboot 節點如需改無 lease fixture，只限編排者核准的六個節點；保留原斷言及 nodeid，另加鏡像存在時 BUSY＋零 TX 反例。
   - 其他紅燈定因後回報，不自行放寬或擴權修改。

5. **完成契約、L1 與文件。**
   - BC-23：`kind: BC`、`scope: rust_only`、`ab_participation: absolute_only`、`decision_ref: OD-111`。
   - 更新 coverage、l1-only approval、兩個 V051 ID 集合及核准日期。
   - 新 Cargo 節點 add-only 登錄 Linux 三個共同規則集合；Windows 待原生收集清單另列。
   - 不改 schema 形狀、Python golden 或錯誤碼全集。
   - 同步 README 中英、console／self-test 規格、Windows SKILL 手寫段落與作者 fragment。
   - 修正作者「file push/pull bare human 必然 BUSY」敘述：保留現行 file RPC agent 政策。
   - PR 明列來源作者、Python 撤除、BC-23、回歸 case 評估及尚未取得的 Windows 證據。

=== 不得做的事 ===

- 不改 Python production code，不採用作者 Python 測試與其機械同步 golden。
- 不改 POSIX lease／peer grace 的既有語意、com0com、Telnet、來源授權或活動窗。
- 不修 i227、i294、issue 264，亦不擴充 bridge 生命周期。
- 不新增錯誤碼、不刪除或改名既有測試、不放寬斷言、不移除鎖定節點。
- 不把 Linux native TCP 或 Windows cross-check 宣稱為 Windows 原生／COM／TeraTerm 實機通過。

=== 驗收 ===

- 新增測試有可歸因的紅燈與修正後綠燈。
- `sw-core`、`sw-daemon` 含 `test-fakes` 的 crate 測試通過。
- Windows GNU 目標的 workspace tests cross-check 通過。
- 受影響 crate clippy 完成，檢視本包檔案的診斷。
- DESIGN 第 5 節列出的五個契約治理 pytest 檔、`tools.contracts check` 通過。
- L1 add-only 登記完成，`regen_lock.py --check` 通過；policy check 為 0 fail。
- 核對作者 head 確實為交付 HEAD 的 ancestor；撤除清單中的基線內容與承接前起點一致。
- 一輪實作、一次 terra 審查；完整性核對涵蓋上述全部工作，不以測試全綠代替交付完整。
- Windows 原生收集與 TeraTerm smoke 由編排者安排；未執行者明列，不預標 PASS。
=== 0.5.1 共同規則（每個工作包都適用）===
裁決：`git -C /home/paul_chen/prj_pri/serialwrap-worktrees/parity-amendments show origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/v051.md`（OD-111 與先前仍有效的裁決）。

1. **Python 基線凍結**：不得修改 `sw_core/`、`serialwrapd.py`、`serialwrap`。Rust 與 Python 的行為差異登錄到 `contracts/behavior_changes.yaml`（BC），格式照既有條目，並附測試；A/B 黑箱測試以既有的 `rust_only` 標記或差分登錄處理，不得刪除或放寬既有斷言。
2. **先紅後綠**：先寫會紅的測試並貼出紅燈輸出，再修。產品修正要有從 RPC／CLI 入口到實際行為的接線證據（ServiceRig／CoreHarness／黑箱任一）。
3. **判準不得放寬**：不刪除、不改名既有測試（改名等於移除鎖定節點），不放寬期限或斷言來讓測試通過。
4. **鎖檔**：新增的測試節點用 `python3 tests/acceptance/regen_lock.py --l1-add-only --set <set> --platform linux --collect <file>` 登記（collect 檔要含該集合既有節點；pytest 進 `p7_gate:python3-m-pytest-q-tests` 與 `s21_gate:pytest_full`；cargo 進 `p7_gate:cargo-test-workspace`、`s21_core`、`s22_gate:cargo_workspace`），最後 `regen_lock.py --check` 通過。Windows 集合不登記，在回報列出待 Windows 原生收集的節點。
5. **契約、golden、案例分母**：只在本包明列要改時才改。權威 YAML（`contracts/*.yaml`、`contracts/behavior_changes.yaml` 等）目前沒有寫入工具，以人工變更維護，每項變更要對應裁決或本包說明；由工具衍生的區段、golden、鎖檔一律用工具產生，不得手寫，也不得為了通過驗證而改驗證器。改了要在回報寫明 `contract_revision` 是否改變。
6. **不順手做別的事**：發現範圍外的問題寫進回報的 SPEC-GAPS，不另做。
7. **changelog fragment**：`changelog.d/<issue>-<slug>.md`，YAML frontmatter 照既有檔（`type` 為 change／deprecate／docs／feat／fix／perf／refactor／remove／security 之一，要有 `scope`）。整包共用一個，第一個子項建立。
8. **執行環境**：先 `export TMPDIR="$(mktemp -d /tmp/sw.XXXXXX)" CARGO_TARGET_DIR="/srv/thala-e/serialwrap-cargo/v051-p279" CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=6`（cargo target 放在 E 槽的 serialwrap 專用目錄，`p279` 換成本包代號；只寫這個目錄，不得寫入 `/srv/thala-e` 的其他位置）、`set -o pipefail`；命令一律 `nice -n 10`，同時只跑一個 cargo；**workspace 或 crate 層級的 cargo build／test／check／clippy，以及整檔 pytest，一律包進 `heavy-slot serialwrap-v051-p279 nice -n 10 <命令>`**（與 thala 共用的主機重負載 slot，拿不到會每 20 秒排隊；單一小測試、git、讀檔不用包）；測試前景執行，輸出 `tee` 存檔。
9. **主機安全**：不碰正式 serialwrap daemon（`serialwrap.service`）與 `~/.config/serialwrap`、`~/.local/state/serialwrap`、`/dev/ttyUSB*`；不在主機跑 `test_s24_supervision_systemd.py`；不跑容器；不透過 WSL interop 執行 Windows 程式、不開任何 COM。停掉的測試留下的拋棄式 daemon 以 `/proc/<pid>/exe` 判斷後清掉，不得用 `pkill -f`。
10. **驗收**：本項新增／修改的測試；受影響 crate 的 `cargo test -p <crate> --features test-fakes --no-fail-fast`；`cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu`；`cargo clippy -p <crate> --tests --features test-fakes -- --cap-lints warn`（只看本項改的檔）；`python3 -m policy_check --repo . --pr-base-ref origin/main`（0 fail）；`regen_lock.py --check`。不跑全套 pytest、`cargo test --workspace`、gate 正式執行、Windows 原生。
11. **提交**：只逐檔 stage 本項變更，繁中 Conventional Commit；引用 issue 寫「issue N」（不要寫 #N）；結尾附 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
12. **回報**：做了什麼、改了哪些檔與函式、紅綠燈輸出、新增 nodeid、BC 條目、SPEC-GAPS、需要編排者或擁有者決定的事；最後一行 `REMAINING: none` 或列出本項未完成的事。
