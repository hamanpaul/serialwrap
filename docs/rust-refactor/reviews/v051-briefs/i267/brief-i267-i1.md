0.5.1 工作包 i267（一包一項）：修正 issue 267。session 停在終態 last_error（例如 CREDENTIALS_UNRESOLVED）時，被動的 RX stall 診斷被遮蔽，self-test 也給出誤導的分類。本項新增條件式的 transport 健康診斷。起點：分支 `fix/267-passive-stall-diagnosis`，從 origin/main 分出。

=== 必讀 ===
- 設計稿：`/home/paul_chen/b-log/rust-refactor/exec/v051/i267/design-i267.md`，內含事實核對、方案、設計決策、契約與測試計畫，先讀完。
- 裁決：`git -C /home/paul_chen/prj_pri/serialwrap-worktrees/parity-amendments show origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/v051.md`。先讀以下各節：
  - 「0.5.1 行為差異登錄規則」（含第 8 點補充）
  - 「OD-114」
  - 「其餘工作包的 BC 編號」
  - 「編排者定案（2026-11-24）：i268、i227、i267、i294 設計」
- 本包登錄 **BC-21**：`scope: rust_only`、`ab_participation: absolute_only`、`decision_ref: OD-111`，格式照 BC-06。
- issue 全文在 `/home/paul_chen/b-log/rust-refactor/exec/v051/i267/issue-*.md`，也可以用 `gh issue view`。

=== 編排者定案（依 OD-112，擁有者可推翻）===
設計稿第 3 節標【需編排者定案】的各項，除下列各點外，一律採用設計稿的「建議」值。
- 9 項全部採用建議值，沒有例外。重點如下：
  - 條件式 `transport_health` 欄位。
  - RX 靜默 60 秒標 `rx_quiet`；有效 TX 之後 10 秒仍無 RX，才標 `stall_suspected`。
  - 本輪一律 `corroboration=not_collected`，只回報「疑似」。
  - 不自動 TX，不覆寫帳密終態，不寫 state.json。
  - USB reset 與通知（issue 233）不在本包。

=== 工作包說明（設計稿草擬，編排者已核）===
0.5.1 工作包 i267：修正終態 last_error 遮蔽被動 RX 停流診斷及 self-test 誤導分類。BC 編號 **BC-21**。採單一子項 **i267-i1**。

起點：分支 ``fix/267-passive-stall-diagnosis``；起點 HEAD ``fix/267-passive-stall-diagnosis``。設計讀碼基準為 `e5cee8c3`，實作前核對指定起點與裁決。

**背景**

- `reprobe.rs:127` 對 `CREDENTIALS_UNRESOLVED` 早退，這項帳密保護必須保留。
- 現有 transport stall 判斷僅接在 probe 失敗路徑，因此終態 session 即使原本收過 RX、後來停流，也沒有獨立診斷。
- `session_core.rs:8886` 的 ATTACHED self-test 最後可能回 `ATTACHED_NOT_READY／console_attach`，即使 human console 已經連著。
- Python 凍結。只修 Rust，差異登錄 BC-21；不新增錯誤碼。

**本包要做的事**

1. **完成同一條被動診斷資料流。**
   - 在 `transport_stall.rs` 新增純判定及被動提示；保留原 probe 判定與 golden。
   - 從 reader 的非空原始 RX、writer 的已確認 TX 取得證據，按 bridge epoch 隔離。
   - 不直接以現有 `last_tx_age_s` 當確實 TX；Rejected、NotSubmitted、unknown 不得建立 witness。
   - TX 寫入期間有 RX 時不建立 witness；持續 TX 不延後第一筆未獲 RX 的觀測期限。
   - 判定資料只存數值與時間，不保存 payload，不改既有 TX／WAL／receipt 語意。

