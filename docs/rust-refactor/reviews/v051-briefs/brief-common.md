=== 0.5.1 共同規則（每個工作包都適用）===
裁決：`git -C /home/paul_chen/prj_pri/serialwrap-worktrees/parity-amendments show origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/v051.md`（OD-111 與先前仍有效的裁決）。

1. **Python 基線凍結**：不得修改 `sw_core/`、`serialwrapd.py`、`serialwrap`。Rust 與 Python 的行為差異登錄到 `contracts/behavior_changes.yaml`（BC），格式照既有條目，並附測試；A/B 黑箱測試以既有的 `rust_only` 標記或差分登錄處理，不得刪除或放寬既有斷言。
2. **先紅後綠**：先寫會紅的測試並貼出紅燈輸出，再修。產品修正要有從 RPC／CLI 入口到實際行為的接線證據（ServiceRig／CoreHarness／黑箱任一）。
3. **判準不得放寬**：不刪除、不改名既有測試（改名等於移除鎖定節點），不放寬期限或斷言來讓測試通過。
4. **鎖檔**：新增的測試節點用 `python3 tests/acceptance/regen_lock.py --l1-add-only --set <set> --platform linux --collect <file>` 登記（collect 檔要含該集合既有節點；pytest 進 `p7_gate:python3-m-pytest-q-tests` 與 `s21_gate:pytest_full`；cargo 進 `p7_gate:cargo-test-workspace`、`s21_core`、`s22_gate:cargo_workspace`），最後 `regen_lock.py --check` 通過。Windows 集合不登記，在回報列出待 Windows 原生收集的節點。
5. **契約、golden、案例分母**：只在本包明列要改時才改。權威 YAML（`contracts/*.yaml`、`contracts/behavior_changes.yaml` 等）目前沒有寫入工具，以人工變更維護，每項變更要對應裁決或本包說明；由工具衍生的區段、golden、鎖檔一律用工具產生，不得手寫，也不得為了通過驗證而改驗證器。改了要在回報寫明 `contract_revision` 是否改變。
6. **不順手做別的事**：發現範圍外的問題寫進回報的 SPEC-GAPS，不另做。
7. **changelog fragment**：`changelog.d/<issue>-<slug>.md`，YAML frontmatter 照既有檔（`type` 為 change／deprecate／docs／feat／fix／perf／refactor／remove／security 之一，要有 `scope`）。整包共用一個，第一個子項建立。
8. **執行環境**：先 `export TMPDIR="$(mktemp -d /tmp/sw.XXXXXX)" CARGO_TARGET_DIR="/srv/thala-e/serialwrap-cargo/v051-<本包代號>" CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=6`（cargo target 放在 E 槽的 serialwrap 專用目錄，`<本包代號>` 換成本包代號；只寫這個目錄，不得寫入 `/srv/thala-e` 的其他位置）、`set -o pipefail`；命令一律 `nice -n 10`，同時只跑一個 cargo；**workspace 或 crate 層級的 cargo build／test／check／clippy，以及整檔 pytest，一律包進 `heavy-slot serialwrap-v051-<本包代號> nice -n 10 <命令>`**（與 thala 共用的主機重負載 slot，拿不到會每 20 秒排隊；單一小測試、git、讀檔不用包）；測試前景執行，輸出 `tee` 存檔。
9. **主機安全**：不碰正式 serialwrap daemon（`serialwrap.service`）與 `~/.config/serialwrap`、`~/.local/state/serialwrap`、`/dev/ttyUSB*`；不在主機跑 `test_s24_supervision_systemd.py`；不跑容器；不透過 WSL interop 執行 Windows 程式、不開任何 COM。停掉的測試留下的拋棄式 daemon 以 `/proc/<pid>/exe` 判斷後清掉，不得用 `pkill -f`。
10. **驗收**：本項新增／修改的測試；受影響 crate 的 `cargo test -p <crate> --features test-fakes --no-fail-fast`；`cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu`；`cargo clippy -p <crate> --tests --features test-fakes -- --cap-lints warn`（只看本項改的檔）；`python3 -m policy_check --repo . --pr-base-ref origin/main`（0 fail）；`regen_lock.py --check`。不跑全套 pytest、`cargo test --workspace`、gate 正式執行、Windows 原生。
11. **提交**：只逐檔 stage 本項變更，繁中 Conventional Commit；引用 issue 寫「issue N」（不要寫 #N）；結尾附 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
12. **回報**：做了什麼、改了哪些檔與函式、紅綠燈輸出、新增 nodeid、BC 條目、SPEC-GAPS、需要編排者或擁有者決定的事；最後一行 `REMAINING: none` 或列出本項未完成的事。
