0.5.1 工作包 i294 第 2 項（共 2 項，接在第 1 項之後）：prompt 身分與尾端裁切。修正 issue 262（輸出中的 `=> ` 被當成 prompt）與 issue 271（沒有換行的 stdout 被 prompt 裁掉），即工作包說明的「i294-i2」。起點：分支 `fix/294-completion-echo-anchor` 的目前 HEAD（第 1 項已提交）。

=== 必讀 ===
- 設計稿：`/home/paul_chen/b-log/rust-refactor/exec/v051/i294/design-i294.md`，內含事實核對、方案、設計決策、契約與測試計畫，先讀完。
- 裁決：`git -C /home/paul_chen/prj_pri/serialwrap-worktrees/parity-amendments show origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/v051.md`。先讀以下各節：
  - 「0.5.1 行為差異登錄規則」（含第 8 點補充）
  - 「OD-114」
  - 「其餘工作包的 BC 編號」
  - 「編排者定案（2026-11-24）：i268、i227、i267、i294 設計」
- 本包登錄 **BC-22（第 1 項已登錄，本項補進同一筆）**：`scope: rust_only`、`ab_participation: absolute_only`、`decision_ref: OD-111`，格式照 BC-06。
- issue 全文在 `/home/paul_chen/b-log/rust-refactor/exec/v051/i294/issue-*.md`，也可以用 `gh issue view`。
- **本項範圍**：只做工作包說明的「i294-i2」，包括接入 prompt 證據、無換行與 NUL、模糊 prompt 降階、三組整合驗收，最後更新修復狀態。

=== 編排者定案（依 OD-112，擁有者可推翻）===
設計稿第 3 節標【需編排者定案】的各項，除下列各點外，一律採用設計稿的「建議」值。
- 第 1、2、4～10 項採用建議值。
- **第 3 項「無 echo／無法重建 echo」改為以下做法**（與設計稿不同）。用意是避免讓終端不回顯的使用者，原本成功的命令全部變成失敗：
  - 找不到命令 echo，而且從命令 TX 到判定的窗口內，可信身分判斷沒有任何 human TX：照舊判定完成，但仍要符合第 4 項的 prompt 位置規則（prompt 在 snapshot 結尾、位於最後一行行首），不要求 echo。
  - 找不到 echo，而窗口內有 human TX：才採設計稿的做法。沿用原等待上限，到期回 `status=error`、`PROMPT_TIMEOUT`、`partial=true`、`recovery_action=NONE`，保留可用輸出，不自動重送、不另送救援鍵。
  - 兩種情況都要有測試。BC-22 與 changelog 要寫明：只有 human 交錯而且無 echo 時，才會改回 partial。
- 照設計稿拆成兩項：第 1 項是 echo 邊界與 prefix prompt；第 2 項是 prompt 身分、#262、#271。整包完成後，0.5.0 已知問題第 2 點（agent 命令 done 但輸出為空）才算解除。

=== 工作包說明（設計稿草擬，編排者已核）===
0.5.1 工作包 i294：修正命令完成判定三個缺陷，三組獨立驗收，共用 BC-22。

起點：分支 ``fix/294-completion-echo-anchor``，HEAD ``fix/294-completion-echo-anchor``。設計核對基準為 main `e5cee8c3c5242f25b9aca4fb0d8b6485de9ff112`；實作前依指定起點重查位置。

**背景**

- issue 294：human 整行已送出但 prompt 尚未回來時，agent 命令可被 human prompt 提前完成，stdout 為空或殘餘輸出。
- issue 262：echo 後的 `Path => ` 是命令自己的輸出，仍可誤中寬鬆 prompt；echo 錨不能單獨修好。
- issue 271：無換行 stdout 與 prompt 同行，現行依 regex match 起點裁切，會刪掉輸出。
- OD-111 凍結 Python；OD-112 要求編排者定案細節；OD-114 採兩個循序子項，各一輪實作及一次 terra 審查。最後僅重跑整包驗收與一次完整性審查，不另加多視角流程。
- 本 brief 的產品選擇須先由編排者將 DESIGN 第 3 節定案附入；不得由實作者自行切換降階策略。