2. **增加診斷輸出並接上 self-test。**
   - 建議門檻：當前世代曾有 RX，靜默 60 秒為 `rx_quiet`；另有確認 TX、其完成觀測後 10 秒仍無 RX，為 `stall_suspected`。
   - `session.list` 的 session 增加條件式 `transport_health`；self-test 使用相同快照。
   - 疑似時建議回 `ok=true`、`classification=TRANSPORT_STALL`、`recommended_action=check_host_transport`，附診斷物件，不附 `probe_ok`。
   - `corroboration=not_collected`；不得宣稱已證實 USB STALL。
   - 有可信 busid 才附該裝置的人工復原指引；Windows 不輸出 Linux 指令。
   - 保留既有安全、busy、quiet 與 passthrough 分類優先序。
   - RX 恢復或 bridge 世代更換即清除診斷；原本的 `last_error`、detail、state、重探資料不受影響。

   **以上門檻、欄位、優先序及「僅疑似」範圍，須以編排者記錄的定案為準。無 TX 的長時間靜默建議只加標記、不回 STALL。**

3. **契約、測試與文件隨實作交付。**
   - BC-21 採 `rust_only／absolute_only／OD-111`，所屬 P7。
   - 修改 RPC schema、coverage_map、l1-only 核准及兩個指定 BC 集合；不修改錯誤碼集合或比較器。
   - 新增 `transport_health_i267.rs` 與 `transport_health_rpc_i267.rs`，先紅後綠；至少有真 ServiceRig 的缺帳密終態接線案例。
   - 覆蓋門檻、正常 RX、從未 RX、RX 恢復、世代交替、RX 尚未 actor dispatch、TX 結果資格及分類優先序。
   - 診斷期間驗證 UART bytes、write、OS submit、WAL TX 均不增加。
   - 新節點加入指定 Linux L1 集合；保留 Python golden。
   - 同步 README 中英、診斷文件、self-test spec 及一份 changelog fragment。

**不得做的事**

- 不覆寫帳密終態，不啟動額外 probe、不自動送任何 bytes。
- 不改 Python、登入 gate、重探 allowlist、i300 退避、release／clear／bind／recover 的既有行為。
- 不新增背景通知、kernel 日誌收集、USB reset、cadence 學習、設定入口或持久化診斷。
- 不放寬既有斷言、刪除／改名鎖定節點或修改驗證器取得綠燈。
- p287、i268、i306、i311 的重疊位置由編排者安排合併順序；不另實作它們的功能。

**驗收**

- 上述新測試先紅後綠，保存輸出；ServiceRig 證明終態仍可看見疑似停流與正確 action。
- RX 恢復後診斷自動消失，缺帳密終態保持原樣。
- sw-core、sw-daemon crate 測試、Windows cross-check、相關 clippy、契約追加測試、contracts check、policy check、L1 check 通過。
- 契約 revision、BC-21 coverage、新增 nodeid 與 Windows 待收集節點交代完整。
- 一輪實作、一次 terra 審查；依 OD-114，本包另做必要的整包驗收重跑與一位審查者核對。
- 真實 FTDI／USB-IP STALL 與 Windows 原生未跑就列為未驗證。kernel 佐證、通知及 USB 復原留後續，不宣稱本輪完成。
- 若既有鎖定斷言或定案與實作需求衝突，明列 SPEC-GAPS 及受影響項目，不以 `REMAINING: none` 隱藏缺項。
=== 0.5.1 共同規則（每個工作包都適用）===
裁決：`git -C /home/paul_chen/prj_pri/serialwrap-worktrees/parity-amendments show origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/v051.md`（OD-111 與先前仍有效的裁決）。