**本包要做的事**

1. **i294-i1：echo 邊界與 prefix prompt**
   - 在 Rust 一般 agent line／background 路徑建立 completion evaluator。
   - 使用完整 echo，支援分包與既有具證據的 BusyBox／readline 折行；echo 前 human 殘餘不得進入正常 stdout。
   - 完成與擷取共用 snapshot／邊界；不成功後重新 search 全段 RX。
   - 實作編排者定案的無 echo／半行降階；不額外 TX、不重送。
   - 補 `i294_*` 對應 ActorRig 與 RPC 測試、BC-22 第一階段登錄、L1 及共用 changelog。
   - 明列 #262、#271 尚待 i2，不宣稱整包完成。

2. **i294-i2：prompt 身分、#262 與 #271**
   - 在 i1 結果上接入 epoch 內 prompt evidence；只從可辨識 READY 結果或完整 echo 前綴取得，不從任意 regex match 學習。
   - 一般 prompt 必須在 echo 後、最後一行行首且結束於 snapshot 尾端。
   - 同行 stdout 僅以已知、具辨識性的完整 prompt suffix 裁切；短／未知 prompt 不猜測。
   - 分別驗收 `=> ` 分段輸出、`printf abc`、NUL、一般換行輸出及未知 prompt 的 partial。
   - 完成 BC-22、coverage、L1 與文件。以 issue 294 定位更新 README 中英及 release install 文件，不誤改目前第 2 點 #297。

預定主要檔案：`crates/sw-core/src/command_completion.rs`、`op_runner.rs`、`uart_bridge.rs`、`login_fsm.rs` 的純觀測 hook、`session_core.rs` 的歧義結算、`command_exec.rs`、`crates/sw-daemon/tests/rpc_console.rs`，以及本包明列的契約、鎖檔、文件與 fragment。

**不得做的事**

- 不改 Python 程式、既有 golden、schema 形狀、錯誤碼集合、既有案例分母與鎖定節點。
- 不改 `completion_regex`、passthrough、登入判準、reboot／recover／file transfer。
- 不注入 sentinel、不包裝命令、不先送換行、不清 human 半行、不新增 human 在途閘門。
- 不改 ownership、deferred replay、救援鍵或 #264；不得順手承接其他工作包。
- 不為相容性失敗補假 echo、延長期限、放寬斷言或刪測試；衝突列出精確節點交編排者裁決。

**驗收**

- 先貼出指定缺陷測試在修正前的紅燈，再提交綠燈與 RPC 接線證據。
- i1：human 舊 prompt、prpl／Speedport prefix、deferred A→human→B、分包折行、無 echo、raw 半行及 TX 不增加。
- i2：#262 的 A／B／C 分段全文；#271 的 `abc`／NUL 精確結果；未知 prompt 保留輸出與 partial；三票互不回歸。
- DESIGN 第 5 節命令全數通過；契約登錄、coverage 與 L1 add-only 完整，policy 0 fail。
- 既有 producer、wrapped echo、background、timeout、登入樣本及 passthrough 斷言保留。
- BC-22 完整記錄 Rust／Python 差異；回報實際 `contract_revision`、新增 nodeids、Windows 待收集節點及 SPEC-GAPS。
- 三票全部驗收後才解除對應已知問題；未完成部分明列後續，不以 i1 完成代替整包交付。
=== 0.5.1 共同規則（每個工作包都適用）===
裁決：`git -C /home/paul_chen/prj_pri/serialwrap-worktrees/parity-amendments show origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/v051.md`（OD-111 與先前仍有效的裁決）。

1. **Python 基線凍結**：不得修改 `sw_core/`、`serialwrapd.py`、`serialwrap`。Rust 與 Python 的行為差異登錄到 `contracts/behavior_changes.yaml`（BC），格式照既有條目，並附測試；A/B 黑箱測試以既有的 `rust_only` 標記或差分登錄處理，不得刪除或放寬既有斷言。
2. **先紅後綠**：先寫會紅的測試並貼出紅燈輸出，再修。產品修正要有從 RPC／CLI 入口到實際行為的接線證據（ServiceRig／CoreHarness／黑箱任一）。
3. **判準不得放寬**：不刪除、不改名既有測試（改名等於移除鎖定節點），不放寬期限或斷言來讓測試通過。
4. **鎖檔**：新增的測試節點用 `python3 tests/acceptance/regen_lock.py --l1-add-only --set <set> --platform linux --collect <file>` 登記（collect 檔要含該集合既有節點；pytest 進 `p7_gate:python3-m-pytest-q-tests` 與 `s21_gate:pytest_full`；cargo 進 `p7_gate:cargo-test-workspace`、`s21_core`、`s22_gate:cargo_workspace`），最後 `regen_lock.py --check` 通過。Windows 集合不登記，在回報列出待 Windows 原生收集的節點。
5. **契約、golden、案例分母**：只在本包明列要改時才改。權威 YAML（`contracts/*.yaml`、`contracts/behavior_changes.yaml` 等）目前沒有寫入工具，以人工變更維護，每項變更要對應裁決或本包說明；由工具衍生的區段、golden、鎖檔一律用工具產生，不得手寫，也不得為了通過驗證而改驗證器。改了要在回報寫明 `contract_revision` 是否改變。
6. **不順手做別的事**：發現範圍外的問題寫進回報的 SPEC-GAPS，不另做。
7. **changelog fragment**：`changelog.d/<issue>-<slug>.md`，YAML frontmatter 照既有檔（`type` 為 change／deprecate／docs／feat／fix／perf／refactor／remove／security 之一，要有 `scope`）。整包共用一個，第一個子項建立。
8. **執行環境**：先 `export TMPDIR="$(mktemp -d /tmp/sw.XXXXXX)" CARGO_TARGET_DIR="/srv/thala-e/serialwrap-cargo/v051-i294" CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=6`（cargo target 放在 E 槽的 serialwrap 專用目錄，`i294` 換成本包代號；只寫這個目錄，不得寫入 `/srv/thala-e` 的其他位置）、`set -o pipefail`；命令一律 `nice -n 10`，同時只跑一個 cargo；**workspace 或 crate 層級的 cargo build／test／check／clippy，以及整檔 pytest，一律包進 `heavy-slot serialwrap-v051-i294 nice -n 10 <命令>`**（與 thala 共用的主機重負載 slot，拿不到會每 20 秒排隊；單一小測試、git、讀檔不用包）；測試前景執行，輸出 `tee` 存檔。
9. **主機安全**：不碰正式 serialwrap daemon（`serialwrap.service`）與 `~/.config/serialwrap`、`~/.local/state/serialwrap`、`/dev/ttyUSB*`；不在主機跑 `test_s24_supervision_systemd.py`；不跑容器；不透過 WSL interop 執行 Windows 程式、不開任何 COM。停掉的測試留下的拋棄式 daemon 以 `/proc/<pid>/exe` 判斷後清掉，不得用 `pkill -f`。
10. **驗收**：本項新增／修改的測試；受影響 crate 的 `cargo test -p <crate> --features test-fakes --no-fail-fast`；`cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu`；`cargo clippy -p <crate> --tests --features test-fakes -- --cap-lints warn`（只看本項改的檔）；`python3 -m policy_check --repo . --pr-base-ref origin/main`（0 fail）；`regen_lock.py --check`。不跑全套 pytest、`cargo test --workspace`、gate 正式執行、Windows 原生。
11. **提交**：只逐檔 stage 本項變更，繁中 Conventional Commit；引用 issue 寫「issue N」（不要寫 #N）；結尾附 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
12. **回報**：做了什麼、改了哪些檔與函式、紅綠燈輸出、新增 nodeid、BC 條目、SPEC-GAPS、需要編排者或擁有者決定的事；最後一行 `REMAINING: none` 或列出本項未完成的事。