1. **Python 基線凍結**：不得修改 `sw_core/`、`serialwrapd.py`、`serialwrap`。Rust 與 Python 的行為差異登錄到 `contracts/behavior_changes.yaml`（BC），格式照既有條目，並附測試；A/B 黑箱測試以既有的 `rust_only` 標記或差分登錄處理，不得刪除或放寬既有斷言。
2. **先紅後綠**：先寫會紅的測試並貼出紅燈輸出，再修。產品修正要有從 RPC／CLI 入口到實際行為的接線證據（ServiceRig／CoreHarness／黑箱任一）。
3. **判準不得放寬**：不刪除、不改名既有測試（改名等於移除鎖定節點），不放寬期限或斷言來讓測試通過。
4. **鎖檔**：新增的測試節點用 `python3 tests/acceptance/regen_lock.py --l1-add-only --set <set> --platform linux --collect <file>` 登記（collect 檔要含該集合既有節點；pytest 進 `p7_gate:python3-m-pytest-q-tests` 與 `s21_gate:pytest_full`；cargo 進 `p7_gate:cargo-test-workspace`、`s21_core`、`s22_gate:cargo_workspace`），最後 `regen_lock.py --check` 通過。Windows 集合不登記，在回報列出待 Windows 原生收集的節點。
5. **契約、golden、案例分母**：只在本包明列要改時才改。權威 YAML（`contracts/*.yaml`、`contracts/behavior_changes.yaml` 等）目前沒有寫入工具，以人工變更維護，每項變更要對應裁決或本包說明；由工具衍生的區段、golden、鎖檔一律用工具產生，不得手寫，也不得為了通過驗證而改驗證器。改了要在回報寫明 `contract_revision` 是否改變。
6. **不順手做別的事**：發現範圍外的問題寫進回報的 SPEC-GAPS，不另做。
7. **changelog fragment**：`changelog.d/<issue>-<slug>.md`，YAML frontmatter 照既有檔（`type` 為 change／deprecate／docs／feat／fix／perf／refactor／remove／security 之一，要有 `scope`）。整包共用一個，第一個子項建立。
8. **執行環境**：先 `export TMPDIR="$(mktemp -d /tmp/sw.XXXXXX)" CARGO_TARGET_DIR="/srv/thala-e/serialwrap-cargo/v051-i267" CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=6`（cargo target 放在 E 槽的 serialwrap 專用目錄，`i267` 換成本包代號；只寫這個目錄，不得寫入 `/srv/thala-e` 的其他位置）、`set -o pipefail`；命令一律 `nice -n 10`，同時只跑一個 cargo；**workspace 或 crate 層級的 cargo build／test／check／clippy，以及整檔 pytest，一律包進 `heavy-slot serialwrap-v051-i267 nice -n 10 <命令>`**（與 thala 共用的主機重負載 slot，拿不到會每 20 秒排隊；單一小測試、git、讀檔不用包）；測試前景執行，輸出 `tee` 存檔。
9. **主機安全**：不碰正式 serialwrap daemon（`serialwrap.service`）與 `~/.config/serialwrap`、`~/.local/state/serialwrap`、`/dev/ttyUSB*`；不在主機跑 `test_s24_supervision_systemd.py`；不跑容器；不透過 WSL interop 執行 Windows 程式、不開任何 COM。停掉的測試留下的拋棄式 daemon 以 `/proc/<pid>/exe` 判斷後清掉，不得用 `pkill -f`。
10. **驗收**：本項新增／修改的測試；受影響 crate 的 `cargo test -p <crate> --features test-fakes --no-fail-fast`；`cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu`；`cargo clippy -p <crate> --tests --features test-fakes -- --cap-lints warn`（只看本項改的檔）；`python3 -m policy_check --repo . --pr-base-ref origin/main`（0 fail）；`regen_lock.py --check`。不跑全套 pytest、`cargo test --workspace`、gate 正式執行、Windows 原生。
11. **提交**：只逐檔 stage 本項變更，繁中 Conventional Commit；引用 issue 寫「issue N」（不要寫 #N）；結尾附 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
12. **回報**：做了什麼、改了哪些檔與函式、紅綠燈輸出、新增 nodeid、BC 條目、SPEC-GAPS、需要編排者或擁有者決定的事；最後一行 `REMAINING: none` 或列出本項未完成的事。
