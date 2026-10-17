# P1 Stage 0：Python 基線行為變更（BC-01～BC-05、OD-11、SC／DF、F12、Release manifest）實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Python 基線（`sw_core/`）上實作 OD-04 已核准的 BC-01～BC-05、OD-11 隔離實例測試裝置 allowlist（SC-19）、調和表指派到 P1 的 SC／DF 項目、Drain 診斷欄位、F12／`p0-wal-live` WAL oracle 修訂，以及 Python 側 release manifest 與版本配對；每項先寫失敗測試、再實作，讓 Python 先滿足絕對契約，作為 Rust（P5～P8）承接與 Sequential A/B 的基準。

**Architecture:** 只改 Python 產品碼與測試資產，不建立 Rust crate。新增三個 Python 模組作為與 Rust PAL 對應的接縫：`sw_core/wal_fs.py`（對應 `sw-pal` `WalFs`，arch §4.5）、`sw_core/win32_files.py`（Windows ctypes：`CreateFileW` share-delete、`DeletePending`、FileId、`LockFileEx`）、`sw_core/release_manifest.py`（對應 arch §10）。TX 三態 `TxResult`、`DrainReceipt` 的 Python 形狀與 arch §4.2／§5.5 的 Rust 型別一一對應，讓 Rust 實作與黑箱契約共用同一語意。所有 Windows 行為以可注入的 API 物件撰寫，Linux 上用替身做 L1、Windows 原生行為以 WSL interop 呼叫 Windows Python 實跑。

**Tech Stack:** Python 3.10+（Linux）、Windows Python 3.12（WSL interop 原生執行）、pytest、PyYAML、pyserial（Windows）、ctypes（`kernel32`、`msvcrt`）、stdlib `fcntl`／`termios`。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md` §5.1.4、§5.2、§5.3、§5.4、§6.1、§6.2、§7.4、§8、§9.1、§9.4、§9.5、§12.2 Stage 0；`docs/rust-refactor/owner-decisions-2026-10-15.md` OD-04、OD-05、OD-08、OD-10、OD-11、OD-12、OD-13；`docs/rust-refactor/plans/00-master-plan.md`；`docs/rust-refactor/plans/rust-architecture.md` §3.1、§3.8、§4.2、§4.4、§4.5、§4.9、§5.4～§5.11、§6.1、§7.7、§10；`docs/rust-refactor/plans/spec-reconciliation.md` §1、§2、§3；盤點 `inventory/02`、`03`、`04`、`05`、`06`、`07`、`08`、`10`（HEAD `93553e6`，`VERSION`=`0.3.2`）。

---

## Global Constraints

以下摘自 `00-master-plan.md` Global Constraints 與規格，只列本 Phase 適用者；各 Task 不再重複。

- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間（ADR-07、§9.3）；`CREDENTIALS_UNRESOLVED` 只屬後者。本 Phase 不新增任何未列於 `contracts/error_codes.yaml` 的碼；`COMMAND_ABORTED_IN_FLIGHT` 由 BC-03 生效，狀態由 `APPROVED` 改 `IMPLEMENTED`。
- 錯誤碼計數由 `contracts/error_codes.yaml` 資料產生，不得硬編碼 78（§9.2、OD-06）。
- CLI JSON：`json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 等價輸出（`_print` 另帶 `sort_keys=True`，`CLI-11.16`）；`state.json`／WAL 以 `sort_keys=True`。
- 背壓上限：`CMD_PENDING_MAX=256`、`CMD_HISTORY_MAX=512`、`BG_CAPTURE_MAX_COUNT=64`、`DEFERRED_INPUT_MAX_BYTES=262144`（BC-02：超過整段清空）、console line buffer `16385` B（SC-16）。
- Drain timeout 3,000 ms → `RECOVERING`＋`drain_stalled:true`＋`drain_timeout_epoch`（取 `bridge_generation`，§5.2.5、SC-04）。Python 常數 `sw_core.constants.DRAIN_TIMEOUT_MS = 3000`，測試可 monkeypatch。
- WAL rotate 門檻 `R = DEFAULT_WAL_ROTATE_BYTES = 67108864`，條件 `size >= R`、寫入前檢查、WAL 與 mirror 各自判斷（`WAL-D7`，§1.1.3）；不得改值。
- Windows 序列埠：`CreateFileW(OPEN_EXISTING, dwShareMode=0)`（Python 經 pyserial 既有路徑）；WAL handle：`FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE`（§6.2、§8.2）。
- Windows RPC 預設 `tcp://127.0.0.1:48700`；信任範圍＝整機本機使用者（OD-12），文件不得宣稱「同使用者隔離」；`serialwrap remote` 經 ssh 暴露 RPC 時，信任範圍延伸至通過該 ssh 驗證者（OD-12）。
- 虛擬 COM allowlist、sandbox 或 `--force-spawn` 均不得繞過 Tier 2 獨占與交接（§6.2 末段、OD-11）。
- 凡偏離基線一律登記於 `contracts/behavior_changes.yaml`（BC／SC／DF，arch §0.2），本 Phase 依 P0「狀態推進協定」（`01-stage0-contracts.md` Global Constraints）把對應項 `status` 由 `APPROVED` 改為 `IMPLEMENTED`；`VERIFIED` 只在指定 artifact＋環境取得證據後由擁有者標記。**`contracts/` 內任何檔都不得寫 `contract_revision` 鍵**（tree SHA 無法存進它所雜湊的 tree；P0 `test_bc_real_no_contract_revision_key`）；整數版本一律是 `catalog_revision`（檔案頂層 `revision`、`approved_at_revision`、schema 的 `x-catalog-revision`），修改 schema 檔時依 P0 規則遞增 `x-catalog-revision`。實作 commit SHA 只記在 PR 描述與 `contracts/captures/` 證據 JSON，`git rev-parse HEAD:contracts` 只在 release manifest／capture 時計算。
- 契約同步（P0 C03／C04／C09／C16／C17）：凡把某變更翻成 `IMPLEMENTED`，**同一 commit** 內必須：(a) 該變更引用的碼在 `contracts/error_codes.yaml` 改 `status: IMPLEMENTED`、`reachable: true`、補 `contract_tests`；(b) `contracts/rpc_schema.json`／`contracts/cli_schema.json` 中 `change` 為該變更的 `x-pending` 項併入正式欄位並自 `x-pending` 刪除；(c) 新增或修改的 WAL META 事件寫入 P0 建立的唯一 META schema `contracts/wal_meta_events.schema.json`（只修改、不另建）；(d) 執行 `python3 -m tools.contracts check` 通過，並以 `python3 -m tools.contracts skill-section --namespace error_code`／`--namespace session_last_error` 重新產生兩份 SKILL 的受管區段。YAML 欄位依 P0 loader：`retryable` 必須寫成字串 `"no"`（不得寫 bare `no`，PyYAML 會轉成 bool）、碼的介面寫在 `scope.interfaces`、變更項 `scope ∈ {both, rust_only, python_when_manifest_present}`。
- `VERSION` 本 Phase **不 bump**（維持 `0.3.2`）：P0 已定 `applies_to.python.min_version: "0.4.0"` 為目標 release 版號，由 Stage 0 release PR 帶 `release:0.4.0` label 一次 bump（避免 feature PR 觸發 R-07）。
- pytest marker：註冊與稽核的唯一 owner 為 P0 T14（`tests/conftest.py` 的 10 個 marker 與 `tests/test_resource_markers.py`；marker 一律寫在檔內 `pytestmark`，P2 Task 13 **不建立** `tests/platform_resources.yaml`，見 01 T14）。本 Phase **不得**另行註冊 marker；每個新測試檔依 P0 T14 規則標註「恰一個平台 marker（`core_portable`／`posix_only`／`windows_native`）＋至少一個資源 marker」，同檔混合平台時平台 marker 放 class／函式層（不另設 YAML 登記）。本文件測試片段中的 `windows_native`、`windows_com`、`posix_pty` 即 P0 的同名 marker；只標 `posix_pty` 者一律補 `posix_only`，只標 `windows_com` 者一律補 `windows_native`。
- 新測試模組比照 P0 執行規則：**不得在模組頂層 import 本 Phase 才新增的模組或名稱**（`sw_core.wal_fs`、`sw_core.win32_files`、`sw_core.release_manifest`、`sw_core.test_allowlist`、`sw_core.filelock`、`realhw.wal_oracle`、`TxResult`、`DrainReceipt` 等），一律在測試函式內以 `_load()` 輔助函式延遲 import，讓 `python3 -m pytest -q tests/` 在任何階段都能完成 collection；本文件測試片段寫成頂層 import 者，撰寫時照此改寫，斷言不變。
- Python 風格：`from __future__ import annotations`、完整型別標註；設定物件 frozen dataclass，執行期狀態 mutable；RPC 分派維持平面 if/elif；RPC 回應一律 `dict[str, Any]`＋`ok: bool`，失敗附 `error_code`，例外不得穿越 RPC 邊界（`EXCEPTION` 既有 framing 除外）。
- 測試：`python3 -m pytest -q tests/` 為準（CLAUDE.md）；不得引入新的失敗；「已知 flaky」不得當作放行理由。修改既有測試資產（例如 `tests/test_bounded_memory.py::test_deferred_buffer_capped`）屬規格 §10.4 的測試工具變更，PR 須註明並經獨立審閱者確認。
- 被 SKIP／ignored 的 Windows 或硬體測試**不得**計為 PASS；報告中標「證據不足」（00-master-plan §3）。
- repo 政策：不得 commit 到 `main`；本 Phase 工作分支為 `wt/rust-refactor/p1-stage0-bc`（PR base `feature/rust-refactor`，符合 CLAUDE.md 分支政策「base 為 feature 分支時來源須為 `wt/<slug>/<subtask>`」）；每個 code task 附 `changelog.d/` direct-child fragment（frontmatter `type` 取 `feat`／`fix`／`change`／`security`）；commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；Phase 完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。
- 新增 CLI 子命令（`daemon restart`、`remote stop`）須同步 `README.md` 英文與繁中兩段（R-16 help 反映、R-18）。
- 新增 env 變數（`SERIALWRAP_CONFIG`、`SERIALWRAP_TEST_DEVICE_ALLOWLIST`、`SERIALWRAP_RELEASE_MANIFEST`、`SERIALWRAP_TEST_KILL_AFTER_SAVE`（T21））一律加入 `tests/conftest.py` 的 pop 清單，避免外層 shell 洩漏（#120）。

## Review Focus

1. WAL 常駐 handle：`CreateFileW`→`msvcrt.open_osfhandle` 所有權轉移、失敗清理與唯一 close；rotate／reset 依規格 §8.2 在同一把鎖內 flush→sync→close→rename→reopen，外部 reader 阻擋時有界降級；部分寫入不得產生重複 seq 或被串接的 NDJSON 行。
2. 外部 unlink／替換／`DeletePending` 自癒：META `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 的 `loss_flag:true`、`potentially_lost_seq_range`（未知時 `null`＋`range_known:false`，不猜測）、重試有界、重建失敗不宣稱 healthy 且停止自動 TX。
3. TX 三態：TX WAL 只記 OS 確認的 bytes 且在 `_write_lock` 內 append；BC-03 映射（≥1 byte→`COMMAND_ABORTED_IN_FLIGHT`、不明→`SEND_FAILED`、未提交→`FLUSHED_BY_RECOVERY`）且任何路徑都不自動重送。
4. Drain 與交接：release 在 handle 關閉回條（`handle_closed`、`writer_converged`、`reader_stopped` 全為真）前不得回成功；3,000 ms 逾時回 `SESSION_RECOVERING`＋`drain_stalled`；重複 release 去重；drain 未完成不得重開同一端點（含 manager 層所有直接 `bridge.stop()` 呼叫點）；舊命令晚到的完成不得改新 session 狀態；BC-04 開檔獨占失敗不得清除保護後繼續收送。
5. BC-02 上下文失效：失效後 console thread 已讀入但尚未處理的舊按鍵、以及舊 `owner_gen` 的 deferred 項目一律不得回放（含已排入、等待 `_write_lock` 的 raw／replay 資料）；未失效時 #53 Soft Preempt 回放逐字不變。
6. OS write 前授權（T20）：命令出列後、實際 write 前觸發 quiet、release、cancel、lease 切換 → 零 TX。授權重驗與每次非阻塞 `os.write` 必須在同一 `_tx_fence_lock` 臨界區內（不得「驗證→放鎖→write」）；失效端持同一把鎖；鎖不跨 EAGAIN backoff 或完成等待（R0-F1）。Windows pyserial 段寫入會在鎖內阻塞，drain 取鎖因此有上限，3,000 ms 期限自請求起算並涵蓋 Phase 1 等鎖；guard 內寫入卡超過 3 s 時仍準時回 `drain_stalled`，且回條前不重開 handle（R3-F3）。自動登入、密碼（`send_secret`）、ready／reconfirm probe 與 recover 按鍵帶 `system_probe` permit（含 `login_gen`），每次提交重驗 Quiet、登入世代與 Drain；pre-check 後才 arm quiet 或推進世代 → 零 TX（R2-F1）。Quiet 只擋 agent 命令；有效 interactive lease（含 ATTACHED autoboot 救援）在 Quiet 中仍可送出，但照樣受 drain、flash、世代、取消與 lease ownership 檢查（§5.2.1，R2-F2）。

---

## 0. 執行協定（TDD 兩段式）

1. **Stage A0（測試設施與純型別）**：先完整做完 T01（A 與 B 步驟都做），因為它本身是測試程式碼（替身、runner、平台 skip）加上替身依賴的純 TX 結果型別（`TxResult`、`TxIncompleteError` 家族），其他 Task 的測試依賴它。
2. **Stage A（全 Phase 測試先行）**：依序執行 T02～T21 的 A1、A2 步驟：寫測試、跑測試、確認失敗，每個 Task 一個 `test(...)` commit。A2 的「預期原因」以**Stage A 當下**判定：各 Task 列出的原因是「其相依 Task 已實作」時的原因；Stage A 當下若因相依 Task 尚未實作而得到相依模組的 `ModuleNotFoundError`／`AttributeError`（測試函式內 `_load()` 拋出，collection 必須成功），亦屬有效紅燈，但實作者在該 Task 的 B2 前必須先確認其列出原因成立。出現 collection error、與本 Task 無關的 import error 時先修測試，不得當作紅燈已驗證。
3. **Stage A1.5（測試審核，鎖定斷言前）**：每個 Task 的 A1 交付後，由**非該 Task 測試作者、非實作者**的審核者（依 §0.1「Test reviewer」欄）逐條檢查測試本身的缺陷（錯誤 API／key、浮點邊界、無 sleep 的等待迴圈、與行為規格自相矛盾的斷言、必然先綠的「紅燈」）；可行時以一次性 spike（不 commit）空跑關鍵案例。審核結論與修正記入該 Task 的 `test(...)` commit 訊息本文，之後斷言才鎖定。下列測試為**回歸防護（既綠）**，A2 允許其已通過、不當作紅燈證據：T10 `test_fd_not_inherited_by_child`、T11 `test_released_still_session_not_ready`、T02 `test_restart_resumes_max_seq`、T04 `test_reset_directory_missing_raises_and_keeps_seq`、T09 `test_soft_preempt_replay_unchanged_without_invalidation`、T12 `test_human_lease_not_expired_by_tick`。
4. **Stage B（實作）**：依下列相依順序執行 B1～B3：T02 → T03 → T04 → T05 → T06 → T07 → T08 → T09 → T10 → T11 → T12 → T20 → T13 → T14 → T18 → T15 → T16 → T17 → T19 → T21。每個 Task 的 B2 除了該 Task 測試與「相關既有測試」清單外，**必跑全套 `python3 -m pytest -q tests/`**；允許失敗的集合**只限**尚未進入 Stage B 之 Task 的新測試檔（逐檔列在該次 B2 紀錄中），其餘任何失敗（含既有測試）都必須在本 Task 內修正或依第 5 點走測試資產變更。Stage B 最後一步跑全套 `python3 -m pytest -q tests/`（零失敗）、Windows 原生 `scripts/stage0/win-pytest.sh -m windows_native tests/`、`python3 -m tools.contracts check`、`python3 -m policy_check --repo .`。
5. **測試作者與實作者**：依各 Task 宣告。測試作者在 A1.5 鎖定後不得再改該 Task 測試的斷言；實作者若認為測試有誤，回報測試作者與審查者裁決（規格 §10.4），不得自行放寬。**既有測試資產變更**一律在該 Task 的「測試資產變更」小節逐檔列出（檔案、測試名、改動理由、對應 BC／SC／DF），只有列出者可改，且須獨立審閱；未列出的既有測試變紅＝實作缺陷。
6. **雙審**：Phase 結束後依 00-master-plan §1 做 sonnet max＋codex `gpt-6.1-sol` medium 雙審；審查重點以本文件 Review Focus 為準。

### 0.1 Task 總表

模型分派依 00-master-plan §2：並行、平台、狀態機（WAL、Arbiter、Drain、鎖、監管停止交接、可信來源跨層傳遞）一律 Codex `gpt-6-luna` max；Haiku 只負責範圍明確、介面已定的模組；同一 Task 的測試作者、測試審核者（A1.5）與實作者三者模型不同。

| Task | 標題 | 範圍 | Test author | Test reviewer（A1.5） | Implementer |
|---|---|---|---|---|---|
| T01 | Stage 0 測試設施：替身、平台 skip、TX 結果純型別、Windows 原生 pytest runner | 基礎 | haiku-xhigh | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max（`win-pytest.sh` 的 Windows 行程樹終止、WSLENV、staging 與 exit code 屬平台高風險，與 P3 Task 17 同類，R0-F6） |
| T02 | BC-01a WAL 常駐 handle、每筆 flush、唯一 close、DF-17 | BC-01、DF-17、SC-11（WAL 端） | haiku-xhigh | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| T03 | BC-01b Windows `CreateFileW` share-delete handle 與 `win32_files` | BC-01、SC-10（API 層） | agy-gemini-3.8-flash-high | haiku-xhigh | codex-gpt-6-luna-max |
| T04 | BC-01c rotate／reset 於常駐 handle 下同步與 DF-03 歸檔後綴 | BC-01、DF-03 | agy-gemini-3.8-flash-high | haiku-xhigh | codex-gpt-6-luna-max |
| T05 | BC-01d 外部 unlink／替換／DeletePending 偵測、有界自癒、缺口 META、停止自動 TX | BC-01 | haiku-xhigh | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| T06 | BC-03／SC-07 TX 三態、TX WAL 只記確認 bytes、可取消寫入、DF-15 精確 wal_seq | BC-03、SC-07、DF-15 | agy-gemini-3.8-flash-high | haiku-xhigh | codex-gpt-6-luna-max |
| T07 | SC-04a Bridge 兩段式 Drain 與 `DrainReceipt` | SC-04 | haiku-xhigh | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| T08 | SC-04b Session Drain、release／attach 交接回應、drain 診斷欄位、BC-03 中止映射、SC-11 有序關機與 `daemon.handoff_stop` 原子交接 | SC-04、BC-03、SC-11 | agy-gemini-3.8-flash-high | haiku-xhigh | codex-gpt-6-luna-max |
| T09 | BC-02 DeferredBuffer 整段清空與上下文標記 | BC-02 | haiku-xhigh | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| T10 | BC-04 Per-UART 獨占、holder 診斷、Windows ACCESS_DENIED 映射、SC-05 同步收回、DF-04 | BC-04、SC-05、DF-04 | agy-gemini-3.8-flash-high | haiku-xhigh | codex-gpt-6-luna-max |
| T11 | Gate 錯誤碼：SC-01、SC-02、SC-03、SC-06、DF-02 | SC-01/02/03/06、DF-02 | agy-gemini-3.8-flash-high | haiku-xhigh | codex-gpt-6-luna-max |
| T12 | SC-13 lease tick＋tombstone、SC-14 flash probe 資格、SC-16 console 行上限 | SC-13/14/16 | haiku-xhigh | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| T13 | OD-11／SC-19 隔離實例測試裝置 allowlist | SC-19 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | haiku-xhigh |
| T14 | BC-05 Windows `remote -R/-L`、SC-27 `connect`／`benches`、SC-21 `remote stop`、跨行程檔案鎖 | BC-05、SC-21、SC-27 | haiku-xhigh | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| T15 | 平台管理：SC-08、SC-09、SC-10、SC-12、SC-20、DF-14 | SC-08/09/10/12/20、DF-14 | agy-gemini-3.8-flash-high | haiku-xhigh | codex-gpt-6-luna-max |
| T16 | 缺陷修正群組：DF-01、DF-06、DF-07、DF-08、DF-09、DF-10、DF-11、DF-12、DF-13、DF-16 | DF 群組 | haiku-xhigh | codex-gpt-6-luna-max（DF-01/06/07/08/09）／agy-gemini-3.8-flash-high（DF-10～13/16） | agy-gemini-3.8-flash-high（DF-01/06/07/08/09：event engine、client）／codex-gpt-6-luna-max（DF-10～13/16：setup、sudo 閘、監管） |
| T17 | F12 `f12-wal-path-live` 與 L3 `p0-wal-live` 改為 seq／marker／CRC oracle | OD-13、TST-D-7 | haiku-xhigh | codex-gpt-6-luna-max | agy-gemini-3.8-flash-high |
| T18 | Python 側 release manifest、`tree_sha256`、`version_pairing`、DF-05 daemon 定位 | SC-25（Python 側）、DF-05、OD-13 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | haiku-xhigh |
| T19 | Stage 0 凍結值擷取、S1-W WAL 微基準與短測 | §1.1.2、§1.1.3、§8.1、OD-10 | haiku-xhigh | codex-gpt-6-luna-max | agy-gemini-3.8-flash-high |
| T20 | OS write 前授權 permit（quiet／release／cancel／lease／owner_gen 零 TX） | SC-04、BC-02、BC-03、§5.2.2、§11.3 | haiku-xhigh | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| T21 | Stage 0 實機回歸 case、程序 runbook 與 Spike 3 腳本（撰寫；實跑 BLOCKED-OWNER） | BC-01、BC-02、BC-03、BC-04、SC-04、BC-05、#155 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | haiku-xhigh |

---

## Task T01：Stage 0 測試設施：替身、平台 skip、TX 結果純型別、Windows 原生 pytest runner

- **範圍**：本 Phase 共用測試基礎；唯一的產品碼是**純型別**（`TxResult` dataclass 與 `TxIncompleteError` 例外家族，不改任何行為），因為替身 `FakePort.write` 的預設回傳值依賴它，必須在其他 Task 之前存在（不得以臨時替代型別讓測試虛假通過）。marker 的**註冊**屬 P0 T14，本 Task 只在既有 collection hook 內補平台／資源條件 skip。
- **Test author model**：haiku-xhigh　**Test reviewer（A1.5）**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max（00-master-plan §2：平台行程管理歸 Codex；與 04 Task 17 `win-runner.sh`／`win-test.sh` 同類，R0-F6）。**Windows 執行步驟**（B2 的 `scripts/stage0/win-pytest.sh …` 實跑）需寫入 `/mnt/c` 並呼叫 `cmd.exe`／`powershell.exe`／`taskkill.exe`，Codex `-s workspace-write` 沙箱可能擋下，一律由 orchestrator 執行並把 `summary.json`／`junit.xml` 路徑交給實作者；Codex 實作者只改程式碼（同 07 執行順序第 5 點）。
- **Changelog fragment**：`changelog.d/rust-p1-stage0-test-infra.md`（`type: change`，`scope: tests`）：新增 Stage 0 行為變更測試共用替身、TX 結果型別、平台條件 skip 與 Windows 原生 pytest runner。
- **BLOCKED-OWNER**：Windows host 的 `py -3.12` 需已安裝 `pytest`、`PyYAML`、`pyserial`、`jsonschema`；缺少時 runner 以 exit 3 回報，安裝需擁有者同意（`py -3.12 -m pip install --user pytest pyyaml pyserial jsonschema==4.10.3`），在同意前所有 `windows_native` 結果記為「證據不足」。

**Files**
- Create: `tests/stage0_fakes.py`、`scripts/stage0/win-pytest.sh`、`scripts/stage0/junit_summary.py`（R5-F3）、`tests/test_stage0_fakes.py`
- Modify: `sw_core/serial_port.py`（新增 `TxResult`，不改既有函式）、`sw_core/uart_io.py`（新增 `TxIncompleteError`、`TxUnknownError`、`SerialNotReady` 類別，不改既有函式）、`tests/conftest.py`（在 P0／P2 既有的 `pytest_collection_modifyitems` 內補平台條件 skip——**不得**新增 `pytest_configure` 或註冊 marker；pop 清單加 `SERIALWRAP_CONFIG`、`SERIALWRAP_TEST_DEVICE_ALLOWLIST`、`SERIALWRAP_RELEASE_MANIFEST`）（本 Task 新測試檔帶 P0 T14 `pytestmark`）

**Interfaces**
- Consumes：`sw_core.uart_io.UARTBridge`、`sw_core.wal.WalWriter`、`sw_core.session_manager.SessionManager`（皆既有）；T02 產生的 `sw_core.wal_fs.PosixWalFs`（**延遲 import**：只在替身方法內 import，使本模組在 Stage A 可被 import）。
- Produces（Python，產品純型別；T05／T06 直接沿用，不得重複定義）：
  ```python
  # sw_core/serial_port.py
  @dataclass(frozen=True)
  class TxResult:
      outcome: Literal["not_submitted", "completed", "unknown"]
      confirmed: int            # OS 已確認寫出的 bytes（unknown 時為已確認前綴 k，未知尾段＝requested-confirmed）
      requested: int
      error: str | None = None
      @property
      def complete(self) -> bool: return self.outcome == "completed" and self.confirmed == self.requested
  # sw_core/uart_io.py
  class TxIncompleteError(OSError):                      # OSError 子類：既有 `except OSError` 寫入失敗路徑語意不變
      def __init__(self, result: TxResult) -> None       # self.result = result；errno＝EIO（cancelled 為 ECANCELED、draining 為 EAGAIN）；
                                                         # str = f"tx incomplete: {result.outcome} {result.confirmed}/{result.requested} {result.error}"
  class TxUnknownError(TxIncompleteError): ...            # 結果不明（pyserial write timeout／SerialException）；errno 沿用原例外（timeout→ETIMEDOUT）
  class SerialNotReady(TxIncompleteError, RuntimeError): ...   # 「serial not ready」：同時為 RuntimeError，與現況 RuntimeError("serial not ready") 相容
  ```
- Produces（測試替身）：
  ```python
  class ScriptedWrite:
      """取代 os.write：依序消費 actions；int=回傳寫出 bytes 數，BaseException=raise，callable(data)->int。"""
      def __init__(self, actions: list[int | BaseException | Callable[[bytes], int]]) -> None
      calls: list[bytes]
      def __call__(self, fd: int, data: bytes | memoryview) -> int
  class FakePort:
      """SerialPort 介面替身（無 fileno → 走 UARTBridge 的 port.write 路徑）。"""
      def __init__(self, write_results: list[Any] | None = None, *, block: threading.Event | None = None) -> None
      writes: list[bytes]; close_calls: int; opened: bool
      def open(self) -> None; def configure(self, profile: Any) -> None
      def read(self, max_bytes: int = 8192) -> bytes; def write(self, payload: bytes, *, cancel: threading.Event | None = None) -> Any
      def close(self) -> None; def set_baud(self, baud: int) -> None; def is_alive(self) -> bool; def fileno(self) -> int | None
  def make_bridge(tmp_path: Path, *, port: FakePort | None = None, com: str = "COM0", wal: WalWriter | None = None) -> tuple[UARTBridge, WalWriter]
  def wal_records(wal: WalWriter) -> list[dict[str, Any]]
  def make_manager(tmp_path: Path, profiles: list[SessionProfile], *, wal: WalWriter | None = None) -> SessionManager
  def make_profile(name: str = "p", com: str = "COM0", by_id: str = "/dev/serial/by-id/X", *, platform: str = "prpl") -> SessionProfile
  ```
  `FakePort.write` 依 `write_results` 依序回傳：`TxResult` 物件原樣回傳；`BaseException` 則 raise；未提供時回 `TxResult("completed", len(payload), len(payload))`；`block` 有值時在寫入前 `block.wait()`。
- Rust 對應：arch §9.1「TxWriter 以 FakeSerial（可注入部分寫入、Unknown、阻塞、取消失敗）」、「WalWriter 以 FakeWalFs」——Python 替身行為為其參考模型。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_fakes.py`
  ```python
  from __future__ import annotations
  import threading
  import pytest
  from stage0_fakes import ScriptedWrite, FakePort, make_bridge, wal_records

  def test_scripted_write_consumes_actions_in_order():
      w = ScriptedWrite([2, OSError(5, "EIO")])
      assert w(3, b"abcd") == 2
      with pytest.raises(OSError):
          w(3, b"cd")
      assert w.calls == [b"abcd", b"cd"]

  def test_fake_port_blocks_until_event_set():
      gate = threading.Event()
      port = FakePort(block=gate)
      done = threading.Event()
      t = threading.Thread(target=lambda: (port.write(b"x"), done.set()))
      t.start()
      assert not done.wait(0.1)
      gate.set()
      assert done.wait(1.0)
      assert port.writes == [b"x"]

  def test_make_bridge_uses_fake_port_and_isolated_wal(tmp_path):
      bridge, wal = make_bridge(tmp_path, port=FakePort())
      assert wal.wal_path.startswith(str(tmp_path))
      assert wal_records(wal) == []

  @pytest.mark.windows_native
  def test_windows_native_marker_only_runs_on_nt():
      import os
      assert os.name == "nt"

  def test_tx_result_complete_property():
      from sw_core.serial_port import TxResult
      assert TxResult("completed", 3, 3).complete is True
      assert TxResult("completed", 2, 3, "short_write").complete is False
      assert TxResult("unknown", 3, 3).complete is False

  def test_tx_incomplete_error_family_is_oserror_and_runtimeerror_compatible():
      import errno
      from sw_core.serial_port import TxResult
      from sw_core.uart_io import SerialNotReady, TxIncompleteError, TxUnknownError
      r = TxResult("not_submitted", 0, 1, "serial not ready")
      assert isinstance(SerialNotReady(r), RuntimeError) and isinstance(SerialNotReady(r), OSError)
      e = TxUnknownError(TxResult("unknown", 2, 5, "SerialTimeoutException: x"), errno.ETIMEDOUT)
      assert isinstance(e, TxIncompleteError) and e.errno == errno.ETIMEDOUT and e.result.confirmed == 2

  def test_junit_summary_records_exit_and_duplicates(tmp_path):   # R5-F3：summary 必帶 rc／finished／duplicates
      import importlib.util, pathlib
      p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "stage0" / "junit_summary.py"
      spec = importlib.util.spec_from_file_location("junit_summary", p); js = importlib.util.module_from_spec(spec); spec.loader.exec_module(js)
      x = tmp_path / "j.xml"
      x.write_text('<testsuites><testsuite tests="3" failures="0" errors="0" skipped="1">'
                   '<testcase classname="tests.test_a" name="t1"/><testcase classname="tests.test_a" name="t1"/>'
                   '<testcase classname="tests.test_a" name="t2"><skipped message="x"/></testcase></testsuite></testsuites>', encoding="utf-8")
      s = js.summarize(x, pytest_exit=0, require_native=False)
      assert s["rc"] == 0 and s["pytest_exit"] == 0 and s["finished"] is True
      assert s["duplicates"] == ["tests/test_a.py::t1"] and [k["nodeid"] for k in s["skipped"]] == ["tests/test_a.py::t2"]
      assert js.summarize(x, pytest_exit=2, require_native=False)["rc"] == 2
      assert js.summarize(tmp_path / "missing.xml", pytest_exit=0, require_native=False)["finished"] is False
  ```
  （`test_junit_summary_records_exit_and_duplicates` 標 `core_portable`＋`memory_only`；`test_windows_native_marker_only_runs_on_nt` 標 `windows_native`＋`memory_only`；其餘 test 標 `core_portable`＋`memory_only`，`test_fake_port_blocks_until_event_set` 不開 socket／子行程，資源仍為 `memory_only`。依 Global Constraints，`stage0_fakes`／`sw_core` 新名稱一律於函式內載入。）
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_fakes.py` → 預期各 test 在函式內 `_load()` 時 `ModuleNotFoundError: No module named 'stage0_fakes'`／`ImportError: cannot import name 'TxResult'`（collection 成功）。
- [ ] **B1 實作**：
  - `sw_core/serial_port.py`／`sw_core/uart_io.py`：只新增上述純型別（`TxUnknownError(result, errno_value=errno.EIO)`）；本 Task 不改任何既有函式行為。
  - `tests/stage0_fakes.py` 依 Interfaces 實作；`make_bridge` 建 `WalWriter(wal_dir=str(tmp_path/"wal"))` 與 `UARTBridge(com, device_path="fake", profile=UartProfile(), wal=wal)`，有 `port` 時直接設 `bridge._serial = port; bridge._serial_fd = None`（不呼叫 `start()`）；`make_manager` 比照 `tests/test_device_handoff.py::_Base._mgr`，以 `SessionManager(profiles, wal or WalWriter(wal_dir=str(tmp_path/"wal")), on_ready=lambda _s: None, on_detached=lambda _s: None, state_path=str(tmp_path/"state.json"))` 建立（`state_path` 為既有建構參數，盤點 04 §2.1），不改模組層 `STATE_PATH`。
  - `tests/conftest.py`：**不新增 `pytest_configure`、不註冊 marker**（P0 T14 唯一定義處）。在 `tests/conftest.py` 的 `pytest_collection_modifyitems` 內補下列 skip（marker 來自檔內 `pytestmark`，P0 T14）；該 hook 尚未存在時由本 Task 建立唯一定義，P2 Task 13 之後併入同一定義，不另建第二個：
    ```python
    for item in items:
        if "windows_native" in item.keywords and os.name != "nt":
            item.add_marker(pytest.mark.skip(reason="windows_native：請以 scripts/stage0/win-pytest.sh 執行（證據不足，非 PASS）"))
        if "posix_only" in item.keywords and os.name == "nt":
            item.add_marker(pytest.mark.skip(reason="posix_only：Windows 不適用"))
        if "windows_com" in item.keywords and not os.environ.get("SW_TEST_COM_PAIR"):
            item.add_marker(pytest.mark.skip(reason="BLOCKED-OWNER：需 OD-08 COM pair（SW_TEST_COM_PAIR；證據不足，非 PASS）"))
    ```
    pop 清單加入三個新 env。
  - `scripts/stage0/win-pytest.sh <pytest args…>`（`set -euo pipefail`）：
    1. `repo=$(git rev-parse --show-toplevel)`；`run_id="$(date +%y%m%d-%H%M%S)-$$"`。
    2. `lad_win=$(cmd.exe /c 'echo %LOCALAPPDATA%' 2>/dev/null | tr -d '\r')`；`stage="$(wslpath -u "$lad_win")/Temp/sw-stage0/$run_id/repo"`；`mkdir -p "$stage"`。
    3. 複製工作樹：`git -C "$repo" ls-files -z --cached --others --exclude-standard --deduplicate`，逐項略過工作樹中已不存在的路徑（已刪除但仍在 index 者），其餘 `cp --parents -t "$stage"`（工作目錄必須在 Windows 本機磁碟，避免 9P 改變分享與鎖語意，arch §9.4）。
    4. `cd "$stage"`；先 `cmd.exe /c py -3.12 -c "import pytest, yaml, serial, jsonschema"`，失敗時 stderr 印 `win-pytest: Windows Python 缺 pytest/PyYAML/pyserial/jsonschema` 並 `exit 3`。
    5. Collection 前排除不適用檔案：以 AST（不 import）讀各 `tests/**/test_*.py` 的**模組層** `pytestmark`，平台 marker 為 `posix_only` 者逐一加 `--ignore=tests/<file>`（避免頂層 `import pty`／`termios`／`fcntl` 在 Windows collection 失敗；規則與 P2 03 Task 13 `resource_report.should_ignore_on_windows` 相同，P2 合併後改呼叫該函式）；AST 解析失敗時 exit 4 並印 `win-pytest: 無法解析 <file> 的 pytestmark`。
    6. 轉送 env：`SW_TEST_COM_PAIR`、`SW_WIN_*` 以外由呼叫端指定的 `SW_TEST_*` 變數，經 `WSLENV` 追加（`export WSLENV="${WSLENV:+$WSLENV:}SW_TEST_COM_PAIR"` 等）傳給 Windows 行程。
    7. 執行：`cmd.exe /c py -3.12 -m pytest -q -rs -p no:cacheprovider --junitxml=<stage>/junit.xml "$@"`，以背景行程啟動並記錄 Windows PID（`cmd.exe /c start /b` 不可用時以 `powershell.exe -NoProfile -Command "Start-Process -PassThru …"` 取得 `python.exe` PID）；逾時 `${SW_WIN_TIMEOUT:-900}` 秒時以 `taskkill.exe /PID <pid> /T /F` 終止整棵行程樹（`timeout` 只殺得到 WSL 端的 `cmd.exe`，不足以終止 `python.exe`），rc 記 124。
    8. 以 `scripts/stage0/junit_summary.py`（stdlib、可在 Linux 單測；`summarize(junit, *, pytest_exit, require_native) -> dict`）解析 `junit.xml` 產生 `<stage>/../summary.json`：`{"collected":[nodeid…],"passed":[…],"failed":[…],"skipped":[{"nodeid","reason"}…]}`；**呼叫端帶 `-m windows_native` 時，若有任何 `windows_native` item 被 skip，rc 改為 5**（被 skip 的原生測試不得計為 PASS）；印出 summary 路徑。summary 另記 `"rc"`（最終 exit code）、`"pytest_exit"`（pytest 原始 exitstatus）、`"finished"`（junit 根元素存在且 pytest 行程正常結束）與 `"duplicates"`（`collected` 中重複的 nodeid）；並對 Windows 端 pytest 一律設 `SW_RESOURCE_REPORT=<stage>\resource-report.json`（P2 Task 13 交付後生效；P2 前為 no-op），把該檔一併複製回 `<stage>/../`（R5-F3：Stage 0 L1 出口由 P2 `l1_judge` 以此報告對凍結的 `s0_bc`／`s0_win_pytest` nodeid 清單判定；`summary.json` 的 `passed` 非空不等於出口 PASS）。
    9. `SW_WIN_KEEP=1` 時保留 staging，否則 `rm -rf` 該 run 目錄但保留 `summary.json`／`junit.xml`；結束印 `win-pytest: staging=<path> exit=<rc>`。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_fakes.py`（Linux：6 passed、1 skipped）；`scripts/stage0/win-pytest.sh tests/test_stage0_fakes.py -m windows_native`（Windows：1 passed、rc 0）；`python3 -m pytest -q tests/test_conftest_isolation.py tests/test_liveguard.py tests/test_resource_markers.py`；全套 `python3 -m pytest -q tests/`。
- [ ] **B3 commit**：`test(stage0): 新增行為變更測試替身、平台 marker 與 Windows 原生 pytest runner`

---

## Task T02：BC-01a WAL 常駐 handle、每筆 flush、唯一 close、DF-17

- **範圍**：BC-01（常駐開檔、每筆 flush、Python 實作須驗證 handle 所有權與唯一 close）；DF-17（WAL 與 mirror 失敗只重試失敗的那個檔）；SC-11 的 WAL 端（`WalWriter.close()` flush＋sync 後關閉）。依據：規格 §8.1「Flush 與持久化」、§8.2「常駐開檔」；盤點 04 §1.4；arch §4.5、§5.8。
- **Test author model**：haiku-xhigh　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-bc01-wal-persistent-handle.md`（`type: perf`，`scope: wal`）：WAL 與 mirror 改為常駐 handle、每筆 flush，關機時 sync 後關閉；mirror 寫入失敗不再產生重複 seq 的 WAL 行（DF-17）。
- **BLOCKED-OWNER**：無（效能證據見 T19）。

**Files**
- Create: `sw_core/wal_fs.py`、`tests/test_stage0_wal_handle.py`
- Modify: `sw_core/wal.py`、`sw_core/constants.py`（新增 `WAL_IDENTITY_CHECK_INTERVAL_S = 0.05`、`WAL_RECREATE_BACKOFF_S = (0.01, 0.05, 0.2)`、`WAL_RECREATE_RETRY_INTERVALS_S = (1.0, 2.0, 5.0)`（之後封頂 5.0）、`DRAIN_TIMEOUT_MS = 3000`）、`contracts/rpc_schema.json`（`health.status` 的 `wal` 子物件新增 `unhealthy_reason`，見規格第 7 點）、`contracts/behavior_changes.yaml`（DF-17 狀態推進）、`contracts/daemon_logging.yaml`（同步 WAL logger 呼叫點）、`docs/rust-refactor/plans/rust-architecture.md`（回填 §3.1 常數表及 §5.8 尾行語意）

> `contracts/daemon_logging.yaml` 的 WAL `source` 行號須對齊 AST logger 呼叫位置；T04／T05 後續修改 `sw_core/wal.py` 時同步重校。

**Interfaces**
- Consumes：`sw_core.util.dumps_stable`、`to_printable`、`now_iso`、`monotonic_ns`（既有）。
- Produces（Python）：
  ```python
  # sw_core/wal_fs.py
  class PartialWriteError(OSError):
      sent: int                                             # 失敗前 OS 已確認寫出的 bytes（0 ≤ sent < len(data)）
  class WalFile(Protocol):
      def write_all_flush(self, data: bytes) -> None: ...   # 一次 write（可分多個 syscall）＋user space 不留緩衝；
                                                            # 中途失敗一律 raise PartialWriteError(sent=…)（errno 沿用原例外）
      def sync(self) -> None: ...                           # os.fsync ｜ FlushFileBuffers
      def identity(self) -> tuple[int, int]: ...            # (st_dev, st_ino) ｜ (VolumeSerialNumber, FileId128)
      def unlinked(self) -> bool: ...                       # POSIX st_nlink==0 ｜ Windows DeletePending
      def close(self) -> None: ...                          # 冪等；第二次呼叫為 no-op
  class WalFs(Protocol):
      def open_append(self, path: str) -> WalFile: ...
      def path_identity(self, path: str) -> tuple[int, int] | None: ...   # None＝不存在
      def size(self, path: str) -> int | None: ...
      def rename_noreplace_or_suffix(self, src: str, base_dst: str) -> str: ...  # DF-03（T04）
      def sync_dir(self, path: str) -> None: ...
      def ensure_dir(self, path: str) -> None: ...
      def dir_writable(self, path: str) -> bool: ...                     # arch §4.5 WalFs::dir_writable；health 診斷用
  class PosixWalFs: ...      # open(O_WRONLY|O_APPEND|O_CREAT|O_CLOEXEC, 0o666) → os.fdopen(fd, "ab", buffering=0)
  def default_wal_fs() -> WalFs   # os.name=="nt" → WindowsWalFs()（T03）；否則 PosixWalFs()

  # sw_core/wal.py
  class WalWriter:
      def __init__(self, wal_dir: str | None = None, rotate_bytes: int = DEFAULT_WAL_ROTATE_BYTES, *,
                   fs: WalFs | None = None, clock: Callable[[], float] | None = None) -> None
                   # 不提供 sleep 注入：WalWriter 任何路徑都不得持 _lock 睡眠（重建退避以 clock 判定，見 T05）
      def append(self, *, com: str, direction: str, source: str, payload: bytes,
                 cmd_id: str | None = None, loss_flag: bool = False,
                 meta: dict[str, Any] | None = None) -> dict[str, Any]          # 簽章不變
      def close(self) -> None                                                    # 新增；冪等
      @property
      def closed(self) -> bool
  ```
- Rust 對應：`sw-pal` `WalFs`／`WalFile`（arch §4.5）、`sw-core` `WalWriter::append` 步驟 1～6 與 shutdown（arch §5.8）。Python 的 `tuple[int, int]` 對應 Rust `FileIdentity(u128, u128)`。

**行為規格（實作者必須逐條滿足）**
1. handle **延遲開啟**：第一次 append 才開 WAL 與 mirror（維持現況「無流量時不產生檔案」的可觀測行為，`health.wal_file_exists` 不變）。
2. 每筆 append：鎖外算 b64／crc；鎖內 `rotate_if_needed`（T04）→ 名稱消失檢查（第 4 點）→ `identity_check`（T05）→ `seq += 1` → 序列化 → `wal.write_all_flush(line_bytes)` → `mirror.write_all_flush(to_printable(payload).encode("utf-8"))`。檔案以 binary unbuffered 開啟；`line_bytes = (dumps_stable(record) + "\n").encode("utf-8")`。POSIX 上與現況逐位元組相同；**Windows 上現況為文字模式 `open(path, "a", encoding="utf-8")`，WAL 行尾與 mirror 中的換行實際寫成 `\r\n`，新格式只寫 `\n`——此為 BC-01 附帶的位元組差異**，於 `contracts/behavior_changes.yaml` BC-01 的說明欄註明，並以 `windows_native` 測試釘死 WAL 新格式；讀取端（`_iter_matching`／`tail_*`、T17 oracle、Rust reader）必須同時接受舊 WAL 的 `\r\n` 行尾（`json.loads` 前 `rstrip("\r\n")`）。
3. DF-17 與部分寫入：WAL、mirror 各自獨立重試，WAL 行任何情況下同一 seq 最多出現一次，且不得產生被串接的 NDJSON 行。以 `PartialWriteError.sent` 判定進度：
   - `sent == 0`（含 `write_all_flush` 前開檔失敗；非 `PartialWriteError` 的一般 `OSError` 一律視為 `sent == 0`，測試替身直接擲 `OSError` 即走此路）：關閉該 handle、`ensure_dir`、重開、重寫**整行**一次。
   - `0 < sent < len(data)`：**不得重寫整行**；在同一 handle 上只補寫剩餘的 `data[sent:]` 一次（POSIX `O_APPEND`／Windows `FILE_APPEND_DATA` 且唯一 writer 持鎖，故補寫緊接前綴）；補寫仍失敗 → 設 `_dirty_tail[which]=True`（下一次對該檔寫入前先寫一個 `b"\n"` 終結殘行，殘行成為一筆會被讀取端判為無效的行，不得與下一筆串接），本筆視為寫入失敗。
   - `write_all_flush` 已回傳（全部寫出）後任何後續步驟的例外都不得觸發重寫。
   - WAL 重試仍失敗：回傳 record `loss_flag=True`、`write_failures += 1`、`last_write_error` 記重試例外；若例外由 `PartialWriteError` 包裝，使用其底層 `OSError` 格式化（`f"{type(os_error).__name__}: {os_error}"`），保留既有健康欄位文字；mirror 重試仍失敗：`write_failures += 1`、`last_write_error` 更新，回傳 record 的 `loss_flag` **維持** `False`（權威紀錄 WAL 已寫入）。既有 warning log 文字保留（盤點 04 §1.4 第 2、3 點）。
4. 名稱消失（**不受** T05 的 50 ms 節流）：每筆 append 在寫入前檢查 `not os.path.isdir(wal_dir)` 或 rotate 已取得的 `fs.size(path) is None`（名稱檢查重用 rotate 的 size 結果，不增加 `size` syscall 次數）；命中時關閉該 handle、`ensure_dir`、重開（`_recreated_count += 1`，既有 warning 保留）——常駐 fd 對已刪 inode 寫入不會報錯，若不先檢查名稱，`rmtree` 後的紀錄會寫進虛空（`tests/test_wal_missing_detection.py` 既有三個自癒測試依賴此行為）。META 補寫與 identity／DeletePending 比對由 T05 負責；T05 的節流只套用在後兩者。
5. `close()`：持鎖；WAL、mirror 各自 `sync()` 後 `close()`；設 `closed=True`。之後的 append 不重開檔：回傳 record `loss_flag=True`、`write_failures += 1`、`last_write_error="WalClosed: writer closed"`，seq 照常遞增。
6. 任一 handle 開啟成功、另一個失敗時，已開啟者保留供下次使用，不得洩漏或重複開啟；例外路徑中只有持有者負責 close。
7. 權威 WAL 不可寫即不健康（規格 §8.2「記錄不可用時」、§1.3）：WAL（非 mirror）在第 3 點重試後仍失敗，或首次開檔失敗（含 ENOSPC、EIO、EACCES、`WalClosed` 以外）→ `unhealthy=True`、`unhealthy_reason="write_failed"`；下一筆 WAL 成功寫入即清除（`unhealthy=False`、`unhealthy_reason=None`）。mirror-only 失敗不影響 `unhealthy`。`health()` 在既有 10 鍵之後新增 `"unhealthy_reason"`（`None`｜`"write_failed"`｜`"recreate_failed"`（T05）），`healthy = 既有公式 and not self.unhealthy`；`unhealthy` property 在本 Task 建立，T05 擴充 `recreate_failed`。T05 的「停止自動 TX」對兩種 reason 一視同仁。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_wal_handle.py`
  ```python
  from __future__ import annotations
  import json
  import os
  import pytest
  from sw_core.wal import WalWriter
  from sw_core.wal_fs import PosixWalFs

  class CountingFs(PosixWalFs):
      def __init__(self) -> None:
          super().__init__()
          self.opens: list[str] = []
          self.closed: list[str] = []
          self.fail_write_once: set[str] = set()
      def open_append(self, path: str):
          self.opens.append(path)
          inner = super().open_append(path)
          fs = self
          class Wrapped:
              def write_all_flush(self, data: bytes) -> None:
                  if path in fs.fail_write_once:
                      fs.fail_write_once.discard(path)
                      raise OSError(28, "No space left on device")
                  inner.write_all_flush(data)
              def sync(self) -> None: inner.sync()
              def identity(self): return inner.identity()
              def unlinked(self) -> bool: return inner.unlinked()
              def close(self) -> None:
                  fs.closed.append(path); inner.close()
          return Wrapped()

  def _lines(path: str) -> list[dict]:
      with open(path, encoding="utf-8") as fp:
          return [json.loads(l) for l in fp if l.strip()]

  def test_handles_opened_lazily_once_and_reused(tmp_path):
      fs = CountingFs()
      w = WalWriter(wal_dir=str(tmp_path), fs=fs)
      assert fs.opens == []
      assert not os.path.exists(w.wal_path)
      for i in range(100):
          w.append(com="COM0", direction="RX", source="device", payload=b"x%d" % i)
      assert fs.opens == [w.wal_path, w.mirror_path]

  def test_each_record_visible_to_reader_immediately(tmp_path):
      w = WalWriter(wal_dir=str(tmp_path), fs=CountingFs())
      rec = w.append(com="COM0", direction="TX", source="agent:x", payload=b"ls\n", cmd_id="c1")
      assert _lines(w.wal_path)[-1]["seq"] == rec["seq"]
      with open(w.mirror_path, "rb") as fp:
          assert fp.read() == b"ls\n"

  def test_line_bytes_identical_to_baseline_format(tmp_path):
      w = WalWriter(wal_dir=str(tmp_path), fs=CountingFs())
      rec = w.append(com="COM0", direction="META", source="system", payload=b"", meta={"z": 1, "a": "中"})
      raw = open(w.wal_path, "rb").read()
      expected = (json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
      assert raw == expected

  def test_mirror_failure_does_not_duplicate_wal_seq(tmp_path):
      fs = CountingFs()
      w = WalWriter(wal_dir=str(tmp_path), fs=fs)
      w.append(com="COM0", direction="RX", source="device", payload=b"a")
      fs.fail_write_once.add(w.mirror_path)
      rec = w.append(com="COM0", direction="RX", source="device", payload=b"b")
      seqs = [r["seq"] for r in _lines(w.wal_path)]
      assert seqs == [1, 2]
      assert rec["loss_flag"] is False
      assert w.health()["write_failures"] == 0  # 重試成功不計失敗
      assert open(w.mirror_path, "rb").read() == b"ab"

  def test_wal_failure_retried_only_on_wal(tmp_path):
      fs = CountingFs()
      w = WalWriter(wal_dir=str(tmp_path), fs=fs)
      w.append(com="COM0", direction="RX", source="device", payload=b"a")
      fs.fail_write_once.add(w.wal_path)
      w.append(com="COM0", direction="RX", source="device", payload=b"b")
      assert [r["seq"] for r in _lines(w.wal_path)] == [1, 2]
      assert open(w.mirror_path, "rb").read() == b"ab"
      assert fs.opens.count(w.mirror_path) == 1

  def test_close_is_idempotent_and_syncs(tmp_path, monkeypatch):
      fs = CountingFs()
      synced: list[int] = []
      monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
      w = WalWriter(wal_dir=str(tmp_path), fs=fs)
      w.append(com="COM0", direction="RX", source="device", payload=b"a")
      w.close(); w.close()
      assert sorted(fs.closed) == sorted([w.wal_path, w.mirror_path])
      assert len(synced) >= 2
      assert w.closed is True

  def test_append_after_close_is_recorded_as_loss_without_reopen(tmp_path):
      fs = CountingFs()
      w = WalWriter(wal_dir=str(tmp_path), fs=fs)
      w.append(com="COM0", direction="RX", source="device", payload=b"a")
      w.close()
      rec = w.append(com="COM0", direction="RX", source="device", payload=b"b")
      assert rec["loss_flag"] is True and rec["seq"] == 2
      assert w.health()["last_write_error"] == "WalClosed: writer closed"
      assert len(fs.opens) == 2
  ```
  另以表格補齊（同檔）：

  | 測試名 | 斷言 |
  |---|---|
  | `test_mirror_permanent_failure_keeps_wal_loss_flag_false` | mirror 兩次寫入都失敗 → WAL 行存在、回傳 `loss_flag is False`、`health()["write_failures"] == 1` |
  | `test_wal_permanent_failure_sets_loss_flag` | WAL 兩次都失敗 → 回傳 `loss_flag is True`、`write_failures == 1`、seq 仍被消耗（下一筆 seq 為 3） |
  | `test_dir_removed_still_self_heals` | `shutil.rmtree(tmp_path)` 後**立即**（不推進 clock）append → 檔案重建、紀錄落在新檔、`health()["recreated_count"] == 1`（第 4 點不受節流） |
  | `test_file_unlinked_name_check_unthrottled` | 只 `os.unlink(wal_path)` 後立即 append → 新檔存在且含該筆（META 由 T05 補，本 Task 不斷言 META） |
  | `test_empty_meta_first_append_creates_empty_mirror` | 首筆空 payload META 仍延遲建立空 mirror 檔並持有 handle；不寫 mirror bytes，也不計為失敗 |
  | `test_restart_resumes_max_seq` | 寫 5 筆後新建 `WalWriter(wal_dir=…)` 的下一筆 seq 為 6（盤點 04 §1.7） |
  | `test_restart_terminates_unterminated_wal_tail_before_new_record` | 重啟時若 WAL 最後 byte 不是 LF，下一筆前先終結舊殘行；新紀錄仍為完整且可解析的獨立行 |
  | `test_append_reuses_rotation_size_checks_for_name_detection` | 每次 append 的 WAL／mirror `fs.size` 各呼叫一次；名稱檢查重用 rotate 探測結果 |
  | `test_partial_write_then_eio_completes_suffix_only` | WAL 第一次 `os.write` 只寫前 10 bytes 後擲 `OSError(EIO)`、補寫成功 → 檔案中該筆為完整單行、JSON 可解析、全檔 seq 唯一 |
  | `test_partial_write_suffix_failure_terminates_tail` | 前綴寫出後補寫也失敗 → 回傳 `loss_flag is True`；下一筆 append 前先寫 `\n`：檔案中殘行獨立成一行（`json.loads` 失敗）、下一筆為完整獨立行、seq 不重複 |
  | `test_full_write_then_exception_not_rewritten` | 包裝 `write_all_flush` 在委派完整寫出後才擲例外 → 該 seq 只出現一次 |
  | `test_partial_write_error_preserves_underlying_os_message` | POSIX 包裝的 `PartialWriteError` 保留底層 `OSError` 的 errno／訊息與 cause |
  | `test_health_last_write_error_uses_underlying_oserror` | `health.last_write_error` 對部分寫入錯誤仍使用基線 `OSError: [Errno …] …` 格式 |
  | `test_wal_persistent_failure_marks_unhealthy_write_failed` | WAL 兩次寫入都 `OSError(ENOSPC)` → `health()["healthy"] is False`、`unhealthy_reason == "write_failed"`、`w.unhealthy is True`；下一筆成功 → 恢復 `None`／`True` |
  | `test_first_open_failure_marks_unhealthy` | `open_append` 擲 `PermissionError` → `unhealthy_reason == "write_failed"` |
  | `test_mirror_only_failure_keeps_healthy` | 只有 mirror 永久失敗 → `unhealthy is False` |
  | `test_native_line_ending_is_lf_only`（`windows_native`） | Windows 上寫一筆後原始 bytes 以 `b"}\n"` 結尾且不含 `b"\r\n"`；預先放入 `\r\n` 行尾的舊檔可被 `tail_raw` 正常讀出 |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_wal_handle.py` → 預期 `ModuleNotFoundError: No module named 'sw_core.wal_fs'`。commit：`test(wal): BC-01 常駐 handle 與 DF-17 失敗測試`
- [ ] **B1 實作**：
  - `PosixWalFs.open_append`：`fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o666)`；`fp = os.fdopen(fd, "ab", buffering=0)`；`fdopen` 失敗時 `os.close(fd)` 再 raise。`write_all_flush`：`view = memoryview(data)`；迴圈 `n = fp.write(view[sent:])`，`n is None or n <= 0` → `PartialWriteError(errno.EIO, "short write", sent=sent)`；`OSError` → 以 `e.strerror or str(e)` 建立 `PartialWriteError(e.errno, ..., sent=sent)`（`from e`）。`identity`：`st = os.fstat(fp.fileno()); return (st.st_dev, st.st_ino)`。`unlinked`：`os.fstat(...).st_nlink == 0`。`close`：`if not self._closed: self._closed = True; fp.close()`。`path_identity`：`os.stat` → `(st_dev, st_ino)`，`FileNotFoundError` → `None`。`sync_dir` 的 POSIX／Windows 分支只留在 `PosixWalFs` seam 內；T02 的 `rename_noreplace_or_suffix` 不實作，交由 T04 定義並驗證。
  - `WalWriter`：新增 `_fs`、`_wal_fp: WalFile | None`、`_mirror_fp: WalFile | None`、`_closed`、`_dirty_tail: dict[str, bool]`、`_unhealthy`、`_unhealthy_reason`、`_clock = clock or time.monotonic`；`_write_record_locked(record, payload)` 改為：
    ```python
    line = (dumps_stable(record) + "\n").encode("utf-8")
    mirror = to_printable(payload).encode("utf-8")
    wal_err = self._write_one_locked("wal", line)      # 依 PartialWriteError.sent 分流：sent==0→close→ensure_dir→reopen→整行重寫一次；sent>0→只補寫 data[sent:]，再失敗設 _dirty_tail
    if mirror:
        mirror_err = self._write_one_locked("mirror", mirror)
    else:
        try:
            self._open_handle_locked("mirror")  # 首筆空 META 仍建立空 mirror 檔，不寫入 bytes
        except OSError:
            pass
        mirror_err = None
    ```
    `_rotate_if_needed(sizes: dict[str, int | None] | None = None) -> bool` 保留既有 bool 回傳；`append` 傳入 size snapshot dict，供名稱消失檢查重用 rotate 已取得的結果。
    `_write_one_locked(which, data) -> BaseException | None`：`_dirty_tail[which]` 為真時先寫 `b"\n"`（成功才清旗標；`\n` 寫入失敗視同本筆 `sent==0`，依第 3 點重開後重寫 `\n`＋整行一次，**不得**在殘行未終結時直接寫入該行——與 Rust arch §5.8 第 5 點「`b"\n" ++ data` 合併計算 sent」位元組結果相同，R2-F3）；handle 為 None 時先 `open_append`；依規格第 3 點以 `PartialWriteError.sent` 分流（`sent==0`→關閉、`ensure_dir`、重開、重寫整行一次；`sent>0`→同 handle 補寫 `data[sent:]` 一次，失敗設 `_dirty_tail`）；成功且第一次例外為 `FileNotFoundError`（目錄消失）時沿用既有 `_recreated_count += 1` 與 warning；仍失敗回傳例外。`append` 依規格第 3、7 點設定 `loss_flag`、`write_failures`、`last_write_error`、`unhealthy`。空 payload 的 mirror 不寫 bytes；首次 append 仍 best-effort 開啟 mirror，以保留基線建立空 mirror 檔的行為，開啟失敗不計失敗且不影響 WAL。第 4 點名稱檢查置於 `_rotate_if_needed` 之後、寫入之前。
  - `_iter_matching`／`tail_*` 讀取時對每行先 `rstrip("\r\n")`（接受 Windows 舊檔 `\r\n`）；無法解析的行沿用現況處理（略過）。
  - `contracts/rpc_schema.json` 的 `health.status` 結果 schema：`wal` 子物件新增 `unhealthy_reason`（`null`｜`"write_failed"`｜`"recreate_failed"`），遞增 `x-catalog-revision`；P0 的 rpc schema 漂移測試須通過。
  - `close()`、`closed` 依規格第 5 點。
  - `_load_last_seq` 啟動時掃描有效 seq，掃描完即關、不持有 handle；另檢查非空 WAL 最後 byte，若不是 LF 則設定 WAL `dirty_tail`，避免重啟後把新紀錄串接到殘行。
- [ ] **B2 執行與證據狀態**：Linux 執行 `python3 -m pytest -q tests/test_stage0_wal_handle.py tests/test_wal.py tests/test_wal_integrity.py tests/test_wal_missing_detection.py tests/test_log_tail_latest.py tests/test_contracts_rpc_schema.py`（`test_wal_missing_detection.py` 三個自癒測試必須不改即通過）及全套依 §0 第 4 點。原生 Windows `scripts/stage0/win-pytest.sh tests/test_stage0_wal_handle.py` 在 T03 的 `WindowsWalFs`／share-delete handle 落地前預期有 delete-sharing 失敗，記為「待 T03 重跑／證據不足」，不得列 PASS；T03 後須原生重跑該檔與其計畫指定的 WAL 核心測試。
- **測試資產變更**：無。
- [ ] **B3 commit**：`perf(wal): BC-01 改為常駐 handle 與每筆 flush，mirror 失敗不重複 WAL seq`

---

## Task T03：BC-01b Windows `CreateFileW` share-delete handle 與 `win32_files`

- **範圍**：BC-01 Windows 部分：WAL／mirror handle 帶 `FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE`，`open_osfhandle` 所有權轉移、失敗清理、唯一 close；`DeletePending` 與 FileId 查詢。另提供 SC-10 所需 `LockFileEx` 包裝（由 T15 使用）。依據：規格 §8.2「常駐開檔」第二段、§6.2 Tier 1；arch §4.4、§4.5。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-bc01-windows-share-delete.md`（`type: fix`，`scope: wal`）：Windows WAL 以 `CreateFileW` 取得含 share-delete 的常駐 handle，外部改名或刪除不再被 daemon 阻擋。
- **BLOCKED-OWNER**：端點防毒（代表機台為第三方企業 EDR，OD-05）對常駐 handle 與 rename 的攔截紀錄查詢需擁有者權限；本 Task 只能提供「本機實跑通過」證據，EDR 攔截數據記為證據不足。

**Files**
- Create: `sw_core/win32_files.py`、`tests/test_stage0_wal_winfs.py`
- Modify: `sw_core/wal_fs.py`（新增 `WindowsWalFs`）

**Interfaces**
- Consumes：T02 的 `WalFile`／`WalFs` Protocol。
- Produces（Python）：
  ```python
  # sw_core/win32_files.py（模組在 POSIX 可 import；呼叫時才載入 kernel32／msvcrt）
  GENERIC_WRITE = 0x40000000; FILE_APPEND_DATA = 0x0004; SYNCHRONIZE = 0x00100000
  FILE_SHARE_ALL = 0x7; OPEN_ALWAYS = 4; OPEN_EXISTING = 3
  FILE_ATTRIBUTE_NORMAL = 0x80; FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
  LOCKFILE_EXCLUSIVE_LOCK = 0x2; LOCKFILE_FAIL_IMMEDIATELY = 0x1
  @dataclass
  class WinApi:
      CreateFileW: Callable[..., int]; CloseHandle: Callable[[int], int]
      GetFileInformationByHandleEx: Callable[..., int]; LockFileEx: Callable[..., int]; UnlockFileEx: Callable[..., int]
      SetFileInformationByHandle: Callable[..., int]; FlushFileBuffers: Callable[[int], int]
      GetLastError: Callable[[], int]
      open_osfhandle: Callable[[int, int], int]; get_osfhandle: Callable[[int], int]
  def load_api() -> WinApi                                   # 真實 ctypes.windll.kernel32＋msvcrt；非 nt → OSError(errno.ENOSYS)
  def open_shared_append(path: str, *, api: WinApi | None = None) -> io.FileIO   # CreateFileW(FILE_APPEND_DATA|SYNCHRONIZE, FILE_SHARE_ALL, OPEN_ALWAYS)
  def delete_pending(fd: int, *, api: WinApi | None = None) -> bool             # FileStandardInfo.DeletePending
  def file_identity(fd: int, *, api: WinApi | None = None) -> tuple[int, int]   # FileIdInfo：(VolumeSerialNumber, int.from_bytes(FileId, "little"))
  def path_identity(path: str, *, api: WinApi | None = None) -> tuple[int, int] | None
  def lock_file_ex(fd: int, *, exclusive: bool = True, fail_immediately: bool = True, api: WinApi | None = None) -> bool
  def unlock_file_ex(fd: int, *, api: WinApi | None = None) -> None
  def delete_classic(path: str, *, api: WinApi | None = None) -> None   # 測試輔助：以 DELETE 存取開檔後
      # SetFileInformationByHandle(FileDispositionInfo(4), {DeleteFile=TRUE})——明確走 classic delete-pending 語意
  def delete_posix_semantics(path: str, *, api: WinApi | None = None) -> None
      # SetFileInformationByHandle(FileDispositionInfoEx(21), DELETE|POSIX_SEMANTICS)

  # sw_core/wal_fs.py
  class WindowsWalFs(api: WinApi | None = None):  # 實作 WalFs；所有 Windows API 可注入；unlinked 僅用 delete_pending
  ```
- Rust 對應：arch §4.5 `WalFs::open_append` Windows 分支、`WalFile::identity`／`unlinked`；arch §4.4 Windows `LockFileEx(EXCLUSIVE|FAIL_IMMEDIATELY, offset 0, len 1)`。

**行為規格**
1. `open_shared_append`：`h = CreateFileW(path, FILE_APPEND_DATA|SYNCHRONIZE, FILE_SHARE_ALL, None, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)`；`INVALID_HANDLE_VALUE` → `OSError(winerror=GetLastError())`（以 `ctypes.WinError(code)` 產生，符合 G3 `str(OSError)` 格式）。`fd = open_osfhandle(h, os.O_APPEND | os.O_WRONLY | getattr(os, "O_BINARY", 0))`；此呼叫失敗時 `CloseHandle(h)` 一次再 raise；成功後 handle 所有權轉移給 fd，之後**只**透過 `io.FileIO(fd, "ab", closefd=True)` 關閉，不得再呼叫 `CloseHandle(h)`。`io.FileIO` 建構失敗時 `os.close(fd)` 一次。
2. `path_identity`：以 `CreateFileW(path, 0, FILE_SHARE_ALL, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)` 開查詢 handle，取 FileIdInfo 後 `CloseHandle`；`ERROR_FILE_NOT_FOUND`(2)／`ERROR_PATH_NOT_FOUND`(3) → `None`；`ERROR_ACCESS_DENIED`(5)（檔案處於 delete-pending）→ 回傳 `(0, 0)` 作為「名稱存在但不可開啟」哨兵，呼叫端視為 identity 不符。
3. `lock_file_ex`：對 `get_osfhandle(fd)` 呼叫 `LockFileEx(h, flags, 0, 1, 0, byref(OVERLAPPED()))`（offset 0、長度 1，與 `msvcrt.locking(fd, LK_NBLCK, 1)` 在 position 0 時鎖定同一區段，互斥相容）；`fail_immediately` 且 `GetLastError()==ERROR_LOCK_VIOLATION(33)` → 回 `False`；其他失敗 raise。
4. 刪除語意不得依賴 `os.remove`：Windows 10 1809 起 NTFS 上的 `DeleteFileW` 會優先嘗試 POSIX delete semantics（以 OD-05 代表機台實測確認並記入測試輸出），因此 classic delete-pending 情境一律以 `delete_classic()` 觸發；測試在斷言 `delete_pending` 前先確認名稱仍存在且 `path_identity == (0, 0)`（classic 語意已生效），否則 FAIL（不得 SKIP 或改判 PASS）。
5. `sync`：`WindowsWalFs` 的 `WalFile.sync()` 在 `FILE_APPEND_DATA|SYNCHRONIZE` handle 上走 `os.fsync`（CPython `_commit`→`FlushFileBuffers`），必須以 `windows_native` 測試證實不擲 `ERROR_ACCESS_DENIED`；`sync_dir` 在 Windows 為 no-op 並於模組 docstring 記錄「Windows 無目錄 fsync 對應操作，rename 的持久性依 NTFS journal」，以 `windows_native` 測試釘死其行為（規格 §8.1：POSIX 目錄同步與 Windows 對應操作須分別驗證）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_wal_winfs.py`
  ```python
  from __future__ import annotations
  import os
  import pytest
  from sw_core import win32_files as w32

  class FakeApi:
      def __init__(self, *, fail_osfhandle: bool = False) -> None:
          self.closed: list[int] = []
          self.fail_osfhandle = fail_osfhandle
          self.create_args: list[tuple] = []
      def CreateFileW(self, *a):
          self.create_args.append(a); return 1234
      def CloseHandle(self, h): self.closed.append(h); return 1
      def open_osfhandle(self, h, flags):
          if self.fail_osfhandle: raise OSError(24, "Too many open files")
          return os.open(os.devnull, os.O_WRONLY)
      def GetLastError(self): return 0
      def get_osfhandle(self, fd): return 1234
      def GetFileInformationByHandleEx(self, *a): return 1
      def LockFileEx(self, *a): return 1
      def UnlockFileEx(self, *a): return 1
      def SetFileInformationByHandle(self, *a): return 1
      def FlushFileBuffers(self, h): return 1

  def test_module_imports_on_posix_without_windll():
      assert hasattr(w32, "open_shared_append")

  def test_create_uses_share_all_and_append_access(tmp_path):
      api = FakeApi()
      fp = w32.open_shared_append(str(tmp_path / "w"), api=api)
      fp.close()
      _, access, share, _, disposition, _, _ = api.create_args[0]
      assert access == w32.FILE_APPEND_DATA | w32.SYNCHRONIZE
      assert share == w32.FILE_SHARE_ALL == 0x7
      assert disposition == w32.OPEN_ALWAYS

  def test_open_osfhandle_failure_closes_raw_handle_exactly_once(tmp_path):
      api = FakeApi(fail_osfhandle=True)
      with pytest.raises(OSError):
          w32.open_shared_append(str(tmp_path / "w"), api=api)
      assert api.closed == [1234]

  def test_success_path_never_calls_closehandle_directly(tmp_path):
      api = FakeApi()
      fp = w32.open_shared_append(str(tmp_path / "w"), api=api)
      fp.close()
      assert api.closed == []

  def test_load_api_on_posix_raises_enosys():
      if os.name == "nt":
          pytest.skip("POSIX only")
      with pytest.raises(OSError) as ei:
          w32.load_api()
      import errno
      assert ei.value.errno == errno.ENOSYS

  @pytest.mark.windows_native
  def test_native_handle_allows_rename_and_delete_while_open(tmp_path):
      p = str(tmp_path / "raw.wal.ndjson")
      fp = w32.open_shared_append(p)
      fp.write(b"x\n")
      os.replace(p, p + ".1")          # 缺 FILE_SHARE_DELETE 時 PermissionError
      w32.delete_classic(p + ".1")     # 明確 classic 語意（os.remove 在新版 Windows 可能走 POSIX semantics）
      assert os.path.exists(p + ".1") and w32.path_identity(p + ".1") == (0, 0)   # classic 已生效，否則 FAIL
      assert w32.delete_pending(fp.fileno()) is True
      fp.close()
      assert not os.path.exists(p + ".1")

  @pytest.mark.windows_native
  def test_native_identity_changes_after_replace(tmp_path):
      p = str(tmp_path / "a")
      fp = w32.open_shared_append(p)
      ident = w32.file_identity(fp.fileno())
      assert w32.path_identity(p) == ident
      os.replace(p, p + ".old")
      open(p, "wb").close()
      assert w32.path_identity(p) != ident
      fp.close()

  @pytest.mark.windows_native
  def test_native_lockfileex_conflicts_with_msvcrt_locking(tmp_path):
      import msvcrt, subprocess, sys
      p = str(tmp_path / "lock")
      fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
      msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
      code = ("import os,sys; from sw_core import win32_files as w; "
              f"fd=os.open({p!r}, os.O_RDWR); sys.exit(0 if w.lock_file_ex(fd) is False else 1)")
      assert subprocess.run([sys.executable, "-c", code], cwd=os.getcwd()).returncode == 0
  ```
  表格補充（同檔，皆 `windows_native`）：

  | 測試名 | 斷言 |
  |---|---|
  | `test_native_windows_walfs_unlinked_reports_delete_pending` | `WindowsWalFs().open_append(p)` 後 `w32.delete_classic(p)` → `.unlinked() is True` |
  | `test_native_os_remove_semantics_recorded` | 以 JUnit property `os_remove_semantics` 記錄 `os.remove` 在本機是 classic 或 POSIX semantics；兩種結果都不得使 `WindowsWalFs` 的偵測失準：之後 `.unlinked()` 為真或 `path_identity` 為 `None` |
  | `test_native_sync_on_append_handle` | `WindowsWalFs().open_append(p)` 寫入後 `.sync()` 不擲例外（`FlushFileBuffers` 在 `FILE_APPEND_DATA\|SYNCHRONIZE` handle 上可用） |
  | `test_native_sync_dir_is_documented_noop` | `WindowsWalFs().sync_dir(dir)` 不擲例外、不開啟任何 handle（spy `CreateFileW` 呼叫數 0） |
  | `test_native_path_identity_missing_returns_none` | 不存在路徑 → `None` |
  | `test_native_reader_without_share_delete_blocks_rename` | 以 Python `open(p, "rb")` 持有（無 share-delete）時 `os.replace(p, q)` 擲 `PermissionError`（此為 T04 外部 reader 阻擋案例的前提） |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_wal_winfs.py` → 預期 `ImportError: cannot import name 'win32_files'`。commit：`test(wal): BC-01 Windows share-delete handle 失敗測試`
- [ ] **B1 實作**：依 Interfaces 與行為規格；`WinApi` 欄位在 `load_api()` 以 `ctypes.WinDLL("kernel32", use_last_error=True)` 綁定並設定 `argtypes`／`restype`（`HANDLE=ctypes.c_void_p`），`GetLastError` 綁 `ctypes.get_last_error`；結構 `FILE_STANDARD_INFO{LARGE_INTEGER AllocationSize; LARGE_INTEGER EndOfFile; DWORD NumberOfLinks; BOOLEAN DeletePending; BOOLEAN Directory}`（class 1）、`FILE_ID_INFO{ULONGLONG VolumeSerialNumber; BYTE FileId[16]}`（class 18）。`WindowsWalFs(api: WinApi | None = None)` 將替身或原生 API 傳給所有 `win32_files` 呼叫；`WindowsWalFile.unlinked()` 僅呼叫 `delete_pending`。`WindowsWalFs.rename_noreplace_or_suffix` 共用 T04 演算法；`sync` 走 `os.fsync`（CPython 在 Windows 對應 `_commit`→`FlushFileBuffers`）。`default_wal_fs()` 在 `os.name == "nt"` 回 `WindowsWalFs()`。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_wal_winfs.py tests/test_stage0_wal_handle.py`；`scripts/stage0/win-pytest.sh tests/test_stage0_wal_winfs.py tests/test_stage0_wal_handle.py tests/test_wal.py tests/test_wal_integrity.py`。
- **測試資產變更**（獨立審閱：Reviewer A 確認 API 修正未放寬斷言）：
  - `tests/test_stage0_wal_handle.py::test_dir_removed_still_self_heals`、`::test_file_unlinked_name_check_unthrottled`：`CountingFs` base 由 `PosixWalFs` 改為 `type(default_wal_fs())`，讓原生 Windows 使用 share-delete 後端；兩項自癒斷言不變。
  - `tests/test_stage0_wal_winfs.py::test_native_os_remove_semantics_recorded`、`::test_native_sync_on_append_handle`：依 `WalFile` Protocol 將 `write()` 改為 `write_all_flush()`；前者另以 JUnit property 留存刪除語意。
  - `tests/test_stage0_wal_winfs.py`：新增可攜替身案例，覆蓋 `WindowsWalFs` API 注入、DeletePending、FileId／路徑錯誤映射、查詢 handle 清理、LockFileEx 錯誤映射、檔案大小與歸檔後綴（含 `.999` 用盡）。
- [ ] **B3 commit**：`fix(wal): BC-01 Windows WAL handle 改用 CreateFileW share-delete 並提供 LockFileEx 包裝`

---

## Task T04：BC-01c rotate／reset 於常駐 handle 下同步與 DF-03 歸檔後綴

- **範圍**：BC-01「正常 Rotate／Reset」：依規格 §8.2 逐字順序——同一 writer 序列化停止插入、flush／sync、**關閉自身 handle**、rename／reset、重開；外部 reader 阻擋時有界降級（`rotation_failed:true`）。DF-03：同名歸檔加 `.1`、`.2`… 不覆蓋。依據：規格 §8.2（權威；arch §5.8 目前寫「rename 後才關閉」與規格不一致，本 Task 同步修訂 arch §5.8 為規格順序）；盤點 04 §1.8、§1.9。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-bc01-rotate-reset-sync.md`（`type: fix`，`scope: wal`）：rotate／reset 於常駐 handle 下先 sync 再 rename 並重開；同秒歸檔改加數字後綴，不再覆蓋舊歸檔（DF-03）。
- **BLOCKED-OWNER**：無。

**Files**
- Create: `tests/test_stage0_wal_rotate.py`
- Modify: `sw_core/wal.py`、`sw_core/wal_fs.py`、`contracts/daemon_logging.yaml`、`docs/rust-refactor/plans/rust-architecture.md` §4.5／§5.8、`docs/rust-refactor/plans/06-slice21-daemon-core.md`（rotate／reset 順序改為 flush→`sync()`→關閉舊 handle→rename→下次寫入時開新檔→`sync_dir`；rename 失敗時重開原路徑）

**Interfaces**
- Consumes：T02 `WalFs`；T03 `WindowsWalFs`。
- Produces（Python）：
  ```python
  def rename_noreplace_or_suffix(self, src: str, base_dst: str) -> str
      # 依序嘗試 base_dst、base_dst+".1"、…、base_dst+".999"；優先用平台原子 no-replace；不支援時先 hard link 後 unlink，若 hard link 也不支援則確認目的地不存在後用 os.rename；全部名稱均占用 → FileExistsError
  WalWriter.reset(self) -> dict[str, Any]
      # {"ok": True, "previous_seq": int, "rotated_suffix": str}；rotated_suffix＝WAL 歸檔實際後綴（含 ".N"）；
      # 沒有 WAL 可歸檔時為 ts（與現況相同）
  ```
- Rust 對應：arch §4.5 `rename_noreplace_or_suffix`、§5.8 rotate／reset。

**行為規格**
1. rotate（每筆 append 前、鎖內，WAL 與 mirror 各自判斷 `size >= R`）：`fp.sync()` → `fp.close()`、該 handle 設 None → `dst = fs.rename_noreplace_or_suffix(path, f"{path}.{ts}")` → 成功後 `fs.sync_dir(dir)`；下次寫入時開新檔。`ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())`（UTC，與現況相同）。
2. rename 失敗（含 Windows 外部 reader 無 share-delete 造成的 `PermissionError`）：立即以 `open_append(path)` 重開**原路徑**（同一檔，繼續附加）、本筆 record 帶 `rotation_failed: true`、log 既有 warning 文字；下一筆 append 再次嘗試（現況語意）。重開也失敗時依 T02 第 3、7 點處理（本筆 `loss_flag`、`unhealthy_reason="write_failed"`），不得無限重試。
3. size 判斷沿用 `fs.size(path)`（檔案系統大小，含已 flush 內容）。
4. reset：持鎖；`prev_seq=self._seq`；兩檔各自 `size > 0` 時 sync→close→rename（共用同一 `ts`，各自計算後綴）；`fs.sync_dir(dir)`（目錄不存在時沿用現況：例外穿出、`_seq` 不重設）；`self._seq = 0`；回傳 `rotated_suffix` 為 WAL 歸檔的實際後綴（`os.path.basename(dst)[len("raw.wal.ndjson."):]`）。
5. rotate 與 reset 之後，T05 的 identity 基準與 `file_first_seq` 重設為「下一筆寫入新檔的 seq」。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_wal_rotate.py`
  ```python
  from __future__ import annotations
  import json
  import os
  import pytest
  import sw_core.wal as wal_mod
  from sw_core.wal import WalWriter
  from sw_core.wal_fs import PosixWalFs

  class OpsFs(PosixWalFs):
      def __init__(self) -> None:
          super().__init__(); self.ops: list[tuple[str, str]] = []; self.rename_error: BaseException | None = None
      def open_append(self, path):
          self.ops.append(("open", os.path.basename(path)))
          inner = super().open_append(path); fs = self
          class W:
              def write_all_flush(self, d): inner.write_all_flush(d)
              def sync(self): fs.ops.append(("sync", os.path.basename(path))); inner.sync()
              def identity(self): return inner.identity()
              def unlinked(self): return inner.unlinked()
              def close(self): fs.ops.append(("close", os.path.basename(path))); inner.close()
          return W()
      def rename_noreplace_or_suffix(self, src, base_dst):
          if self.rename_error is not None:
              raise self.rename_error
          dst = super().rename_noreplace_or_suffix(src, base_dst)
          self.ops.append(("rename", os.path.basename(dst))); return dst

  @pytest.fixture
  def fixed_ts(monkeypatch):
      monkeypatch.setattr(wal_mod.time, "strftime", lambda fmt, t=None: "20261015-030617")

  def _fill(w: WalWriter, n: int) -> None:
      for i in range(n):
          w.append(com="COM0", direction="RX", source="device", payload=b"y" * 40)

  def test_rotate_syncs_closes_then_renames_and_reopens(tmp_path, fixed_ts):
      # 規格 §8.2 順序：flush／sync → 關閉自身 handle → rename → 重開
      fs = OpsFs(); w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=fs)
      _fill(w, 10)
      wal_ops = [op for op in fs.ops if op[1].startswith("raw.wal.ndjson")]
      i = wal_ops.index(("sync", "raw.wal.ndjson"))
      assert wal_ops[i + 1] == ("close", "raw.wal.ndjson")
      assert wal_ops[i + 2][0] == "rename"
      assert wal_ops[i + 3] == ("open", "raw.wal.ndjson")

  def test_same_second_rotations_get_numeric_suffix(tmp_path, fixed_ts):
      w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=OpsFs())
      _fill(w, 40)
      names = sorted(n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson."))
      assert "raw.wal.ndjson.20261015-030617" in names
      assert "raw.wal.ndjson.20261015-030617.1" in names
      seqs = []
      for n in names + ["raw.wal.ndjson"]:
          seqs += [json.loads(l)["seq"] for l in open(tmp_path / n, encoding="utf-8") if l.strip()]
      assert sorted(seqs) == list(range(1, 41))   # 無覆蓋、無遺失

  def test_reset_reports_actual_suffix_on_collision(tmp_path, fixed_ts):
      w = WalWriter(wal_dir=str(tmp_path), fs=OpsFs())
      _fill(w, 2)
      (tmp_path / "raw.wal.ndjson.20261015-030617").write_text("old\n")
      r = w.reset()
      assert r == {"ok": True, "previous_seq": 2, "rotated_suffix": "20261015-030617.1"}
      assert (tmp_path / "raw.wal.ndjson.20261015-030617").read_text() == "old\n"
      rec = w.append(com="COM0", direction="RX", source="device", payload=b"z")
      assert rec["seq"] == 1

  def test_rotate_rename_failure_reopens_same_path_and_marks_record(tmp_path, fixed_ts):
      fs = OpsFs(); w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=100, fs=fs)
      _fill(w, 3)
      before = open(tmp_path / "raw.wal.ndjson", "rb").read()
      n = len(fs.ops)
      fs.rename_error = PermissionError(13, "Permission denied")
      rec = w.append(com="COM0", direction="RX", source="device", payload=b"q")
      assert rec.get("rotation_failed") is True
      wal_ops = [op for op in fs.ops[n:] if op[1] == "raw.wal.ndjson"]
      assert wal_ops[:3] == [("sync", "raw.wal.ndjson"), ("close", "raw.wal.ndjson"), ("open", "raw.wal.ndjson")]
      after = open(tmp_path / "raw.wal.ndjson", "rb").read()
      assert after.startswith(before) and json.loads(after.splitlines()[-1])["seq"] == rec["seq"]   # 同檔繼續附加
      fs.rename_error = None
      rec2 = w.append(com="COM0", direction="RX", source="device", payload=b"r")
      assert "rotation_failed" not in rec2
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_rotate_threshold_is_ge_and_checked_before_write` | `rotate_bytes=R`，檔案大小恰為 `R` 時下一筆先 rotate；大小 `R-1` 時不 rotate（`WAL-D7`） |
  | `test_wal_and_mirror_rotate_independently` | mirror 小於門檻時只產生 WAL 歸檔 |
  | `test_reset_without_files_returns_ts_suffix` | 無檔案時 `rotated_suffix == "20261015-030617"` 且 `ok is True` |
| `test_reset_directory_missing_raises_and_keeps_seq`（`posix_only`） | `rmtree` 後 `reset()` 擲 `OSError`、`current_seq` 不變（盤點 04 §1.9；POSIX 目錄同步語意） |
  | `test_native_rotate_with_open_handle_succeeds`（`windows_native`） | Windows 上常駐 handle 存在時 rotate 產生歸檔、新檔 seq 連續 |
| `test_native_rotate_blocked_by_reader_degrades`（`windows_native`） | 另一 handle 以 `open(p,"rb")` 持有時 record 帶 `rotation_failed: true`、重開原路徑後寫入持續、無遺失 seq；reader 關閉後下一筆新增恰一個 WAL 歸檔 |
| `test_rotate_falls_back_when_atomic_rename_is_unsupported`（`posix_only`） | 原子 no-replace 回 `EINVAL` 時 rotate 仍建立歸檔及 `.1` 後綴，紀錄不帶 `rotation_failed` |
| `test_reset_reports_suffix_when_atomic_rename_is_unsupported`（`posix_only`） | 原子 no-replace 回 `EINVAL` 且時間戳歸檔已存在時，reset 保留既有內容並回傳實際 `.1` 後綴 |
| `test_rename_falls_back_when_hardlinks_are_unsupported`（`posix_only`） | hard link 不支援且目的地在檢查後出現時，不覆蓋既有歸檔，改用下一個後綴 |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_wal_rotate.py` → 預期 `AttributeError: 'PosixWalFs' object has no attribute 'rename_noreplace_or_suffix'` 與 suffix 斷言失敗。commit：`test(wal): BC-01 rotate／reset 同步與 DF-03 後綴失敗測試`
- [ ] **B1 實作**：依行為規格改 `_rotate_if_needed` 與 `reset`；`rename_noreplace_or_suffix` 放在 `PosixWalFs`（`WindowsWalFs` 繼承同一實作，Windows `os.rename` 本身不覆蓋）。POSIX 優先使用原子 no-replace；syscall 回 `EINVAL`、`ENOSYS`、`EOPNOTSUPP` 或 `ENOTSUP` 時降級為 `os.link` 後 `os.unlink`；若 hard link 也失敗，目的地仍不存在才用 `os.rename`，若已存在則轉成 `FileExistsError` 讓迴圈嘗試下一個後綴。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_wal_rotate.py tests/test_wal_integrity.py tests/test_wal.py`；`scripts/stage0/win-pytest.sh tests/test_stage0_wal_rotate.py`。
- **測試資產變更**（本次獨立審閱確認必要）：
  - `tests/test_thread_survival.py::test_wal_rotation_failure_best_effort_does_not_kill_reader`：故障注入由 monkeypatch `os.replace` 改為注入 `WalFs.rename_noreplace_or_suffix`，因輪替改走檔案系統介面；保留原有存活與 `rotation_failed` 斷言。
  - `tests/test_stage0_wal_rotate.py::test_reset_directory_missing_raises_and_keeps_seq` 改標 `posix_only`，保留原斷言；案例使用 `PosixWalFs`，且缺目錄錯誤依賴 POSIX 目錄同步語意。
  - `tests/test_stage0_wal_rotate.py::test_native_rotate_blocked_by_reader_degrades` 比較阻擋前後的 WAL 歸檔集合，確認 reader 關閉後確實新增一個歸檔。
  - 新增原子改名 syscall 不支援及 hard link 不支援時的 rotate／reset 降級測試，確認 DF-03 不覆蓋且 reset 回傳實際後綴。
- [ ] **B3 commit**：`fix(wal): BC-01 rotate／reset 依規格先 sync 並關閉 handle 再改名重開，同名歸檔加數字後綴（DF-03）`

---

## Task T05：BC-01d 外部 unlink／替換／DeletePending 偵測、有界自癒、缺口 META、停止自動 TX

- **範圍**：BC-01「外部 unlink／替換偵測」「遺失範圍」「記錄不可用時」；META 欄位具體化（規格 §8.2 要求 Stage 0 契約測試前具體化並記錄契約 revision）。依據：規格 §8.2、§5.4 末段；arch §3.8、§5.8 unlink 偵測。
- **Test author model**：haiku-xhigh　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-bc01-wal-unlink-self-heal.md`（`type: feat`，`scope: wal`）：偵測 WAL 被外部刪除或替換，有界重建後寫入 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 缺口紀錄；重建失敗時標示不健康並停止自動 TX。
- **BLOCKED-OWNER**：無（Windows 經典 delete 與 POSIX-semantics delete 兩種模式皆以 `windows_native` 測試於本機實跑）。

**Files**
- Create: `tests/test_stage0_wal_unlink.py`
- Modify: `sw_core/wal.py`、`sw_core/wal_fs.py`（新增尾端換行查詢，供替換檔重建前隔離 META 行）、`sw_core/uart_io.py`（自動 TX 阻擋）、`sw_core/service.py`（`health.status.wal` 帶出新欄位，無需改碼若直接回傳 `WalWriter.health()`）、`docs/rust-refactor/plans/rust-architecture.md` §3.8／§4.5／§5.8（補 `com:"WAL"`、`previous_identity` 字串格式、尾端換行檢查與重試時序）、`contracts/behavior_changes.yaml`（BC-01 → `IMPLEMENTED`；不得寫 `contract_revision`）、`contracts/wal_meta_events.schema.json`（**P0 T10 已建立的唯一 META schema，只修改、不另建**：`WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 補外層 `com:"WAL"`、`previous_identity` pattern、`x-status` 改 `IMPLEMENTED`，遞增 `x-catalog-revision`）、`contracts/daemon_logging.yaml`（同步新增／移動後的 WAL logger 呼叫來源行）、`tests/test_wal_missing_detection.py`（測試資產變更，見下）

**測試資產變更**（需獨立審閱，PR 註明）：
- `tests/test_wal_missing_detection.py::TestWalHealth::test_recreated_wal_is_readable_again` 原斷言 `len(rows) == 1`；BC-01 重建後先寫 META，改為先過濾 `row["dir"] != "META"` 後斷言 1 列，並另斷言恰有 1 筆 `meta.event == "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"`。`test_append_recreates_deleted_wal_dir`、`test_append_records_failure_when_recreate_fails` 不改（依 T02 第 4 點不節流的名稱檢查即可通過；後者的失敗路徑對應本 Task 第 5 點）。
- `tests/test_stage0_wal_handle.py::test_dir_removed_still_self_heals` 與 `::test_file_unlinked_name_check_unthrottled`：更新 WAL 行 seq／META 順序預期；META 會先消耗一個 seq，名稱檢查不節流的原斷言仍保留。
- `tests/test_stage0_wal_unlink.py::test_identity_query_oserror_is_unknown_and_append_continues`：加入三種 identity 查詢 `OSError` 注入，確認查詢失敗只略過該次分類，append 仍寫入原檔且不新增 META 或 unhealthy 狀態。
- `tests/test_stage0_wal_unlink.py::test_identity_query_oserror_at_rotate_threshold_keeps_current_file`：identity 查詢遇 `OSError` 且大小達輪替門檻時，確認該路徑保留現行 handle、略過該次輪替並繼續 append；包含 path／handle identity 與 `unlinked` 三種查詢。

**Interfaces**
- Consumes：T02／T03 `WalFs.path_identity`、`WalFile.identity`、`WalFile.unlinked`；T02 的 `unhealthy`／`unhealthy_reason`；T01 的 `TxResult`、`TxIncompleteError`；`sw_core.constants.WAL_IDENTITY_CHECK_INTERVAL_S`、`WAL_RECREATE_BACKOFF_S`、`WAL_RECREATE_RETRY_INTERVALS_S`。
- Produces（Python）：
  ```python
  WalWriter.unhealthy -> bool                      # property（T02 建立）；本 Task 增加 reason "recreate_failed"
  WalWriter.health() -> dict[str, Any]
      # 既有 10 鍵＋"unhealthy_reason": None | "write_failed" | "recreate_failed"；
      # healthy = 既有公式 and not self.unhealthy
  # META 紀錄（凍結；契約檔 contracts/wal_meta_events.schema.json）
  {"dir": "META", "source": "system", "com": "WAL", "cmd_id": None, "len": 0, "crc32": "00000000",
   "payload_b64": "", "loss_flag": True,
   "meta": {"event": "WAL_RECREATED_AFTER_EXTERNAL_UNLINK",
            "reason": "external_unlink" | "replaced" | "dir_missing",
            "previous_identity": "<dev>:<ino>" | None,          # 十進位整數，冒號分隔
            "potentially_lost_seq_range": [first, last] | None,
            "range_known": bool}}
  # sw_core/uart_io.py
  class WalUnavailableError(TxIncompleteError): ...         # TxIncompleteError 由 T01 建立；本類別在此 Task 新增
  AUTO_TX_SOURCES: frozenset[str] = frozenset({"system"})   # 判定：source == "system" or source.startswith("system:")
  ```
- Rust 對應：arch §3.8 META schema（本 Task 補 `com:"WAL"`）、§5.8 unlink 偵測步驟 1～6、§5.4 fence `wal_unhealthy`（`LoginProbe`／`ReadyReconfirm`／`SystemRecoverKey`／`SelfTestProbe` 被擋）。

**行為規格**
1. 偵測分兩層：(a) **名稱消失**（`not isdir(wal_dir)` 或 `fs.size(wal_path) is None`）**每筆都檢查、不節流**（T02 第 4 點的同一檢查，本 Task 在其命中時補 META）；(b) **identity／DeletePending 比對**平時只在距上次比對 `>= WAL_IDENTITY_CHECK_INTERVAL_S` 時做（50 ms 只是節流間隔，不代表遺失窗口上限）；若本筆因 `size >= R` 即將輪替並清除現有 handle／identity，須先完成該 handle 的失效分類，以免輪替抹除替換證據。mirror 同樣檢查但只重開、不寫 META。分類依下列順序，取第一個命中者：
   1. `not os.path.isdir(wal_dir)` → `dir_missing`；
   2. `ident = fs.path_identity(wal_path)`；`ident is None`（名稱不存在）或 `ident == (0, 0)`（Windows delete-pending 哨兵，T03 第 2 點）→ `external_unlink`；
   3. `ident != fp.identity()`（名稱存在且為真實但不同的 identity）→ `replaced`（POSIX `os.replace` 覆蓋後舊 inode 的 `st_nlink` 也會變 0，因此 identity 比對必須先於 `unlinked()`）；
   4. identity 相同但 `fp.unlinked()` 為真 → `external_unlink`。
   5. `path_identity`、`identity` 或 `unlinked` 查詢若擲出 `OSError`（包含檔案系統不支援 `FileIdInfo` 的 50／87），該次 identity 視為未知：略過本次 identity-based 分類，使用目前 handle 繼續一般 append；若該路徑同時達輪替門檻，保留其 handle 並略過該路徑本次輪替，以免清除尚未能判讀的替換證據。不得因查詢錯誤觸發重建、寫 META、設 unhealthy，亦不得讓查詢例外離開 `append`。這只處理 identity 證據不可用，不覆蓋已由每筆名稱檢查確認的目錄／路徑消失；後續一般寫入若失敗，依 T02 寫入失敗規格處理。
2. 命中後：記 `previous_identity = f"{dev}:{ino}"`（取不到為 `None`）、遺失範圍、關閉自身 handle、`ensure_dir`，做**一次** `open_append` 嘗試。WalWriter 任何路徑都**不得持 `_lock` 睡眠**：重試的退避以 `clock` 判定——第 i 次失敗（i=1,2,3）後，下一次嘗試只在距該次失敗 `>= WAL_RECREATE_BACKOFF_S[i-1]`（0.01、0.05、0.2 s）的下一筆 append 時進行；第 4 次失敗起間隔依 `WAL_RECREATE_RETRY_INTERVALS_S`（1 s、2 s、之後封頂 5 s）；未到嘗試時間的 append 不做任何開檔 syscall，直接回傳 `loss_flag=True`（seq 照常消耗）。
3. 遺失範圍：`file_first_seq` 為目前 WAL 檔第一筆 seq（啟動掃描時取現行檔第一個非空行；該行能解析為 dict 且 `seq` 為 int 才算已知，否則為 `None`；rotate／reset／重建後設為下一筆寫入的 seq）。範圍＝`[file_first_seq, self._seq]`（`self._seq` 為 META 之前最後已消耗的 seq，含不健康期間消耗而未落盤的 seq）；`file_first_seq is None` 或 `self._seq < file_first_seq` → `potentially_lost_seq_range=None`、`range_known=False`。禁止填入猜測值。
4. 重建成功：先寫 META（消耗一個 seq、外層 `loss_flag:true`），再寫原紀錄；`_recreated_count += 1`；`unhealthy=False`；`unhealthy_reason=None`。
5. 重建失敗：**第一次失敗即**設 `unhealthy=True`、`unhealthy_reason="recreate_failed"`（重建失敗不得宣稱 healthy），本筆回傳 `loss_flag=True`、`write_failures += 1`；之後依第 2 點的時間表做有界重試（不得無限迴圈、不得持鎖睡眠、不得強制關閉他人 handle）。不健康期間的範圍在成功重建時一併涵蓋（第 3 點）。
6. 停止自動 TX：`UARTBridge.send_bytes` 在 `_state_lock` 內、flash gate 靜默丟棄之後檢查 `getattr(self.wal, "unhealthy", False)`（既有測試以無此屬性的 stub WAL 建 bridge；順序保留 RPC-H-07）——`unhealthy_reason` 為 `write_failed`（T02）或 `recreate_failed` 皆然——且 `source == "system" or source.startswith("system:")` → 不寫 OS、回 `TxResult("not_submitted", 0, len(payload), "WAL_UNHEALTHY")` 並 raise `WalUnavailableError(result)`；agent／human 來源不受此限（規格只要求停止新增自動 TX）。來源分類必須涵蓋實際的自動 TX 呼叫者：實作前以 `grep -n 'source="system' sw_core/*.py` 列出 login probe、ready reconfirm、recover 鍵、self-test probe 等全部呼叫點並逐一確認以 `system`／`system:` 開頭，未符合者在本 Task 內改正並列入 PR 說明。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_wal_unlink.py`
  - 補 `core_portable` 案例 `test_identity_query_oserror_is_unknown_and_append_continues`：替身分別令路徑 identity、handle identity、`unlinked` 查詢擲 `OSError`，確認 append 不外拋、不寫重建 META、不設 unhealthy，且記錄持續寫入當前 WAL。
  ```python
  from __future__ import annotations
  import json
  import os
  import shutil
  import pytest
  from sw_core.wal import WalWriter
  from sw_core.wal_fs import PosixWalFs

  class Clock:
      def __init__(self) -> None: self.t = 1000.0
      def __call__(self) -> float: return self.t
      def advance(self, s: float) -> None: self.t += s

  def _lines(p):
      return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]

  def _w(tmp_path, clock, fs=None):
      return WalWriter(wal_dir=str(tmp_path), fs=fs or PosixWalFs(), clock=clock)

  def test_external_unlink_recreates_and_writes_meta_first(tmp_path):
      clk = Clock(); w = _w(tmp_path, clk)
      for i in range(3):
          w.append(com="COM0", direction="RX", source="device", payload=b"a")
      st = os.stat(w.wal_path)
      os.unlink(w.wal_path)
      clk.advance(0.06)
      rec = w.append(com="COM0", direction="RX", source="device", payload=b"b")
      lines = _lines(w.wal_path)
      meta = lines[0]
      assert meta["dir"] == "META" and meta["com"] == "WAL" and meta["source"] == "system"
      assert meta["loss_flag"] is True and meta["seq"] == 4
      assert meta["meta"] == {
          "event": "WAL_RECREATED_AFTER_EXTERNAL_UNLINK", "reason": "external_unlink",
          "previous_identity": f"{st.st_dev}:{st.st_ino}",
          "potentially_lost_seq_range": [1, 3], "range_known": True,
      }
      assert lines[1]["seq"] == rec["seq"] == 5
      assert w.health()["recreated_count"] == 1 and w.health()["healthy"] is True

  def test_replaced_file_detected(tmp_path):
      clk = Clock(); w = _w(tmp_path, clk)
      w.append(com="COM0", direction="RX", source="device", payload=b"a")
      tmp = tmp_path / "other"; tmp.write_text("")
      os.replace(tmp, w.wal_path)
      clk.advance(0.06)
      w.append(com="COM0", direction="RX", source="device", payload=b"b")
      metas = [l for l in _lines(w.wal_path) if l["dir"] == "META"]
      assert metas[0]["meta"]["reason"] == "replaced"

  def test_dir_missing_reason(tmp_path):
      d = tmp_path / "wal"; clk = Clock(); w = _w(d, clk)
      w.append(com="COM0", direction="RX", source="device", payload=b"a")
      shutil.rmtree(d); clk.advance(0.06)
      w.append(com="COM0", direction="RX", source="device", payload=b"b")
      assert _lines(w.wal_path)[0]["meta"]["reason"] == "dir_missing"

  def test_check_is_throttled_to_interval(tmp_path):
      calls = []
      class F(PosixWalFs):
          def path_identity(self, p):
              calls.append(p); return super().path_identity(p)
      clk = Clock(); w = _w(tmp_path, clk, fs=F())
      for _ in range(10):
          w.append(com="COM0", direction="RX", source="device", payload=b"a")
      wal_calls = [c for c in calls if c == w.wal_path]
      assert len(wal_calls) <= 1
      clk.advance(0.06)   # 不用 0.05：1000.0+0.05-1000.0 在浮點下 < 0.05
      w.append(com="COM0", direction="RX", source="device", payload=b"a")
      assert len([c for c in calls if c == w.wal_path]) == len(wal_calls) + 1

  def test_unknown_range_when_first_seq_unknown(tmp_path):
      (tmp_path / "raw.wal.ndjson").write_text("garbage-not-json\n" + json.dumps({"seq": 7}) + "\n")
      clk = Clock(); w = _w(tmp_path, clk)
      w.append(com="COM0", direction="RX", source="device", payload=b"a")
      os.unlink(w.wal_path); clk.advance(0.06)
      w.append(com="COM0", direction="RX", source="device", payload=b"b")
      meta = _lines(w.wal_path)[0]["meta"]
      assert meta["potentially_lost_seq_range"] is None and meta["range_known"] is False

  def test_bounded_retry_then_unhealthy_and_auto_tx_stops(tmp_path, monkeypatch):
      import time as _time
      monkeypatch.setattr(_time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("WalWriter 不得睡眠")))
      class Broken(PosixWalFs):
          broken = False; attempts = 0
          def open_append(self, p):
              if self.broken and p.endswith("raw.wal.ndjson"):
                  self.attempts += 1; raise PermissionError(13, "Access is denied")
              return super().open_append(p)
      fs = Broken(); clk = Clock(); w = _w(tmp_path, clk, fs=fs)
      w.append(com="COM0", direction="RX", source="device", payload=b"a")
      os.unlink(w.wal_path); fs.broken = True; clk.advance(0.06)
      rec = w.append(com="COM0", direction="RX", source="device", payload=b"b")
      assert rec["loss_flag"] is True and fs.attempts == 1          # 每筆最多一次嘗試
      h = w.health()
      assert h["healthy"] is False and h["unhealthy_reason"] == "recreate_failed" and w.unhealthy is True
      w.append(com="COM0", direction="RX", source="device", payload=b"b2")   # 未到退避時間：不嘗試
      assert fs.attempts == 1
      clk.advance(0.011); w.append(com="COM0", direction="RX", source="device", payload=b"b3")
      assert fs.attempts == 2                                        # 第 1 次失敗後 WAL_RECREATE_BACKOFF_S[0]=0.01 s
      from stage0_fakes import FakePort, make_bridge
      from sw_core.uart_io import WalUnavailableError
      port = FakePort(); bridge, _ = make_bridge(tmp_path / "b", port=port, wal=w)
      with pytest.raises(WalUnavailableError):
          bridge.send_bytes(b"\x03", source="system:recover")
      assert port.writes == []
      bridge.send_bytes(b"ls\n", source="agent:x")
      assert port.writes == [b"ls\n"]
      fs.broken = False; clk.advance(0.06)
      w.append(com="COM0", direction="RX", source="device", payload=b"c")
      meta = [l for l in _lines(w.wal_path) if l["dir"] == "META"][0]["meta"]
      assert meta["potentially_lost_seq_range"][0] == 1
      assert w.unhealthy is False
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_mirror_unlink_reopens_without_meta` | 只刪 mirror → 下一筆 mirror 重建，WAL 無 META 紀錄 |
  | `test_first_seq_after_rotate_used_for_range` | rotate 後新檔第一筆 seq＝N，刪新檔後 META 範圍 `[N, last]` |
  | `test_meta_conforms_to_contract_schema` | 以 `jsonschema` 驗 META 紀錄符合 `contracts/wal_meta_events.schema.json`（P0 唯一 META schema）中 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 分支 |
  | `test_health_unhealthy_reason_null_when_healthy` | 正常時 `health()["unhealthy_reason"] is None` |
  | `test_unthrottled_name_check_writes_meta_immediately` | `os.unlink(wal_path)` 後**不推進 clock** 立即 append → 新檔第一行即 META `reason == "external_unlink"` |
  | `test_write_failed_also_blocks_auto_tx` | T02 的 `unhealthy_reason == "write_failed"` 時 `send_bytes(source="system:login")` 擲 `WalUnavailableError`、`port.writes == []` |
  | `test_replaced_file_at_rotate_threshold_is_detected_before_rotation` | 替換檔大小恰為／高於 `rotate_bytes` 時，先保留替換證據與原遺失 seq 範圍，再依 `>= R` 輪替並在現行檔寫 META |
  | `test_replaced_unterminated_file_keeps_meta_on_a_parseable_line` | 替換檔尾端沒有換行時，先終結殘行；`tail_raw()` 仍能讀到獨立 META 與後續紀錄 |
  | `test_second_unlink_range_starts_at_first_meta_seq` | 同一 writer 連續兩次 unlink；第二筆 META 範圍由第一筆 META 的 seq 開始 |
  | `test_zero_identity_sentinel_and_unlinked_handle_trigger_recreation` | 行為替身分別提供 `(0, 0)` sentinel、identity 相同但 `unlinked() is True`，兩案皆分類為 `external_unlink` |
  | `test_recreate_retry_schedule_checks_each_gap_and_caps_at_five_seconds` | fake clock 在每一重試間隔前 1 ms 不觸發，達間隔後觸發；涵蓋 10／50／200 ms、1／2／5／5 s |
  | `test_replacement_tail_inspection_failure_stays_unhealthy_until_retry` | 尾端查詢擲 `OSError` 時不寫 META、保持 `recreate_failed`；退避到期後確認／終結尾端、寫出可讀 META 並恢復健康 |
  | `test_identity_query_oserror_at_rotate_threshold_keeps_current_file` | path／handle identity 或 `unlinked` 查詢擲 `OSError` 且檔案達輪替門檻時，保留現行 handle 並略過該路徑輪替；正常 append、不寫 META、不設 unhealthy |
  | `test_flash_gate_precedes_wal_unhealthy_rejection` | WAL 不健康且 flash gate 開啟時，自動 TX 仍照基線靜默丟棄，不先擲 `WalUnavailableError` |
  | `test_bridge_with_stub_wal_without_unhealthy_attr` | 以無 `unhealthy` 屬性的 stub WAL 建 bridge → `send_bytes(source="system:x")` 照常寫出 |
  | `test_native_classic_delete_pending_with_external_reader`（`windows_native`） | 外部以 share-all handle 持有，`w32.delete_classic(p)` 後（先斷言 `path_identity == (0, 0)` 確認 classic 生效）`unlinked()` 為真；重建因 access denied 失敗 → `unhealthy`；外部 handle 關閉並依退避時間表後重建成功、META `reason == "external_unlink"` |
  | `test_native_posix_semantics_delete`（`windows_native`） | 以 `w32.delete_posix_semantics(p)`（`SetFileInformationByHandle(FileDispositionInfoEx, FILE_DISPOSITION_FLAG_DELETE\|FILE_DISPOSITION_FLAG_POSIX_SEMANTICS)`）刪除 → 名稱立即消失、重建成功、META `reason == "external_unlink"` |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_wal_unlink.py` → 預期 `TypeError: WalWriter.__init__() got an unexpected keyword argument 'clock'`（T02 實作後則為 META 斷言失敗）。commit：`test(wal): BC-01 外部刪檔自癒與缺口 META 失敗測試`
- [ ] **B1 實作**：
  - `WalWriter` 新增 `_file_first_seq: int | None`、`_last_identity_check: float`、`_recreate_failures: int`、`_next_recreate_at: float | None`、`_wal_identity: tuple[int, int] | None`（開檔時取 `fp.identity()`）。`_check_identity_locked() -> str | None` 依第 1 點順序回傳 reason；`_recreate_locked(reason)` 依行為規格第 2～5 點（不呼叫任何 sleep）；META 以內部 `_append_locked(com="WAL", direction="META", source="system", payload=b"", loss_flag=True, meta=…)` 寫入（共用 seq／序列化路徑，不遞迴觸發偵測）。
  - `sw_core/uart_io.py`：`WalUnavailableError` 與 `send_bytes` 檢查（`TxResult`／`TxIncompleteError` 由 T01 建立，本 Task 直接使用）。
  - `contracts/wal_meta_events.schema.json`（修改 P0 已建檔，不另建）：`WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 分支 `required: [event, reason, previous_identity, potentially_lost_seq_range, range_known]`，`reason` enum 三值，`previous_identity` `^[0-9]+:[0-9]+$` 或 null，沿用 P0 的 `range_known` if／then 與 `minimum: 1`、`x-constraint: "first <= last"`，`additionalProperties:false`；外層補 `com: "WAL"`；`x-status` 改 `IMPLEMENTED`；遞增 `x-catalog-revision`。P0 `test_meta_schema_single_source` 必須仍通過。
  - `rust-architecture.md` §3.8：在 META 範例補「外層 `com:"WAL"`；`previous_identity` 為十進位 `<dev>:<ino>`（Windows 為 `<VolumeSerialNumber>:<FileId128>`）」。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_wal_unlink.py tests/test_stage0_wal_handle.py tests/test_stage0_wal_rotate.py tests/test_wal_missing_detection.py tests/test_doctor.py tests/test_uart_flash_bridge.py tests/test_command_completion.py tests/test_console_unlock.py tests/test_contracts_wal_state_schema.py`；`python3 -m tools.contracts check`；`scripts/stage0/win-pytest.sh tests/test_stage0_wal_unlink.py`；全套依 §0 第 4 點。
- [ ] **B3 commit**：`feat(wal): BC-01 偵測外部刪檔並有界重建，寫入缺口 META 且不健康時停止自動 TX`

---

## Task T06：BC-03／SC-07 TX 三態、TX WAL 只記確認 bytes、可取消寫入、DF-15 精確 wal_seq

- **範圍**：BC-03 的 I/O 層（TX 三態：未提交／確認 N bytes／不明）；SC-07（TX WAL 只記 OS 已確認的 bytes，順序等於實際寫入順序，在 `_write_lock` 內 append）；SC-04 前置（`_write_all` 遇 EAGAIN 可被取消，`UIO-12.2-b`）；DF-15（RX observer、event engine、background capture 改用該 RX chunk 的精確 WAL seq）。依據：規格 §5.1.4、§8.1 TX；盤點 06 §5、§12.3；arch §4.2 `TxOutcome`、§5.4。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-bc03-tx-outcome.md`（`type: fix`，`scope: uart`）：TX 區分未提交、已確認 N bytes、結果不明，TX WAL 只記確認寫出的 bytes 並依實際寫入順序；寫入等待可被取消；RX 觀察者取得該 chunk 的精確 WAL seq（DF-15）。
- **BLOCKED-OWNER**：(1) 實體 USB-UART 在 RTS/CTS 被拉住時的部分寫入與取消（需實機 bench）；(2) Windows 原生 COM handle 上 pyserial write timeout 造成的 `unknown` 結果（OD-08 COM gate）。本 Task 以替身與 PTY 驗證，上述兩項記為證據不足。

**Files**
- Create: `tests/test_stage0_tx_outcome.py`
- Modify: `sw_core/serial_port.py`、`sw_core/uart_io.py`、`sw_core/session_manager.py`（`_on_bridge_rx` 收 `wal_seq`、兩處 bridge 建構 lambda）、`sw_core/service.py`（RX observer 參數不變，值改精確）、`contracts/wal_meta_events.schema.json`（新增 `tx_outcome_unknown` 事件分支，遞增 `x-catalog-revision`）、`contracts/daemon_logging.yaml`（新增 TX 帳本淘汰 WARNING 並同步來源行）、`tests/test_stage0_wal_unlink.py`（flash gate 預期結果改為 TxResult）、`docs/rust-refactor/plans/rust-architecture.md` §3.8／§4.2／§5.7、`docs/rust-refactor/plans/06-slice21-daemon-core.md`（Unknown 變體攜帶 confirmed）

**相容規則（凍結；保護既有測試替身）**
1. `send_bytes` 一律以兩個位置參數呼叫 `self._write_all(fd, payload)`（取消事件由 `_write_all` 內部讀 `self._tx_cancel`，T07），以相容 `tests/test_uart_flash_bridge.py:65,91`、`tests/test_refine_ownership.py:477,523,596` 以兩參數 lambda 取代 `_write_all` 的寫法；`_write_all` 回傳 `None` 視為 `TxResult("completed", n, n)`。
2. port 路徑：僅在 `port.write` 簽名接受 `cancel` 關鍵字時才傳（`inspect.signature` 結果以 port 型別快取），以相容 `tests/test_serial_port.py:363` 的 `_FakeSerialPort.write(self, payload) -> None`；回傳 `None` 視為 `completed, n`。
3. WAL：`rec = self.wal.append(...)`；`seq = rec.get("seq") if isinstance(rec, dict) else None`，以相容回傳 `None` 的 stub WAL（`tests/test_uart_flash_bridge.py::_RecordingWal`、`tests/test_command_completion.py::Wal`、`tests/test_console_unlock.py::Wal`）；`getattr(self.wal, "unhealthy", False)` 同 T05。
4. 以 `object.__new__(UARTBridge)` 建立、缺新欄位的 bare bridge（`tests/test_concurrency_races.py:25`、`tests/test_suspend_resume_reentrant.py`）：新欄位（`_tx_accounts`、`_tx_frozen`、`_tx_cancel` 等）一律以 `getattr(self, "<name>", <預設>)` 讀取或以 class 層預設值提供，不得要求這些測試補欄位。
5. `_PySerialPort.write` 對 `SerialTimeoutException`／`SerialException`／普通 `OSError` 維持**擲出 `OSError` 子類**（`TxUnknownError`，errno 沿用現況映射：timeout→`ETIMEDOUT`、其他→例外 errno 或 `EIO`）；普通 `OSError` 時 `.result.confirmed` 保留此呼叫已確認的前綴。此規則相容 `tests/test_serial_port.py::test_write_timeout_maps_to_oserror_etimedout`、`test_write_serialexception_maps_to_oserror`；結果資訊放在例外的 `.result`。

**Interfaces**
- Consumes：T02 `WalWriter.append`（回傳 record 含 `seq`）；T01 的 `TxResult`、`TxIncompleteError`、`TxUnknownError`、`SerialNotReady`（本 Task 不重複定義）。
- Produces（Python）：
  ```python
  # sw_core/serial_port.py
  class SerialPort(ABC):
      def write(self, payload: bytes, *, cancel: threading.Event | None = None) -> TxResult | None
          # 抽象簽章改為回傳 TxResult；結果不明時 raise TxUnknownError（.result 帶已確認前綴）
  # sw_core/uart_io.py
  UARTBridge._write_all(self, fd: int, payload: bytes, *, cancel: threading.Event | None = None) -> TxResult
      # cancel 為 None 時使用 self._tx_cancel（T07）
  UARTBridge.send_bytes(self, payload: bytes, *, source: str, cmd_id: str | None = None,
                        log: bool = True, _allow_during_flash: bool = False) -> TxResult
  UARTBridge.tx_account(self, cmd_id: str) -> tuple[int, bool]    # (confirmed_bytes 累計, 曾有 unknown)；讀後移除；容量 512 FIFO
  UARTBridge.tx_settle(self, cmd_id: str) -> None                 # R1-F8：命令終態（非 drain 撤銷路徑）結算＝移除；未知 cmd_id 為 no-op
  UARTBridge.tx_set_active(self, cmd_id: str | None) -> None      # R1-F8：目前前景命令；FIFO 溢位淘汰時跳過它（不淘汰在途證據）
  SessionManager._on_bridge_rx(self, session_id: str, data: bytes, wal_seq: int | None = None) -> None
  ```
- Rust 對應：arch §4.2 `TxOutcome { NotSubmitted, Completed(usize), Unknown { confirmed: usize } }`／`TxResult { outcome, error }`、`SerialWriter::write_gated(data, cancel, gate)`（每次 OS 提交經 `SubmitGate`，R0-F1）；§5.4 OS 提交時 fence 與「完成後把 TxResult 交給 WalWriter（只記錄確認的 bytes）」；§5.7 `confirmed_bytes`／`unknown` 累計。**arch §4.2 的 `Unknown` 必須帶已確認前綴**：本 Task 同步修訂 arch §4.2 為 `Unknown { confirmed: usize }`（`confirmed` 為結果不明前 OS 已確認的 bytes，未知尾段＝`len - confirmed`），§5.7 映射改為「`confirmed ≥ 1`（不論最後為 Completed 或 Unknown）→ `COMMAND_ABORTED_IN_FLIGHT`；`Unknown{confirmed:0}` → `SEND_FAILED`」。

**TX 結果判定表（凍結）**

| 情境 | `TxResult` | TX WAL | `send_bytes` 行為 |
|---|---|---|---|
| 全部寫出 | `completed, n=len` | 一筆，payload 全長 | 回傳 |
| POSIX 寫出 k 後 `OSError`（非 EAGAIN） | `completed, k, err="<Exc>: <msg>"` | k>0 時一筆 `payload[:k]`；k=0 無 | raise `TxIncompleteError` |
| POSIX `os.write` 回 0 | `completed, sent, err="short_write"` | sent>0 時一筆 | raise |
| EAGAIN 期間 `cancel` 被設 | `completed, sent, err="cancelled"` | sent>0 時一筆 | raise |
| pyserial 迴圈已確認寫出 k bytes 後遇 `SerialTimeoutException` 或其他 `SerialException`（k 可為 0） | `unknown, k, err=…`（未知尾段 `requested-k`） | k>0 時一筆 TX `payload[:k]`；另寫一筆 META `{"event":"tx_outcome_unknown","requested_bytes":M,"confirmed_bytes":k,"unknown_range":[k,M],"error":…}`（`source`、`cmd_id` 同原請求） | raise `TxUnknownError`（`OSError` 子類） |
| pyserial `n` 為 0 或 None | `completed, sent, err="short_write"` | sent>0 時一筆 | raise |
| port 寫入擲出沒有結構化進度的普通 `OSError` | `unknown, 0, len`；若後端已掌握確認前綴則以該值回報 | 已確認前綴記 TX，另記 `tx_outcome_unknown` META | raise `TxUnknownError` |
| flash gate 擋下（`UIO-12.3-d`） | `not_submitted, 0` | 無 | 回傳、不 raise（保留靜默語意） |
| raw console deferred（admission 關） | `not_submitted, 0, err="deferred"` | 無 | 回傳、不 raise |
| serial 未就緒 | `not_submitted, 0, err="serial not ready"` | 無 | raise `SerialNotReady`（`TxIncompleteError`＋`RuntimeError` 雙重子類，與現況 `RuntimeError("serial not ready")` 相容） |
| `log=False`（secret、echo 分片） | 依實際 | 不記（`UIO-12.3-c`） | 依實際 |

`send_command_echo_paced`（`UIO-5.3`）：各 slice 以 `log=False` 送出、最後在 `_write_lock` 外 append 單筆 TX 的現況改為——整個 echo-paced 序列**持 `_write_lock`** 送完（他來源不得插隊，WAL 順序＝OS 順序）；以 `try/finally` 在**仍持鎖**時把「已確認寫出的 slices 串接」append 成單筆 TX（含中途 `TxIncompleteError`／`TxUnknownError` 的已確認前綴），再重拋原例外；`unknown` 時另寫 `tx_outcome_unknown` META。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_tx_outcome.py`
  ```python
  from __future__ import annotations
  import errno
  import threading
  import time
  import pytest
  import sw_core.uart_io as uio
  from sw_core.serial_port import TxResult
  from sw_core.uart_io import TxIncompleteError
  from stage0_fakes import FakePort, ScriptedWrite, make_bridge, wal_records

  def _posix_bridge(tmp_path, monkeypatch, actions):
      bridge, wal = make_bridge(tmp_path)
      bridge._serial = object(); bridge._serial_fd = 99
      sw = ScriptedWrite(actions); monkeypatch.setattr(uio.os, "write", sw)
      return bridge, wal, sw

  def test_partial_then_eio_records_only_confirmed_bytes(tmp_path, monkeypatch):
      bridge, wal, _ = _posix_bridge(tmp_path, monkeypatch, [3, OSError(errno.EIO, "I/O error")])
      with pytest.raises(TxIncompleteError) as ei:
          bridge.send_bytes(b"hello", source="agent:x", cmd_id="c1")
      r = ei.value.result
      assert (r.outcome, r.confirmed, r.requested) == ("completed", 3, 5)
      tx = [x for x in wal_records(wal) if x["dir"] == "TX"]
      assert len(tx) == 1 and tx[0]["len"] == 3 and tx[0]["cmd_id"] == "c1"
      import base64
      assert base64.b64decode(tx[0]["payload_b64"]) == b"hel"

  def test_first_write_error_records_no_tx(tmp_path, monkeypatch):
      bridge, wal, _ = _posix_bridge(tmp_path, monkeypatch, [OSError(errno.EIO, "I/O error")])
      with pytest.raises(TxIncompleteError):
          bridge.send_bytes(b"hello", source="agent:x")
      assert [x for x in wal_records(wal) if x["dir"] == "TX"] == []

  def test_eagain_wait_is_cancellable(tmp_path, monkeypatch):
      bridge, wal = make_bridge(tmp_path)
      def always_eagain(fd, data):
          raise BlockingIOError(errno.EAGAIN, "again")
      monkeypatch.setattr(uio.os, "write", always_eagain)
      cancel = threading.Event()
      threading.Timer(0.05, cancel.set).start()
      t0 = time.monotonic()
      r = bridge._write_all(99, b"abc", cancel=cancel)
      assert time.monotonic() - t0 < 1.0
      assert r == TxResult("completed", 0, 3, "cancelled")

  def test_pyserial_timeout_is_unknown_and_writes_meta(tmp_path):
      port = FakePort(write_results=[TxResult("unknown", 0, 5, "SerialTimeoutException: Write timeout")])
      bridge, wal = make_bridge(tmp_path, port=port)
      with pytest.raises(TxIncompleteError) as ei:
          bridge.send_bytes(b"hello", source="agent:x", cmd_id="c9")
      assert ei.value.result.outcome == "unknown"
      recs = wal_records(wal)
      assert [x for x in recs if x["dir"] == "TX"] == []
      meta = [x for x in recs if x["dir"] == "META"]
      assert meta[0]["meta"] == {"event": "tx_outcome_unknown", "requested_bytes": 5, "confirmed_bytes": 0,
                                 "unknown_range": [0, 5], "error": "SerialTimeoutException: Write timeout"}
      assert meta[0]["cmd_id"] == "c9" and meta[0]["source"] == "agent:x"

  def test_pyserial_partial_then_timeout_keeps_confirmed_prefix(tmp_path):
      from sw_core.uart_io import TxUnknownError
      import errno as _e
      port = FakePort(write_results=[TxUnknownError(TxResult("unknown", 2, 5, "SerialTimeoutException: t"), _e.ETIMEDOUT)])
      bridge, wal = make_bridge(tmp_path, port=port)
      with pytest.raises(TxUnknownError) as ei:
          bridge.send_bytes(b"hello", source="agent:x", cmd_id="c8")
      assert (ei.value.result.confirmed, ei.value.result.requested) == (2, 5)
      import base64
      tx = [x for x in wal_records(wal) if x["dir"] == "TX"]
      assert len(tx) == 1 and base64.b64decode(tx[0]["payload_b64"]) == b"he"
      assert bridge.tx_account("c8") == (2, True)

  def test_partial_writes_from_two_sources_do_not_interleave(tmp_path, monkeypatch):
      bridge, wal = make_bridge(tmp_path); bridge._serial = object(); bridge._serial_fd = 99
      seen: list[bytes] = []; gate = threading.Event()
      def w(fd, data):
          seen.append(bytes(data))
          if bytes(data) == b"AAAAA": gate.set(); time.sleep(0.05); return 2   # A 只寫出 2 bytes
          return len(data)
      monkeypatch.setattr(uio.os, "write", w)
      ta = threading.Thread(target=bridge.send_bytes, args=(b"AAAAA",), kwargs={"source": "agent:a"}); ta.start()
      gate.wait(1.0)
      tb = threading.Thread(target=bridge.send_bytes, args=(b"BB",), kwargs={"source": "agent:b"}); tb.start()
      ta.join(); tb.join()
      assert seen == [b"AAAAA", b"AAA", b"BB"]                 # OS 寫入序列：A[:2]、A[2:]、B
      import base64
      assert [base64.b64decode(x["payload_b64"]) for x in wal_records(wal) if x["dir"] == "TX"] == [b"AAAAA", b"BB"]

  def test_flash_gate_returns_not_submitted_silently(tmp_path):
      port = FakePort(); bridge, wal = make_bridge(tmp_path, port=port)
      bridge.set_flash_mode(True)
      r = bridge.send_bytes(b"x", source="agent:x")
      assert r == TxResult("not_submitted", 0, 1, None)
      assert port.writes == [] and wal_records(wal) == []

  def test_tx_wal_order_equals_os_write_order(tmp_path, monkeypatch):
      bridge, wal = make_bridge(tmp_path); bridge._serial = object(); bridge._serial_fd = 99
      order: list[bytes] = []
      def slow_write(fd, data):
          order.append(bytes(data)); time.sleep(0.02); return len(data)
      monkeypatch.setattr(uio.os, "write", slow_write)
      ts = [threading.Thread(target=bridge.send_bytes, args=(p,), kwargs={"source": "agent:x"})
            for p in (b"AAAA", b"BBBB", b"CCCC")]
      for t in ts: t.start()
      for t in ts: t.join()
      import base64
      logged = [base64.b64decode(x["payload_b64"]) for x in wal_records(wal) if x["dir"] == "TX"]
      assert logged == order

  def test_rx_observer_receives_exact_chunk_seq(tmp_path):
      got: list[int | None] = []
      bridge, wal = make_bridge(tmp_path)
      bridge._on_rx_data = lambda data, wal_seq=None: got.append(wal_seq)
      wal.append(com="COM1", direction="RX", source="device", payload=b"other")
      bridge._handle_serial_rx(b"mine")
      mine = [x for x in wal_records(wal) if x["com"] == "COM0"][0]
      assert got == [mine["seq"]]

  def test_tx_account_accumulates_confirmed_and_unknown(tmp_path, monkeypatch):
      bridge, wal, _ = _posix_bridge(tmp_path, monkeypatch, [5, 2, OSError(errno.EIO, "x")])
      bridge.send_bytes(b"hello", source="agent:x", cmd_id="c1")
      with pytest.raises(TxIncompleteError):
          bridge.send_bytes(b"abcd", source="agent:x", cmd_id="c1")
      assert bridge.tx_account("c1") == (7, False)
      assert bridge.tx_account("c1") == (0, False)   # 讀後移除

  def test_tx_account_settled_on_completion_and_active_never_evicted(tmp_path, monkeypatch):   # R1-F8
      bridge, wal, _ = _posix_bridge(tmp_path, monkeypatch, [1] * 2000)
      bridge.tx_set_active("fg")
      bridge.send_bytes(b"f", source="agent:x", cmd_id="fg")          # 最早插入的在途前景命令
      for i in range(600):                                              # > 512：已完成命令逐一結算，不累積
          bridge.send_bytes(b"x", source="agent:x", cmd_id=f"c{i}")
          bridge.tx_settle(f"c{i}")
      assert set(bridge._tx_accounts) == {"fg"}
      for i in range(600):                                              # 漏結算（洩漏）時 FIFO 上限仍成立，但跳過 active
          bridge.send_bytes(b"x", source="agent:x", cmd_id=f"leak{i}")
      assert len(bridge._tx_accounts) == 512 and "fg" in bridge._tx_accounts
      assert bridge.tx_account("fg") == (1, False)                      # drain 映射仍拿得到在途證據
      bridge.tx_settle("nope")                                          # 未知 cmd_id：no-op
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_write_returns_zero_is_short_write` | `ScriptedWrite([2, 0])` → `TxResult("completed", 2, 5, "short_write")`、TX WAL 1 筆 len 2 |
  | `test_serial_not_ready_raises_runtimeerror_subclass` | `_serial=None,_serial_fd=None` → `pytest.raises(RuntimeError)` 且 `isinstance(exc, TxIncompleteError)` 且 `isinstance(exc, OSError)` |
  | `test_secret_not_logged_even_on_partial` | `send_secret` 部分寫出 → 無 TX 紀錄（`UIO-12.3-c`） |
  | `test_pyserial_port_write_maps_timeout_to_unknown` | `_PySerialPort.write` 在替身 `serial.Serial.write` 先回 2 再擲 `SerialTimeoutException` 時擲 `TxUnknownError`（`isinstance(e, OSError)`、`e.errno == errno.ETIMEDOUT`、`e.result == TxResult("unknown", 2, n, …)`） |
  | `test_posix_port_write_returns_txresult` | `_PosixSerialPort.write` 部分寫出後 EIO 回 `completed, k` |
  | `test_echo_paced_partial_logs_confirmed_slices_only` | echo-paced 停滯時單筆 TX 只含實際確認寫出的 slices（`UIO-5.3`），且 append 發生在持 `_write_lock` 期間（第二來源的 TX 紀錄排在其後） |
  | `test_write_all_legacy_none_return_is_completed` | 以兩參數 lambda（回 `None`）取代 `_write_all` → `send_bytes` 回 `TxResult("completed", n, n)`，不擲例外 |
  | `test_port_without_cancel_kwarg_still_works` | `port.write(self, payload) -> None` 的替身 → 寫入成功、未傳 `cancel` |
  | `test_stub_wal_returning_none_does_not_break_rx` | `append()` 回 `None` 的 stub WAL → `_handle_serial_rx` 仍送達 observer（`wal_seq is None`） |
  | `test_no_auto_resend_after_partial_in_file_transfer` | `file_transfer` 的分段寫入第 2 段得 `completed, k<n` 或 `unknown` → 該段 bytes 在 `port.writes` 中只出現一次（不重送），結果為錯誤 |
  | `test_no_auto_resend_after_partial_in_login_fsm` | login FSM 送帳號時得部分寫入 → 不重送該行，FSM 進入既有失敗分支 |
  | `test_echo_paced_oserror_keeps_confirmed_bytes_and_unknown_meta` | 第一個 slice 已確認，下一個 slice 擲普通 `OSError` → 一筆 TX 僅含前綴、一筆 unknown META，TX 帳本保留確認 bytes 與 unknown |
  | `test_echo_paced_logs_confirmed_bytes_on_unexpected_exception` | echo 等待在 slice 已確認後擲未預期例外 → finally 仍於鎖內寫出唯一一筆 TX 前綴並重拋原例外 |
  | `test_tx_account_updates_before_wal_append_failure` | TX WAL append 失敗 → 命令帳本仍保留 OS 已確認的 bytes |
  | `test_pyserial_plain_oserror_keeps_confirmed_prefix` | pyserial 已確認前綴後擲普通 `OSError` → `TxUnknownError.result.confirmed` 保留前綴 |
  | `test_echo_wait_returns_current_progress_when_tx_cancelled` | echo 尚未到達時設定 TX cancel → 等待立即回傳目前 acked 數 |
  | `test_legacy_on_rx_data_single_arg_still_supported` | `on_rx_data=lambda d: …`（舊簽章）仍可運作，不傳 `wal_seq` |
  | `test_background_capture_from_seq_uses_exact_seq` | background capture 的 `last_seq` 等於該 chunk 的 RX seq（多 COM 交錯下） |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_tx_outcome.py` → 預期 `ImportError: cannot import name 'TxResult'`（或斷言 `send_bytes` 回傳 `None`）。commit：`test(uart): BC-03／SC-07 TX 三態與確認 bytes 記錄失敗測試`
- [ ] **B1 實作**：
  - `_write_all`：`sent=0`；迴圈 `os.write`；`BlockingIOError`／EAGAIN → `if cancel and cancel.wait(EAGAIN_BACKOFF_S): return TxResult("completed", sent, n_req, "cancelled")`（`EAGAIN_BACKOFF_S=0.01`，`cancel` 為 None 時 `time.sleep(0.01)` 並同時檢查 `self._tx_cancel`，見 T07）；其他 `OSError` → `TxResult("completed", sent, n_req, f"{type(e).__name__}: {e}")`；`n<=0` → `"short_write"`；完成 → `TxResult("completed", n_req, n_req)`。
  - `_PosixSerialPort.write` 委派相同演算法（抽成模組函式 `_posix_write_all(fd, payload, cancel) -> TxResult`，`UARTBridge._write_all` 也呼叫它，消除重複實作）；`_PySerialPort.write`：迴圈中 `SerialTimeoutException`／`SerialException` → `raise TxUnknownError(TxResult("unknown", sent, n, f"{type(exc).__name__}: {exc}"), <現況 errno>) from exc`（`sent` 為已確認前綴）；`not n` → 回 `TxResult("completed", sent, n, "short_write")`；完成回 `completed, n`。
  - `send_bytes`：持 `_write_lock` 全程；`_state_lock` 內做 flash gate、T05 WAL 不健康檢查、T07 fence、raw deferred（T20 再加 permit 驗證）；鎖外（仍持 `_write_lock`）依相容規則 1／2 寫入取得 `result`（`TxUnknownError` 被捕捉後取其 `.result`，記錄完再重拋同一例外）；**仍持 `_write_lock`** 時先更新 `_tx_accounts[cmd_id]`（`cmd_id` 非 None 時；R1-F8：新 key 使表超過 512 時淘汰最舊且 `!= self._tx_active_cmd` 的項目並以 logger `serialwrap`、訊息模板 `tx account evicted (cmd_id=%s)` 記 WARNING，在途前景命令的累計永不被淘汰），再依序 append 已確認 TX bytes 與 unknown META；WAL append 失敗不得略過帳本更新。釋放鎖後 `if not result.complete and result.error not in (None, "deferred"): raise TxIncompleteError(result)`（unknown 重拋 `TxUnknownError`；serial 未就緒 raise `SerialNotReady`）；回傳 `result`。
  - `contracts/wal_meta_events.schema.json` 新增 `tx_outcome_unknown` 分支：`required: [event, requested_bytes, confirmed_bytes, unknown_range, error]`，整數非負、`unknown_range` 兩元素、`x-behavior-change: "BC-03"`。
  - `_handle_serial_rx`：`rec = self.wal.append(...)`；`seq = rec.get("seq")`；呼叫 `self._on_rx_data` 前以 `inspect.signature` 判定是否接受 `wal_seq` 關鍵字（結果以 callback 物件的 `id()` 快取，callback 被替換時重新判定；測試會在建構後直接指派 `_on_rx_data`），接受時 `self._on_rx_data(data, wal_seq=seq)`，否則 `self._on_rx_data(data)`。`SessionManager` 兩處 lambda 改為 `lambda data, wal_seq=None, sid=session_id: self._on_bridge_rx(sid, data, wal_seq)`；`_on_bridge_rx` 以 `wal_seq if wal_seq is not None else self._wal.current_seq` 傳給 observers 與 background capture。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_tx_outcome.py tests/test_uart_io.py tests/test_serial_port.py tests/test_interactive_raw.py tests/test_file_transfer.py tests/test_event_engine.py tests/test_issue159_bg_fast_capture.py tests/test_agent_defer_tx.py tests/test_human_agent_coexist.py tests/test_multiagent_e2e.py tests/test_uart_flash_bridge.py tests/test_refine_ownership.py tests/test_command_completion.py tests/test_console_unlock.py tests/test_concurrency_races.py tests/test_login_fsm.py`；`python3 -m tools.contracts check`；全套依 §0 第 4 點。
- **測試資產變更**（需獨立審閱，PR 註明）：
  - `tests/test_stage0_tx_outcome.py::test_no_auto_resend_after_partial_in_login_fsm`：修正新建測試的 `SessionProfile`／`SessionAuth` 欄位名稱，並在收到空 probe 時注入 login prompt，確保測試確實抵達帳號部分寫入分支；斷言不變，對應 BC-03。Reviewer A（Claude Sonnet, max）獨立確認原 fixture 會在驗證行為前以 TypeError 失敗、prompt 注入必要且斷言未放寬。
  - `tests/test_stage0_wal_unlink.py::test_flash_gate_precedes_wal_unhealthy_rejection`：依 BC-03 新介面將舊的 `None` 預期改為精確的 `TxResult("not_submitted", 0, requested)`，並延遲 import；Reviewer A（Claude Sonnet, max）確認該斷言符合凍結結果表且更嚴格。Reviewer B（Codex gpt-6.1-sol）對本 Task 無發現。
- [ ] **B3 commit**：`fix(uart): BC-03 TX 區分三態並只記確認寫出的 bytes，RX 觀察者改用精確 seq`

---

## Task T07：SC-04a Bridge 兩段式 Drain 與 `DrainReceipt`

- **範圍**：SC-04 的 bridge 層：Phase 1 停止新授權（fence）、喚醒 reader、取消 writer；Phase 2 等在途 I/O 收斂、關閉 handle、產生回條；WAL 不可寫時回條列出未落盤證據但不阻止關 handle；回條前不得重開。依據：規格 §5.2.3、§5.2.4、§5.2.5；盤點 06 §7.3、§12.2；arch §5.5。
- **Test author model**：haiku-xhigh　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-sc04-bridge-drain.md`（`type: fix`，`scope: uart`）：UART bridge 停止改為兩段式 drain：先凍結 TX 與取消等待，確認在途寫入收斂並關閉 handle 後才產生回條。
- **BLOCKED-OWNER**：USB-UART 拔插與驅動卡住造成 write 不返回的實機情境（3,000 ms 在雙平台實測，規格 §5.2.5「須在雙平台實測」）。

**Files**
- Create: `tests/test_stage0_bridge_drain.py`
- Modify: `sw_core/uart_io.py`

**Interfaces**
- Consumes：T06 `TxResult`、`_write_all(cancel=…)`；T02 `WalWriter`。
- Produces（Python）：
  ```python
  @dataclass(frozen=True)
  class DrainReceipt:
      epoch: int
      handle_closed: bool
      reader_stopped: bool
      writer_converged: bool
      tx_in_flight: TxResult | None
      unrecorded: tuple[str, ...]          # 例："TX cmd_id=c1 bytes=3 (WAL write failed: OSError: …)"
      preserved: PreservedConsoles | None
      error: str | None
  UARTBridge.begin_drain(self, epoch: int, *, preserve_consoles: bool, deadline: float | None = None) -> None
      # R3-F3：deadline 為 drain 共同期限（time.monotonic() 刻度）；None → 進入本函式、取任何鎖之前的 monotonic()+DRAIN_TIMEOUT_MS/1000
  UARTBridge.finish_drain(self, timeout_s: float, *,
                          on_late_receipt: Callable[[DrainReceipt], None] | None = None) -> DrainReceipt | None
      # R3-F3：實際期限 = min(begin_drain 記錄的 _drain_deadline, 呼叫當下 monotonic()+timeout_s)；不得從 finish 才重新起算
  UARTBridge.drain_pending -> bool          # property：begin 後、回條產生前為 True
  UARTBridge.stop(self, *, preserve_consoles: bool = False) -> PreservedConsoles | None
      # 保留簽章；= begin_drain(epoch=-1)＋finish_drain(DRAIN_TIMEOUT_MS/1000)；逾時時記 WARNING 並回 None（handle 由背景完成關閉）
  UARTBridge.start(self) -> None            # drain_pending 時 raise RuntimeError("drain in progress")
  ```
- Rust 對應：arch §5.5 Phase 1 步驟 1、3、7，Phase 2 步驟 1～5；`WorkerStoppedReceipt { epoch, stop_id, tx_outcomes, unrecorded, wal_error, handle_closed }`；arch §4.2 `CloseToken::close` 保證不雙重關閉。

**行為規格**
1. `begin_drain`：進入時、取任何鎖之前先算 `deadline`（未傳入時為 `time.monotonic() + DRAIN_TIMEOUT_MS/1000`），之後持 `_state_lock`：`_drain_deadline=deadline`（R3-F3：drain 共同期限從請求起算，涵蓋 Phase 1 等鎖時間；class 層預設 `float("inf")`，相容 bare bridge；重複呼叫去重時不改寫）、`_tx_frozen=True`、`_drain_epoch=epoch`、`_drain_pending=True`、`_preserve_on_drain=preserve_consoles`；`_stop_event.set()`；`_tx_cancel.set()`（`_write_all` 在 EAGAIN backoff 檢查此事件）；BC-02 的 `invalidate_deferred("drain")`（T09 實作；T07 先呼叫一個 no-op 鉤子 `self._on_drain_invalidate()`，T09 換成實作）。重複呼叫為 no-op（去重）。
2. fence：`send_bytes` 在 `_state_lock` 內 `if self._tx_frozen: result = TxResult("not_submitted", 0, len(payload), "draining")` 並 raise `TxIncompleteError`。此處只是 pre-check：**只有凍結前已實際提交 OS 的 bytes**（T20 起：在 `_tx_fence_lock` 臨界區內完成 `os.write` 提交者）才視為已提交、照實記錄；通過 pre-check 但尚未提交的寫入，於 T20 的提交點重驗 `_tx_frozen` 而零 TX（R0-F1，T20 `test_release_by_other_thread_after_authorize_before_submit_zero_tx`）。T07 至 T20 之間此窗口為已知缺口，不得以本 Task 測試宣稱已關閉。
3. `finish_drain(timeout_s)`：`deadline = min(self._drain_deadline, monotonic()+timeout_s)`（R3-F3：期限自 `begin_drain` 起算，不從 finish 重新計時；已過期時剩餘時間為 0，下列等待都以 `timeout=0` 嘗試一次）；join reader thread（剩餘時間）；join console thread（剩餘時間）——沿用現行 `stop()` 的守衛：呼叫者即該 thread 本身時不 join（該 thread 於回呼返回後自行結束，`reader_stopped` 以 `_stop_event` 已設且 `_loop` 不再取新資料判定）；`acquired = self._write_lock.acquire(timeout=max(0, remaining))`；三者都完成 → 在 `_state_lock` 內抽出 preserved 與待關 clients（同現行 `stop()` 第 2 步）、清欄位；鎖外 `port.close()` 恰一次、listener close、client fds close；`release _write_lock`；組回條（`handle_closed=True`；`port.close()` 擲例外時 `handle_closed=False`、`error=str(exc)`）。任一未在期限內完成 → 啟動 daemon thread `serialwrap-drain-<com>` 以無期限方式完成同樣步驟，完成後呼叫 `on_late_receipt(receipt)`，本呼叫回 `None`。
4. 回條的 `unrecorded`：Phase 2 期間 WAL append 回傳 `loss_flag=True` 的 TX／RX 以字串列出；`tx_in_flight` 為凍結當下在途寫入的 `TxResult`（無則 None）。
5. `_drain_pending` 只在**成功**回條（`handle_closed and writer_converged and reader_stopped`）產生後才清除；`port.close()` 失敗的回條不清除 `_drain_pending`（handle 狀態不明，不得重開）。`start()` 在 `_drain_pending` 時拒絕。`stop()` 被呼叫兩次：第二次回傳 `None`（與基線相同：第一次已清空 clients，避免同一批 consoles 被重複歸屬），`port.close()` 不重複。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_bridge_drain.py`
  ```python
  from __future__ import annotations
  import errno
  import threading
  import time
  import pytest
  import sw_core.uart_io as uio
  from sw_core.uart_io import DrainReceipt, TxIncompleteError
  from stage0_fakes import FakePort, make_bridge

  def test_begin_drain_freezes_new_tx(tmp_path):
      port = FakePort(); bridge, _ = make_bridge(tmp_path, port=port)
      bridge.begin_drain(7, preserve_consoles=False)
      with pytest.raises(TxIncompleteError) as ei:
          bridge.send_bytes(b"x", source="agent:x")
      assert ei.value.result.outcome == "not_submitted" and ei.value.result.error == "draining"
      assert port.writes == []

  def test_idle_drain_returns_receipt_and_closes_once(tmp_path):
      port = FakePort(); bridge, _ = make_bridge(tmp_path, port=port)
      bridge.begin_drain(7, preserve_consoles=False)
      r = bridge.finish_drain(1.0)
      assert isinstance(r, DrainReceipt)
      assert (r.epoch, r.handle_closed, r.writer_converged, r.reader_stopped) == (7, True, True, True)
      assert port.close_calls == 1 and bridge.drain_pending is False

  def test_inflight_write_delays_receipt_until_converged(tmp_path):
      gate = threading.Event(); port = FakePort(block=gate); bridge, _ = make_bridge(tmp_path, port=port)
      t = threading.Thread(target=lambda: bridge.send_bytes(b"abc", source="agent:x", cmd_id="c1")); t.start()
      time.sleep(0.05)
      late: list[DrainReceipt] = []
      got_late = threading.Event()
      bridge.begin_drain(3, preserve_consoles=False)
      r = bridge.finish_drain(0.1, on_late_receipt=lambda rc: (late.append(rc), got_late.set()))
      assert r is None and bridge.drain_pending is True and port.close_calls == 0
      with pytest.raises(RuntimeError):
          bridge.start()
      gate.set(); t.join(1.0)
      assert got_late.wait(2.0)
      assert late[0].handle_closed is True and late[0].epoch == 3
      assert port.writes == [b"abc"] and port.close_calls == 1

  def test_eagain_writer_is_cancelled_by_drain(tmp_path, monkeypatch):
      bridge, _ = make_bridge(tmp_path); bridge._serial = FakePort(); bridge._serial_fd = 99
      monkeypatch.setattr(uio.os, "write", lambda fd, d: (_ for _ in ()).throw(BlockingIOError(errno.EAGAIN, "again")))
      errs: list[BaseException] = []
      t = threading.Thread(target=lambda: _capture(errs, bridge)); t.start()
      time.sleep(0.05)
      bridge.begin_drain(1, preserve_consoles=False)
      r = bridge.finish_drain(1.0)
      t.join(1.0)
      assert r is not None and r.writer_converged is True
      assert isinstance(errs[0], TxIncompleteError) and errs[0].result.error == "cancelled"

  def _capture(errs, bridge):
      try:
          bridge.send_bytes(b"zzz", source="agent:x")
      except BaseException as e:  # noqa: BLE001
          errs.append(e)

  def test_stop_twice_closes_port_once(tmp_path):
      port = FakePort(); bridge, _ = make_bridge(tmp_path, port=port)
      bridge.stop()
      assert bridge.stop() is None          # 基線語意：第二次無 preserved
      assert port.close_calls == 1
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_receipt_lists_unrecorded_when_wal_write_fails` | WAL fs 全失敗時在途寫入完成 → 回條 `handle_closed is True`、`unrecorded` 含 `"TX cmd_id=c1 bytes=3"` 前綴字串 |
  | `test_begin_drain_twice_is_noop` | 第二次 `begin_drain(9, …)` 不改 `epoch`（回條仍為第一次的 epoch） |
  | `test_preserve_consoles_returned_in_receipt` | PTY bridge `start()` 後 attach 一個 console，`begin_drain(…, preserve_consoles=True)` → 回條 `preserved.clients` 含該 client、其 fd 未關閉（`posix_pty`） |
  | `test_port_close_error_reports_handle_not_closed` | `FakePort.close` 擲 `OSError` → `handle_closed is False`、`error` 非空、`drain_pending` 仍為 True |
  | `test_reader_thread_stops_within_poll` | PTY bridge `start()` 後 drain，回條 `reader_stopped is True` 且耗時 < 1.0 s（`posix_pty`） |
  | `test_finish_drain_deadline_counts_from_begin`（R3-F3，舊「finish 才起算」實作必紅） | `FakePort(block=gate)` 的 writer 阻塞中；`t0 = time.monotonic()`；`bridge.begin_drain(3, preserve_consoles=False, deadline=t0 + 0.3)`；`time.sleep(0.25)`；`r = bridge.finish_drain(1.0)` → `r is None`、`time.monotonic() - t0 < 0.45`（舊實作要等到 t0+1.25 s）、`bridge.drain_pending is True`、`port.close_calls == 0`；`begin_drain` 未帶 `deadline` 時 `bridge._drain_deadline` 落在 `[t_call + DRAIN_TIMEOUT_MS/1000, t_ret + DRAIN_TIMEOUT_MS/1000]`（`t_call`／`t_ret` 為呼叫前後的 monotonic）；`gate.set()` 後 late receipt 照常產生、`port.close_calls == 1` |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_bridge_drain.py` → 預期 `ImportError: cannot import name 'DrainReceipt'`。commit：`test(uart): SC-04 bridge 兩段式 drain 失敗測試`
- [ ] **B1 實作**：依行為規格；`_write_lock` 維持 `threading.Lock`（`acquire(timeout=)` 可用）；`stop()` 改為 `begin_drain(-1, preserve_consoles=…)`＋`finish_drain(DRAIN_TIMEOUT_MS/1000)`，`None` 時 log WARNING `"serialwrap: bridge %s drain 逾時，handle 於背景關閉"` 並回 `None`（呼叫端原本對 `None` 的處理即「無 preserved」）。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_bridge_drain.py tests/test_uart_io.py tests/test_suspend_resume_reentrant.py tests/test_session_console_endpoint.py tests/test_thread_survival.py tests/test_concurrency_races.py tests/test_uart_flash_bridge.py tests/test_device_handoff.py`；全套依 §0 第 4 點。
- **測試資產變更**：無。
- [ ] **B3 commit**：`fix(uart): SC-04 bridge 停止改為兩段式 drain 並產生 handle 關閉回條`

---

## Task T08：SC-04b Session Drain、release／attach 交接回應、drain 診斷欄位、BC-03 中止映射、SC-11 有序關機

- **範圍**：SC-04 session 層：斷線、I/O error、release、clear、bind 的 detach 期間為 `RECOVERING`；release 等回條才成功；3,000 ms 逾時 `drain_stalled`／`drain_timeout_epoch`；回條遲到仍完成；去重；drain 中拒絕 attach／重開。`SessionPublic` 在 `capture` 後新增 `drain_stalled`、`drain_timeout_epoch`。BC-03 中止映射（`COMMAND_ABORTED_IN_FLIGHT`／`SEND_FAILED`／`FLUSHED_BY_RECOVERY`，不自動重送）。SC-11 有序關機（drain 所有 worker→關 handle→WAL sync→exit）；SC-11 交接停止 `daemon.handoff_stop`（共同 admission 邊界內原子 busy 判定＋凍結新操作＋token，供 §12.1 更新／回滾步驟 2～3；R1-F1，見本 Task 末段 C1～C5）。依據：規格 §12.1、§5.2.4、§5.2.5、§5.3、§6.2 交接、§9.4 Drain 列；調和表 SC-04、owner 確認項 #4 預設、ERR-7.3-9；arch §5.5、§5.7、§5.9、§6.1 停止順序。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-sc04-session-drain-bc03.md`（`type: feat`，`scope: session`）：release／recover／斷線改為 drain barrier：期間狀態為 RECOVERING，handle 關閉回條後才完成 release；逾時回報 `drain_stalled`；已確認寫出 ≥1 byte 後中止的命令回 `COMMAND_ABORTED_IN_FLIGHT`；daemon 關機先 drain 再同步 WAL。
- **BLOCKED-OWNER**：(1) L2 F10 `device release`／ThrowawayDaemon 交接案例在實機 bench 重跑；(2) 程序中斷測試（release 持久化提交前後 kill daemon）在實機 bench 驗證 §6.2「重啟仍保持 released」；(3) Windows 原生 COM 上 drain 時限實測（OD-08）。

**Files**
- Create: `tests/test_stage0_session_drain.py`
- Modify: `sw_core/session_manager.py`、`sw_core/uart_io.py`（`wait_for_regex_from` 遇 drain 立即返回）、`sw_core/arbiter.py`（in-flight 命令結果映射時保留 `error_code`；無新欄位）、`sw_core/service.py`（`stop()` 有序關機、`device.release` 回應原樣傳出）、`sw_core/daemon.py`（`BLOCKING_RPC_METHODS` 加入 `device.release`、`device.attach`、`session.clear`、`session.bind`；停止順序不變，確認 `service.stop()` 在 `lock.release()` 前完成；非優雅停止時的 log）、`sw_core/cli.py`（`_LONG_RPC_METHODS` 加入同四個方法）、`tests/test_rpc_blocking_offload.py`（新增案例，非既有斷言變更）、`sw_core/constants.py`（`DRAIN_TIMEOUT_MS` 已於 T02 建立）、`contracts/behavior_changes.yaml`（SC-04、BC-03、SC-11 → `IMPLEMENTED`；不寫 `contract_revision`）、`contracts/error_codes.yaml`（`COMMAND_ABORTED_IN_FLIGHT`：`status: IMPLEMENTED`、`reachable: true`、`scope.interfaces: [rec]`、`retryable: "no"`（字串）、`contract_tests` 指向本 Task 斷言該碼的 test node；P0 C03／C09／C16 須通過）、`contracts/rpc_schema.json`（`x-pending` 的 SC-04 `SessionPublic` 兩欄併入正式欄位並刪除該 `x-pending` 項；`device.release`／`device.attach` 回應 schema 補 drain 逾時形狀；四方法的 blocking／long-timeout 旗標）、`contracts/cli_schema.json`（`x-timeouts.long_methods` 同步）、`contracts/wal_meta_events.schema.json`（新增 `command_aborted_in_flight` 分支）

**相容規則（凍結）**：`SessionManager._detach_session_locked(session, *, reason, drop_consoles=False)` **保留**為同步薄包裝，行為維持「呼叫 `bridge.stop(preserve_consoles=not drop_consoles)`」，供既有直接呼叫它的測試（`tests/test_device_handoff.py:81,95,140,457`、`tests/test_bootloader_recovery.py:861`、`tests/test_refine_ownership.py:865`、`tests/test_session_capture.py:145`、`tests/test_session_bind.py:1038`）與非 drain 路徑使用；drain barrier 另立 `_begin_drain_locked`／`_await_drain`／`_complete_drain`，五個 detach 呼叫點改走後者。因此**測試資產變更：無**。

**Interfaces**
- Consumes：T07 `begin_drain`／`finish_drain`／`DrainReceipt`；T06 `tx_account`、`TxIncompleteError`；T02 `WalWriter.close()`。
- Produces（Python）：
  ```python
  # _ForegroundOperation（frozen dataclass）新增欄位；所有建立點（session_manager.py 現況 3 處：
  # execute_command、file 傳輸、self-test）都必須明確傳入
  cmd_id: str | None = None                       # execute_command 傳入 arbiter 的 cmd_id；file／self-test 為 None
  kind: Literal["command", "file", "self_test"] = "command"
  SessionManager._begin_foreground_operation_locked(self, session, *, kind: str, cmd_id: str | None = None,
                                                    expected_duration_s: float | None = None) -> tuple[...]
  SessionManager._draining_by_ids: dict[str, _DrainJob]   # by_id → 尚未收到成功回條的 drain；含非五個呼叫點的直接 stop
  # SessionRuntime 新欄位（mutable dataclass）
  drain_stalled: bool = False
  drain_timeout_epoch: int | None = None
  _pending_drain: "_DrainJob | None" = None
  @dataclass
  class _DrainJob:
      session_id: str
      bridge: UARTBridge
      epoch: int                                  # Phase 1 當下的 session.bridge_generation
      kind: Literal["release", "recover", "io_error", "device_removed", "clear", "bind", "shutdown"]
      reason: str                                 # 寫入 last_error 的值（如 "RELEASED"、"BRIDGE_DOWN:<r>"）
      drop_consoles: bool
      aborted_cmd_id: str | None                  # Phase 1 撤銷的 foreground 命令
      done: threading.Event
      deadline: float                             # R3-F3：started_at + DRAIN_TIMEOUT_MS/1000（monotonic；shutdown 例外見下）；bridge.begin_drain 與 _await_drain 共用
      receipt: DrainReceipt | None = None
  SessionManager._begin_drain_locked(self, session: SessionRuntime, *, kind: str, reason: str,
                                     drop_consoles: bool, started_at: float) -> _DrainJob | None
      # R3-F3：started_at 為呼叫點進入方法、取 self._lock 之前的 time.monotonic()；kind="shutdown" 時 job.deadline 改為 SC-11 的共用期限（進入 shutdown_drain 的時點＋total_timeout_s），不另縮短為 3 s
  SessionManager._await_drain(self, job: _DrainJob) -> DrainReceipt | None   # 不持 self._lock；逾時設 stalled
  SessionManager._complete_drain(self, job: _DrainJob, receipt: DrainReceipt) -> None
  SessionManager.release_device(self, selector: str, *, source: str = "cli", reason: str | None = None) -> dict[str, Any]
  SessionManager.begin_shutdown(self) -> None
      # SC-11 stopping gate（R1-F2）：持 self._lock 設 self._shutting_down=True，冪等；之後 update_devices 建動態 session、_spawn_attach、
      # _attach_by_id／_attach_by_id_dynamic、attach_device、bind／clear 後置重開一律不建 UARTBridge（自動路徑 log `shutdown 中，略過 attach <by_id>`；
      # RPC 路徑 raise RuntimeError("daemon stopping")，RPC 層轉 EXCEPTION）
  SessionManager.shutdown_drain(self, *, total_timeout_s: float = 5.0) -> dict[str, Any]
      # {"graceful": bool, "stalled": [session_id…]}；graceful＝所有 drain 皆得成功回條
  SessionManager._map_aborted_result(self, cmd_id: str, result: dict[str, Any] | BaseException, *,
                                     bridge: UARTBridge, aborted_by_drain: bool) -> dict[str, Any]
      # 由 bridge.tx_account(cmd_id) 取累計；無測試專用接縫，測試直接預填 bridge._tx_accounts
  # SessionRuntime.to_public_dict()：在 "capture" 之後依序插入 "drain_stalled"、"drain_timeout_epoch"
  ```
- Rust 對應：arch §5.5（StopOp 去重 `pending_stop`、Phase 1 步驟 1～8、回條驗證、逾時處置）、§5.7（`confirmed_bytes ≥ 1` → `COMMAND_ABORTED_IN_FLIGHT`；全 `NotSubmitted`／`Completed(0)` → `FLUSHED_BY_RECOVERY`；`Unknown` 且 0 → `SEND_FAILED`）、§5.9 release 持久化順序、§6.1 停止順序。

**回應形狀（凍結；owner 確認項 #4 預設）**

| 情境 | 回應 |
|---|---|
| release 成功（回條 ≤ 3,000 ms） | `{"ok":true,"session":…,"closed_consoles":int,"aborted_cmd":bool}`（與現況相同） |
| release drain 逾時 | `{"ok":false,"error_code":"SESSION_RECOVERING","drain_stalled":true,"drain_timeout_epoch":<bridge_generation>,"session":…}` |
| drain 中再次 release | 同上一列（不啟動第二組關閉） |
| 回條遲到完成後再次 release | `{"ok":true,"already_released":true,"session":…}` |
| drain 中 `device.attach` | `{"ok":false,"error_code":"SESSION_RECOVERING","drain_stalled":<bool>,"drain_timeout_epoch":<int\|null>,"session":…}` |
| release 持久化失敗 | 現況：例外穿出 → framing `EXCEPTION`（不回成功）；記憶體已 `RELEASED`、`_released_by_ids` 已含 by_id，繼續阻擋自動 TX 與自動 attach（`RPC-H-11`、`WAL-D10`、規格 §6.2 交接）。若此時是遲到回條（`on_late_receipt` 背景 thread），例外不穿越 thread：log ERROR `serialwrap: release 持久化失敗（session=%s）：%s`，記憶體同樣維持 `RELEASED` 並記 `_release_persist_pending=True`；下一次 `device.release` 走到 `already_released` 分支時先重試 `_save_state()` 一次，成功才回 `{"ok":true,"already_released":true,…}`，失敗同樣穿出 `EXCEPTION` |

**狀態流（凍結）**

| 觸發 | Phase 1（持 `_lock`） | 回條後 |
|---|---|---|
| release | `RECOVERING`；`_released_by_ids.add(by_id)`（立即阻擋自動 attach）；accepted → `FLUSHED_BY_RECOVERY` | 成功回條後：先在記憶體設 `RELEASED`（`released_by`／`released_at`／`released_reason`）、`drain_stalled=False`，**再** `_save_state()`（`_save_state` 由 `state=="RELEASED"` 的 session 組出 released 列，與基線同序）；`_save_state()` 成功才回 ok，失敗依回應表穿出 `EXCEPTION` 且記憶體維持 `RELEASED`（arch §5.9「寫入失敗時記憶體仍標 RELEASED」） |
| `_handle_bridge_down`（io_error） | `RECOVERING`、`last_error="BRIDGE_DOWN:<r>"` | 裝置在 → `ATTACHING`＋`_spawn_attach`；否則 `DETACHED`（`last_error` 依現況覆寫規則） |
| `_detach_by_id`（device_removed） | `RECOVERING` | `DETACHED`、`DEVICE_REMOVED`／`DEVICE_REBOUND_REQUIRED`；改 path 者再 `_spawn_attach` |
| `clear_session` | `RECOVERING` | 裝置在 → `ATTACHING` |
| `bind_session` | `RECOVERING` | 依 `bind_session` 現況的後置規則（新 by_id 在線 → `ATTACHING`＋`_spawn_attach(new_by_id)`；否則 `DETACHED`、`last_error` 依現況），只是延後到成功回條之後執行 |
| shutdown | 不改公開狀態；accepted → `FLUSHED_BY_SHUTDOWN` | 全部成功回條 → `WalWriter.close()`（sync＋close）、`graceful:true`；5 s 總上限內未全部收斂 → **不得**宣稱優雅停止：`WalWriter.sync()` 但**不 close**（仍有 worker 可能 append，關閉會把其 I/O 證據改記為遺失）、`graceful:false`、log ERROR `serialwrapd: 有序關機逾時，未收斂 session=%s（非優雅停止，SC-11 不計 PASS）`；之後行程結束屬強制終止，不計為 SC-11 PASS |
| 任何直接 `bridge.stop()`（`_attach_by_id`／`_attach_by_id_dynamic` 的 attach 失敗收尾等 8 處，見 B1） | 不改公開狀態（沿用各自現況） | 收到成功回條前 by_id 留在 `_draining_by_ids`，`_spawn_attach`／`_attach_by_id`／`attach_device` 見到即不開新 bridge |

**BC-03 中止映射（凍結）**

| `tx_account(cmd_id)` | 是否被 drain 撤銷 | 命令紀錄 |
|---|---|---|
| confirmed ≥ 1 | 是 | `status:"error"`、`error_code:"COMMAND_ABORTED_IN_FLIGHT"` |
| confirmed ≥ 1，因 `TxIncompleteError`（非 drain，例如寫到一半 I/O error） | 否 | `COMMAND_ABORTED_IN_FLIGHT` |
| confirmed = 0 且 unknown | 是／否 | `SEND_FAILED`（沿用既有映射，ERR-7.3-9） |
| confirmed = 0 且無 unknown | 是 | `FLUSHED_BY_RECOVERY` |
| confirmed = 0 且無 unknown | 否 | 維持現況（`SEND_FAILED`） |

每筆 `COMMAND_ABORTED_IN_FLIGHT` 另寫 WAL META `{"event":"command_aborted_in_flight","confirmed_tx_bytes":N,"tx_outcome":"complete"|"partial"|"unknown"}`（`source:"system"`、`cmd_id` 為該命令）；`partial` 表示最後一次寫入未全數完成，`unknown` 表示已確認 N≥1 bytes 後結果不明（T06 pyserial 前綴）。「confirmed ≥ 1」列不論最後結果為 completed 或 unknown 都適用。任何路徑都不重送。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_session_drain.py`
  ```python
  from __future__ import annotations
  import json
  import threading
  import time
  import pytest
  import sw_core.constants as const
  import sw_core.session_manager as sm_mod
  from stage0_fakes import FakePort, make_manager, make_profile, wal_records

  BY_ID = "/dev/serial/by-id/X"

  @pytest.fixture
  def short_drain(monkeypatch):
      monkeypatch.setattr(const, "DRAIN_TIMEOUT_MS", 150)
      monkeypatch.setattr(sm_mod, "DRAIN_TIMEOUT_MS", 150, raising=False)

  def _ready_with_port(tmp_path, port):
      from sw_core.uart_io import UARTBridge
      from sw_core.config import UartProfile
      mgr = make_manager(tmp_path, [make_profile(by_id=BY_ID)])
      s = mgr.get_session("COM0")
      b = UARTBridge(com="COM0", device_path="fake", profile=UartProfile(), wal=mgr._wal)
      b._serial = port; b._serial_fd = None
      with mgr._lock:
          s.bridge = b; s.state = "READY"; s.bridge_generation = 4
      return mgr, s, b

  def test_release_transitions_recovering_then_released(tmp_path):
      gate = threading.Event(); port = FakePort(block=gate)
      mgr, s, b = _ready_with_port(tmp_path, port)
      t = threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x", cmd_id="c1")); t.start()
      time.sleep(0.05)
      out: dict = {}
      rt = threading.Thread(target=lambda: out.update(mgr.release_device("COM0"))); rt.start()
      time.sleep(0.05)
      assert mgr.get_session("COM0").state == "RECOVERING"
      assert BY_ID in mgr._released_by_ids
      gate.set(); rt.join(3.0); t.join(1.0)
      assert out["ok"] is True and mgr.get_session("COM0").state == "RELEASED"
      assert port.close_calls == 1
      state = json.load(open(mgr._state_path, encoding="utf-8"))
      assert "p:COM0" in state["released"]

  def test_release_drain_timeout_shape(tmp_path, short_drain):
      gate = threading.Event(); port = FakePort(block=gate)
      mgr, s, b = _ready_with_port(tmp_path, port)
      threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
      time.sleep(0.05)
      r = mgr.release_device("COM0")
      assert {k: r[k] for k in ("ok", "error_code", "drain_stalled", "drain_timeout_epoch")} == {
          "ok": False, "error_code": "SESSION_RECOVERING", "drain_stalled": True, "drain_timeout_epoch": 4}
      assert r["session"]["state"] == "RECOVERING" and r["session"]["drain_stalled"] is True
      r2 = mgr.release_device("COM0")
      assert r2["error_code"] == "SESSION_RECOVERING"
      a = mgr.attach_device("COM0")
      assert a["ok"] is False and a["error_code"] == "SESSION_RECOVERING"
      gate.set()
      for _ in range(100):
          if mgr.get_session("COM0").state == "RELEASED":
              break
          time.sleep(0.02)
      assert mgr.get_session("COM0").state == "RELEASED"
      assert mgr.get_session("COM0").to_public_dict()["drain_stalled"] is False
      assert mgr.release_device("COM0")["already_released"] is True
      assert port.close_calls == 1

  def test_public_dict_drain_fields_follow_capture(tmp_path):
      mgr = make_manager(tmp_path, [make_profile(by_id=BY_ID)])
      keys = list(mgr.get_session("COM0").to_public_dict().keys())
      i = keys.index("capture")
      assert keys[i + 1:i + 3] == ["drain_stalled", "drain_timeout_epoch"]
      d = mgr.get_session("COM0").to_public_dict()
      assert d["drain_stalled"] is False and d["drain_timeout_epoch"] is None

  @pytest.mark.parametrize("account,expected", [
      ((5, False), "COMMAND_ABORTED_IN_FLIGHT"),     # 全數寫出後被中止
      ((2, False), "COMMAND_ABORTED_IN_FLIGHT"),     # 部分寫出
      ((2, True), "COMMAND_ABORTED_IN_FLIGHT"),      # 已確認 2 bytes 後結果不明
      ((0, True), "SEND_FAILED"),
      ((0, False), "FLUSHED_BY_RECOVERY"),
  ])
  def test_bc03_abort_mapping(tmp_path, account, expected):
      mgr, s, b = _ready_with_port(tmp_path, FakePort())
      if account != (0, False):
          b._tx_accounts["c1"] = account                # 預填 T06 的累計表（公開路徑：_map_aborted_result 經 tx_account 讀取）
      res = mgr._map_aborted_result("c1", {"ok": False}, bridge=b, aborted_by_drain=True)
      assert res["ok"] is False and res["error_code"] == expected
      if expected == "COMMAND_ABORTED_IN_FLIGHT":
          metas = [r for r in wal_records(mgr._wal) if r["dir"] == "META"]
          assert metas[-1]["meta"]["event"] == "command_aborted_in_flight"
          assert metas[-1]["cmd_id"] == "c1" and metas[-1]["meta"]["confirmed_tx_bytes"] == account[0]
  ```
  > `_tx_accounts` 的值形狀為 `(confirmed_bytes, saw_unknown)`，與 T06 `tx_account()` 回傳一致；本測試不使用任何測試專用接縫。

  表格補充（同檔）：

  | 測試名 | 斷言 |
  |---|---|
  | `test_bridge_down_goes_recovering_then_attaching` | `_handle_bridge_down` 期間 state 為 `RECOVERING`；回條後裝置在線 → `ATTACHING`，`_spawn_attach` 被呼叫一次 |
  | `test_device_removed_goes_recovering_then_detached` | `update_devices` 移除裝置 → 先 `RECOVERING`，回條後 `DETACHED`、`last_error == "DEVICE_REMOVED"` |
  | `test_clear_and_bind_use_recovering_during_drain` | `clear_session`／`bind_session` 在 writer 阻塞時 state 為 `RECOVERING` |
  | `test_duplicate_release_starts_single_drain` | 兩執行緒同時 release → `bridge.begin_drain` 只被呼叫一次（spy） |
  | `test_no_reopen_before_receipt` | drain 未完成時 `_spawn_attach(BY_ID)` 不建立新 bridge（`_attach_by_id` 檢查 `_pending_drain` 與 `_draining_by_ids`） |
  | `test_direct_bridge_stop_timeout_blocks_reopen` | `_attach_by_id` attach 失敗收尾的直接 `bridge.stop()` 逾時（writer 阻塞）→ by_id 在 `_draining_by_ids`、`_spawn_attach(BY_ID)` 不建新 bridge；回條遲到後才可再 attach，preserved consoles 交給 retained 而非洩漏 |
  | `test_failed_close_receipt_keeps_recovering` | `FakePort.close` 擲 `OSError` → 回條 `handle_closed is False`：session 維持 `RECOVERING`、`_pending_drain` 不清、release 回 `SESSION_RECOVERING`（不得 ok）、`attach_device` 不開新 handle、`drain_stalled` 維持 True |
  | `test_release_drain_deadline_includes_lock_wait`（R3-F3，舊「finish 才起算」實作必紅） | `short_drain`（150 ms）；`FakePort(block=gate)` 的 writer 阻塞中；helper thread 持 `mgr._lock` 0.3 s 後放開；主 thread 於 helper 取得鎖後 `t0 = time.monotonic()` 再呼叫 `mgr.release_device("COM0")` → 回 `SESSION_RECOVERING`＋`drain_stalled is True`、`drain_timeout_epoch == 4`，且 `time.monotonic() - t0 < 0.4`（舊實作為 0.3＋0.15 s 以上）；`port.close_calls == 0`、`attach_device("COM0")` 回 `SESSION_RECOVERING`；`gate.set()` 後 2 s 內 `RELEASED`、`port.close_calls == 1` |
  | `test_aborted_command_not_resent` | 被中止命令的 cmd_id 在 WAL 中只有原本的 TX 紀錄，無重複 payload；`port.writes` 中該 payload 只出現一次 |
  | `test_drain_abort_mapping_survives_settle_race`（R1-F8） | 真實 arbiter：`c_done` 完成（已 `tx_settle`）後 `c_fg`（`timeout_s=30`）寫出 2 bytes 並等待 prompt；release 與 `bridge.tx_settle("c_done")` 由兩執行緒同時呼叫（barrier）→ `c_fg` 紀錄 `error_code == "COMMAND_ABORTED_IN_FLIGHT"`、META `confirmed_tx_bytes == 2`；`c_done` 紀錄不變；之後 `bridge._tx_accounts` 不含 `c_fg`、`c_done`（drain 映射讀後移除＋結算），且 `c_fg` 在映射前從未被 `tx_settle`（spy） |
  | `test_running_command_aborted_by_release_records_code` | 真實 arbiter、`timeout_s=30` 的命令寫出 ≥1 byte 後 release → **1 s 內** `command.get` 的 `error_code == "COMMAND_ABORTED_IN_FLIGHT"`、`status == "error"`（`wait_for_regex_from` 遇 drain 立即返回、不進 CTRL_C 恢復路徑） |
  | `test_late_completion_does_not_touch_new_session` | 舊命令等待中 release → 回條 → `attach_device` → 新命令開始 → 舊命令的 inner 才返回：新 session 的 `state`、`capture`、lease、`last_error`、`foreground_busy` 皆不被舊命令改動，舊命令紀錄為 `COMMAND_ABORTED_IN_FLIGHT`／`FLUSHED_BY_RECOVERY`，其 I/O 證據仍在 WAL |
  | `test_reject_paths_have_zero_side_effects` | drain 中 `attach_device`、重複 release、drain 中 `command.submit` 的拒絕回應之外：`port.writes == []`、WAL 無新 TX、`state.json` 未改寫 |
  | `test_shutdown_drains_then_closes_wal` | `service.stop()` 呼叫順序（spy）為：各 bridge `begin_drain` → `finish_drain` → `WalWriter.close`；accepted 命令 `FLUSHED_BY_SHUTDOWN`；`shutdown_drain()` 回 `{"graceful": True, "stalled": []}` |
  | `test_shutdown_gate_blocks_hotplug_after_drain_start`（R1-F2，修正前必 FAIL） | READY 的 COM0；spy 在 `shutdown_drain` 的第一個 `_begin_drain_locked(kind="shutdown")` 時呼叫 `update_devices` 加入新 `/s/by-id/usb-NEW`、再呼叫 `_spawn_attach("/s/by-id/usb-NEW")` → `FakePort` 開檔次數對 usb-NEW 為 0、未建 `UARTBridge`、`session.list` 無新動態 session；log 含 `shutdown 中，略過 attach /s/by-id/usb-NEW`；`attach_device("COM0")` raise `RuntimeError("daemon stopping")`；`shutdown_drain()` 回 `{"graceful": True, "stalled": []}` |
  | `test_shutdown_waits_inflight_attach_before_wal_close`（R1-F2） | `_attach_by_id("/s/by-id/usb-NEW")` thread 卡在 `FakePort.open`（gate 前已啟動）時呼叫 `service.stop()`，0.2 s 後放行 → spy 順序為「該 thread 結束（bridge 已 `stop()`、port.writes == []）」→ `WalWriter.close`；`WalWriter.close` 後 WAL 無新 append；`service.stop()` 順序 spy 首項為 `begin_shutdown`，且在 `engine.stop`、`watcher.stop` 之前 |
  | `test_shutdown_timeout_is_reported_not_graceful` | 一個 writer 永久阻塞時 `service.stop()` 在 5.5 s 內返回，但 `shutdown_drain()` 回 `graceful is False`、`stalled` 含該 session；`WalWriter.sync` 被呼叫、`WalWriter.close` **未**被呼叫（`wal.closed is False`）；log 含「非優雅停止」 |
  | `test_release_persist_failure_keeps_blocking` | `_save_state` 擲 `OSError` → 例外穿出、記憶體 `RELEASED`、`BY_ID in _released_by_ids`、之後 `_spawn_attach(BY_ID)` 不 attach、`send_bytes(source="system:x")` 無 bridge 可寫 |
  | `test_release_rpc_offloaded_from_event_loop` | （`tests/test_rpc_blocking_offload.py` 新案例）`device.release` 阻塞於 drain 期間，同一 daemon 的 `health.ping` 在 0.5 s 內回應 |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_session_drain.py` → 預期 `KeyError: 'drain_stalled'`／state 斷言 `'DETACHED' != 'RECOVERING'`。commit：`test(session): SC-04 drain barrier、BC-03 中止映射與有序關機失敗測試`
- [ ] **B1 實作**：
  - 新增 `_begin_drain_locked`（Phase 1：記 retained human owner、`aborted_cmd_id = session._foreground_operation.cmd_id if session._foreground_operation is not None else None`、撤銷 foreground token、`bridge.begin_drain(session.bridge_generation, preserve_consoles=not drop_consoles, deadline=job.deadline)`（R3-F3：`job.deadline = t0 + DRAIN_TIMEOUT_MS/1000`，`t0` 為該 detach 呼叫點**進入方法、取 manager `_lock` 之前**的 `time.monotonic()`，由呼叫點傳入 `_begin_drain_locked(..., started_at=t0)`；去重命中既有 job 時沿用舊 job 的期限）、`state="RECOVERING"`、pop lease、停止 capture、`on_detached(sid)`（accepted → `FLUSHED_BY_RECOVERY`；shutdown 改由 `unregister_session(sid, error_code="FLUSHED_BY_SHUTDOWN")`）、`session._pending_drain=job`、`_draining_by_ids[by_id]=job`）與 `_complete_drain`（持 `_lock`：驗證 `session._pending_drain is job` 與 `job.epoch`；**再驗證回條 `handle_closed and writer_converged and reader_stopped`**——任一為假：只把回條存入 `job.receipt` 供診斷（`last_error` 不改、`drain_stalled` 維持／設為 True），session 維持 `RECOVERING`、`_pending_drain` 與 `_draining_by_ids` 不清、不得 release／reattach／開新 handle，直到擁有者以 `session.recover --force` 等既有人工路徑處理；全為真：`session.bridge=None`、retained consoles＝`receipt.preserved`、依狀態流表設定最終狀態與 `last_error`、`detached_at`、reset reprobe、`drain_stalled=False`、`_draining_by_ids.pop(by_id)`、`drain_timeout_epoch` 保留最後一次逾時值供診斷（凍結：完成後維持原值，不清為 null））。`_detach_session_locked` 依相容規則保留為同步薄包裝。
  - `_execute_command_inner` 建立前景操作時傳 `kind="command", cmd_id=cmd_id`；file 傳輸與 self-test 的兩個建立點分別傳 `kind="file"`、`kind="self_test"`（`cmd_id=None`）。
  - 五個 detach 呼叫點（`clear_session`、`release_device`、`bind_session`、`_detach_by_id`、`_handle_bridge_down`）改為「鎖內 `_begin_drain_locked` → 鎖外 `_await_drain` → `_complete_drain`」；`_await_drain` 呼叫 `bridge.finish_drain(max(0.0, job.deadline - time.monotonic()), on_late_receipt=lambda r: self._complete_drain(job, r))`（R3-F3：與 bridge 端 `_drain_deadline` 為同一期限，等 manager `_lock` 與 Phase 1 的時間都算在 3,000 ms 內）；回 `None` 時持鎖設 `drain_stalled=True`、`drain_timeout_epoch=job.epoch`。release 的回應依凍結表；release 在 `_complete_drain` 中依狀態流表「先設記憶體 `RELEASED`、再 `_save_state()`」，`_save_state()` 例外在同步路徑穿出（framing `EXCEPTION`），在遲到回條路徑依回應表處理。
  - 其餘直接 `bridge.stop()` 呼叫點（現況 `session_manager.py` 約 2653、2686、2692、2738、3089、3114、3121、3161 行：`_attach_by_id`／`_attach_by_id_dynamic` 的 attach／login 失敗收尾與 rebind；PROBE bridge 的 `probe_bridge.stop()` 不在此列）：一律改為「呼叫前在鎖內登記 `_draining_by_ids[by_id]`（kind `"recover"`）→ `bridge.stop()`／`finish_drain` → 只有成功回條才清除；逾時回 `None` 時以 `on_late_receipt` 清除並把遲到的 `preserved` 交給 retained consoles」；`_spawn_attach`、`_attach_by_id`、`attach_device` 見 `by_id in _draining_by_ids` 即不開新 bridge（`attach_device` 回 T08 凍結的 `SESSION_RECOVERING` 形狀）。
  - 前景等待者喚醒：`UARTBridge.wait_for_regex_from` 迴圈每輪檢查 `self._stop_event.is_set() or self._tx_frozen`，成立即回 `False`；`_execute_command_inner` 在 `matched is False` 時若前景 token 已被撤銷（`session._foreground_operation is not operation`），直接回 `_map_aborted_result(..., aborted_by_drain=True)`，**不**進 CTRL_C／`_recover_after_failure`。
  - 舊命令不得修改新 session（規格 §5.2.4、§5.3；arch §5.6）：`_execute_command_inner` 與 `_recover_after_failure` 在等待結束後的**每一個**狀態寫入點（capture、lease、`state`、`last_error`、`foreground_busy`、recovery 啟動、reprobe 計數）之前，持 `_lock` 核對 `session._foreground_operation is operation`、`session.bridge is operation.bridge` 與 `session.bridge_generation == operation.bridge_generation`；任一不符 → 不寫入、不啟動 recovery，只回傳映射後結果（I/O 證據已由 bridge 寫入 WAL）。
  - RPC 層：`daemon.BLOCKING_RPC_METHODS` 加入 `device.release`、`device.attach`、`session.clear`、`session.bind`（避免在單執行緒 asyncio loop 上阻塞至 3 s，期間 `health.ping`／`session.list` 凍結）；CLI `_LONG_RPC_METHODS` 同步加入（最壞延遲＝Phase 1 的 `unregister_session` join ≤1 s＋drain 3 s＋fsync，逼近 `DEFAULT_RPC_TIMEOUT_S=5.0`）；B2 以 `test_release_rpc_offloaded_from_event_loop` 實測最壞延遲並記入 PR，若超過 4.5 s 回報擁有者調整等待分配，不得自行改 3,000 ms。
  - 去重：`_begin_drain_locked` 在 `session._pending_drain is not None` 時回傳既有 job，不呼叫 `begin_drain`；release 對既有 release job 等同一個 `done` 事件（等到剩餘時限）。
  - `attach_device`、`_attach_by_id`、`_spawn_attach`：`session._pending_drain is not None` → 不開 bridge；`attach_device` 回凍結形狀。
  - BC-03：`execute_command` 外層以 `try/except BaseException` 包住 inner；若 `cmd_id == job.aborted_cmd_id`（任何仍在 pending 或已完成的 drain job）或捕捉到 `TxIncompleteError`，呼叫 `_map_aborted_result(cmd_id, result_or_exc, bridge=operation.bridge, aborted_by_drain=<是否被 drain 撤銷>)` 依凍結表回傳 `{"ok": False, "error_code": …}`；arbiter 既有 worker 把 `ok:false` 的 `error_code` 寫入紀錄（`status="error"`），不需新欄位。
  - TX 累計結算（R1-F8，Rust 對應 P5 T9 結算協定）：`_execute_command_inner` 開始寫入前 `bridge.tx_set_active(cmd_id)`；`execute_command` 的 `finally` 中，**僅當**未走 drain 撤銷映射（`cmd_id != job.aborted_cmd_id`）時呼叫 `bridge.tx_settle(cmd_id)`，再 `bridge.tx_set_active(None)`；drain 撤銷路徑的累計由 `_map_aborted_result` 經 `tx_account` 讀後移除，不得先被 settle 掉。測試：T06 `test_tx_account_settled_on_completion_and_active_never_evicted`，以及本 Task `test_drain_abort_mapping_survives_settle_race`（前景命令在途時 drain Phase 1 撤銷，同時另一已完成命令 settle → 前景命令仍得 `COMMAND_ABORTED_IN_FLIGHT`，`_tx_accounts` 最終不含兩者）。
  - SC-11：`SessionManager.shutdown_drain(total_timeout_s=5.0)` 先隱含 `begin_shutdown()`，以共用期限 join 已在途的 attach thread（gate 設下前已啟動者；attach thread 在建立 `UARTBridge` 前與安裝 `session.bridge` 前各持 `_lock` 檢查 `_shutting_down`，後者成立時立即 `bridge.stop()`、不送 login probe），再對所有有 bridge 的 session 依序 `_begin_drain_locked(kind="shutdown")`，以同一期限等待，回傳 `{"graceful", "stalled"}`（期限內未結束的 attach thread 以 `attach:<by_id>` 列入 `stalled`、`graceful=False`）；`SerialwrapService.stop()` 順序改為：`self._sessions.begin_shutdown()`（R1-F2，先於 engine.stop 與 watcher.stop，使 handler 執行期間與 watcher 最後一輪的熱插都不開新 UART）→ engine.stop → watcher.stop → `shutdown_drain()` → arbiter unregister（`FLUSHED_BY_SHUTDOWN`）→ flash endpoint stop → `graceful` 時 `self._wal.close()`，否則依狀態流表只 `sync()` 並記錄非優雅停止；daemon 的 exit code 不因此改變（避免改變監管器重啟語意），非優雅停止只以 log 與 `shutdown_drain` 結果揭露。此為 Python 基線行為；Rust daemon（P5 Task 19、P8 Task 12）於同一情境刻意改回 exit 1 加 `serialwrapd: shutdown incomplete: …` 診斷（SC-11 細化、DF 候選，調和表 SC-11 列），不屬 A/B 比較範圍，A/B 案例不製造停機未收斂；本 Task 不得以 Python 的 exit code 作為 Rust 該情境的 oracle。
  - `contracts/wal_meta_events.schema.json` 新增 `command_aborted_in_flight` 分支（`required: [event, confirmed_tx_bytes, tx_outcome]`、`tx_outcome ∈ {complete, partial, unknown}`、`x-behavior-change: "BC-03"`）。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_session_drain.py tests/test_device_handoff.py tests/test_bootloader_recovery.py tests/test_refine_ownership.py tests/test_session_capture.py tests/test_arbiter_flush_on_recovery.py tests/test_bad_command_recovery.py tests/test_resilience_hotplug.py tests/test_resilience_timeout.py tests/test_session_bind.py tests/test_state_persistence_atomic.py tests/test_state_save_race.py tests/test_flashing_state.py tests/test_rpc_blocking_offload.py tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py tests/test_contracts_rpc_schema.py tests/test_contracts_catalog.py`；`python3 -m tools.contracts check`；全套依 §0 第 4 點。
- [ ] **B3 commit**：`feat(session): SC-04 drain barrier 與 BC-03 中止映射，release 等 handle 關閉回條才完成`

**SC-11 交接停止 `daemon.handoff_stop`（R1-F1；Stage 1 由 Rust CLI `setup` 更新／回滾呼叫，P4 Task 12 步驟 3；Rust-only `supervision-mode set` 停止 Python daemon 時亦只經此 RPC，`reason:"supervision_set"`，P4 Task 12 `run_supervision_switch`，R1-F3。Python `setup` 本身的 reconcile 仍送一般 `daemon.stop`，不在本項範圍）**

背景：P4 update 原本在 CLI 端 `session.list` 預檢後送一般 `daemon.stop`；預檢與停止之間其他 client 可開始 `file.push` 或觸發 ttyMCU flash，而上方 SC-11 shutdown drain 的 Phase 1 會撤銷前景操作（`TransferAbort`）並停止 flash endpoint——是**中止**而非等待完成，DUT 會留下部分檔案／部分燒寫，違反規格 §12.1 步驟 2～3。CLI 無法關閉此窗口，故 daemon 必須提供「busy 判定＋凍結所有新操作＋發交接 token」同一臨界區的原子交接，凍結後才進入 SC-11 停止。

- Files（追加）：Create `sw_core/admission.py`、`tests/test_stage0_handoff_stop.py`；Modify `sw_core/daemon.py`（`daemon.handoff_stop` 與 `daemon.stop` 同層處理）、`sw_core/service.py`（`handoff_stop()`、所有有副作用 RPC 入口 admit）、`sw_core/arbiter.py`（`submit` admit、紀錄終態 `done`）、`sw_core/session_manager.py`（file／self-test／recover／attach／release／clear／bind／lease admit；`recovering`／`flashing`／`lease` 旗標在 `_lock` 內同步發布）、MCU flash endpoint 模組（sync-probe 寫第一個 byte 前 admit）、`contracts/rpc_schema.json`（P0 `x-pending` 的 SC-11 `adds_method: daemon.handoff_stop` 併入 `x-methods` 並刪除該項；`x-wire.version_absent_for` 加 `daemon.handoff_stop`）、`contracts/error_codes.yaml`（`FLUSHED_BY_SHUTDOWN` 的 `scope.interfaces` 加 `rpc`、`meaning` 附註 `reason:"handoff_frozen"`、`contract_tests` 指向本段測試；**不新增碼**）、`tests/test_contracts_rpc_schema.py`（`test_methods_equal_dispatch_plus_daemon_stop` 期望改為 `_dispatch_methods() | {"daemon.stop", "daemon.handoff_stop"}`）。
- Interfaces（Python；Rust 對應見 arch §6.2 第 7 步與 P8 Task 14）：
  ```python
  # sw_core/admission.py —— 共同 admission 邊界。self._lock 為葉鎖：持有時不得呼叫任何其他元件；鎖序 SessionManager._lock → AdmissionGate._lock
  @dataclass(frozen=True)
  class Ticket: kind: str; com: str | None; seq: int
  class AdmissionGate:
      def admit(self, kind: str, com: str | None) -> Ticket | None    # 已凍結 → None（呼叫端零副作用拒絕）；否則登記 ticket
      def done(self, ticket: Ticket) -> None                           # 冪等
      def set_flag(self, com: str, flag: Literal["flashing", "recovering", "lease"], on: bool) -> None
      def freeze_if_idle(self, reason: str) -> tuple[str | None, dict[str, Any]]
          # 同一次持鎖內：任一 ticket 或旗標存在 → (None, {"by_com": {com: {"ops": [kind…], "flags": [...]}}, "flashing": bool})、不凍結；
          # 否則 frozen=True、token=uuid4().hex、回 (token, {})。凍結不可解除（交接只以停止收場）
      frozen: bool
  SerialwrapService.handoff_stop(self, params: dict[str, Any]) -> dict[str, Any]
      # reason=str(params.get("reason") or "update")；token 非 None → {"ok": True, "stopping": True, "handoff_token": token}，daemon 層寫出回應後走 daemon.stop 同一停止路徑（SC-11）
      # busy → {"ok": False, "error_code": "FLASHING_BUSY"（有 flash ticket 或 flashing 旗標）| "SESSION_BUSY",
      #          "busy": [{"com","state","foreground_busy","outstanding_commands","interactive_session_id","recovering","ops"}]}（列依 com 排序；state 等欄位於判定後另查 manager，只供回報）
  ```
- admission 點（每個都必須在**任何副作用之前**呼叫 `admit`——入列、第一個 TX byte、狀態轉移、`state.json` 改寫）：`rpc_schema.json` 中 `side_effects` 非空且 `retryable_readonly == false` 的全部方法（`command.submit` 的 ticket 持有到命令紀錄終態，等同 `outstanding_commands`；`file.push`／`file.pull`、`session.self_test`、`session.recover`、`device.release` 等持有到 handler 返回；`session.interactive_open` 持有到 lease close／expire）；ttyMCU sync-probe（admit 失敗時端點維持沉默、不轉送任何 byte、session 不進 FLASHING，ticket 持有到離開 FLASHING）。凍結後被拒的 RPC 回 `{"ok": false, "error_code": "FLUSHED_BY_SHUTDOWN", "reason": "handoff_frozen"}`。唯讀方法、`health.*`、`daemon.stop`、`daemon.handoff_stop` 不經 admission。human console 位元組不經 admission（可安全中止，SC-11 照舊處理）。
- [ ] **C1 寫失敗測試**：`tests/test_stage0_handoff_stop.py`
  ```python
  from __future__ import annotations
  import re
  import threading
  import pytest
  from stage0_fakes import FakePort, make_service, wal_records

  def _rpc(svc, method, params=None):
      return svc.daemon_dispatch_for_test({"id": 1, "method": method, "params": params or {}})   # 走 daemon.py 同一分派（含 daemon.* 分支）

  def test_handoff_file_push_after_last_probe(tmp_path):
      gate = threading.Event(); port = FakePort(block=gate)
      svc = make_service(tmp_path, ready=["COM0"], port=port)
      assert all(not s["foreground_busy"] for s in _rpc(svc, "session.list")["sessions"])   # CLI 預檢看到閒置
      payload = b"x" * 65536
      out: dict = {}
      t = threading.Thread(target=lambda: out.update(_rpc(svc, "file.push", {"selector": "COM0", "data_b64": payload.hex()})))
      t.start(); svc.wait_until_admitted("file.push", "COM0", timeout=1.0)              # 預檢之後才開始的 push
      r = _rpc(svc, "daemon.handoff_stop", {"reason": "update"})
      assert (r["ok"], r["error_code"]) == (False, "SESSION_BUSY")
      assert r["busy"][0]["com"] == "COM0" and r["busy"][0]["ops"] == ["file.push"]
      assert svc.stop_requested is False and svc.admission.frozen is False
      gate.set(); t.join(10.0)
      assert out["ok"] is True                                                           # push 完整完成，未被截斷
      assert b"".join(port.writes).count(b"x") >= len(payload)
      r2 = _rpc(svc, "daemon.handoff_stop", {"reason": "update"})
      assert r2["ok"] is True and r2["stopping"] is True and re.fullmatch(r"[0-9a-f]{32}", r2["handoff_token"])
      assert "version" not in r2 and svc.stop_requested is True

  @pytest.mark.parametrize("first", ["probe", "handoff", "race"])
  def test_handoff_flash_races_prepare(tmp_path, first):
      for _ in range(200 if first == "race" else 1):
          svc, mcu = make_service(tmp_path, ready=["COM0"], with_fake_mcu=True)
          res: dict = {}
          probe = lambda: res.__setitem__("probe", svc.flash_endpoint.sync_probe_for_test(b"\x55\x55"))   # flasher 自身 sync bytes
          handoff = lambda: res.__setitem__("handoff", _rpc(svc, "daemon.handoff_stop", {"reason": "rollback"}))
          if first == "probe":
              probe(); handoff()
              assert res["handoff"]["error_code"] == "FLASHING_BUSY"                      # 預檢後才開始的 flash 必擋下交接
          elif first == "handoff":
              handoff(); probe()
              assert res["handoff"]["ok"] is True
          else:
              barrier = threading.Barrier(2)
              ts = [threading.Thread(target=lambda f=f: (barrier.wait(), f())) for f in (probe, handoff)]
              [x.start() for x in ts]; [x.join(5.0) for x in ts]
          if res["handoff"]["ok"]:                                                       # 交接成立 ⇒ probe 必被拒、MCU 零 byte、未進 FLASHING
              assert res["probe"] == "refused_frozen" and mcu.received == b""
              assert svc.session_state("COM0") != "FLASHING"
          else:                                                                          # probe 先 admit ⇒ 交接必為 FLASHING_BUSY、daemon 不停止
              assert res["handoff"]["error_code"] == "FLASHING_BUSY" and res["probe"] == "admitted"
              assert svc.stop_requested is False and svc.admission.frozen is False
          svc.close_for_test()

  def test_push_after_freeze_rejected_zero_tx(tmp_path):
      port = FakePort(); svc = make_service(tmp_path, ready=["COM0"], port=port)
      assert _rpc(svc, "daemon.handoff_stop")["ok"] is True
      before = len([r for r in wal_records(svc.wal) if r["dir"] == "TX"])
      r = _rpc(svc, "file.push", {"selector": "COM0", "data_b64": "eA=="})
      assert (r["ok"], r["error_code"], r["reason"]) == (False, "FLUSHED_BY_SHUTDOWN", "handoff_frozen")
      assert port.writes == [] and len([x for x in wal_records(svc.wal) if x["dir"] == "TX"]) == before
      assert svc.session_public("COM0")["foreground_busy"] is False
  ```
  表格補充（同檔）：

  | 測試名 | 斷言 |
  |---|---|
  | `test_command_submit_races_handoff`（200 次 Barrier 競態） | 每次恰為二者之一：交接 ok ⇒ `command.submit` 回 `FLUSHED_BY_SHUTDOWN`／`reason:"handoff_frozen"`、`port.writes == []`、無命令紀錄；submit 被受理 ⇒ 交接回 `SESSION_BUSY`、`ops` 含 `command.submit`、daemon 未停止。不得出現「交接 ok 且命令已入列」 |
  | `test_every_mutating_method_rejected_after_freeze`（參數化：由 `rpc_schema.json` 取 `side_effects` 非空且 `retryable_readonly == false` 的方法，不硬編碼清單） | 凍結後每個方法皆回 `FLUSHED_BY_SHUTDOWN`／`handoff_frozen`；`port.writes == []`、`state.json` mtime 未變；同時 `health.ping`、`session.list`、`mcu.status` 仍 `ok:true` |
  | `test_handoff_reason_supervision_set_same_semantics`（R1-F3；參數化 `reason ∈ {"update","rollback","revert","supervision_set"}`） | 各 reason：`file.push` 進行中（ticket 已 admit）→ `SESSION_BUSY`、`ops` 含 `file.push`、未凍結、未停止，push 完成後 DUT 端位元組完整；flash sync-probe ticket 存在 → `FLASHING_BUSY`；全閒置 → `ok:true`＋32 hex token、凍結後 `file.push` 回 `FLUSHED_BY_SHUTDOWN`／`handoff_frozen`、`port.writes == []`。四個 reason 的回應除 token 值外逐欄相同（reason 不影響 busy 判定與凍結語意） |
  | `test_handoff_busy_does_not_freeze` | lease 開啟（`interactive_session_id` 非 null）時交接回 `SESSION_BUSY`、`ops:["session.interactive_open"]`；之後 `command.submit` 照常受理 |
  | `test_handoff_recovering_flag_is_busy` | `_handle_bridge_down` drain 進行中交接 → `SESSION_BUSY`、`recovering:true` |
  | `test_handoff_ok_then_sc11_stop_order` | 真 daemon 行程：交接回應行為 `{"id":1,"ok":true,"stopping":true,"handoff_token":…}`（無 `version`）先寫出，之後才 `begin_shutdown`；`shutdown_drain()` 回 `{"graceful": True, "stalled": []}`、無任何 `COMMAND_ABORTED_IN_FLIGHT`／`TransferAbort`；exit 0 |
  | `test_handoff_stop_contract_merged` | `rpc_schema.json` `x-methods` 含 `daemon.handoff_stop`、`x-pending` 無 `adds_method: daemon.handoff_stop`、`x-wire.version_absent_for` 含之；`error_codes.yaml` `FLUSHED_BY_SHUTDOWN.scope.interfaces` 含 `rpc` |
- [ ] **C2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_handoff_stop.py` → 預期 `METHOD_NOT_FOUND`（`daemon.handoff_stop` 未實作）／`AttributeError: admission`。commit：`test(daemon): SC-11 原子交接停止與 admission 競態失敗測試`
- [ ] **C3 實作**：依上方 Interfaces 與 admission 點；`freeze_if_idle` 與每個 `admit` 共用 `AdmissionGate._lock`，因此任一操作不是在凍結前 admit（ticket 可見 → 交接回 busy），就是在凍結後 admit（被拒、零副作用）；`daemon.py` 在交接 ok 時先寫回應再呼叫既有 stop 觸發（與 `daemon.stop` 同路徑，SC-11 順序不變），busy 時不觸發停止、不凍結。測試輔助（`daemon_dispatch_for_test`、`wait_until_admitted`、`sync_probe_for_test`）放 `tests/stage0_fakes.py`，不進 production 介面。
- [ ] **C4 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_handoff_stop.py tests/test_stage0_session_drain.py tests/test_contracts_rpc_schema.py tests/test_contracts_catalog.py tests/test_flashing_state.py tests/test_multiagent_e2e.py`；`python3 -m tools.contracts check`。
- [ ] **C5 commit**：`feat(daemon): 新增 daemon.handoff_stop 原子交接，凍結新操作後才有序關機`

---

## Task T09：BC-02 DeferredBuffer 整段清空與上下文標記

- **範圍**：BC-02：超過 `DEFERRED_INPUT_MAX_BYTES` 整段清空（不截頭留尾）；Reboot／Quiet 重新啟動、Recovery、Release、Flash（含 FlashSyncMatched，`SES-16.4-05`）時丟棄整段舊輸入；保留擷取時的 owner／connection 上下文，避免已讀入但尚未處理的舊按鍵於清空後再補入；未失效時 #53 正常回放不變；stale console prune 時一併刪其 deferred。依據：規格 §5.2.6、§5.4、§9.1 BC-02；盤點 06 §6.7、§12.5，05 §15、§16.1；arch §5.11 DeferredBuffer。
- **Test author model**：haiku-xhigh　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-bc02-deferred-buffer.md`（`type: fix`，`scope: console`）：human 延遲輸入超過上限或遇重開機、recovery、release、flash 時整段丟棄，不再截頭留尾，也不會在上下文失效後回放舊按鍵。
- **BLOCKED-OWNER**：L2 F5（console coexist）在實機 bench 重跑。

**Files**
- Create: `tests/test_stage0_deferred.py`
- Modify: `sw_core/uart_io.py`、`sw_core/session_manager.py`（`SessionRuntime.arm_boot_quiet`、`_handle_reboot_command` 進 RECOVERING、`enter_flashing`、`recover_session`、`_recover_after_failure` 呼叫 `invalidate_deferred`）、`tests/test_bounded_memory.py`（`test_deferred_buffer_capped` 改為 BC-02 新語意；**測試資產變更，需獨立審閱**）、`sw_core/constants.py`（`DEFERRED_INPUT_MAX_BYTES` docstring 改為「超過即整段清空」）、`docs/rust-refactor/plans/rust-architecture.md` §5.3、§5.11（見 Rust 對應）

**測試資產變更**（需獨立審閱）：僅 `tests/test_bounded_memory.py::test_deferred_buffer_capped`（下方 A1 全文）。`tests/test_uart_io.py:124`、`tests/test_interactive_raw.py:298` 以 `bytes(buf)` 讀 entry、`tests/test_suspend_resume_reentrant.py:14,59` 以 bare bridge 指派 `bytearray`——依下方相容規則無須修改。

**相容規則（凍結）**：`_DeferredEntry` 為 `bytearray` 子類，附加屬性 `owner_gen`（`bytes(entry)`、`len(entry)`、`entry.extend` 與現況相同）；讀取 entry 世代一律 `getattr(entry, "owner_gen", getattr(self, "_owner_gen", 0))`，bridge 世代一律 `getattr(self, "_owner_gen", 0)`，使直接指派 `bytearray`、以 `object.__new__` 建立且缺 `_owner_gen` 的 bare bridge 維持現況行為（視為目前世代）。

**Interfaces**
- Consumes：T07 `begin_drain` 內的 `_on_drain_invalidate` 鉤子。
- Produces（Python）：
  ```python
  class _DeferredEntry(bytearray):
      owner_gen: int                                           # 建構：_DeferredEntry(data=b"", owner_gen=0)
  UARTBridge._deferred_buffers: dict[str, bytearray]           # 值為 _DeferredEntry（bytearray 子類），舊碼／測試的 bytearray 仍相容
  UARTBridge.owner_gen -> int                                 # property；語意＝arch 的 deferred_epoch：**只在失效時遞增**
  UARTBridge.invalidate_deferred(self, reason: str) -> int    # owner_gen += 1；清空全部；回傳丟棄 bytes 數；>0 時 log WARNING
  UARTBridge._handle_console_rx(self, client: ConsoleClient, data: bytes, *,
                                captured_owner_gen: int | None = None) -> None
  UARTBridge._append_deferred_locked(self, client_id: str, payload: bytes, owner_gen: int) -> None
  ```
- Rust 對應：arch §5.11 `Entry { client_id, client_lifetime, owner_gen, bytes }`；「回放時只送 `owner_gen` 等於目前上下文、且 client 仍存活的項目」；「mailbox 中帶舊 `owner_gen` 的 `ConsoleInputBytes` 一律丟棄」。Python 的 `client_id` 為每條連線唯一 uuid，等同 `client_lifetime`。**arch 勘誤（本 Task 同步修訂 arch §5.3／§5.11）**：arch 的 `owner_gen` 在 lease 開關、suspend 深度 0↔1、soft preempt 都遞增，若照字面拿它判定 DeferredBuffer 回放，#53 Soft Preempt（suspend→resume 必然跨越 owner_gen 變更）累積的按鍵會全部被丟。改為另立 `deferred_epoch: u64`（只在本 Task 第 2 點的失效事件遞增），`Entry` 與 `ConsoleInputBytes` 帶 `deferred_epoch_at_capture`，回放與 mailbox 判定比對 `deferred_epoch`；`owner_gen` 維持原用途（TxPermit 的 owner 規則）。Python 的 `UARTBridge.owner_gen` 即 arch 的 `deferred_epoch`。

**行為規格**
1. `_append_deferred_locked`：entry 不存在或 entry 世代（相容規則）`!= owner_gen` → 以新 `_DeferredEntry(b"", owner_gen=owner_gen)` 取代；`len(entry) + len(payload) > DEFERRED_INPUT_MAX_BYTES` → 刪除整個 entry（連同本次 payload 一起丟棄），log WARNING `"serialwrap: deferred 輸入溢位，client=%s 丟棄 %d bytes（BC-02）"`；否則 extend。三個寫入點（盤點 06 §6.7）全部改用此函式。
2. `invalidate_deferred(reason)`：持 `_state_lock`：`self._owner_gen += 1`；計算總 bytes 後 `clear()`；>0 時 log WARNING `"serialwrap: deferred 輸入因 %s 失效，丟棄 %d bytes（BC-02）"`。呼叫點（規格 §5.2.6 的 Reboot／Quiet 重新啟動、Recovery、Release、Flash 全部涵蓋）：
   - `begin_drain`（reason `"drain"`；涵蓋 release、clear、bind、斷線）；
   - `SessionManager.enter_flashing`（FlashSyncMatched，`"flash"`）——**不**放在 `set_flash_mode(True)`：`service._BridgeProbe` 在每個 flash 候選 probe 起點也呼叫 `set_flash_mode(True)`、未命中即解除，規格 §5.3 只在 FlashSyncMatched 才失效，probe 未匹配時上下文必須恢復有效；
   - `SessionRuntime.arm_boot_quiet`（`arm_boot_quiet` 是 `SessionRuntime` 方法，`session_manager.py` 約 450 行；以 `fn = getattr(self.bridge, "invalidate_deferred", None); if callable(fn): fn("boot_quiet")` 守衛，因既有測試的 `bridge` 常為 `None` 或 `MagicMock`）；
   - `SessionManager._handle_reboot_command` 進 `RECOVERING` 前（`"reboot"`）；
   - `SessionManager.recover_session` 入口（READY 路徑，`"recover"`）與 `_recover_after_failure` 送 CTRL_C／CTRL_D 恢復鍵之前（`"recover"`）。
3. 擷取上下文：`_loop` 在每輪頂端於 `_state_lock` 內取 `gen = self._owner_gen`，該輪讀到的 console 資料一律以 `captured_owner_gen=gen` 呼叫 `_handle_console_rx`；Windows `_console_loop` 同理。`_handle_console_rx` 在 `_state_lock` 內若 `captured_owner_gen is not None and captured_owner_gen != self._owner_gen` → 丟棄（log DEBUG），不 echo、不送 UART、不進 deferred、不進 line buffer。raw 直送與回放在離開 `_state_lock`、等待 `_write_lock` 期間仍可能被失效：此競態由 T20 的 permit（攜帶擷取時的 `owner_gen`，於 OS write 前在 `_state_lock` 內再驗證；不符者**丟棄**，不得改標為目前世代重新 defer）關閉；本 Task 只負責把擷取世代傳到 `send_bytes` 呼叫點。
4. 回放（`resume_interactive` 歸零、`_set_interactive_admission(True)`）：只回放 `entry.owner_gen == self._owner_gen` 的項目，且 client 仍在 `_clients`；其餘丟棄。未發生失效時語意與 #53 現況逐字相同。
5. `_prune_stale_consoles_locked` 移除 client 時同時 `self._deferred_buffers.pop(client_id, None)`。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_deferred.py`；並把 `tests/test_bounded_memory.py::test_deferred_buffer_capped` 改為：
  ```python
  def test_deferred_buffer_capped(monkeypatch):
      monkeypatch.setattr(uio, "DEFERRED_INPUT_MAX_BYTES", 100)
      b = object.__new__(uio.UARTBridge)
      b._state_lock = threading.RLock(); b._flash_mode = False; b._interactive_owner = None
      b._agent_active = True; b._suspended_owner = "human:cid1"; b._deferred_buffers = {}; b._owner_gen = 0
      client = uio.ConsoleClient(client_id="cid1", label="x", master_fd=-1, slave_fd=-1, slave_path="", attached_at=0.0)
      b._handle_console_rx(client, b"A" * 500)
      assert "cid1" not in b._deferred_buffers          # BC-02：超過上限整段清空
      b._handle_console_rx(client, b"B" * 10)
      assert bytes(b._deferred_buffers["cid1"]) == b"B" * 10   # _DeferredEntry 為 bytearray 子類
  ```
  新檔：
  ```python
  from __future__ import annotations
  import logging
  import os
  import pytest
  import sw_core.uart_io as uio
  from stage0_fakes import FakePort, make_bridge

  def _client(cid="cid1"):
      return uio.ConsoleClient(client_id=cid, label="x", master_fd=-1, slave_fd=-1, slave_path="", attached_at=0.0)

  def _suspended_bridge(tmp_path, port):
      b, _ = make_bridge(tmp_path, port=port)
      c = _client(); b._clients = {c.client_id: c}
      b._interactive_owner = f"human:{c.client_id}"
      b.suspend_interactive()
      return b, c

  def test_soft_preempt_replay_unchanged_without_invalidation(tmp_path):
      port = FakePort(); b, c = _suspended_bridge(tmp_path, port)
      b._handle_console_rx(c, b"ls", captured_owner_gen=b.owner_gen)
      b.resume_interactive()
      assert port.writes == [b"ls"]

  def test_boot_quiet_invalidation_drops_deferred(tmp_path):
      port = FakePort(); b, c = _suspended_bridge(tmp_path, port)
      b._handle_console_rx(c, b"ls", captured_owner_gen=b.owner_gen)
      assert b.invalidate_deferred("boot_quiet") == 2
      b.resume_interactive()
      assert port.writes == []

  def test_stale_mailbox_bytes_dropped_after_invalidation(tmp_path):
      port = FakePort(); b, c = _suspended_bridge(tmp_path, port)
      captured = b.owner_gen
      b.invalidate_deferred("reboot")
      b._handle_console_rx(c, b"rm -rf x", captured_owner_gen=captured)
      assert c.client_id not in b._deferred_buffers
      b.resume_interactive()
      assert port.writes == []

  def test_overflow_clears_and_logs(tmp_path, monkeypatch, caplog):
      monkeypatch.setattr(uio, "DEFERRED_INPUT_MAX_BYTES", 8)
      port = FakePort(); b, c = _suspended_bridge(tmp_path, port)
      with caplog.at_level(logging.WARNING, logger="serialwrap"):
          b._handle_console_rx(c, b"123456789", captured_owner_gen=b.owner_gen)
      assert c.client_id not in b._deferred_buffers
      assert "BC-02" in caplog.text

  def test_flash_probe_miss_keeps_deferred_context(tmp_path):
      # service._BridgeProbe 在每個候選 probe 起點 set_flash_mode(True)、未命中即解除：上下文不得失效
      port = FakePort(); b, c = _suspended_bridge(tmp_path, port)
      gen = b.owner_gen
      b._handle_console_rx(c, b"ls", captured_owner_gen=gen)
      b.set_flash_mode(True); b.set_flash_mode(False)
      assert b.owner_gen == gen
      b.resume_interactive()
      assert port.writes == [b"ls"]

  def test_drain_invalidates_deferred(tmp_path):
      port = FakePort(); b, c = _suspended_bridge(tmp_path, port)
      b._handle_console_rx(c, b"ls", captured_owner_gen=b.owner_gen)
      b.begin_drain(1, preserve_consoles=True)
      assert b._deferred_buffers == {}
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_admission_gate_replay_skips_old_owner_gen` | admission 關閉期間累積的 entry，在 `invalidate_deferred` 後 `_set_interactive_admission(True)` 回傳空清單 |
  | `test_prune_stale_console_drops_its_deferred` | `_prune_stale_consoles_locked` 移除 client 後 `_deferred_buffers` 無該 key |
  | `test_session_arm_boot_quiet_invalidates_bridge` | `session.arm_boot_quiet()`（`SessionRuntime` 方法；session 有真實 bridge）→ bridge `owner_gen` 加 1 |
  | `test_session_arm_boot_quiet_tolerates_none_or_mock_bridge` | `session.bridge` 為 `None` 或 `MagicMock()` 時 `arm_boot_quiet()` 不擲例外 |
  | `test_reboot_command_entering_recovering_invalidates` | agent reboot 命令經 `SessionManager._handle_reboot_command` 進 `RECOVERING` 前呼叫 `invalidate_deferred("reboot")`（spy） |
  | `test_enter_flashing_invalidates` | `enter_flashing`（FlashSyncMatched）後 bridge `owner_gen` 加 1、deferred 清空、`resume_interactive()` 後 `port.writes == []` |
  | `test_recover_session_invalidates` | READY session `recover_session()` → 入口呼叫 `invalidate_deferred("recover")`（spy），之前累積的 deferred 不回放 |
  | `test_recover_after_failure_invalidates_before_keys` | 命令逾時進 `_recover_after_failure` → `invalidate_deferred("recover")` 早於 CTRL_C 寫入（spy 呼叫順序） |
  | `test_send_bytes_raw_context_deferred_uses_entry` | raw console context 遇 admission 關閉時 entry 帶目前 `owner_gen` |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_deferred.py tests/test_bounded_memory.py::test_deferred_buffer_capped` → 預期 `AttributeError: 'UARTBridge' object has no attribute 'owner_gen'`／`assert 'cid1' not in …` 失敗。commit：`test(console): BC-02 DeferredBuffer 整段清空與上下文標記失敗測試`
- [ ] **B1 實作**：依行為規格；`_owner_gen` 初值 0，於 `__init__` 建立；`stop()`／`finish_drain` 既有 `_deferred_buffers.clear()` 保留。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_deferred.py tests/test_bounded_memory.py tests/test_uart_io.py tests/test_suspend_resume_reentrant.py tests/test_agent_defer_tx.py tests/test_interactive_raw.py tests/test_human_agent_coexist.py tests/test_service_human_console.py tests/test_boot_quiet.py tests/test_ready_reconfirm_bounds.py tests/test_flash_probe.py tests/test_flash_service_wiring.py`；全套依 §0 第 4 點。
- [ ] **B3 commit**：`fix(console): BC-02 DeferredBuffer 溢位與上下文失效時整段清空並拒絕舊按鍵回放`

---

## Task T10：BC-04 Per-UART 獨占、holder 診斷、Windows ACCESS_DENIED 映射、SC-05 同步收回、DF-04

- **範圍**：BC-04：POSIX 對已開啟 tty fd 取非阻塞 `flock` 並設 `TIOCEXCL`（`O_CLOEXEC`）；失敗不啟動收送；holder 診斷（`pids`、`holder_unknown`，掃描失敗不得當作安全證明）；Windows pyserial `dwShareMode=0` 開檔失敗的 `ERROR_ACCESS_DENIED` → `DEVICE_STILL_HELD{pids:[], holder_unknown:true}`。SC-05：`device.attach` 同步重新取得獨占，失敗回 `DEVICE_STILL_HELD` 或 `DEVICE_NOT_PRESENT`，成功才 `ATTACHING`。DF-04：動態 session 的 RELEASED 跨任意次重啟保留；裝置離線 tick 不得把 RELEASED 降為 DETACHED。依據：規格 §6.2 POSIX／Windows／交接、§9.1 BC-04；OD-04 備註（daemon 持有時無法再直接開 tty，屬預期）；盤點 06 §10.2、§12.1，05 §12.4，04 §2.6；arch §4.2 `OpenError`／`HolderReport`、§5.9 attach。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-bc04-uart-exclusive.md`（`type: security`，`scope: uart`）：POSIX 開啟 UART 時取得 flock 並設 TIOCEXCL（關閉與失敗路徑先以 TIOCNXCL 解除），已被占用時拒絕收送並回報 holder；Windows 存取拒絕回 `DEVICE_STILL_HELD`（`holder_unknown:true`）；`device attach` 改為同步取得獨占；動態 session 的 released 狀態跨重啟保留（DF-04）。
- **BLOCKED-OWNER**：(1) Windows 原生 COM 第二次開啟得 `ERROR_ACCESS_DENIED`（需 OD-08 com0com pair 或實體 USB-UART 直連，`windows_com` 測試）；(2) 受限 `/proc`（`hidepid=2` 重新掛載需 root）與 `CAP_SYS_ADMIN` 例外情境；(3) Spike 5 Stage 0 BC-04／交接實機驗證（外部 minicom 先開埠、實板 handle 繼承）。

**Files**
- Create: `tests/test_stage0_uart_exclusive.py`
- Modify: `sw_core/serial_port.py`、`sw_core/uart_io.py`（`preopened_port`、`owns_port`）、`sw_core/session_manager.py`（`attach_device`、`_probe_external_holder` 回 `HolderReport` 形狀、`_attach_by_id`／`_reresolve_profile_on_reattach`／`_attach_by_id_dynamic` 使用預開 port、`__init__` released 還原與實體化、`_mark_missing_devices_locked`、`_save_state` released 組出）、`sw_core/cli.py`（`device attach --force` help 說明與實際 holder 行為一致）、`contracts/rpc_schema.json`（`x-pending` 的 BC-04 `DEVICE_STILL_HELD.holder_unknown` 併入 `device.attach` 回應 schema 正式欄位並刪除該 `x-pending` 項；`holder_unknown` 屬回應 schema，不是 `error_codes.yaml` 欄位）、`contracts/error_codes.yaml`（`DEVICE_STILL_HELD` 的 `contract_tests` 補本 Task 測試 node；`behavior_change` 維持 `[BC-04, SC-05]`）、`contracts/behavior_changes.yaml`（BC-04、SC-05、DF-04 → `IMPLEMENTED`）、`tests/test_resource_markers.py`（**測試資產變更**：P0 T14 第 6 條「`windows_com`／`physical_uart` 使用數為 0」的斷言改為「`windows_com` 只允許出現在模組層或 class 層 `pytestmark` 含 `windows_native` 的項目，且每個使用者同時帶 `windows_native`」；需獨立審閱，PR 註明）（本 Task 新檔以 `pytestmark` 標 `windows_native`＋`windows_com`）

**Interfaces**
- Consumes：T07 `drain_pending`；T08 `_pending_drain`。
- Produces（Python）：
  ```python
  # sw_core/serial_port.py
  @dataclass(frozen=True)
  class HolderReport:
      pids: tuple[int, ...]
      holder_unknown: bool
      status: Literal["ok", "permission", "unknown", "unsupported"]
  class DeviceHeldError(OSError):
      def __init__(self, device_path: str, *, holder: HolderReport, os_error: str | None = None) -> None
  class DeviceNotPresentError(OSError): ...
  def probe_holders(real_path: str, *, proc_root: str = "/proc", exclude_pid: int | None = None) -> HolderReport
      # POSIX：掃 proc_root/*/fd，readlink 比 realpath／原路徑或 st_rdev；listdir(proc_root) 失敗 → status="permission"、holder_unknown=True
      # Windows：HolderReport((), True, "unsupported")
  def classify_pyserial_open_error(message: str) -> tuple[Literal["held", "not_found", "other"], int | None]
      # 解析 pyserial 訊息中的 "(…, None, <winerror>)"；5（ACCESS_DENIED）、32（SHARING_VIOLATION）→ "held"；2、3 → "not_found"
  _PosixSerialPort.open(self) -> None
      # os.open(O_RDWR|O_NOCTTY|O_NONBLOCK|O_CLOEXEC)；EBUSY → DeviceHeldError；
      # fcntl.flock(fd, LOCK_EX|LOCK_NB)：BlockingIOError → close(fd)、DeviceHeldError；
      # fcntl.ioctl(fd, termios.TIOCEXCL)：失敗 → close(fd)、raise OSError
      # 鎖後重掃 probe_holders(realpath, exclude_pid=os.getpid())：pids 非空 → _release_exclusive(fd)、close(fd)、DeviceHeldError（termios／RX／TX 之前；force 不略過）
      # TIOCEXCL 成功之後 open() 內的任何失敗返回（重掃拒絕或其他例外）一律 _release_exclusive(fd) 後才 close(fd)（R0-F3）；
      # open() 成功後 configure() 失敗由呼叫端 close() 收尾，同樣經 _release_exclusive
  _PosixSerialPort.close(self) -> None
      # 冪等；fd 存在時：_release_exclusive(fd) → fcntl.flock(fd, LOCK_UN) → os.close(fd)。
      # 呼叫端（UARTBridge.stop／finish_drain）保證 RX／TX 已收斂後才 close；owns_port=False 時不呼叫。
  _PosixSerialPort._release_exclusive(fd: int) -> bool
      # fcntl.ioctl(fd, termios.TIOCNXCL)；成功 → True；OSError errno ∈ {EIO, ENXIO, ENODEV}（已 hangup／拔除）→ True；
      # 其他 OSError → log WARNING "serialwrap: TIOCNXCL failed on %s: %s"、回 False（呼叫端仍照常 close，不得保留 fd）
      # 理由：TIOCEXCL 是 tty 物件旗標、不隨 fd 關閉清除；PTY master 仍開啟（FakeTarget／bridge 受測端）時殘留會讓
      #       PROBE→正式 bridge 重開與 release 後外部工具開啟都 EBUSY（R0-F3 實測）
  # sw_core/uart_io.py
  UARTBridge.__init__(..., preopened_port: SerialPort | None = None, owns_port: bool = True)
      # start() 有 preopened_port 時跳過 open_serial_port()／port.open()，仍呼叫 configure；
      # owns_port=False 時 stop()／finish_drain 不 close 該 port（交還呼叫者），回條 handle_closed 以「已交還」計
  # sw_core/session_manager.py
  SessionManager.attach_device(self, selector: str, *, force: bool = False) -> dict[str, Any]
  @dataclass
  class _AttachOp:
      token: object; session_id: str; by_id: str; bridge_generation: int; port: SerialPort | None
  SessionManager._attach_ops: dict[str, _AttachOp]        # by_id → 進行中的同步 attach（唯一）
  SessionManager._preopened_ports: dict[str, _AttachOp]   # by_id → 已獨占且已重驗的 port，attach thread 消費；未消費於 release／detach／hot-unplug 時 close 恰一次
  ```
- Rust 對應：arch §4.2 `SerialBackend::open_exclusive` POSIX 序列（`open(O_RDWR|O_NOCTTY|O_NONBLOCK|O_CLOEXEC) → flock(LOCK_EX|LOCK_NB) → ioctl(TIOCEXCL)`）、`OpenError::{Held, AccessDenied, NotFound, Other}`、`HolderReport`、`UartExclusive::probe_holders`；§5.9 attach 步驟 1～4。

**`device.attach` 結果表（凍結）**

| 情境 | 回應 | 狀態 |
|---|---|---|
| drain 進行中 | 見 T08 凍結形狀 | 不變 |
| 已 attached 且非 RELEASED | `{"ok":true,"already_attached":true,"session":…}` | 不變 |
| 裝置不在 | `{"ok":false,"error_code":"DEVICE_NOT_PRESENT","selector":…,"device_by_id":…}` | 不變 |
| 非 force 預檢 `pids` 非空 | `{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[…],"holder_unknown":false,"selector":…}` | 不變（RELEASED 保留） |
| 同步開檔 `DeviceHeldError`（含 force 時鎖後重掃識別到未取 flock 的外部 holder） | `{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[…],"holder_unknown":<bool>,"selector":…}` | 不變 |
| 同步開檔 `DeviceNotPresentError`／`FileNotFoundError` | `DEVICE_NOT_PRESENT` | 不變 |
| 同步開檔其他 `OSError` | `{"ok":true,"session":…}` | `DETACHED`、`last_error="ATTACH_FAILED:<ExcType>"`（arch §5.9 步驟 3） |
| 成功 | `{"ok":true,"session":…}` | 清 released、`_save_state()`、`ATTACHING`、port 交給 `_attach_by_id` |

`holder_unknown` 規則：`pids` 非空 → `false`；`pids` 為空（掃描失敗、權限不足、Windows、或鎖衝突但查不到 PID）→ `true`。熱插拔自動 attach 遇 `DeviceHeldError` → `DETACHED`、`last_error="ATTACH_FAILED:DeviceHeldError"`（沿用動態前綴）。

**非合作式外部 holder（凍結；規格 §6.2「拒絕已識別的外部衝突」）**：`flock` 是 advisory、`TIOCEXCL` 不撤銷既有 handle，一般 minicom 先 open 不取 flock 時本 daemon 的 open／flock／TIOCEXCL 都會成功。因此 `_PosixSerialPort.open` 在三步成功後、**啟動任何 RX／TX 之前**再做一次 `probe_holders(realpath, exclude_pid=os.getpid())`：
- `pids` 非空（已識別的外部 holder；不論 `status`——`status="permission"` 時已收集到的 pid 仍是已識別）→ 在 termios 設定與任何 RX／TX 之前先 `TIOCNXCL`（本端剛設的旗標會殘留在外部 holder 仍持有的 tty 上）再關閉 fd、raise `DeviceHeldError(holder=report)`（`holder_unknown:false`）。
- 此檢查位於 `_PosixSerialPort.open` 內，所有開埠路徑都經過：daemon 啟動時的初次自動 attach、熱插拔自動 attach、PROBE、`attach_device(force=False)` 與 **`attach_device(force=True)`**。`force` 只略過開檔前的預檢快照（結果表第 4 列），**不得**略過已識別的外部 holder（規格 §6.2）；`force=True` 遇此情形同樣回 `DEVICE_STILL_HELD`、session 維持 `RELEASED`、UART 零 TX。
- `pids` 為空且 `status ∈ {"permission","unknown"}`（掃描本身失敗）→ **不**視為安全證明但也不拒絕（否則受限 `/proc` 環境無法開任何埠）：照常開啟，log WARNING `serialwrap: 無法確認 %s 的外部 holder（status=%s），BC-04 僅保證 serialwrap 實例互斥`，並把結果放進 `daemon status` 的 `foreign_holders`（`multi_open` 既有欄位）為 `{"device":…, "holder_unknown": true}`。
- 本 daemon 自身其他 fd（同 pid）被排除，不構成衝突。
`--force-spawn`、OD-11 allowlist、sandbox 皆不影響上述判定（規格 §6.2 末段、ADR-02）。

**同步 attach 的操作身分（凍結）**：`attach_device` 在鎖內建立唯一 `_AttachOp`（同 by_id 已有進行中 op → 回 `SESSION_RECOVERING` 形狀，不開第二個 handle），鎖外 open；回鎖後**重驗**：op 仍是 `_attach_ops[by_id]`、session 仍存在且 `profile.device_by_id == by_id`、`bridge_generation` 未變、無 `_pending_drain`／`_draining_by_ids`、裝置仍在 `_devices`。任一失效 → 立即 `port.close()`（恰一次）、清 op，回應：裝置已不在 → `DEVICE_NOT_PRESENT`；drain 中 → T08 的 `SESSION_RECOVERING` 形狀；session 已不存在 → `SESSION_NOT_FOUND`；by_id 被換綁或 generation 改變 → `SESSION_RECOVERING` 形狀（呼叫端重試）。重驗通過才寫入 `_preopened_ports[by_id]`、清 released、`_save_state()`、`ATTACHING`、`_spawn_attach(by_id)`。

**預開 port 與 PROBE bridge（凍結）**：`_attach_by_id` 取到 `_preopened_ports[by_id]` 時，`_reresolve_profile_on_reattach` 與 `_attach_by_id_dynamic` 的 PROBE 改以 `UARTBridge("PROBE", …, preopened_port=port, owns_port=False)` 使用**同一個** port（不得再對同一裝置第二次獨占 open——同行程另一個 open file description 的 flock 與 `TIOCEXCL` 都會衝突，現況的 `except Exception: detected=None` 會把它靜默吞掉，使 `profile_source=="fallback"` 的 session 永遠不再 re-detect）；PROBE `stop()` 交還 port 而不關閉，之後正式 bridge 以 `preopened_port=port, owns_port=True` 接手。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_uart_exclusive.py`
  ```python
  from __future__ import annotations
  import os
  import subprocess
  import sys
  import time
  import pytest
  from sw_core.config import UartProfile
  from sw_core import serial_port as sp

  # 同檔混合平台（P0 T14 規則 2）：不放模組層平台 marker。下列 POSIX 測試集中在
  # class TestPosixExclusive（@pytest.mark.posix_only＋@pytest.mark.posix_pty＋需要時 process_rpc），
  # manager 層替身測試放 class TestManagerAttach（core_portable＋memory_only），
  # Windows COM 測試為獨立函式（windows_native＋windows_com）。片段為易讀以頂層函式呈現。

  @pytest.fixture
  def pty_path():
      m, s = os.openpty()
      path = os.ttyname(s)
      os.close(s)
      yield path
      os.close(m)

  def test_second_open_of_same_tty_is_held(pty_path):
      a = sp._PosixSerialPort(pty_path, UartProfile()); a.open()
      try:
          b = sp._PosixSerialPort(pty_path, UartProfile())
          with pytest.raises(sp.DeviceHeldError) as ei:
              b.open()
          assert ei.value.holder.holder_unknown is True   # 持有者是自身 pid，被排除 → 查不到
      finally:
          a.close()

  def test_external_flock_holder_reported_with_pid(pty_path):
      code = ("import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_NOCTTY); "
              "fcntl.flock(fd, fcntl.LOCK_EX); print('ok', flush=True); time.sleep(30)")
      child = subprocess.Popen([sys.executable, "-c", code, pty_path], stdout=subprocess.PIPE, text=True)
      try:
          assert child.stdout.readline().strip() == "ok"
          with pytest.raises(sp.DeviceHeldError) as ei:
              sp._PosixSerialPort(pty_path, UartProfile()).open()
          assert child.pid in ei.value.holder.pids and ei.value.holder.holder_unknown is False
      finally:
          child.kill(); child.wait()
      p = sp._PosixSerialPort(pty_path, UartProfile()); p.open(); p.close()   # 程序結束後可取得

  def test_tiocexcl_set_on_opened_fd(pty_path, monkeypatch):
      import fcntl, termios
      calls = []
      real = fcntl.ioctl
      monkeypatch.setattr(sp.fcntl, "ioctl", lambda fd, req, *a: (calls.append(req), real(fd, req, *a))[1])
      p = sp._PosixSerialPort(pty_path, UartProfile()); p.open(); p.close()
      assert termios.TIOCEXCL in calls

  def test_close_clears_tiocexcl_reopen_with_master_open(pty_path, monkeypatch):
      # R0-F3：fixture 全程持有 PTY master；close 若只解 flock、關 fd 而未 TIOCNXCL，第一次 plain open 即 EBUSY
      if os.geteuid() == 0:
          pytest.skip("EVIDENCE_MISSING: TIOCEXCL bypassed for root (§6.2)")
      import errno, fcntl, termios
      events = []
      real_ioctl, real_flock = fcntl.ioctl, fcntl.flock
      monkeypatch.setattr(sp.fcntl, "ioctl", lambda fd, req, *a: (events.append(("ioctl", req)), real_ioctl(fd, req, *a))[1])
      monkeypatch.setattr(sp.fcntl, "flock", lambda fd, op: (events.append(("flock", op)), real_flock(fd, op))[1])
      for _ in range(3):                                   # PROBE → 正式 bridge → recover 的 open／close 循環
          events.clear()
          p = sp._PosixSerialPort(pty_path, UartProfile()); p.open(); p.close()
          tail = [e for e in events if e in (("ioctl", termios.TIOCNXCL), ("flock", fcntl.LOCK_UN))]
          assert tail == [("ioctl", termios.TIOCNXCL), ("flock", fcntl.LOCK_UN)]   # 先解除獨占，再解鎖、關 fd
          try:
              fd = os.open(pty_path, os.O_RDWR | os.O_NOCTTY)   # 外部工具角度：一般 open 必須成功
          except OSError as e:
              pytest.fail(f"plain open after close must not fail, errno={errno.errorcode.get(e.errno)}")
          os.close(fd)

  def test_post_lock_reject_clears_excl(pty_path, monkeypatch):
      # R0-F3：TIOCEXCL 成功後被鎖後重掃拒絕，失敗路徑也必須 TIOCNXCL 才 close（成功路徑中 probe_holders 只在重掃呼叫）
      if os.geteuid() == 0:
          pytest.skip("EVIDENCE_MISSING: TIOCEXCL bypassed for root (§6.2)")
      monkeypatch.setattr(sp, "probe_holders", lambda *a, **k: sp.HolderReport((4242,), False, "ok"))
      with pytest.raises(sp.DeviceHeldError) as ei:
          sp._PosixSerialPort(pty_path, UartProfile()).open()
      assert ei.value.holder.pids == (4242,)
      monkeypatch.undo()
      fd = os.open(pty_path, os.O_RDWR | os.O_NOCTTY)          # 不得 EBUSY
      os.close(fd)

  def test_symlink_to_same_device_conflicts(pty_path, tmp_path):
      link = tmp_path / "by-id-link"; link.symlink_to(pty_path)
      a = sp._PosixSerialPort(pty_path, UartProfile()); a.open()
      try:
          with pytest.raises(sp.DeviceHeldError):
              sp._PosixSerialPort(str(link), UartProfile()).open()
      finally:
          a.close()

  def test_fd_not_inherited_by_child(pty_path):
      a = sp._PosixSerialPort(pty_path, UartProfile()); a.open()
      try:
          out = subprocess.run([sys.executable, "-c",
              "import os,sys; print(any(os.path.realpath(f'/proc/self/fd/{f}')==sys.argv[1] "
              "for f in os.listdir('/proc/self/fd')))", os.path.realpath(pty_path)],
              capture_output=True, text=True, close_fds=False)
          assert out.stdout.strip() == "False"
      finally:
          a.close()

  def test_holder_scan_permission_failure_is_unknown(tmp_path):
      r = sp.probe_holders("/dev/null", proc_root=str(tmp_path / "missing"))
      assert r == sp.HolderReport((), True, "permission")

  @pytest.mark.parametrize("msg,expected", [
      ("could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)", ("held", 5)),
      ("could not open port 'COM3': OSError(22, 'The semaphore timeout period has expired.', None, 121)", ("other", 121)),
      ("could not open port 'COM9': FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)", ("not_found", 2)),
      ("garbage", ("other", None)),
  ])
  def test_classify_pyserial_open_error(msg, expected):
      assert sp.classify_pyserial_open_error(msg) == expected
  ```
  表格補充（同檔；manager 層使用 `stage0_fakes.make_manager`，以 monkeypatch 替換 `open_serial_port` 為回傳可控替身）：

  | 測試名 | 斷言 |
  |---|---|
  | `test_pyserial_access_denied_raises_device_held_unknown` | 替身 `serial.Serial.open` 擲 `SerialException("…PermissionError(13, 'Access is denied.', None, 5)")` → `_PySerialPort.open()` 擲 `DeviceHeldError`，`holder == HolderReport((), True, "unsupported")` |
  | `test_attach_device_success_is_sync_and_attaching` | RELEASED session；開檔成功 → 回 `{"ok": True, …}`、state `ATTACHING`、`_preopened_ports[by_id]` 存在、`_spawn_attach` 被呼叫 |
  | `test_attach_device_held_keeps_released` | 開檔擲 `DeviceHeldError(pids=(4321,))` → `{"ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [4321], "holder_unknown": False, "selector": "COM0"}`；state 仍 `RELEASED`；`state.json` released 未變（檔案 bytes 與 mtime 不變）、替身 port 無任何 `writes`、WAL 無新 TX |
  | `test_external_open_without_flock_is_rejected` | （`posix_only`、`posix_pty`、`process_rpc`）子行程只 `os.open(pty, O_RDWR\|O_NOCTTY)`、不 flock → `_PosixSerialPort.open()` 擲 `DeviceHeldError`、`child.pid in holder.pids`、fd 已關閉 |
  | `test_attach_device_force_rejects_plain_external_holder` | （`posix_only`、`posix_pty`、`process_rpc`；先寫、須先 FAIL：現況 force 略過預檢後開檔成功）RELEASED session；子行程只 `os.open(pty, O_RDWR\|O_NOCTTY)`、不 flock、印 `ok` 後持有 → `attach_device("COM0", force=True)` 回應**完全等於** `{"ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [child.pid], "holder_unknown": False, "selector": "COM0"}`；state 仍 `RELEASED`；`state.json` bytes 與 mtime 不變；`_preopened_ports` 不含該 by_id、`_spawn_attach` 未被呼叫；PTY master 端以 `select` 讀 1.0 s 為 0 byte（UART 零 TX）；WAL 無新 TX 記錄；子行程結束後同一呼叫回 `ok: True`、`ATTACHING` |
  | `test_auto_attach_plain_external_holder_zero_tx` | （`posix_only`、`posix_pty`、`process_rpc`）子行程先普通 `open` 持有 PTY slave（不 flock）後才啟動 throwaway daemon（`SERIALWRAP_BY_ID_DIR` 只含該 symlink）→ 5 s 內 session 為 `DETACHED`、`last_error == "ATTACH_FAILED:DeviceHeldError"`、從未出現 `ATTACHING`／`READY`；PTY master 全程讀到 0 byte（無 prompt probe、無登入 TX）；`device attach --selector COM0 --force` 回 `DEVICE_STILL_HELD`、`pids == [child.pid]`、`holder_unknown: false` |
  | `test_post_lock_scan_partial_permission_with_pids_rejects` | monkeypatch `probe_holders` 回 `HolderReport((123,), False, "permission")`（三步皆成功）→ `_PosixSerialPort.open()` 擲 `DeviceHeldError`、`holder.pids == (123,)`、fd 已關閉（`os.fstat` 擲 `EBADF`）、`apply_termios`／`tcsetattr` 未被呼叫 |
  | `test_holder_scan_failure_opens_with_warning` | `probe_holders` 回 `status="permission"`、`pids=()` → open 成功、log WARNING 含「無法確認」、`daemon status.foreign_holders` 含 `holder_unknown: true` |
  | `test_two_daemons_compete_for_same_tty` | （`posix_only`、`posix_pty`、`process_rpc`）兩個 throwaway daemon（獨立 socket／state／run，`SERIALWRAP_BY_ID_DIR` 指向同一個只含該 PTY symlink 的 sandbox）先後啟動 → 恰一個 session 為 `ATTACHED`／`READY`，另一個 `DETACHED` 且 `last_error == "ATTACH_FAILED:DeviceHeldError"`；PTY 對端只看到一個 reader |
  | `test_force_spawn_does_not_bypass_uart_lock` | `daemon start --force-spawn` 起第二個 daemon 時同上結果（`--force-spawn` 只略過 CLI 跨 socket 預檢，ADR-02） |
  | `test_attach_op_invalidated_by_release_during_open` | 替身 `open()` 阻塞期間另一執行緒 `release_device`／`bind_session`／`update_devices([])` → 回應分別為 `SESSION_RECOVERING` 形狀／`SESSION_RECOVERING` 形狀／`DEVICE_NOT_PRESENT`；候選 port `close_calls == 1`；未寫入 `_preopened_ports` |
  | `test_concurrent_attach_device_single_open` | 兩執行緒同時 `attach_device("COM0")` → `open()` 只被呼叫一次 |
  | `test_preopened_port_reused_by_probe_and_bridge` | `profile_source=="fallback"` 的動態 session 經 `attach_device` 後，PROBE 與正式 bridge 使用同一個 port 物件、`open()` 總呼叫數 1、re-detect 有執行（`profile_source` 更新）、PROBE `stop()` 未關閉 port |
  | `test_attach_device_preflight_has_holder_unknown_field` | 預檢 `pids=[99]` → 回應含 `"holder_unknown": False` |
  | `test_attach_device_not_present_on_open` | 開檔擲 `DeviceNotPresentError` → `DEVICE_NOT_PRESENT` |
  | `test_attach_device_other_error_detaches` | 開檔擲 `OSError(5)` → `ok: True`、state `DETACHED`、`last_error == "ATTACH_FAILED:OSError"` |
  | `test_hotplug_attach_held_sets_last_error` | 熱插 `_attach_by_id` 開檔被占 → `DETACHED`、`last_error == "ATTACH_FAILED:DeviceHeldError"`，不啟動 reader |
  | `test_dynamic_released_survives_two_restarts` | 動態 session release → 重建 `SessionManager` 兩次（同 `state.json`）→ `by_id in _released_by_ids` 且 `state.json["released"]` 仍含該列（DF-04） |
  | `test_dynamic_released_attachable_after_restart` | 動態 session release → 重建 `SessionManager` → `get_session("<COM>")` 存在且 state `RELEASED`（init 依 `_loaded_released` 實體化）→ `attach_device("<COM>")` 成功、`ATTACHING` |
  | `test_released_unavailable_uboot_template_remains_blocked` | released `uboot-template` 重啟時已無可解析 template → 不合成空 `ready_probe` profile；保留 `_loaded_released` 與 `_released_by_ids` 阻擋並記 WARNING（符合 DF-04 缺失 template 規則） |
  | `test_offline_tick_keeps_released` | RELEASED session 裝置離線後 `update_devices([])` → state 仍 `RELEASED` |
  | `test_unconsumed_preopened_port_closed_on_release` | attach 成功但 attach thread 尚未消費時 release → 該 port `close()` 恰一次 |
  | `test_release_then_external_plain_open_succeeds` | （`posix_only`、`posix_pty`、`process_rpc`；非 root，root 下 skip 記「證據不足」；R0-F3，修正前必 FAIL）測試持有 PTY master 全程不關；throwaway daemon 對該 slave 到 `READY` → `device release` 回 ok → 子行程（模擬外部燒錄工具）一般 `os.open(slave, O_RDWR\|O_NOCTTY)` **成功**（exit 0；`EBUSY` 判 FAIL）、寫 `b"X"` 後關閉，master 端讀到 `b"X"` → `device attach` 回 `ok: True` → 20 s 內 `READY` |
  | `test_dynamic_probe_then_bridge_reopen_native` | （`posix_only`、`posix_pty`、`process_rpc`；非 root；R0-F3）無 pin／sticky 的動態 session（非 `attach_device` 路徑，PROBE bridge 自行 open／close 後正式 bridge 再 open），測試持有 PTY master 並在收到 `\n` 時回 prompt → 20 s 內 `profile_source == "detected"` 且 state `READY`；`last_error` 從未為 `ATTACH_FAILED:DeviceHeldError`（PROBE 關閉時未 TIOCNXCL 會讓正式 open EBUSY） |
  | `test_native_second_open_access_denied`（`windows_native`＋`windows_com`；不得在模組層帶任何 POSIX marker，否則 Windows 上永遠被 skip） | `SW_TEST_COM_PAIR` 的 COMa 由 bridge 開啟後，第二個 `_PySerialPort(COMa).open()` 擲 `DeviceHeldError` 且 `holder_unknown is True`；正式 gate 以 runner `summary.json` 確認此 test 實際執行（非 skip） |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_uart_exclusive.py` → 預期 `AttributeError: module 'sw_core.serial_port' has no attribute 'DeviceHeldError'`。commit：`test(uart): BC-04 獨占開檔、holder 診斷與 SC-05 同步收回失敗測試`
- [ ] **B1 實作**：
  - `_PosixSerialPort.open`：依 Interfaces；`EBUSY` 來自 `os.open`（他人已設 TIOCEXCL）與 `BlockingIOError` 來自 `flock`，兩者都 `probe_holders(realpath, exclude_pid=os.getpid())` 後 raise `DeviceHeldError`；`os.open` 的 `ENOENT` → `DeviceNotPresentError`。
  - `_PySerialPort.open`：`SerialException` → `classify_pyserial_open_error(str(exc))`：`held` → `DeviceHeldError(holder=HolderReport((), True, "unsupported"), os_error=str(exc))`；`not_found` → `DeviceNotPresentError`；其他維持 `OSError(errno or EIO, str)`。
  - `probe_holders`：由 `SessionManager._probe_external_holder` 搬移並改回傳 `HolderReport`；`_probe_external_holder` 保留為包裝，回 `{"pids": list, "holder": first|None, "holder_unknown": bool}`（self-test RELEASED 分支沿用 `pids`／`holder`，新鍵附加於尾端）。Windows（`os.name=="nt"`）直接回 `unsupported`。
  - `_PosixSerialPort.open` 三步成功後依「非合作式外部 holder」凍結規則再掃一次 holder。
  - `_PosixSerialPort.close`／`_release_exclusive`：依 Interfaces；TIOCEXCL 成功之後的所有失敗返回與正常 `close()` 都走「`TIOCNXCL` → `LOCK_UN` → `os.close`」；`TIOCNXCL` 非 hangup 類失敗只 WARNING、仍關 fd。`UARTBridge.stop()`／`finish_drain` 在 RX thread join 與 TX 收斂之後才呼叫 `port.close()`（I/O 收斂後、關閉有效 tty fd 前解除獨占；arch §4.2 關閉序）。
  - `attach_device`：依凍結表與「同步 attach 的操作身分」；同步開檔使用 `open_serial_port(real_path, session.profile.uart)` 後 `port.open()`；重驗通過才 `self._preopened_ports[by_id] = op`，再清 released、`_save_state()`、`ATTACHING`、`_spawn_attach(by_id)`。`_attach_by_id` 取 `op = self._preopened_ports.pop(by_id, None)`，PROBE 與正式 bridge 依「預開 port 與 PROBE bridge」共用 `op.port`；正式 bridge `start()` 失敗時由正式 bridge 負責 close（`owns_port=True`），不得雙重 close。
  - DF-04：`__init__` 將 `_loaded_released` 中**不在** `self._sessions` 的列**直接實體化**為 `RELEASED` session（COM 取自 session_id `<profile>:<COM>`、profile 取自 pins／provenance 中的 template，套回 `released_by`／`released_at`／`released_reason`），並把 by_id 加入 `_released_by_ids`——否則 `_spawn_attach` 對 released by_id 早退（`session_manager.py` 約 2291 行），session 永遠不會被實體化，`device attach <COM>` 找不到 selector、只留下封鎖而沒有解除路徑；template 已不存在而無法實體化者保留於 `_loaded_released` 並持續阻擋，log WARNING。`_save_state` 的 `released` = 記憶體 RELEASED session 列 ∪ `_loaded_released` 中未實體化的列。`_mark_missing_devices_locked` 跳過 `state == "RELEASED"`。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_uart_exclusive.py tests/test_serial_port.py tests/test_device_handoff.py tests/test_holder_probe_portability.py tests/test_explicit_target_device_ownership.py tests/test_windows_claim.py tests/test_multi_open_detect.py tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py tests/test_flash_endpoint.py tests/test_resource_markers.py tests/test_contracts_rpc_schema.py`；`python3 -m tools.contracts check`；全套依 §0 第 4 點，並檢查是否有既有測試在 bridge 開啟後以路徑再開同一 PTY slave（BC-04 預期行為；若有，**先**把該檔與測試名追加到本 Task「測試資產變更」並經獨立審閱，PR 註明，才可修改）。
- **測試資產變更**：`tests/test_resource_markers.py` 的 `windows_com` 使用數斷言（見 Files）。B2 審閱後追加下列 T10 測試調整：
  - `tests/test_stage0_uart_exclusive.py`：新增本任務測試，涵蓋預開 handle／holder 診斷／零 TX、PROBE 與正式 bridge 共用 handle、release／bind／hot-unplug 對在途 PROBE 的取消與收斂、同步 open 失效分類、Windows ACCESS_DENIED 到 `DEVICE_STILL_HELD{holder_unknown:true}` 的 RPC 映射、重複 attach 與 RELEASED 清理；PROBE drain 使用阻塞 reader 驗證未收回前不關閉 handle，另驗證遺失的 uboot template 維持持久化阻擋。
  - `tests/test_windows_claim.py::test_no_profiles_windows_passthrough_claim`：注入 fake serial port，避免無硬體的 `COM8` 機制測試觸碰主機 COM 路徑。
  - `tests/test_boot_quiet.py::TestReadyReconfirmGate`：mock bridge 時同步 mock 預開 serial port，維持原測試的記憶體隔離。
  - `tests/test_contracts_gap_codes.py::TestGapLastError`：三個使用 mocked bridge 的 attach case 注入 fake serial port，避免預開階段開啟實際裝置。
  - `tests/test_device_handoff.py::TestAttachDevice` 與 `TestAttachByIdReleasedBackstop`：同步 attach 與 RELEASED backstop 使用 fake serial port，保持零硬體測試邊界。
  - `tests/test_explicit_target_device_ownership.py::TestExplicitTargetDeviceOwnership`：mock attach bridge 的 case 注入 fake serial port。
  - `tests/test_holder_probe_portability.py::TestHolderProbePortability`：更新不可讀 `/proc` 時的凍結結果，要求 `holder_unknown: true`。
  - `tests/test_human_agent_coexist.py::FakeTarget`、`tests/test_multiagent_e2e.py::FakeTarget`：關閉 pytest 行程持有的 slave fd，避免 BC-04 把測試自身誤判為外部 holder；PTY 讀取在暫態錯誤時繼續等待。
  - `tests/test_profile_reresolve_on_attach.py::TestAttachWiring` 與 `_Base._no_bridge`：對新 `preopened_op` 參數放寬替身簽章、為 attach 注入 fake serial port，並讓 PROBE 替身回傳即時安全 drain receipt，符合 BC-04 交接介面。
  - `tests/test_session_bind.py::TestSessionBind`：四個 mock bridge 的 attach case 注入 fake serial port。
  - `tests/test_stage0_session_drain.py::test_release_waits_for_inflight_attach_handle_receipt`：注入 fake serial port；`test_shutdown_waits_inflight_attach_before_wal_close` 以「無殘留 attach op／預開 handle 且 close 恰一次」取代對舊 cleanup callback 的觀察，仍斷言 shutdown 未越過阻塞中的開埠及 WAL 關閉順序。
  - `tests/test_service_human_console.py` 的 `admission_exempt` 變更是 worktree 既有 T08 測試調整，與 T10 無關，本任務未修改。
- [ ] **B3 commit**：`feat(uart): BC-04 POSIX 以 flock＋TIOCEXCL 獨占 UART 並回報 holder，device attach 同步收回`

---

## Task T11：Gate 錯誤碼：SC-01、SC-02、SC-03、SC-06、DF-02

- **範圍**：SC-01（`RECOVERING`＋CmdSubmit／InteractiveOpen → `SESSION_RECOVERING`）；SC-02（`FLASHING`＋CmdSubmit／InteractiveOpen → `FLASHING_BUSY`）；SC-03（execute-time 與 file.* manager gate 回 `AUTOBOOT_QUIET` 時附 `retry_after_s`）；SC-06（pending 計數納入「已 cancel 但 worker 仍執行」）；DF-02（RPC 帶入 `source` 以 `human:` 開頭時 gate 一律以 agent 對待，也不升格為 interactive；owner 確認項 #5 預設）。依據：規格 §5.3、§5.4；盤點 05 §4.4、§5.1、§16.4；arch §5.4 Admission、§5.11。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max（00-master-plan §2：arbiter／gate 狀態機、可信來源跨層傳遞、平台鎖與監管停止交接屬 Codex）
- **Changelog fragment**：`changelog.d/rust-p1-gate-error-codes.md`（`type: fix`，`scope: session`）：RECOVERING 與 FLASHING 狀態送命令改回 `SESSION_RECOVERING`／`FLASHING_BUSY`；已排隊命令遇 autoboot quiet 也附 `retry_after_s`；已取消但仍執行中的命令計入 pending 上限；RPC 偽造的 `human:` 來源不再繞過 gate。
- **BLOCKED-OWNER**：無。

**Files**
- Create: `tests/test_stage0_gate_codes.py`
- Modify: `sw_core/service.py`（`_resolve_session_id`、`command.submit`、`file.push`／`file.pull`、`_on_console_line`、`_send_cb`）、`sw_core/session_manager.py`（`interactive_open`、`_agent_gate_reject_locked`、`execute_command`、`file_push`／`file_pull`、`_recover_after_failure`、`_handle_reboot_command`）、`sw_core/arbiter.py`（`_QueuedCommand.trusted_human`、`submit(trusted_human=…)`、`_count_pending_locked`、`_running`、history eviction）；契約同步 `contracts/behavior_changes.yaml`、`contracts/error_codes.yaml`、`contracts/rpc_schema.json`、`contracts/daemon_logging.yaml`、`sw_core/assets/skill/SKILL.md`、`SKILL_WINDOWS.md`；規格敘述 `docs/serialwrap-spec.md`

**Interfaces**
- Consumes：既有 `_resolve_session_id`、`arbiter.submit`、`execute_command`。
- Produces（Python）：
  ```python
  SerialwrapService._resolve_session_id(self, selector: str, *, source: str = "agent",
                                        trusted_human: bool = False) -> tuple[str | None, dict[str, Any] | None]
  CommandArbiter.submit(self, *, session_id: str, command: str, source: str, mode: str, timeout_s: float,
                        priority: int = 10, expected_duration_s: float | None = None,
                        completion_regex: str | None = None, trusted_human: bool = False) -> dict[str, Any]
  CommandArbiter._running: dict[str, str]          # session_id → 執行中 cmd_id
  SessionManager.execute_command(..., trusted_human: bool | None = None) -> dict[str, Any]
      # None（未指定）＝沿用現況 source.startswith("human:")：保護直接呼叫 manager 的既有測試
      #   （tests/test_boot_quiet.py:921,1220、tests/test_ready_reconfirm_bounds.py:246、
      #    tests/test_session_bind.py:192,218、tests/test_multiagent_stress.py:233）
      # True／False＝明確覆寫；RPC 路徑（SerialwrapService._send_cb）一律明確傳 False 或 True
  SessionManager.file_push(..., trusted_human: bool | None = None) -> dict[str, Any]
  SessionManager.file_pull(..., trusted_human: bool | None = None) -> dict[str, Any]
      # None 沿用直接呼叫 manager 的來源前綴語意；file.* RPC 明確傳 False
  SerialwrapService._send_cb(self, session_id, command, source, cmd_id, timeout_s, mode,
                             expected_duration_s=None, *, trusted_human: bool = False, **kw) -> dict[str, Any]
  ```
  `trusted_human=True` 只由 `SerialwrapService._on_console_line`（daemon 內部 human console 路徑）設定；RPC 參數不得影響它。`source` 字串本身（WAL、命令紀錄）不變。arbiter worker 呼叫 `send_cb` 時**只在 `item.trusted_human is True` 時才傳 `trusted_human=True` 關鍵字**（比照現況 `completion_regex` 的條件傳遞），以相容 `tests/test_issue24_heartbeat.py::fake_send` 等固定參數的替身；`_send_cb` 收不到該關鍵字即為 `False`，因此 RPC 來源的 `human:` 字串一律以 agent 對待（DF-02），而直接呼叫 manager 的既有測試維持現況語意。**測試資產變更**：`tests/test_boot_quiet.py::TestSubmitQuietGate::test_human_source_submit_not_gated` 改為 `test_rpc_human_source_submit_is_gated`，依 DF-02 驗證 RPC 偽造來源遭 quiet gate 拒絕；`tests/test_arbiter_flush_on_recovery.py::_mock_exec` 接受可選 `trusted_human` keyword，以配合 worker 傳遞可信旗標；`tests/test_service_human_console.py::TestServiceHumanConsole::test_human_console_regular_command_uses_line_mode` 與 `test_human_console_sudo_vim_is_interactive` 明確斷言 `trusted_human=True`（同檔既有 `admission_exempt=True` 調整屬 T08）；`tests/test_stage0_gate_codes.py::test_rpc_human_source_not_promoted_on_timeout` 為 fake bridge 補空字串 RX 回應，維持逾時恢復路徑的替身契約，未放寬斷言；`test_gate_reject_zero_side_effects` 改用真 `UARTBridge`、`FakePort` 與 `dir == "TX"` WAL 篩選；另新增本 Task 的邊界與回歸案例（見下表）。
- Rust 對應：arch §5.4 TxClass gate「source 以 `human:` 開頭時另見 DF-02」、Admission 依 SC-01／02／03 檢查；§5.10 arbiter pending（SC-06 計數）。

**錯誤優先序（凍結，submit-time `_resolve_session_id`）**：查無 → 原樣；`RECOVERING` → `{"ok":false,"error_code":"SESSION_RECOVERING","session":…}`；`FLASHING` → `{"ok":false,"error_code":"FLASHING_BUSY","session":…}`；其餘非 READY 依現況（`PROFILE_NOT_COMMAND_CAPABLE`／`SESSION_NOT_READY`）；READY 時非 `trusted_human` 依現況 `READY_UNCONFIRMED` → `AUTOBOOT_QUIET`。execute-time（`_agent_gate_reject_locked`）的 `AUTOBOOT_QUIET` 回應改為 `{"ok":false,"error_code":"AUTOBOOT_QUIET","selector":…,"retry_after_s":<quiet 剩餘秒數 round 1 位；只剩 pending 時 5.0>}`（只補 `retry_after_s`；`selector` 與 `session` 形狀差異保留，ERR-7.3-8）；`READY_UNCONFIRMED` 不帶 `retry_after_s`。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_gate_codes.py`（以 `SerialwrapService` 搭配 `stage0_fakes.make_manager` 的 manager 注入；若 service 建構需 profiles，使用 `SerialwrapService([make_profile()], templates=[])` 並在 `start()` 前直接操作 `_sessions`）
  ```python
  from __future__ import annotations
  import pytest
  from sw_core.arbiter import CommandArbiter

  @pytest.mark.parametrize("state", ["RECOVERING", "FLASHING"])
  def test_submit_time_state_codes(stage0_service, state):
      svc, sess = stage0_service
      sess.state = state
      expected_code = {"RECOVERING": "SESSION_RECOVERING", "FLASHING": "FLASHING_BUSY"}[state]
      r = svc.rpc("command.submit", {"selector": "COM0", "cmd": "ls"})
      assert r["ok"] is False and r["error_code"] == expected_code and r["session"]["state"] == state

  @pytest.mark.parametrize("state", ["RECOVERING", "FLASHING"])
  def test_interactive_open_state_codes(stage0_service, state):
      svc, sess = stage0_service
      sess.state = state
      expected_code = {"RECOVERING": "SESSION_RECOVERING", "FLASHING": "FLASHING_BUSY"}[state]
      r = svc.rpc("session.interactive_open", {"selector": "COM0"})
      assert r["error_code"] == expected_code

  def test_execute_time_quiet_has_retry_after(stage0_service):
      svc, sess = stage0_service
      sess.state = "READY"; sess.bridge = object()
      sess.ready_reconfirm_pending = True
      r = svc._sessions.execute_command("p:COM0", "ls", "agent:x", "c1", timeout_s=5.0, mode="line")
      assert r == {"ok": False, "error_code": "AUTOBOOT_QUIET", "selector": "COM0", "retry_after_s": 5.0}

  def test_rpc_human_source_is_gated_like_agent(stage0_service):
      svc, sess = stage0_service
      sess.state = "READY"; sess.ready_reconfirm_pending = True
      r = svc.rpc("command.submit", {"selector": "COM0", "cmd": "ls", "source": "human:spoof"})
      assert r["error_code"] == "AUTOBOOT_QUIET"

  def test_canceled_but_running_counts_toward_pending():
      import threading
      gate = threading.Event()
      arb = CommandArbiter(send_cb=lambda *a, **k: (gate.wait(5), {"ok": True, "stdout": ""})[1])
      arb.register_session("s")
      try:
          import time
          first = arb.submit(session_id="s", command="c0", source="t", mode="fg", timeout_s=1.0)
          deadline = time.monotonic() + 2.0
          running = False
          while time.monotonic() < deadline:
              with arb._lock:
                  running = arb._running.get("s") == first["cmd_id"]
              if running:
                  break
              time.sleep(0.01)
          assert running, "worker 未在 2 s 內取件"
          arb.cancel(first["cmd_id"])
          with arb._lock:
              assert arb._count_pending_locked("s") == 1
      finally:
          gate.set(); arb.unregister_session("s")
  ```
  > `stage0_service` fixture 寫在本檔：建立 `SerialwrapService`（profiles＝`[make_profile()]`、`templates=[]`），回傳 `(svc, svc._sessions.get_session("COM0"))`，teardown 呼叫 `svc.stop()`。

  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_file_push_quiet_has_retry_after` | `SessionManager.file_push` 遇 quiet → 回應含 `retry_after_s`（剩餘秒數，1 位小數） |
  | `test_execute_time_unconfirmed_has_no_retry_after` | `ready_reconfirm_failed=True` → `READY_UNCONFIRMED`，無 `retry_after_s` |
  | `test_rpc_human_source_not_promoted_on_timeout` | `source="human:x"` 的 RPC 命令逾時 → 不回 `PROMOTE_HUMAN_INTERACTIVE`，走 agent 的 `_recover_after_failure` 分支 |
  | `test_console_line_trusted_human_bypasses_quiet` | `_on_console_line` 送出的命令 `trusted_human=True`，quiet 中仍入列（基線語意） |
  | `test_record_source_string_unchanged_for_spoof` | DF-02 下命令紀錄 `source == "human:spoof"`（只改 gate，不改稽核字串） |
  | `test_queue_full_counts_canceled_running` | 255 accepted＋1 canceled-running → 下一筆 `SESSION_QUEUE_FULL` |
  | `test_released_still_session_not_ready` | `RELEASED` 下 submit → `SESSION_NOT_READY`（規格 §5.3 RELEASED 列，現況保留；回歸防護，既綠） |
  | `test_direct_manager_human_source_keeps_legacy_semantics` | 直接 `mgr.execute_command(sid, "echo hi", "human:tester", "c1")`（未傳 `trusted_human`）→ 與現況相同走 human 語意（quiet 中仍執行） |
  | `test_worker_omits_trusted_human_kwarg_for_rpc` | 以固定 7 參數的 `send_cb` 替身（同 `test_issue24_heartbeat.py::fake_send`）建 arbiter，RPC 來源命令不擲 `TypeError` |
  | `test_worker_passes_trusted_human_kwarg_when_enabled` | worker 只在明確 trusted-human 命令時傳 `trusted_human=True` |
  | `test_console_line_reaches_uart_during_quiet_with_real_bridge` | quiet 中的內部 console line 經真 bridge 產生預期 UART bytes 與 WAL TX |
  | `test_manager_quiet_window_returns_remaining_retry_after` | execute 與 file push 的 active quiet window 回傳剩餘秒數（大於 0、至多 30.0，且非 pending-only 的 5.0） |
  | `test_file_rpc_rechecks_spoofed_human_as_agent` | file.push／file.pull 在第一層通過後 quiet 被 arm，manager 仍收到 `trusted_human=False` 並拒絕 |
  | `test_execute_time_quiet_command_record_has_no_retry_after` | RPC 的 `source="human:spoof"` 在 queue 後遇 execute-time quiet gate，`command.get` 記錄仍為 `AUTOBOOT_QUIET`、保留來源字串且不暴露 `retry_after_s`；零 UART／WAL TX |
  | `test_canceled_running_record_survives_cross_session_history_eviction` | 小型 history 上限下，另一 session 的完成記錄遭淘汰時，仍執行中的 canceled 記錄保留且持續計入 pending |
  | `test_gate_reject_zero_side_effects` | 使用真 `UARTBridge`／`FakePort` 與 `dir == "TX"` 的 WAL 記錄，確認 RECOVERING／FLASHING／quiet 的 command、file 與 interactive-open 拒絕無 UART write 或新 TX |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_gate_codes.py` → 預期 `'SESSION_NOT_READY' != 'SESSION_RECOVERING'` 等斷言失敗與 `AttributeError: 'CommandArbiter' object has no attribute '_running'`。commit：`test(session): SC-01/02/03/06 與 DF-02 gate 錯誤碼失敗測試`
- [ ] **B1 實作**：依錯誤優先序；`command.submit` 與兩個 file RPC 呼叫 `_resolve_session_id(..., trusted_human=False)`；RPC 呼叫 `file_push`／`file_pull` 時也明確傳 `trusted_human=False`，manager 預設 `None` 時保留直接呼叫既有來源前綴語意；`_on_console_line` 以 `trusted_human=True` 呼叫 `arbiter.submit`；worker 僅在 `item.trusted_human` 為 True 時傳該 keyword，`_send_cb` 對 manager 明確傳 `trusted_human`；`execute_command`、`_execute_foreground`、`_recover_after_failure` 與 file manager gate 都依解析後的 `human` 判斷，不直接信任 RPC `source`；`interactive_open` 在 state gate 先回 `RECOVERING`／`FLASHING` 對應碼；quiet 回應依目前 active window 回傳剩餘秒數、僅 reconfirm pending 時回 `READY_RECONFIRM_RETRY_S`；SC-06 用 `_running` 保留執行命令身分，pending 額外計入已 cancel 但仍由 worker 執行的命令，history eviction 跳過 `_running.values()`，worker 在成功或例外收尾、從 `_running` 移除後才淘汰 history。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_gate_codes.py tests/test_boot_quiet.py tests/test_ready_reconfirm_bounds.py tests/test_issue26_interactive_guard.py tests/test_command_guard.py tests/test_stress_submit.py tests/test_bounded_memory.py tests/test_flash_guard.py tests/test_service_human_console.py tests/test_cowork_session_usability.py tests/test_issue24_heartbeat.py tests/test_session_bind.py tests/test_multiagent_stress.py 'tests/test_bootloader_recovery.py::TestInteractiveOpenAllowAttached::test_busy_paths_after_expired_recovery_still_run_post_close' tests/test_contracts_keep_baseline.py::test_daemon_logging_all_calls_listed`；`python3 -m tools.contracts check`（SC-01／02／03／06、DF-02 → `IMPLEMENTED`，錯誤碼契約、RPC schema、兩份 SKILL 受管區段同步；`retry_after_s` 只在直接 submit／manager 回應提供，`command.get` 記錄不包含）；全套依 §0 第 4 點。
- [ ] **B3 commit**：`fix(session): SC-01/02/03/06 修正 gate 錯誤碼與 pending 計數，DF-02 拒絕 RPC 偽造 human 來源`

---

## Task T12：SC-13 lease tick＋tombstone、SC-14 flash probe 資格、SC-16 console 行上限

- **範圍**：SC-13（agent interactive lease 到期由 1 s tick 主動處理：還原 human／回放，保留 tombstone，後續操作回 `INTERACTIVE_EXPIRED`；touch 語意保留）；SC-14（flash probe 候選須不在 RELEASED、RECOVERING 或 drain 中，且無前景操作；`enter_flashing` 加 guard；FlashSyncMatched 清 DeferredBuffer 由 T09 `invalidate_deferred("flash")` 完成）；SC-16（console line buffer 上限 16385 B；被拒整行 `CMD_TOO_LONG`／`SESSION_QUEUE_FULL`／`SESSION_NOT_READY` 寫 WARNING log）。依據：規格 §5.1.3、§5.3、§5.4；盤點 05 §10.5、§12.5，06 §6.4、§12.9，10 §G-02；arch §5.10、§5.11 Lease。
- **Test author model**：haiku-xhigh　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-lease-flash-console-limits.md`（`type: fix`，`scope: session`）：agent interactive lease 到期改由背景 tick 主動收回並保留 tombstone；flash 認線排除 released／recovering／drain 中與前景忙碌的 session；human console 單行超過 16385 bytes 或被拒時記 WARNING。
- **BLOCKED-OWNER**：SC-14 的 MCU flash 實機驗證（`/dev/ttyMCU` 真機程序，CLAUDE.md MCU 段）需實板與 flasher。

**Files**
- Create: `tests/test_stage0_lease_flash_console.py`
- Modify: `sw_core/session_manager.py`（`reconcile_readiness` 呼叫 `_expire_interactive_leases_tick`、`_refresh_interactive_locked`、`attach_console`／`interactive_open` 的到期收尾 admission gate、`interactive_send`／`interactive_status`／`interactive_close` 的 tombstone 判定、`collect_flash_candidates_and_mark`、`enter_flashing`）、`sw_core/uart_io.py`（`_consume_console_input`、`ConsoleClient.line_overflow`）、`sw_core/service.py`（`_on_console_line` 檢查 submit 結果並 log、`_on_flash_open` 拒絕入場時釋放 probe 狀態）、`sw_core/constants.py`（`CONSOLE_LINE_MAX_BYTES = 16385`、`INTERACTIVE_TOMBSTONE_MAX = 16`）
- Modify: `contracts/behavior_changes.yaml`（SC-13／14／16 狀態）、`contracts/error_codes.yaml`（`INTERACTIVE_EXPIRED` 的 SC-13 狀態、scope、contract tests；`SESSION_INTERACTIVE_BUSY` 收錄 ownership restore gate）、`contracts/rpc_schema.json`（interactive open/close 錯誤 side effects 與 close 錯誤順序，遞增 `x-catalog-revision`）、`contracts/daemon_logging.yaml`（新增 WARNING 模板並同步 source 行）、`sw_core/assets/skill/SKILL.md`、`sw_core/assets/skill/SKILL_WINDOWS.md`（重新產生受管 error code 區段）
- Modify（測試資產）：`tests/test_refine_ownership.py`、`tests/test_contracts_keep_baseline.py`（變更理由見「測試資產變更」）

**Interfaces**
- Produces（Python）：
  ```python
  SessionManager._expire_interactive_leases_tick(self, now: float | None = None) -> int   # 回傳本次到期數；post action 在鎖外執行
  ConsoleClient.line_overflow: bool = False          # dataclass 新欄位（預設 False）
  # 凍結 log 樣板（logger "serialwrap"、WARNING）
  "serialwrap: console 行超過 %d bytes 已丟棄（client=%s）"
  "serialwrap: console 行被拒（session=%s client=%s error_code=%s bytes=%d）"
  ```
- Rust 對應：arch §5.11 Lease「actor 每 1s tick 主動處理到期（SC-13），到期後保留 tombstone」、§5.10 interactive tombstone 每 session 16 FIFO、console line buffer 16385 B；§5.3 FlashSyncProbeStart guard。

**行為規格**
1. tick：`reconcile_readiness`（DeviceWatcher 每 1.0 s 呼叫）開頭呼叫 `_expire_interactive_leases_tick()`：持鎖找 `status=="active"`、非 human owner、`expired()` 的 lease → 呼叫 `_close_interactive_locked(session, interactive_id=…, terminal_status="expired", keep_tombstone=True)`；鎖外依序執行 post action（還原 human、回放 deferred；回放受 T09 `owner_gen` 規則限制）。現況 `_close_interactive_locked` 會把 `status` 設 `"closed"` 並 `self._interactive.pop(lease_id)`（`session_manager.py` 約 3244 行），因此「先設 expired 再 close」會連 tombstone 一起刪除；本 Task 為其新增兩個關鍵字參數：`terminal_status: Literal["closed","expired"] = "closed"`、`keep_tombstone: bool = False`——先完成 ownership 收回與 post action 準備，最後依參數設定 `lease.status`，`keep_tombstone=True` 時**不** pop、改記入 `self._tombstones[session_id]`（`collections.deque`，依建立先後），超過 `INTERACTIVE_TOMBSTONE_MAX` 時淘汰最舊者並自 `_interactive` 刪除。既有呼叫點維持預設值（行為不變）；`interactive_send` 現況遇 `expired()` 的分支也改用 `terminal_status="expired", keep_tombstone=True`。
2. `interactive_send`／`interactive_status`／`interactive_close` 遇 `lease.status == "expired"` → `{"ok":false,"error_code":"INTERACTIVE_EXPIRED","interactive_id":…}`（現況 `interactive_send` 以 `lease.status != "active"` 先回 `INTERACTIVE_NOT_FOUND`，判斷順序改為先查 `expired`）；lease 不存在 → `INTERACTIVE_NOT_FOUND`（現況）。`interactive_close` 遇 tombstone 時回 `INTERACTIVE_EXPIRED` 且**不**刪除 tombstone。`interactive_status` 的 touch 延長語意保留（仍不在重試白名單）。
3. flash 候選：`collect_flash_candidates_and_mark` 增加排除條件：`session._pending_drain is not None`、`by_id in _draining_by_ids`、`session.foreground_busy`、`session._foreground_operation is not None`。`enter_flashing`：state ∈ {`RELEASED`} → `{"ok":false,"error_code":"SESSION_NOT_READY","session":…}`；state==`RECOVERING` 或 drain 中 → `{"ok":false,"error_code":"SESSION_RECOVERING","session":…}`；通過後 `set_flash_mode(True)`，並呼叫 T09 的 `bridge.invalidate_deferred("flash")`（FlashSyncMatched；`set_flash_mode` 本身不失效，見 T09 第 2 點）。
4. console 行上限：`_consume_console_input` 一般 byte 分支在 append 前若 `len(tx_buffer) >= CONSOLE_LINE_MAX_BYTES` → `tx_buffer.clear()`、`line_overflow=True`、log 第一個樣板（每行一次）；`line_overflow` 期間後續 byte 丟棄（不 echo）；遇 CR／LF 時若 `line_overflow` → 不 emit 該行、重設旗標。
5. `_on_console_line`：`r = arbiter.submit(...)`；`not r.get("ok")` 且 `r.get("error_code") in {"CMD_TOO_LONG","SESSION_QUEUE_FULL","SESSION_NOT_READY"}` → log 第二個樣板（bytes＝UTF-8 長度）。WAL 契約不變（SC-16 只以 log 提供診斷）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_lease_flash_console.py`
  ```python
  from __future__ import annotations
  import logging
  import time
  import pytest
  import sw_core.uart_io as uio
  from stage0_fakes import FakePort, make_bridge, make_manager, make_profile

  def _ready_mgr(tmp_path):
      from sw_core.uart_io import UARTBridge
      from sw_core.config import UartProfile
      mgr = make_manager(tmp_path, [make_profile()])
      s = mgr.get_session("COM0")
      b = UARTBridge(com="COM0", device_path="fake", profile=UartProfile(), wal=mgr._wal)
      b._serial = FakePort(); b._serial_fd = None
      with mgr._lock:
          s.bridge = b; s.state = "READY"
      return mgr, s

  def test_tick_expires_agent_lease_and_keeps_tombstone(tmp_path):
      mgr, s = _ready_mgr(tmp_path)
      r = mgr.interactive_open("COM0", owner="agent:x", timeout_s=0.05)
      lid = r["interactive_id"]                      # interactive_open 成功回應的鍵（session_manager.py 約 4059／4075 行）
      time.sleep(0.1)
      assert mgr._expire_interactive_leases_tick() == 1
      assert mgr.get_session("COM0").interactive_session_id is None
      assert mgr.interactive_send(lid, data="ls")["error_code"] == "INTERACTIVE_EXPIRED"
      assert mgr.interactive_status(lid)["error_code"] == "INTERACTIVE_EXPIRED"
      assert mgr.interactive_close(lid)["error_code"] == "INTERACTIVE_EXPIRED"
      assert mgr.interactive_send(lid, data="ls")["error_code"] == "INTERACTIVE_EXPIRED"   # close 不刪 tombstone

  def test_reconcile_readiness_expires_agent_lease(tmp_path):
      mgr, s = _ready_mgr(tmp_path)
      lid = mgr.interactive_open("COM0", owner="agent:x", timeout_s=0.05)["interactive_id"]
      time.sleep(0.1)
      mgr.reconcile_readiness()                     # 單獨驗證 DeviceWatcher 的週期入口
      assert s.interactive_session_id is None
      assert mgr._interactive[lid].status == "expired"
      assert mgr.interactive_send(lid, data="ls")["error_code"] == "INTERACTIVE_EXPIRED"

  def test_status_touch_still_extends(tmp_path):
      mgr, s = _ready_mgr(tmp_path)
      lid = mgr.interactive_open("COM0", owner="agent:x", timeout_s=1.0)["interactive_id"]
      for _ in range(4):
          time.sleep(0.3)
          assert mgr.interactive_status(lid)["ok"] is True
      assert mgr._expire_interactive_leases_tick() == 0
      assert mgr._interactive[lid].status == "active"

  def test_flash_candidates_exclude_busy_and_draining(tmp_path):
      mgr = make_manager(tmp_path, [make_profile(platform="passthrough")])
      s = mgr.get_session("COM0")
      s.state = "ATTACHED"; s.foreground_busy = True
      assert mgr.collect_flash_candidates_and_mark() == []

  @pytest.mark.parametrize("state,code", [("RELEASED", "SESSION_NOT_READY"), ("RECOVERING", "SESSION_RECOVERING")])
  def test_enter_flashing_guard(tmp_path, state, code):
      mgr = make_manager(tmp_path, [make_profile()])
      mgr.get_session("COM0").state = state
      r = mgr.enter_flashing("COM0")
      assert r["ok"] is False and r["error_code"] == code

  def test_console_line_over_limit_dropped_and_logged(tmp_path, caplog):
      b, _ = make_bridge(tmp_path)
      c = uio.ConsoleClient(client_id="c1", label="x", master_fd=-1, slave_fd=-1, slave_path="", attached_at=0.0)
      with caplog.at_level(logging.WARNING, logger="serialwrap"):
          lines, _ = b._consume_console_input(c, b"a" * 16386 + b"\r")
      assert lines == []
      assert "console 行超過 16385 bytes" in caplog.text
      lines, _ = b._consume_console_input(c, b"ok\r")
      assert lines == ["ok"]
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_line_exactly_16385_bytes_is_emitted_then_rejected_logged` | 16385 bytes 行被 emit；`_on_console_line` 得 `CMD_TOO_LONG` 並記第二個樣板 |
  | `test_console_line_rejected_not_ready_logged` | ATTACHED（無 queue）時 line 被 `SESSION_NOT_READY` 拒絕 → WARNING 含 `error_code=SESSION_NOT_READY` |
  | `test_tombstone_fifo_bound_16` | 同 session 連續 17 個 lease 到期 → `_interactive` 中該 session 的 expired 數為 16，最舊者回 `INTERACTIVE_NOT_FOUND` |
  | `test_tick_restores_human_owner_after_soft_preempt_expiry` | soft preempt lease 到期 → bridge `interactive_owner` 還原為 human、上下文未失效的 deferred 被回放 |
  | `test_tick_blocks_new_lease_until_owner_restore_finishes` | 用事件控制鎖外收尾交錯；收尾完成前新 open 回 `SESSION_INTERACTIVE_BUSY`，完成後 bridge 與 manager owner 一致 |
  | `test_tick_continues_after_one_session_replay_failure` | 第一個 session replay 回報 TX unknown 時只嘗試一次，第二個 session 仍完成 resume，失敗 session suspend depth 歸零且 owner 清理 |
  | `test_interactive_status_keeps_session_not_ready_precedence` | active lease 到期但 bridge 不存在時仍依凍結順序回 `SESSION_NOT_READY`；既有 expired tombstone 仍回 `INTERACTIVE_EXPIRED` |
  | `test_human_lease_not_expired_by_tick` | human lease 不受 timeout tick 影響（現況只看 peer） |
  | `test_flash_candidates_exclude_pending_drain` | `_pending_drain` 非 None 的 session 不在候選中 |
  | `test_flash_candidates_exclude_draining_by_id_and_foreground_operation` | `_draining_by_ids` 與 `_foreground_operation` 各自存在時都排除候選 |
  | `test_enter_flashing_rejects_active_drain` | `_pending_drain` 或 `_draining_by_ids` 命中時維持 READY 並回 `SESSION_RECOVERING` |
  | `test_on_flash_open_failed_enter_releases_probe_resources` | `enter_flashing` 拒絕命中結果時不鏡射 termios／啟 pump，且釋放 held gates 與 flash-critical 標記 |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_lease_flash_console.py` → 預期 `AttributeError: … '_expire_interactive_leases_tick'`。commit：`test(session): SC-13/14/16 lease tick、flash 資格與 console 行上限失敗測試`
- [ ] **B1 實作**：依行為規格。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_lease_flash_console.py tests/test_login_recovery_lease.py tests/test_issue26_interactive_guard.py tests/test_flash_probe.py tests/test_flash_service_wiring.py tests/test_flashing_state.py tests/test_service_human_console.py tests/test_uart_io.py`；`python3 -m tools.contracts check`；全套依 §0 第 4 點。
- **測試資產變更**（A1.5 re-lock 待獨立審閱；既有斷言維持或依 SC-13 契約精煉，PR 須列明並由獨立審閱者確認）：
  - `tests/test_refine_ownership.py::TestRefineOwnership::test_unknown_closed_expired_and_replaced_interactive_ids_do_not_tx`：被 tombstone 保留的 replaced id 改預期 `INTERACTIVE_EXPIRED`；零 TX 斷言不變（SC-13）。
  - `tests/test_stage0_lease_flash_console.py::test_tick_restores_human_owner_after_soft_preempt_expiry`：加入仍存活的 `ConsoleClient`（`attached_at` 設為目前時間），使替身符合 T09 resume 只回放 live client 的契約；owner／回放斷言不變（SC-13）。
  - `tests/test_stage0_lease_flash_console.py`：補週期 reconcile 收回、touch 非 vacuous、post-close admission 交錯、雙 session replay 失敗隔離、status 錯誤順序與 flash guard 失敗回歸案例（SC-13／14）。
  - `tests/test_contracts_keep_baseline.py::test_last_error_logging_templates_match`：將兩個既有 WARNING source 行更新為目前程式位置；原 HEAD 已因舊行號失敗，不改日誌模板斷言。
- [ ] **B3 commit**：`fix(session): SC-13 lease 到期主動收回、SC-14 flash 認線資格、SC-16 console 行上限與拒絕診斷`

---

## Task T20：OS write 前授權 permit（quiet／release／cancel／lease／owner_gen 零 TX）

- **範圍**：規格 §5.2.2（實際開始 OS write 前必須完成授權檢查）、§11.3 零 TX 不變量、arch §5.4「OS 寫入前：TxWriter 取 Fence 鎖驗證 permit」。T06 的 fence 只涵蓋 flash、WAL、drain、raw admission；本 Task 補上命令出列後、OS write 前才發生的 quiet／reconfirm 啟動、release、cancel、interactive lease 切換，以及 BC-02 raw／replay 資料在等待 `_write_lock` 期間的上下文失效（T09 第 3 點）。**序列化邊界（R0-F1）**：授權重驗與每一次 OS 提交（單次非阻塞 `os.write`）在同一把新鎖 `_tx_fence_lock` 的臨界區內完成，所有失效操作也持同一把鎖修改 gate 欄位；禁止「在 `_state_lock` 內驗證、放鎖、再 `os.write`」——兩者之間的 quiet／release／lease 換手會讓失效世代的 bytes 寫出（規格 §5.2.2）。鎖只跨非阻塞提交，不跨 EAGAIN backoff 或完成等待；唯一例外是 Windows `_PySerialPort` 無法分離提交與完成，每段 ≤64 bytes 的 `serial.write` 在鎖內阻塞（上限 `write_timeout` 5 s），因此 `begin_drain` 取此鎖有上限（`DRAIN_FENCE_ACQUIRE_S`），取不到時先凍結、在途段交 Phase 2 背景完成，drain 3,000 ms 期限自請求起算（R3-F3，規格 §5.2.5）。**自動登入、密碼與 Ready Probe（R2-F1）**：login FSM（prompt probe `"\n"`、帳號、`send_secret` 密碼、`post_login_cmd`、ready nonce probe、console unlock）與 recover 按鍵（`system:recover`）一律以 `tx_class="system_probe"` 的 permit 送出，permit 帶登入世代 `login_gen`，每次 OS 提交都在 `_submit_guard` 內重驗 Quiet、`login_gen` 與 Drain。manager 的 `_boot_quiet_blocks_probe` 與 recover 迴圈的 `boot_quiet_active()` 只是 pre-check：pre-check 通過後 RX thread 才看到 boot banner 而 arm quiet 時，探測、帳號與密碼都不得交 OS（規格 §5.2.1「Login FSM／Ready Probe：Quiet、Drain 或失效世代期間禁止自動 probe」、§5.2.2「讀一次旗標，過一段時間再 write」）。**Self-test nonce probe（R5-F2，DF-19）**：`session.self_test` 的 nonce probe（`source="system:self_test"`）同屬 Ready Probe，也必須經提交點重驗。基線在 `_lock` 內以 `session.boot_quiet_active()` 檢查一次（`session_manager.py` 約 4482 行），離鎖後才在約 4533 行送出；兩者之間 RX thread 看到 boot banner 而 arm quiet 時，nonce 仍會送進 U-Boot、打斷 autoboot。本 Task 改為：在同一 `_lock` 臨界區內、pre-check 通過時建立 `tx_class="self_test_probe"` 的 permit，送出時以 `bridge.system_permit(...)` 包住，每次 OS 提交在 `_submit_guard` 內重驗 boot quiet 期限（**不論** profile 是否有 `console_unlock_env`，與 pre-check `boot_quiet_active()` 同義）與 bridge 世代；被 quiet 拒絕時回傳與 pre-check 相同的 `AUTOBOOT_QUIET` 診斷回應，零 TX、不等 nonce、不 `confirm_ready()`。對應 Rust `TxClass::SelfTestProbe`（06 Task 9 gate 表、Task 14；08 Task 7、Task 8）。
- **Test author model**：haiku-xhigh　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-tx-permit.md`（`type: fix`，`scope: uart`）：每次 UART 寫入在實際送出前重新驗證命令仍有效（未被取消、未進 quiet、未被 release、lease 未切換、human 輸入上下文未失效），不再送出已失效的命令或按鍵；自動登入、密碼與 ready probe 在送出前一刻偵測到開機 banner（quiet）或登入流程已被取代時也不再送出，避免打斷 autoboot；`session self-test` 的 nonce probe 在送出前一刻偵測到開機 banner 時同樣不送出，改回 `AUTOBOOT_QUIET`（`recommended_action: wait`，DF-19）。
- **BLOCKED-OWNER**：無（實機交接情境併入 T21 的 F10 case）。

**Files**
- Create: `tests/test_stage0_tx_permit.py`
- Modify: `sw_core/uart_io.py`、`sw_core/serial_port.py`（`_posix_write_all`／`_PosixSerialPort.write`／`_PySerialPort.write` 接受 `submit_guard`）、`sw_core/session_manager.py`（execute／interactive／console 路徑發 permit；quiet、release、cancel、lease 切換時撤銷；R2-F1：`SessionRuntime.login_gen`、`_push_probe_gate()`、`_begin_login_run_locked()`、四個 login 呼叫點以 `bridge.system_permit(...)` 包住並收斂拒絕、recover 按鍵帶 permit；R5-F2：`_self_test_impl` 於 pre-check 臨界區建 `self_test_probe` permit、送出以 `system_permit(...)` 包住並收斂拒絕、`_push_probe_gate()` 推 `self_test_quiet_until`）、`contracts/behavior_changes.yaml`（R5-F2：DF-19 `status: IMPLEMENTED`，同一 commit、只遞增檔案 `revision`）、`sw_core/arbiter.py`（`cancel()` 通知 manager 撤銷該 cmd_id）、`sw_core/service.py`（cancel 接線）、`tests/stage0_fakes.py`（`FakePort.write` 新增可選 `submit_guard`；R3-F3：`block_in_guard`、`cancel_pending_write`）、`sw_core/constants.py`（R3-F3：`DRAIN_FENCE_ACQUIRE_S`）、`docs/rust-refactor/plans/rust-architecture.md` §5.4（Python 對應說明一行）

**Interfaces**
- Consumes：T06 `send_bytes`／`TxResult`；T07 fence；T09 `owner_gen`；T11 `trusted_human` 分類。
- Produces（Python）：
  ```python
  # sw_core/uart_io.py
  @dataclass(frozen=True)
  class TxPermit:
      op_id: str                        # cmd_id｜"interactive:<id>"｜"console:<client_id>"｜"system:<kind>"
      bridge_generation: int            # 發放當下 session.bridge_generation
      tx_class: Literal["agent", "interactive", "human_line", "human_raw", "system_probe", "self_test_probe"]
                                        # R2-F1："system_probe"＝login FSM 全部 TX（含 send_secret）、ready／reconfirm nonce probe、
                                        # recover 按鍵；對應 Rust `LoginProbe`／`ReadyReconfirm`／`SystemRecoverKey`
                                        # R5-F2："self_test_probe"＝`session.self_test` 的 nonce probe（source "system:self_test"）；
                                        # 對應 Rust `SelfTestProbe`（不帶 login_gen；比對 bridge_generation）
      owner_gen: int | None = None      # human_raw／replay：擷取時的 owner_gen（T09）；interactive：發放當下的 bridge `_owner_gen`
                                        # （R2-F2：manager 於 `_lock` 內驗證 lease 後讀取，與 lease 驗證同一臨界區）
      login_gen: int | None = None      # R2-F1：system_probe 的登入世代（login run 啟動時的 session.login_gen）；recover 按鍵為 None（只受 quiet／drain）
  UARTBridge.send_bytes(..., permit: TxPermit | None = None) -> TxResult
  UARTBridge.send_secret(self, secret: str, *, permit: TxPermit | None = None) -> None   # R2-F1：原樣轉交 send_bytes(..., permit=permit, log=False)
  UARTBridge.set_probe_gate(self, *, quiet_until: float, login_gen_floor: int, self_test_quiet_until: float | None = None) -> None
      # R2-F1：持 _state_lock → _tx_fence_lock 更新 `_gate_probe_quiet_until`（monotonic 期限，0.0＝無 quiet）與
      # `_gate_login_gen_floor`（只增不減：取 max(舊值, 新值)）；class 層預設 0.0／0（相容 bare bridge）
      # R5-F2：同一臨界區更新 `_gate_self_test_quiet_until`（monotonic 期限；None → 取 quiet_until，既有兩參數呼叫不變）；class 層預設 0.0
  UARTBridge.system_permit(self, permit: TxPermit) -> ContextManager[None]
      # R2-F1：thread-local；with 區塊內**同一 thread** 以 `permit=None` 呼叫 send_bytes 且 source 為 "system" 或 "system:secret"
      # （login_fsm 經 send_command／send_secret 送出的全部 bytes）時改用此 permit。以 thread-local 實作，使既有以
      # `fake_ensure_ready(_bridge, _sp, auth=None)` 固定簽章 mock 的測試不需改動（login_fsm 公開函式簽章不變）
      # R5-F2：source "system:self_test" 同樣改用 thread-local permit（只有 `_self_test_impl` 會以 self_test_probe permit 進入），
      # 因此 `send_command(probe, source="system:self_test", cmd_id=None)` 呼叫形狀不變，`tests/test_boot_quiet.py`、
      # `tests/test_rx_flood.py` 等固定簽章 `send_command(cmd, *, source, cmd_id=None)` 的替身 bridge 不需改動
  # sw_core/session_manager.py
  SessionRuntime.login_gen: int = 0                  # R2-F1：登入世代；遞增點見第 3 點
  SessionRuntime._push_probe_gate(self) -> None      # getattr(self.bridge, "set_probe_gate", None) 存在時呼叫：
      # quiet_until = self.boot_quiet_until if not self.profile.console_unlock_env else 0.0（與 `_boot_quiet_blocks_probe` 同義），
      # login_gen_floor = self.login_gen，
      # self_test_quiet_until = self.boot_quiet_until（R5-F2：不論 console_unlock_env，與 self-test pre-check `boot_quiet_active()` 同義）
  SessionManager._self_test_permit_locked(self, session: SessionRuntime, nonce: str) -> TxPermit
      # R5-F2：持 self._lock、與 `_self_test_impl` 的 `boot_quiet_active()` pre-check 同一臨界區（pre-check 為假、即將建 probe 時）呼叫：
      # 回 TxPermit(f"system:self_test:{nonce}", session.bridge_generation, "self_test_probe")（login_gen=None、owner_gen=None）；
      # **不**遞增 login_gen、不呼叫 `_push_probe_gate()`（self-test 不是 login run，不得取代進行中的登入世代，SES-16.3b）
  SessionManager._begin_login_run_locked(self, session: SessionRuntime) -> TxPermit
      # 持 self._lock、與該呼叫點既有的 `_boot_quiet_blocks_probe` pre-check 同一臨界區：session.login_gen += 1 → session._push_probe_gate()
      # → 回 TxPermit(f"system:login:{session.login_gen}", session.bridge_generation, "system_probe", login_gen=session.login_gen)
  def _login_tx_refused(exc: BaseException) -> bool
      # isinstance(exc, TxIncompleteError) 且 (exc.result.error or "").removeprefix("revoked:") in {"quiet", "stale_login_gen"}
  def _self_test_tx_refused(exc: BaseException) -> bool
      # R5-F2：isinstance(exc, TxIncompleteError) 且 (exc.result.error or "").removeprefix("revoked:") == "quiet"
  UARTBridge.set_tx_gate(self, *, generation: int, quiet: bool, lease_owner: str | None) -> None   # 持 _state_lock → _tx_fence_lock 更新
  UARTBridge.revoke_ops(self, op_ids: Iterable[str]) -> None                                        # 持 _tx_fence_lock；FIFO 512
  UARTBridge._tx_fence_lock: threading.Lock          # R0-F1：授權重驗＋單次 OS 提交的唯一臨界區；lock order `_state_lock` → `_tx_fence_lock`，
                                                     # 持 `_tx_fence_lock` 時不得取 `_state_lock` 或呼叫 manager／WAL
  UARTBridge._submit_guard(self, permit: TxPermit | None, payload_len: int) -> ContextManager[str | None]
      # with 進入時取 _tx_fence_lock 並重跑第 1 點全部檢查，yield None（放行）或拒絕原因字串；with 內只允許一次非阻塞 os.write
      # permit is None 且不屬 system 來源（第 2 點：只剩 flash pump）時只跑 drain 凍結與 flash gate，但同樣經過 guard（不存在不經 guard 的 OS 寫入）；
      # R2-F1：source 為 "system"／"system:secret"／"system:recover" 的 permit=None 呼叫**不**落入此分支——先取 system_permit()
      # 的 thread-local permit，沒有時以隱含 TxPermit("system:implicit", <gate generation>, "system_probe", login_gen=None) 判定
      # （仍受 quiet 與 drain 阻擋，只跳過世代比對），因此沒有任何自動 probe 路徑只檢查 drain／flash
      # R5-F2：source 為 "system:self_test" 的 permit=None 呼叫同樣不落入此分支——先取 thread-local permit，沒有時以隱含
      # TxPermit("system:self_test:implicit", <gate generation>, "self_test_probe") 判定（受 `_gate_self_test_quiet_until` 與 drain 阻擋）
  UARTBridge._after_tx_authorize: Callable[[], None] | None   # 測試接縫：pre-check 通過後、第一次進 _submit_guard 前（不持任何 TX 鎖，仍持 _write_lock）
  UARTBridge._inside_tx_submit: Callable[[], None] | None     # 測試接縫：_submit_guard 內重驗通過後、os.write 前（持 _tx_fence_lock）
  # sw_core/serial_port.py
  def _posix_write_all(fd: int, payload: bytes, cancel: threading.Event | None = None, *,
                       submit_guard: Callable[[], ContextManager[str | None]] | None = None) -> TxResult
  # _PosixSerialPort.write／_PySerialPort.write 同樣接受 `submit_guard=`（以 inspect.signature 偵測，與 T06 `cancel` 相容規則同法）
  _PySerialPort.cancel_pending_write(self) -> None
      # R3-F3：best-effort；pyserial Serial 有 cancel_write()（Windows CancelIoEx）時呼叫之，否則 no-op；不取任何 bridge 鎖、不等待完成
  # sw_core/constants.py
  DRAIN_FENCE_ACQUIRE_S = 0.05      # R3-F3：begin_drain 取 _tx_fence_lock 的上限（另受 drain 共同期限剩餘時間限制，取兩者較小值）
  # tests/stage0_fakes.py
  FakePort.__init__(..., *, block=None, block_in_guard: bool = False)   # 新增關鍵字，預設 False（既有呼叫不變）
  FakePort.cancel_write_calls: int = 0; FakePort.cancel_pending_write(self) -> None
      # R3-F3：block_in_guard=True 時 `block.wait()` 改在 submit_guard **之內**（持 _tx_fence_lock）等待，模擬 Windows pyserial 阻塞 serial.write；
      # cancel_pending_write() 只記錄 `cancel_write_calls += 1`、不放行 block（模擬驅動不理會 CancelIoEx，規格 §5.2.3）
  ```
- Rust 對應：arch §5.4 `TxPermit { op_id, epoch, owner_gen, class }`、`FenceState`、第 3 點「OS 提交時：`FenceSubmitGate` 在 Fence 鎖內重驗 `permit.epoch == active_epoch`、`!frozen`、class 對應的 gate 規則、世代規則、`op_id ∉ cancelled` 後才提交」；arch §4.2 `SubmitGate`。R2-F1：`system_probe`＋`login_gen` 對應 08 Task 7 `TxPermit.fsm_gen: Option<u64>`、`FenceState.{fsm_gen_floor, auto_probe_blocked}`、`FenceReject::StaleFsmGen`（`_gate_probe_quiet_until` 僅在 `_boot_quiet_blocks_probe` 語意為真時非零，等同 `auto_probe_blocked`；console unlock profile 不擋，對齊 08 `console_unlock_not_cancelled_by_banner`）；Rust 對照測試為 08 `login_probe_authorized_then_quiet_arm_before_os_submit_is_zero_tx`。R5-F2：`self_test_probe` 對應 `TxClass::SelfTestProbe`——Rust 以 `quiet_active`（arm 時同鎖設定，console unlock profile 亦為 true；不看 `auto_probe_blocked`）與 `epoch` 擋下、`fsm_gen: None`（08 Task 7 admit 規則），`_gate_self_test_quiet_until` 等同 `quiet_active`、`bridge_generation` 比對等同 `epoch`；被擋時回應映射同為 pre-check 的 `AUTOBOOT_QUIET`（06 Task 14 `OpOutcome::SelfTestRefused`）；Rust 對照測試為 06 `selftest_quiet_armed_after_precheck_before_submit_zero_tx`、08 `self_test_probe_gated_by_quiet_active_even_for_console_unlock` 與 `st_probe_console_unlock_banner_after_precheck_zero_tx`。

**行為規格（凍結）**
1. 檢查內容（依序）：drain 凍結（`_tx_frozen` → `"draining"`）、flash gate、`permit.bridge_generation != self._gate_generation` → `"stale_generation"`；`permit.op_id in self._revoked_ops` → `"cancelled"`；`tx_class == "agent"` 且 `self._gate_quiet` → `"quiet"`；`tx_class == "interactive"` 且 `self._gate_lease_owner != permit.op_id` → `"lease"`；`tx_class == "interactive"` 且 `permit.owner_gen != self._owner_gen` → `"lease"`（發放後發生 quiet 重新 arm、lease close／到期／soft preempt／換手等 `_owner_gen` 遞增；之後重新發放的 permit 帶新世代即可放行）；`tx_class == "human_raw"` 且 `permit.owner_gen != self._owner_gen` → **丟棄**（`"stale_owner_gen"`，log DEBUG，不送、不重新 defer、不 raise）。
   - **interactive 不受 quiet 阻擋（R2-F2，凍結）**：規格 §5.2.1「有效 Agent Interactive Lease 可中斷 autoboot；不是任意繞過獨占與 Drain」；Python 基線 `interactive_send`（`session_manager.py` 約 4240 行 `send_bytes(payload, source=lease.owner)`）與 `interactive_open` 的 `command` 皆不查 quiet；Rust 對應 arch §5.4 gate 表 `LeaseOwner`／`InteractiveOpenCmd` 列與 06 Task 7 gate 表 `quiet_active → Ok(())`。因此 `_gate_quiet` 只擋 `agent`（Arbiter 命令；file.* 由 T11 manager gate 擋）；`interactive` 在 quiet 中照樣經過 drain 凍結、flash gate、`stale_generation`、`cancelled`、lease owner 與 `owner_gen` 全部檢查——「不受 quiet 阻擋」不等於繞過 `_submit_guard`、drain 或 lease 驗證。T20 不得在 manager 層 `interactive_send`／`interactive_open` 新增 quiet 拒絕。
   - **system_probe（R2-F1，凍結）**：`tx_class == "system_probe"` 依序檢查 drain 凍結（`"draining"`）→ flash gate → `time.monotonic() < self._gate_probe_quiet_until` → `"quiet"` → `permit.login_gen is not None and permit.login_gen < self._gate_login_gen_floor` → `"stale_login_gen"`；**不**比對 `bridge_generation`（attach 中、READY 前 bridge_generation 尚未遞增；permit 由 `system_permit()` 綁定於發放它的 bridge 物件，bridge 重建即失效，過期與否由 `login_gen` 判定）。上列檢查與其他 class 一樣在 pre-check 跑一次、並在**每一次** `_submit_guard` 臨界區內重跑；quiet 以 monotonic 期限判定（不存 bool），因此 180 s 期滿後不需任何事件即自動放行，reboot recovery「quiet 期滿後再 ensure_ready」路徑不受影響。console unlock profile（`profile.console_unlock_env` 非空）的 `_gate_probe_quiet_until` 恆為 0.0、arm 時不推進 `login_gen`，FSM 在 CFE banner 後等待 RX 靜默的設計不變（對齊 `_boot_quiet_blocks_probe`）。
   - **self_test_probe（R5-F2，凍結）**：`tx_class == "self_test_probe"` 依序檢查 drain 凍結（`"draining"`）→ flash gate（照 T06 既有語意靜默丟棄、不 raise——RPC-H-07 KEEP：FLASHING 中 self-test 仍得 `TARGET_UNRESPONSIVE`）→ `permit.bridge_generation != self._gate_generation` → `"stale_generation"` → `time.monotonic() < self._gate_self_test_quiet_until` → `"quiet"`；**不**比對 `login_gen`、**不**看 `_gate_quiet`（`_gate_quiet` 隨 ready reconfirm 啟動而為真，而 self-test 正是 `READY_UNCONFIRMED` 的建議出口，`recommended_action:"self_test"`，不得被 reconfirm 擋下）。`_gate_self_test_quiet_until` 恆等於 `session.boot_quiet_until`（含 console unlock profile），因此提交點的判定與 pre-check `session.boot_quiet_active()` 完全同義：pre-check 之後才 arm 的 quiet（RX banner、倒數行、reboot 命令）在提交點必被擋下；quiet 期滿（monotonic）或 `clear_boot_quiet()`／`confirm_ready()` 後自動放行。與 `system_probe` 一樣在 pre-check 跑一次、並在**每一次** `_submit_guard` 臨界區內重跑。
   - **pre-check**：`send_bytes` 持 `_write_lock` 後，在 `_state_lock` 內先跑 T06 既有 flash／WAL／drain／deferred 檢查與上列檢查，作為快速拒絕；通過**不算**授權。
   - **提交點（授權，R0-F1）**：寫入迴圈的**每一次** `os.write`（含部分寫入後的續寫）都包在 `with self._submit_guard(permit, n) as reason:` 內：取 `_tx_fence_lock` → 重跑上列檢查 → `reason is None` 才在仍持鎖時呼叫 `_inside_tx_submit`（若有）與**一次** `os.write`（fd 為 `O_NONBLOCK`，立即返回 n 或 EAGAIN）→ 放鎖。EAGAIN backoff（`cancel.wait(EAGAIN_BACKOFF_S)`）與後續等待一律在鎖外。被拒：尚未寫出任何 byte → `TxResult("not_submitted", 0, n, <reason>)`；已確認 k≥1 bytes 後被拒 → `TxResult("completed", k, n, f"revoked:{reason}")`，尾段不得再交 OS（T08 依 confirmed≥1 映射 `COMMAND_ABORTED_IN_FLIGHT`）；`reason == "cancelled"` 且源自 `_tx_cancel`（drain）時維持 T06／T07 既有 `TxResult("completed", sent, n, "cancelled")`。拒絕一律 raise `TxIncompleteError`（`stale_owner_gen` 除外，見上）；沒有任何拒絕路徑會寫 OS 或為未提交 bytes 寫 TX WAL。
   - **失效端**：`begin_drain`（`_tx_frozen`、`_tx_cancel`）、`set_tx_gate`、`revoke_ops`、`invalidate_deferred`／`_owner_gen` 變更、flash gate 進出，都必須在持 `_tx_fence_lock` 時修改上列欄位（既有持 `_state_lock` 的呼叫點依 lock order 巢狀取得）。因此失效與提交互斥：失效先取得鎖 → 之後所有提交被拒（零 TX）；提交先取得鎖 → 失效等到該次非阻塞 `os.write` 返回，該段 bytes 視為已提交、照實記 TX WAL。
   - **drain 取 fence 鎖有上限（R3-F3，凍結）**：`begin_drain` 在 manager `_lock` 與 bridge `_state_lock` 之內執行，不得無上限地等 `_tx_fence_lock`（Windows 一段 guarded `serial.write` 可持鎖到 `write_timeout` 5 s）。程序：(1) `got = self._tx_fence_lock.acquire(timeout=min(DRAIN_FENCE_ACQUIRE_S, max(0.0, self._drain_deadline - time.monotonic())))`；(2) 不論 `got`，依序設 `_tx_frozen = True`（單一屬性指派）、`_tx_cancel.set()`、`invalidate_deferred("drain")` 的欄位變更；(3) `got` 時放鎖；(4) 鎖外（仍持 `_state_lock`）若 port 有 `cancel_pending_write` 則呼叫一次（best-effort，不等待）。`got is False` 時的線性化點改為各 guard 進入時讀 `_tx_frozen`：正在持鎖的那一段在凍結前已通過重驗、已交 OS，照實計為已提交（結果由該段返回後的 `TxResult`／`TxUnknownError` 決定）；之後每一次 `_submit_guard` 進入都讀到 `_tx_frozen is True` 而拒絕，尾段零 TX。凍結後所有 class 都以 `"draining"`／`"cancelled"` 拒絕，不依賴 `_owner_gen` 等其他欄位的鎖內線性化。在途段的完成等待歸 Phase 2：`finish_drain` 以共同期限（T07 第 1、3 點，自請求起算）等 `_write_lock`，期限內未收斂即回 `None`（manager 準時回報 `drain_stalled`），handle 由 `serialwrap-drain-<com>` 背景 thread 在該段返回後關閉；`_drain_pending` 期間 `start()`／attach 一律拒絕，不提前重開 handle。POSIX 路徑的鎖只跨非阻塞 `os.write`，(1) 實務上一定取得，行為與 R0-F1 相同。其他失效端（`set_tx_gate`、`revoke_ops`、flash gate）維持持鎖修改，不在本條範圍。
   - **port 物件路徑**：`_PosixSerialPort.write` 與 `_posix_write_all` 同演算法；`_PySerialPort`（Windows）無法分離提交與完成，改為每段 ≤ `PYSERIAL_SUBMIT_CHUNK = 64` bytes、`submit_guard` 包住該段 `serial.write`（受 `write_timeout` 上限，最長 `_PySerialPort._WRITE_TIMEOUT_S = 5.0` s（arch `PYSERIAL_WRITE_TIMEOUT_S`），**長於** 3,000 ms drain 期限），失效端最多等一段——drain 例外：只等 `DRAIN_FENCE_ACQUIRE_S`，見上條（R3-F3）；不支援 `submit_guard` 的既有替身 port（相容規則）由 bridge 以 `_submit_guard` 包住整個 `port.write` 呼叫（安全優先，失效端等到該呼叫返回）。`tests/stage0_fakes.py` 的 `FakePort.write(payload, *, cancel=None, submit_guard=None)`：有 `submit_guard` 時在 guard 內記錄 `writes`（被拒則不記錄、回 `not_submitted`），`block` 改在 guard **之外**等待（模擬完成等待）；`block_in_guard=True` 時 `block` 改在 guard **之內**等待（模擬 Windows 持鎖阻塞寫入，R3-F3）；無 `submit_guard` 時行為不變。
2. `permit is None`：只允許下列既有內部呼叫者（實作時以 `grep -n "send_bytes(" sw_core/*.py` 列表確認並寫入 PR）：flash pump bytes（flash gate 另行保護）、`send_secret` 內部那一次 `send_bytes`（`source="system:secret"`；R2-F1：它與 login FSM 經 `send_command(source="system")` 送出的 bytes 在 bridge 端一律解析為 `system_permit()` 的 thread-local permit，無則為隱含 `system_probe` permit——**不再**是「只檢查 drain／flash」的路徑；T05 WAL 檢查與 T07 drain fence 照舊）。recover 按鍵（`session_manager.py` 約 3879 行 `send_bytes(payload, source="system:recover")`）改為顯式帶 `permit=TxPermit("system:recover", session.bridge_generation, "system_probe")`（`login_gen=None`）。`session.self_test` 的 nonce probe（`source="system:self_test"`）屬 Ready Probe（規格 §5.2.1），同樣不得只檢查 drain／flash（R5-F2，取代原「維持基線、不新增 quiet 拒絕」）：`_self_test_impl` 以 `with bridge.system_permit(self_test_permit):`（bridge 無此屬性時 `contextlib.nullcontext()`）包住既有 `bridge.send_command(probe, source="system:self_test", cmd_id=None)`，bridge 端解析為 thread-local 的 `self_test_probe` permit；沒有 thread-local permit 時以隱含 `self_test_probe` permit 判定（見 `_submit_guard` 註解），因此 `system:self_test` 也**不再**是只檢查 drain／flash 的路徑。命令（`execute_command`）、interactive（`interactive_send`，以及 `interactive_open` 的 `command`：`tx_class="interactive"`、`op_id="interactive:<id>"`，經 `UARTBridge.send_command(cmd, *, source, cmd_id=None, permit: TxPermit | None = None)` 原樣轉交 `send_bytes(..., permit=permit)`）、human console raw 與 replay 一律帶 permit。
3. 發放與撤銷（lock order：manager `_lock` → bridge `_state_lock`，與現況 `enter_flashing` 一致；bridge 持 `_state_lock` 時不得呼叫 manager）：attach 成功設 `set_tx_gate(generation=session.bridge_generation, …)`；`arm_boot_quiet`／ready reconfirm 啟動與解除時更新 `quiet`（**不**清 `lease_owner`、不關閉 lease；其 `invalidate_deferred` 的 `_owner_gen` 遞增只失效 arm 之前已發放的 interactive permit，R2-F2）；`interactive_open`／close／lease 到期／soft preempt 時更新 `lease_owner`；`release`／drain Phase 1 由 T07 `_tx_frozen` 擋下；`arbiter.cancel(cmd_id)` 經 manager 呼叫 `bridge.revoke_ops([cmd_id])`。
   - **登入世代與 probe gate（R2-F1）**：`SessionRuntime.arm_boot_quiet()`（banner／倒數 RX 經 `_update_boot_quiet_locked`、reboot 命令）在 `profile.console_unlock_env` 為空時先 `login_gen += 1`，再 `_push_probe_gate()`（`quiet_until=boot_quiet_until`、`login_gen_floor=login_gen`），因此 arm 前已發放的 login permit 同時被 `"quiet"` 與 `"stale_login_gen"` 擋下；R5-F2：`_push_probe_gate()` 在**每一次** arm 都呼叫（console unlock profile 亦然：`login_gen` 不動、`quiet_until=0.0`，但 `self_test_quiet_until=boot_quiet_until`），使 self-test permit 在任何 profile 的 arm 之後都以 `"quiet"` 擋下；`clear_boot_quiet()`／`confirm_ready()` 呼叫 `_push_probe_gate()`（`quiet_until=0.0`，floor 不動）；session 指派新 bridge（attach、rebind、reclaim）後於同一 `_lock` 區段呼叫一次 `_push_probe_gate()`。每一次自動 login／probe run 在其 `_boot_quiet_blocks_probe` pre-check 的同一 `_lock` 區段呼叫 `_begin_login_run_locked(session)` 取得新 permit（新 run 推進 floor → 舊 run 剩餘的 bytes 以 `"stale_login_gen"` 拒絕），呼叫點：`_probe_existing_bridge`（attach／recover 共用，約 2499～2503 行）、`_attach_by_id` 的 login 段（約 2649～2673 行）、reprobe（約 3085～3103 行）、reboot recovery 迴圈（約 3435 行，每輪一個新 permit）。lock order 與上段相同：manager `_lock` → bridge `_state_lock` → `_tx_fence_lock`；`arm_boot_quiet` 由 RX 路徑呼叫時 bridge 不持 `_state_lock`（T06 `_handle_serial_rx` 在鎖外回呼 manager）。
4. human raw 直送與回放：`_handle_console_rx`／`resume_interactive`／`_set_interactive_admission(True)` 以擷取時的 `owner_gen` 建 permit（T09 第 3 點），因此失效發生在「已離開 `_state_lock`、等待 `_write_lock`」或「pre-check 通過、尚未進 `_submit_guard`」期間的資料於提交點被丟棄。
5. **login 被拒的收斂（R2-F1，凍結）**：四個 login 呼叫點以 `with bridge.system_permit(permit):`（bridge 無此屬性時 `contextlib.nullcontext()`）包住 `ensure_ready`／`probe_ready`，並在既有結果處理前加 `except TxIncompleteError as exc: if not _login_tx_refused(exc): raise`：被拒時 `ok, err = False, "PROMPT_UNAVAILABLE"`、**不**呼叫 `_refine_probe_failure`（`last_error_detail=None`）；session 為 `READY`（reconfirm 期間）→ state 與 `last_error=None` 不變（與既有 quiet pre-check 早退分支同語意）；非 READY → `ATTACHED`＋`last_error="PROMPT_UNAVAILABLE"`；reboot recovery → 不落 `RECOVERY_ERROR`，直接 `continue` 回迴圈頂端的 quiet 等待（期限延長邏輯不變）。recover 按鍵被拒（`"quiet"`）→ 等同迴圈內 `boot_quiet_active()` 為真的 `break`（剩餘 CTRL-C／CTRL-D 都不送，落到既有 timeout 收尾）。`"draining"`／`"cancelled"` 等其他原因照 T06／T07 既有路徑。login FSM 本身不捕捉這些例外（`login_fsm.py` 公開函式簽章不變），拒絕前已交 OS 的 bytes 照 T06 記錄、任何路徑不重送。與 Rust SC-30 的差異只剩「arm 當下立即結束 RX 等待」：Python 在下一個提交點（或等待逾時）才結束 run，但 arm 之後 wire 上同為 0 byte。
6. **self-test probe 被拒的收斂（R5-F2，DF-19，凍結）**：`_self_test_impl` 在既有 `elif session.boot_quiet_active():` pre-check 之後的 `else` 分支（同一 `_lock` 臨界區，產生 `nonce` 後）呼叫 `_self_test_permit_locked(session, nonce)`；`last_probe_at`、`suspend_human_interactive` 等既有欄位與順序不變。離鎖後的 `try` 區段改為：`offset = bridge.rx_snapshot_len()` → `self._mark_session_tx(session)` → `with bridge.system_permit(permit): bridge.send_command(probe, source="system:self_test", cmd_id=None)`，並以 `except TxIncompleteError as exc: if not _self_test_tx_refused(exc): raise` 收斂：被 quiet 拒絕（含 k≥1 bytes 後 `"revoked:quiet"`）→ 重新取 `self._lock`，回傳與 pre-check 分支**同形**的 `{"ok": True, "classification": "AUTOBOOT_QUIET", "session": session.to_public_dict(), "recommended_action": "wait", **lease_context}`（鍵與順序逐一相同、無 `probe_ok`；外層 `self_test()` 照常附加 `command_capable`）；**不**呼叫 `wait_for_regex_from`（不等 `timeout_s`）、**不**進 RX_FLOOD／`TARGET_UNRESPONSIVE` 分支、**不** `confirm_ready()`、不改 `ready_reconfirm_*`；`finally` 照舊 `resume_interactive()`。其他拒絕原因（`"draining"`／`"cancelled"`／`"stale_generation"`、WAL unhealthy 的 `WalUnavailableError`）照 T06／T07／T05 既有路徑（例外穿出 manager）；flash gate 照舊靜默丟棄而落到 nonce 逾時 `TARGET_UNRESPONSIVE`（RPC-H-07 KEEP）。這些「其他拒絕原因」在 RPC 邊界的實際回應（drain／frozen、stale_generation、cancelled、WAL unhealthy 各一）由本 Task 以 `test_self_test_probe_other_reject_golden` 實跑產生 `contracts/golden/selftest_refused.json`（`{reason: {error_code, ok:false}}`），作為 P5 Rust 對應（06 Task 14 `selftest_probe_other_reject_zero_tx_matches_python`）的 oracle（R5 審查後續）。對外可觀測的唯一差異：pre-check 之後、OS 提交之前 arm quiet 的窗口內，基線會把 nonce 寫上 UART 並回 `OK`／`TARGET_UNRESPONSIVE`，現在零 TX 並立即回 `AUTOBOOT_QUIET`（DF-19）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_tx_permit.py`（`core_portable`＋`memory_only`；以 `_after_tx_authorize` 接縫製造「pre-check 已通過、尚未 OS 提交」的窗口，以 `_inside_tx_submit` 接縫停在提交臨界區內；跨 thread 情境一律以 `threading.Event` 同步、每個等待帶 2 s 上限）

  | 測試名 | 斷言 |
  |---|---|
  | `test_quiet_armed_after_dequeue_before_write_zero_tx` | agent 命令 permit 通過後於 `_after_tx_authorize` 內觸發 `session.arm_boot_quiet()` → 該次寫入不執行：`port.writes == []`、WAL 無 TX、命令結果 `AUTOBOOT_QUIET` 或 `FLUSHED_BY_RECOVERY`（依 T11／T08 映射，不得為 ok） |
  | `test_quiet_armed_by_other_thread_after_authorize_before_submit_zero_tx`（R0-F1，舊「驗證→放鎖→write」實作必紅） | `_after_tx_authorize` 設 `paused` 並等 `resume`；另一 thread 於 `paused` 後呼叫 `session.arm_boot_quiet()`，斷言它在 0.5 s 內返回（writer 未持 TX 鎖）；設 `resume` → `TxIncompleteError`、`result == TxResult("not_submitted", 0, n, "quiet")`、`port.writes == []`、`wal_records` 無 `dir == "TX"` |
  | `test_release_by_other_thread_after_authorize_before_submit_zero_tx`（R0-F1） | 同上窗口，另一 thread 呼叫 `bridge.begin_drain(5, preserve_consoles=False)`（不等 finish）→ `result.outcome == "not_submitted"` 且 `result.error in ("draining", "cancelled")`、`result.confirmed == 0`、`port.writes == []`、無 TX WAL |
  | `test_interactive_lease_handoff_after_authorize_before_submit_zero_tx`（R0-F1） | lease A 的 `interactive_send` 停在 `_after_tx_authorize`；另一 thread 關閉 A 並開 lease B（`set_tx_gate(lease_owner="interactive:B")`、`_owner_gen` 遞增）→ A 的寫入 `not_submitted, "lease"`、`port.writes == []`；之後 B 的 send 正常寫出且 `port.writes == [<B payload>]` |
  | `test_invalidation_during_submit_waits_and_counts_committed`（R0-F1） | `_inside_tx_submit` 設 `in_submit` 並等 `resume`（此時持 `_tx_fence_lock`）；另一 thread 呼叫 `session.arm_boot_quiet()`，斷言 0.2 s 內**未**返回且 `bridge._gate_quiet is False`；設 `resume` → 該寫入 `TxResult("completed", n, n)`、`port.writes == [payload]`、TX WAL 恰 1 筆 `len == n`；quiet thread 於 1 s 內返回；下一筆 agent send → `not_submitted, "quiet"`、`port.writes` 不變 |
  | `test_quiet_between_partial_chunks_stops_tail`（R0-F1） | fd 路徑：`ScriptedWrite([2, BlockingIOError(errno.EAGAIN, "again")])` 且 EAGAIN backoff 期間（以 monkeypatch 的 `cancel.wait` 設 `in_backoff` 並等 `resume`）另一 thread `arm_boot_quiet()` 並斷言 0.5 s 內返回；`resume` 後 → `TxResult("completed", 2, 6, "revoked:quiet")`、`ScriptedWrite.calls == [b"abcdef", b"cdef"]`（第二次為 EAGAIN、未交出 byte；quiet 後不再有第三次呼叫，尾段 `b"cdef"` 永不交 OS）、TX WAL 恰 1 筆 `len == 2`；經 `execute_command` 路徑時命令 `error_code == "COMMAND_ABORTED_IN_FLIGHT"` |
  | `test_os_write_always_inside_tx_fence_lock`（R0-F1） | monkeypatch `uio.os.write`／`FakePort` 的記錄點：每次被呼叫時斷言 `bridge._tx_fence_lock.locked() is True`，且另一 thread `bridge._tx_fence_lock.acquire(blocking=False)` 回 `False`；跑 agent 命令、interactive send、human raw 直送、#53 回放、`send_secret` 各一次 → 違規計數 0 |
  | `test_tx_fence_lock_not_held_during_backoff`（R0-F1） | fd 路徑 `os.write` 永遠 EAGAIN；writer 進 backoff 後另一 thread `bridge._tx_fence_lock.acquire(timeout=0.1)` 回 `True`（鎖不跨等待）；之後 `begin_drain` → writer 以 `"cancelled"` 結束（T07 `test_eagain_writer_is_cancelled_by_drain` 語意不變） |
  | `test_windows_guarded_write_stall_reports_drain_stalled_on_time`（R3-F3；舊「begin_drain 無上限等 fence 鎖、finish 才起算」實作必紅） | 不 monkeypatch `DRAIN_TIMEOUT_MS`（實值 3000）；READY session（`bridge_generation=4`，同 T08 `_ready_with_port`）、`gate = threading.Event()`、`port = FakePort(block=gate, block_in_guard=True)`、`bridge.set_tx_gate(generation=4, quiet=False, lease_owner=None)`；writer thread `bridge.send_bytes(b"abc", source="agent:x", cmd_id="c1", permit=TxPermit("c1", 4, "agent"))`，等到 `port.writes == [b"abc"]` 且 `bridge._tx_fence_lock.locked() is True`（寫入停在 guard 內）；timer thread 於 release 開始後 3.5 s 才 `gate.set()`（模擬 Windows 寫入卡 >3 s）；probe thread 在 release 開始後 0.2 s 呼叫 `mgr._lock.acquire(timeout=0.5)`。`t0 = time.monotonic(); r = mgr.release_device("COM0"); el = time.monotonic() - t0` → `{k: r[k] for k in ("ok","error_code","drain_stalled","drain_timeout_epoch")} == {"ok": False, "error_code": "SESSION_RECOVERING", "drain_stalled": True, "drain_timeout_epoch": 4}`、`2.9 <= el < 3.4`（舊實作 ≥ 6.5 s）；probe 的 acquire 回 `True` 且取得時點 `- t0 < 0.5`（manager `_lock` 未跨 fence 等待）；`r` 返回當下 `port.close_calls == 0`、`bridge.drain_pending is True`、`port.cancel_write_calls == 1`、`mgr.attach_device("COM0")["error_code"] == "SESSION_RECOVERING"`、`pytest.raises(RuntimeError)` 包住 `bridge.start()`（不提前重開 handle）；`gate.set()` 後 2 s 內 state `RELEASED`、`port.close_calls == 1`、`c1` 的 TX WAL 恰 1 筆 `len == 3`（guard 內已交 OS 的段照實記錄） |
  | `test_begin_drain_fence_acquire_bounded_and_freezes_tail`（R3-F3，bridge 層） | `port = FakePort(block=gate, block_in_guard=True)`；thread A 送 `b"abc"`（停在 guard 內，持 `_tx_fence_lock`）；thread B 送 `b"def"`（排在 `_write_lock`）；`t = time.monotonic(); bridge.begin_drain(5, preserve_consoles=False)` 在 `DRAIN_FENCE_ACQUIRE_S + 0.1` s 內返回，返回時 `bridge._tx_fence_lock.locked() is True`、`bridge._tx_frozen is True`、`bridge._tx_cancel.is_set() is True`；`gate.set()` 後 A 得 `TxResult("completed", 3, 3)`、B 得 `TxIncompleteError` 且 `result.outcome == "not_submitted"`、`result.error in ("draining", "cancelled")`；`port.writes == [b"abc"]`、TX WAL 恰 1 筆；`bridge.finish_drain(1.0)` 回條 `handle_closed is True` |
  | `test_pyserial_chunked_write_drain_stops_after_inflight_chunk`（R3-F3，`_PySerialPort` 實碼） | monkeypatch 替身 `serial.Serial`：`write` 第一次呼叫（第一段 64 bytes）阻塞在 `release` Event、之後立即回傳長度，`cancel_write()` 只計數；`_PySerialPort.write(b"x" * 130, cancel=ev, submit_guard=bridge_guard)`（`bridge_guard` 為真實 bridge 的 `lambda: bridge._submit_guard(permit, n)`）於 thread 內執行；第一段阻塞中 `begin_drain` 在 `DRAIN_FENCE_ACQUIRE_S + 0.1` s 內返回、替身 `cancel_write` 計數 1；`release.set()` → 結果 `outcome == "completed"`、`confirmed == 64`、`requested == 130`、`error in ("revoked:draining", "cancelled")`；替身 `write` 恰被呼叫 1 次（後兩段 64＋2 bytes 永不交 OS） |
  | `test_release_after_dequeue_before_write_zero_tx` | 同窗口內 `release_device` → 零 TX；release 完成後 `port.close_calls == 1` |
  | `test_cancel_after_dequeue_before_write_zero_tx` | 同窗口內 `arbiter.cancel(cmd_id)` → 零 TX、命令紀錄 `status == "canceled"` |
  | `test_lease_switch_before_write_zero_tx` | interactive lease A 的 send 在窗口內被 lease B 取代 → 零 TX、回 `INTERACTIVE_EXPIRED` 或 `SESSION_BUSY`（依現況映射） |
  | `test_interactive_rescue_send_during_quiet_reaches_uart`（R2-F2；舊「`tx_class ∈ {agent, interactive}` 受 quiet 擋」規則必紅） | session `ATTACHED`，RX 注入 `b"Hit any key to stop autoboot:  3\r\n"` 並 `session.arm_boot_quiet()`；前置斷言 `session.boot_quiet_active() is True`、`bridge._gate_quiet is True`；`r = mgr.interactive_open("COM0", owner="agent:rescue", timeout_s=30, allow_attached=True)` → `r["ok"] is True`、`r["recovery_mode"] is True`、`r["boot_interrupt"] is True`；`mgr.interactive_send(r["interactive_id"], data="ctrl-c", encoding="key") == {"ok": True, "interactive_id": r["interactive_id"], "bytes": 1}`；`port.writes == [b"\x03"]`；`dir == "TX"` 的 WAL 恰 1 筆、`source == "agent:rescue"`、payload 解碼為 `b"\x03"`；送出後 `bridge._gate_quiet is True`（救援不解除 quiet） |
  | `test_interactive_open_command_during_quiet_reaches_uart`（R2-F2） | 同上前置；以 spy 包住 `bridge.send_bytes` 記錄 `permit`；`mgr.interactive_open("COM0", owner="agent:rescue", timeout_s=30, allow_attached=True, command="stop")` → `ok is True`；`port.writes == [b"stop\n"]`、TX WAL 恰 1 筆 `source == "agent:rescue"`；spy 記錄的 permit `tx_class == "interactive"`、`op_id == f"interactive:{r['interactive_id']}"` |
  | `test_agent_command_during_quiet_zero_tx_while_rescue_lease_sends`（R2-F2 對照組） | 同一 bridge `_gate_quiet is True` 且 rescue lease A 有效：(a) `bridge.send_bytes(b"echo hi\n", source="agent:y", cmd_id="c1", permit=TxPermit("c1", bridge._gate_generation, "agent"))` → `TxIncompleteError`、`result == TxResult("not_submitted", 0, 8, "quiet")`；(b) lease A `interactive_send(data="ctrl-c", encoding="key")` → `ok is True`；最終 `port.writes == [b"\x03"]`（只有救援鍵）、TX WAL 恰 1 筆且無 `cmd_id == "c1"` 的 TX（manager 層 agent 命令 quiet 拒絕回應形狀由 T11 `test_execute_time_quiet_has_retry_after` 釘住） |
  | `test_expired_rescue_lease_during_quiet_zero_tx`（R2-F2） | quiet 中：(a) `timeout_s=0.05` 開 rescue lease A，monkeypatch `time.monotonic` 前進 0.06 s 後 `mgr.interactive_send(A, data="ctrl-c", encoding="key") == {"ok": False, "error_code": "INTERACTIVE_EXPIRED", "interactive_id": A}`；(b) 之後開 lease B（`timeout_s=0.05`），B 的 send 停在 `_after_tx_authorize`，接縫內以過期時點呼叫 `mgr._expire_interactive_leases_tick(now=…)`（T12；連帶 `set_tx_gate(lease_owner=None)`）→ `TxIncompleteError`、`TxResult("not_submitted", 0, 1, "lease")`；兩段皆 `port.writes == []`、無 TX WAL |
  | `test_interactive_permit_before_quiet_rearm_rejected_then_resend_ok`（R2-F2；對齊 06 `lease_send_after_expiry_or_quiet_is_zero_tx`） | quiet 中 rescue lease 有效；其 `interactive_send` 停在 `_after_tx_authorize`，另一 thread 模擬倒數行再次觸發 `session.arm_boot_quiet()`（`bridge._owner_gen` +1、`bridge._gate_lease_owner` 不變）→ 該次 `TxResult("not_submitted", 0, 1, "lease")`、該次 `interactive_send` 回 `ok is False`（錯誤碼同 `test_lease_switch_before_write_zero_tx` 映射）、`port.writes == []`；lease 仍 `status == "active"`；同一 lease 再 `interactive_send(data="ctrl-c", encoding="key")` → `ok is True`、`port.writes == [b"\x03"]` |
  | `test_interactive_during_quiet_still_fenced_by_drain_flash_generation`（R2-F2，參數化） | `bridge._gate_quiet is True`、lease 有效、interactive permit 其餘欄位正確；三列：`bridge.begin_drain(5, preserve_consoles=False)` → `not_submitted` 且 `error in ("draining", "cancelled")`；flash gate 開啟（`bridge.set_flash_mode(True)`）→ 拒絕且不寫出；permit `bridge_generation = bridge._gate_generation - 1` → `"stale_generation"`；三列皆 `port.writes == []`、無 TX WAL |
  | `test_stale_generation_permit_rejected` | 以舊 `bridge_generation` 建 permit → `not_submitted, "stale_generation"`、零 TX |
  | `test_raw_replay_invalidated_while_waiting_write_lock` | 測試持 `_write_lock`；console raw 資料與 replay 各排入一筆；`invalidate_deferred("reboot")`；放鎖 → 兩筆皆未寫入、未重新進 deferred、`port.writes == []` |
  | `test_permit_ok_path_unchanged` | 無失效事件時命令與 #53 回放的 `port.writes` 與 T06／T09 測試期望逐位元組相同 |
  | `test_permit_none_only_for_listed_internal_callers` | AST 掃描 `sw_core/*.py` 的直接呼叫與 `send_bytes` 區域別名；未傳 `permit=` 的呼叫只允許 flash pump、`send_secret` 及兩個相容 adapter 對舊測試替身的 fallback，且依函式核對次數；明確 `permit=None` 一律 FAIL，`system:recover` 必須帶 permit |
  | `test_login_probe_quiet_armed_after_precheck_before_submit_zero_tx`（R2-F1，舊「`_boot_quiet_blocks_probe` 檢查一次後送出」實作必紅） | bridge 層：`session.bridge = bridge`（profile 無 `console_unlock_env`）；`permit = manager._begin_login_run_locked(session)`（持 `_lock` 呼叫）→ `permit.tx_class == "system_probe"`、`permit.login_gen == 1`、`bridge._gate_login_gen_floor == 1`；`with bridge.system_permit(permit): bridge.send_command("", source="system")`，`_after_tx_authorize` 設 `paused` 並等 `resume`；另一 thread 於 `paused` 後以 `bridge._on_rx_data(b"U-Boot 2024.04 (Oct 01 2024)\r\n")`（SessionManager 掛上的 RX 回呼，經 `_on_bridge_rx` → `_update_boot_quiet_locked`）模擬 RX thread 看到 banner，斷言 0.5 s 內返回、`session.boot_quiet_active() is True`、`session.login_gen == 2`、`bridge._gate_login_gen_floor == 2`、`bridge._gate_probe_quiet_until == session.boot_quiet_until`；設 `resume` → `TxIncompleteError`、`exc.result == TxResult("not_submitted", 0, 1, "quiet")`、`port.writes == []`、`wal_records` 無 `dir == "TX"` |
  | `test_attach_probe_banner_after_precheck_sends_no_login_bytes`（R2-F1，manager 端到端） | ATTACHED session（`login_regex`／`password_regex`／`prompt_regex` 設定，帳密解析為 `root`／`s3cret`，無 `console_unlock_env`）；`_after_tx_authorize` 在第一個 system 送出（prompt probe `b"\n"`）時設 `paused` 並等 `resume`；呼叫端 thread 跑 `manager._probe_existing_bridge(session, bridge)`；另一 thread 於 `paused` 後注入 banner RX（同上）再設 `resume`，之後 target 依序輸出 `login: `、`Password: `、`# `（腳本照常回應，用以證明 FSM 不會再送）→ `b"".join(port.writes) == b""`（不含 `b"\n"`、`b"root"`、`b"s3cret"`）、`wal_records` 無 `dir == "TX"` 且 `source` 以 `system` 開頭者、`session.state == "ATTACHED"`、`session.last_error == "PROMPT_UNAVAILABLE"`、`session.last_error_detail is None`、`session.ready_reconfirm_pending is True` |
  | `test_password_not_sent_when_quiet_arms_before_secret_submit`（R2-F1） | 同上 session；腳本：prompt probe `b"\n"` 後回 `login: ` → FSM 送 `b"root\n"`（正常寫出）→ 回 `Password: `；`_after_tx_authorize` 以計數器只在**第 3 次**被呼叫時（依序為 `b"\n"`、`b"root\n"`、secret）設 `paused`／等 `resume`，期間另一 thread 注入 banner RX → `port.writes == [b"\n", b"root\n"]`（逐筆相等，`b"s3cret"` 不出現在 `b"".join(port.writes)`）、`send_secret` 的例外 `result == TxResult("not_submitted", 0, 7, "quiet")`、呼叫端收斂 `last_error == "PROMPT_UNAVAILABLE"`、TX WAL 只有 `\n`、`root\n` 兩筆（secret 本就 `log=False`，也不得有 META 含 secret） |
  | `test_ready_reconfirm_probe_quiet_rearmed_after_precheck_zero_tx`（R2-F1） | READY＋`ready_reconfirm_pending=True`、quiet 已被 prompt 清除；`_probe_existing_bridge` 通過 `_boot_quiet_blocks_probe` pre-check 後，nonce probe（`echo __READY__<nonce>`）停在 `_after_tx_authorize`；另一 thread 注入 banner RX（re-arm）→ `port.writes == []`、`session.state == "READY"`、`session.last_error is None`、`ready_reconfirm_pending is True`、無 `confirm_ready()` 副作用（`boot_quiet_active() is True`） |
  | `test_login_run_superseded_after_precheck_stale_login_gen_zero_tx`（R2-F1） | run A：`permit_a = _begin_login_run_locked(session)`（gen 1）停在 `_after_tx_authorize`；另一 thread 持 `_lock` 呼叫 `_begin_login_run_locked(session)` 取 `permit_b`（gen 2、floor 2）後返回（0.5 s 內）；`resume` → A 的寫入 `TxResult("not_submitted", 0, n, "stale_login_gen")`、`port.writes == []`；之後 `with bridge.system_permit(permit_b): send_command("", source="system")` → `port.writes == [b"\n"]`；quiet 期滿（monkeypatch `uio.time.monotonic` 前進 181 s）後 `permit_a` 仍回 `"stale_login_gen"`（世代失效不因 quiet 解除而復活） |
  | `test_login_probe_drain_after_precheck_zero_tx`（R2-F1） | login permit 停在 `_after_tx_authorize`；另一 thread `bridge.begin_drain(5, preserve_consoles=False)`（不等 finish）→ `result.outcome == "not_submitted"`、`result.error in ("draining", "cancelled")`、`result.confirmed == 0`、`port.writes == []`、無 TX WAL |
  | `test_login_probe_quiet_armed_during_submit_counts_committed`（R2-F1，線性化另一側） | login permit 停在 `_inside_tx_submit`（持 `_tx_fence_lock`）；另一 thread 注入 banner RX，斷言 0.2 s 內**未**返回且 `bridge._gate_probe_quiet_until == 0.0`；`resume` → 該次 `b"\n"` `TxResult("completed", 1, 1)`、TX WAL 恰 1 筆；banner thread 1 s 內返回；同一 permit 的下一筆 `send_command("root", source="system")` → `not_submitted, "quiet"`、`port.writes == [b"\n"]` |
  | `test_console_unlock_login_not_blocked_by_banner`（R2-F1，回歸防護） | profile 設 `console_unlock_env`：login permit 停在 `_after_tx_authorize` 時注入 `CFE version 1.0\r\n`＋`U-Boot 2024.04\r\n` → `bridge._gate_probe_quiet_until == 0.0`、`session.login_gen` 與 `bridge._gate_login_gen_floor` 皆不變；`resume` → 寫入 `completed`、`port.writes == [b"\n"]` |
  | `test_probe_gate_quiet_follows_monotonic_deadline`（R2-F1） | monkeypatch `uio.time.monotonic` 為可控時鐘 t=100：`bridge.set_probe_gate(quiet_until=280.0, login_gen_floor=0)`；`system_probe` permit（`login_gen=None`）送出 → `not_submitted, "quiet"`；時鐘推到 280.0 → 同一呼叫 `completed`（不需任何清除事件）；`set_probe_gate(quiet_until=0.0, login_gen_floor=0)` 在 floor 已為 3 時 → `_gate_login_gen_floor == 3`（只增不減） |
  | `test_implicit_system_source_without_permit_still_quiet_gated`（R2-F1） | 不進 `system_permit()`，直接 `bridge.send_command("", source="system")` 與 `bridge.send_secret("x")`，於 `set_probe_gate(quiet_until=now+10, …)` 之後 → 兩者皆 `not_submitted, "quiet"`、`port.writes == []`；`source="flash"`（flash gate 開啟時）不受此規則影響。R5-F2：另以 `bridge.set_probe_gate(quiet_until=0.0, login_gen_floor=0, self_test_quiet_until=now+10)` 後不進 `system_permit()` 直接 `bridge.send_command("echo __READY__x", source="system:self_test")` → `TxIncompleteError`、`result == TxResult("not_submitted", 0, 16, "quiet")`、`port.writes == []`（隱含 `self_test_probe` permit，取代舊「`system:self_test` 不受此規則影響」） |
  | `test_recover_key_quiet_armed_after_precheck_zero_tx`（R2-F1） | READY session 命令逾時進入 recover 按鍵段；CTRL-C（`b"\x03"`）通過迴圈內 `boot_quiet_active()` pre-check 後停在 `_after_tx_authorize`；另一 thread 注入 banner RX → `port.writes` 不含 `b"\x03"` 與 `b"\x04"`、無 `source == "system:recover"` 的 TX WAL、結果等同 `tests/test_boot_quiet.py::test_ctrl_c_ctrl_d_skipped_inside_quiet_window` 的 quiet 跳過收尾（session 轉 `ATTACHED`） |
  | `test_login_fsm_sends_all_carry_system_probe_permit`（R2-F1） | 以 spy 包住 `UARTBridge._submit_guard` 記錄每次 `(source, permit)`：跑 attach `ensure_ready`（login＋password＋`post_login_cmd`＋nonce）、`probe_ready`、console unlock、reprobe、reboot recovery 各一次 → 每筆 `source == "system"` 或 `"system:secret"` 的 permit 皆 `tx_class == "system_probe"`、`login_gen` 等於該 run `_begin_login_run_locked` 回傳值（同一 run 內不變、不同 run 嚴格遞增）；`system:recover` 的 permit `login_gen is None`；沒有任何一筆 system 來源的 permit 為 None |
  | `test_self_test_probe_other_reject_golden`（R5 審查後續） | 對 drain 起始、release（stale_generation）、cancel、WAL unhealthy 四案各在 pre-check 後、提交前注入 → probe 零 TX、`wait_for_regex_from` 未被呼叫；RPC 回應的 `error_code` 寫入（`SW_REGEN_GOLDEN=1`）或比對 `contracts/golden/selftest_refused.json`，四鍵齊全 |
  | `test_self_test_probe_quiet_armed_after_precheck_before_submit_zero_tx`（R5-F2，DF-19；舊「`boot_quiet_active()` 檢查一次後離鎖送出」實作必紅） | READY session（`ready_probe="echo __READY__${nonce}"`、無 `console_unlock_env`），前置 `session.boot_quiet_active() is False`；`_after_tx_authorize` 在第一次被呼叫時設 `paused` 並等 `resume`（2 s 上限）；呼叫端 thread 跑 `r = mgr.self_test("COM0", timeout_s=2.0)`；另一 thread 於 `paused` 後呼叫 `bridge._on_rx_data(b"U-Boot 2024.04 (Oct 01 2024)\r\n")`，斷言 0.5 s 內返回、`session.boot_quiet_active() is True`、`bridge._gate_self_test_quiet_until == session.boot_quiet_until`；設 `resume` → 呼叫端於 `resume` 後 0.5 s 內返回（不等 nonce 的 2 s）；`port.writes == []`、`wal_records` 無 `dir == "TX"` 且 `source == "system:self_test"` 者；`r["ok"] is True`、`r["classification"] == "AUTOBOOT_QUIET"`、`r["recommended_action"] == "wait"`、`"probe_ok" not in r`；緊接著 `r2 = mgr.self_test("COM0", timeout_s=2.0)`（走 pre-check 分支）→ `list(r.keys()) == list(r2.keys())`、`{k: r[k] for k in ("ok", "classification", "recommended_action", "command_capable")} == {k: r2[k] for k in ("ok", "classification", "recommended_action", "command_capable")}`、`port.writes` 仍為 `[]`；`session.state == "READY"`、`session.ready_reconfirm_pending is True`（arm 設定、未被 `confirm_ready()` 清除） |
  | `test_self_test_probe_console_unlock_banner_after_precheck_zero_tx`（R5-F2） | 同上，但 profile 設 `console_unlock_env`；`paused` 期間注入 `b"CFE version 1.0\r\nU-Boot 2024.04\r\n"` → `session.boot_quiet_active() is True`、`bridge._gate_probe_quiet_until == 0.0`（login FSM 照舊不擋，`test_console_unlock_login_not_blocked_by_banner` 不變）、`session.login_gen` 與 `bridge._gate_login_gen_floor` 皆不變、`bridge._gate_self_test_quiet_until == session.boot_quiet_until`；`resume` → 以 spy 包住 `UARTBridge._submit_guard` 記錄 yield 值：`source == "system:self_test"` 的那次為 `"quiet"`；`port.writes == []`、`r["classification"] == "AUTOBOOT_QUIET"`、`r["recommended_action"] == "wait"` |
  | `test_self_test_probe_quiet_armed_during_submit_counts_committed`（R5-F2，線性化另一側） | READY session、target 不回 nonce；`_inside_tx_submit` 設 `in_submit` 並等 `resume`（持 `_tx_fence_lock`）；呼叫端 `mgr.self_test("COM0", timeout_s=0.3)`；另一 thread 注入 banner RX，斷言 0.2 s 內**未**返回且 `bridge._gate_self_test_quiet_until == 0.0`；設 `resume` → `port.writes == [probe]`（`probe` 為 `b"echo __READY__" + nonce + b"\n"`，整段已提交）、`source == "system:self_test"` 的 TX WAL 恰 1 筆 `len == len(probe)`；banner thread 1 s 內返回；結果 `classification == "TARGET_UNRESPONSIVE"`、`probe_ok is False`（已提交者照實計、不宣稱撤回）；`session.boot_quiet_active() is True`（未 `confirm_ready()`） |
  | `test_self_test_probe_permit_shape_ok_path_unchanged`（R5-F2，回歸） | 以 spy 包住 `UARTBridge._submit_guard` 記錄 `(source, permit)`（記錄法同 `test_login_fsm_sends_all_carry_system_probe_permit`）；READY、無 quiet、target 腳本回 `__READY__<nonce>` 與 prompt；記下 `g0 = session.login_gen`、`f0 = bridge._gate_login_gen_floor` → `r["classification"] == "OK"`、`r["probe_ok"] is True`、`session.ready_reconfirm_pending is False`；`port.writes == [f"echo __READY__{nonce}\n".encode()]`；`source == "system:self_test"` 的記錄恰 1 筆，其 permit `tx_class == "self_test_probe"`、`op_id == f"system:self_test:{nonce}"`、`bridge_generation == session.bridge_generation`、`login_gen is None`；`session.login_gen == g0`、`bridge._gate_login_gen_floor == f0`（self-test 不推進登入世代） |
  | `test_self_test_probe_not_blocked_by_reconfirm_gate`（R5-F2，回歸防護） | READY＋`ready_reconfirm_pending=True`、`boot_quiet_until=0.0`，並 `bridge.set_tx_gate(generation=session.bridge_generation, quiet=True, lease_owner=None)`（模擬 reconfirm 期間 agent gate 為真）；target 回 nonce → `r["classification"] == "OK"`、`port.writes == [probe]`、`session.ready_reconfirm_pending is False`；同一狀態下 agent `TxPermit("c1", bridge._gate_generation, "agent")` 的 `send_bytes` → `not_submitted, "quiet"`（對照：只有 agent 受 `_gate_quiet` 擋） |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_tx_permit.py` → 預期 `ImportError: cannot import name 'TxPermit'`。commit：`test(uart): OS write 前授權 permit 零 TX 失敗測試`
- [ ] **B1 實作**：依行為規格；`_gate_generation`、`_gate_quiet`、`_gate_lease_owner`、`_revoked_ops`、`_tx_fence_lock`（缺時以 `getattr(self, "_tx_fence_lock", None)` 惰性建立）以 class 層預設值提供（相容 bare bridge）；`_revoked_ops` 為 `collections.OrderedDict` 上限 512。T06 `send_bytes`「`_state_lock` 內檢查、鎖外寫入」改為「`_state_lock` 內 pre-check、每次 `os.write` 於 `_submit_guard` 內重驗後提交」；T07 `begin_drain` 改為以有上限的 `_tx_fence_lock.acquire(timeout=min(DRAIN_FENCE_ACQUIRE_S, 剩餘期限))` 設 `_tx_frozen`／`_tx_cancel`，取不到時照設並交由 Phase 2 背景完成（R3-F3，見「drain 取 fence 鎖有上限」）；`sw_core/constants.py` 新增 `DRAIN_FENCE_ACQUIRE_S = 0.05`；`_PySerialPort.cancel_pending_write()` 包 pyserial `cancel_write()`（無此方法時 no-op）。R2-F1：`_gate_probe_quiet_until`（0.0）、`_gate_login_gen_floor`（0）與 `system_permit()` 的 `threading.local` 同樣以 class 層預設值提供；`SessionRuntime.login_gen` 預設 0；`_push_probe_gate()` 對無 `set_probe_gate` 的替身 bridge 為 no-op，使既有 `FakeBridge`／mock `ensure_ready` 測試不需改動。R5-F2：`_gate_self_test_quiet_until`（0.0）以 class 層預設值提供；`_self_test_impl` 對無 `system_permit` 的替身 bridge 用 `contextlib.nullcontext()`，`send_command` 呼叫形狀不變；`contracts/behavior_changes.yaml` 的 DF-19 於本 commit 改 `status: IMPLEMENTED`（只遞增檔案 `revision`）。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_tx_permit.py tests/test_stage0_tx_outcome.py tests/test_stage0_bridge_drain.py tests/test_stage0_fakes.py tests/test_stage0_deferred.py tests/test_stage0_session_drain.py tests/test_stage0_gate_codes.py tests/test_interactive_raw.py tests/test_agent_defer_tx.py tests/test_human_agent_coexist.py tests/test_multiagent_e2e.py tests/test_issue26_interactive_guard.py tests/test_serial_port.py tests/test_concurrency_races.py tests/test_bootloader_recovery.py tests/test_boot_quiet.py tests/test_login_fsm.py tests/test_console_unlock.py tests/test_ready_reconfirm_bounds.py tests/test_login_recovery_lease.py tests/test_login_error_detail.py tests/test_rx_flood.py tests/test_brcm_prompt_regex.py tests/test_uart_flash_bridge.py tests/test_device_handoff.py tests/test_cowork_session_usability.py tests/test_session_bind.py`（`test_bootloader_recovery.py`／`test_boot_quiet.py` 守住 #114 autoboot 倒數救援與 boot quiet 基線，R2-F2；其後八檔守住 login FSM、console unlock、reconfirm 有界化與 flash gate 丟棄 system probe 的基線，R2-F1；最後三檔與 `test_boot_quiet.py`、`test_rx_flood.py`、`test_bootloader_recovery.py` 一起守住 `session.self_test` 各分類與鍵順序基線，R5-F2）；`python3 -m tools.contracts check`（DF-19 `IMPLEMENTED`）；全套依 §0 第 4 點。
- **測試資產變更**（PR 須列明並由獨立審閱者確認）：
  - `tests/test_stage0_tx_permit.py`：`_ready` 補齊成功 attach 所需的記憶體 device、RX buffer 與 bridge snapshot；spy 改用明確 `permit` 簽章，登入 fixture 使用錨定 prompt、完整行尾與固定 timeout，避免替身繞過真實 probe 條件。`test_begin_drain_fence_acquire_bounded_and_freezes_tail` 增加 `owner_gen` 收斂等待，pyserial 假物件明確提供 `is_open` 並斷言固定 chunk 常數；原有安全斷言未放寬。登入世代測試改為逐 run 驗證世代不變且跨 run 嚴格遞增；self-test golden 改經真 RPC framing 擷取 `error_code` 與拒絕原因。新增 `test_reboot_write_uncertain_arms_quiet_before_return`、`test_cancel_between_dequeue_and_manager_registration_zero_tx`、`test_interactive_open_command_expired_before_submit_zero_tx`、`test_bare_bridges_do_not_share_revoked_operation_history`、`test_attach_bridge_quiet_updates_first_attach_permit`、`test_attach_probe_failure_keeps_manual_login_path`（無帳密／未解析帳密，static／dynamic）與 console-unlock／reprobe／reboot-recovery permit 覆蓋、partial-cancel reason、AST alias／`permit=None` 防漏測試（R0-F1、R2-F1、R2-F2、R3-F3、R5-F2）。
  - `tests/stage0_fakes.py::make_service`：建立 bridge 後發布與 session generation 對齊的 TX gate，讓 harness 不會把有效 permit 誤判為 `stale_generation`（R0-F1）。
  - `tests/test_refine_ownership.py::TestRefineOwnership::test_unknown_closed_expired_and_replaced_interactive_ids_do_not_tx`：attach console 前發布 TX gate，保留原零 TX 與 ownership 斷言（R0-F1）。
  - `tests/test_stage0_bridge_drain.py::test_begin_drain_invalidation_runs_inside_state_lock`：以包裝 `_invalidate_deferred_locked` 取代已移除的舊 no-op hook；保留事件交錯與 state/fence lock 斷言，不放寬 drain 語意（R0-F1）。
  - `tests/test_contracts_keep_baseline.py::test_last_error_logging_templates_match`：更新兩個既有 logger AST source 行號至目前位置；保留 WARNING 與 session-last-error 模板逐字比對斷言（logging inventory sync）。
  - `tests/stage0_fakes.py` 的 `FakePort.write` 接受可選 `submit_guard`；R3-F3 新增 `block_in_guard=False`、`cancel_pending_write()`／`cancel_write_calls`，預設行為不變。R2-F1 不改 `login_fsm.py` 公開簽章，R5-F2 不改 `send_command` 呼叫形狀；既有固定簽章替身仍由 adapter 相容。
- [ ] **B3 commit**：`fix(uart): OS 寫入前於同一臨界區重驗並提交，失效者零 TX`

---

## Task T13：OD-11／SC-19 隔離實例測試裝置 allowlist

- **範圍**：OD-11 核准（附限制）的測試專用虛擬裝置白名單：僅對隔離實例生效；正式 daemon 設定遇到即拒絕；只接受明確列出的名稱；不關閉獨占鎖、不更換 serial backend；啟用時 `daemon status` 必須顯示。定案細節依調和表 GAP-G-07。依據：OD-11；規格 §7.4、§9.4「測試裝置選取」、§15.8.2；盤點 07 §1.6、§1.7，10 §G-07；arch §4.3 `DeviceSelection.test_allowlist`、§6.1 步驟 6。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：haiku-xhigh
- **Changelog fragment**：`changelog.d/rust-p1-od11-test-device-allowlist.md`（`type: feat`，`scope: daemon`）：新增隔離實例專用的測試裝置 allowlist（`SERIALWRAP_TEST_DEVICE_ALLOWLIST` 或 `windows.test_device_allowlist`），正式實例設定時拒絕啟動，啟用時於 `daemon status` 顯示。
- **BLOCKED-OWNER**：以 com0com DUT-side pair（Spike 1／OD-08）的實際 COM 名稱驗證 allowlist 選取與 Console-side pair 排除。

**Files**
- Create: `sw_core/test_allowlist.py`（含 `resolve_test_allowlist`、`filter_allowlist`、`ERROR_TEST_DEVICE_ALLOWLIST_REJECTED` 常數）、`tests/test_stage0_test_allowlist.py`
- Modify: `sw_core/daemon.py`（`_run_async` 最前面判定）、`sw_core/service.py`（`SerialwrapService.__init__(..., test_allowlist: AllowlistDecision | None = None)`、`health()` 加欄位、`DeviceWatcher` 來源包裝呼叫 `test_allowlist.filter_allowlist`）、`contracts/behavior_changes.yaml`（SC-19 → `IMPLEMENTED`）、`contracts/error_codes.yaml`（`TEST_DEVICE_ALLOWLIST_REJECTED`：`status: IMPLEMENTED`、`reachable: true`、`contract_tests` 指向本 Task 斷言該碼的 node；依 P0 `01-stage0-contracts.md` 對此碼的要求，同一 commit）、`contracts/rpc_schema.json`（`x-pending` 的 SC-19 `health.status.test_device_allowlist` 併入正式欄位並刪除該項）、`README.md`／`docs/`（隔離實例說明，英繁兩段）

**Interfaces**
- Produces（Python）：
  ```python
  # sw_core/test_allowlist.py
  @dataclass(frozen=True)
  class AllowlistDecision:
      enabled: bool
      coms: tuple[str, ...]             # 原始順序、去重（先出現者保留）
      source: Literal["env", "config", "none"]
      rejected_reason: str | None       # 非 None＝daemon 必須拒絕啟動
  def resolve_test_allowlist(env: Mapping[str, str], config_data: Mapping[str, Any] | None,
                             *, defaults: Mapping[str, str]) -> AllowlistDecision
      # defaults：{"SERIALWRAP_STATE_DIR": <未設定時預設>, "SERIALWRAP_RUN_DIR": …, "SERIALWRAP_CONFIG_DIR": …}
  def filter_allowlist(devices: Mapping[str, DeviceInfo], coms: tuple[str, ...]) -> dict[str, DeviceInfo]
  ```
- Rust 對應：arch §4.3 `DeviceSelection.test_allowlist: Option<Vec<String>>`、§6.1「取得 OD-11 allowlist 判定（SC-19）；違規時 stderr `serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: <reason>`，exit 2」。

**規格（凍結）**
1. 輸入：env `SERIALWRAP_TEST_DEVICE_ALLOWLIST`（逗號分隔、各項 strip、空項忽略）優先；否則 `config.yaml` 的 `windows.test_device_allowlist`（list[str]）。兩者皆無或解析後為空 → `enabled=False`、`source="none"`。比對對象為 `DeviceInfo.by_id`（Windows＝COM 名，大小寫依原字串；POSIX＝symlink 路徑）。
2. 隔離實例判定：`SERIALWRAP_STATE_DIR`、`SERIALWRAP_RUN_DIR`、`SERIALWRAP_CONFIG_DIR` 三者都在 env 中明確設定（strip 後非空）**且**各自 `os.path.normcase(os.path.abspath(os.path.expanduser(v)))` 不等於未設定時的預設路徑。任一不符且 allowlist 啟用 → `rejected_reason`：`"not an isolated instance: <VAR> unset"` 或 `"not an isolated instance: <VAR> equals default"`（依 STATE、RUN、CONFIG 順序回報第一個違規）。
3. 型別錯誤（`windows.test_device_allowlist` 非 list 或含非字串）→ `rejected_reason="invalid windows.test_device_allowlist"`。**config 讀取失敗（審查修正，2026-10-17）**：`config.yaml` 存在但無法讀取或解析（含 PyYAML 拋出的 `ValueError`、`RecursionError`，捕捉範圍為 Exception）時，若 env 未提供非空 allowlist：隔離實例（判定條件同規格 2）→ `rejected_reason="invalid config.yaml: <例外類別名>"`；非隔離實例 → 維持未設定（`enabled=False`、`source="none"`，與基線對壞 config 的容錯相同，正式實例啟動行為不變）。缺檔視為未設定。
4. daemon：判定在 `_run_async` **最前面**（`ensure_runtime_dirs()`、取 lock、建構 `SerialwrapService` 之前——`SessionManager.__init__` 尾段的 `_save_state()` 會改寫 `state.json`，正式實例被拒時不得留下任何副作用）。輸出路徑必須能被 P0 `contracts/code_extraction.yaml` 既有的 `call_arg RuntimeError` sink（`implies_interface: proc`）擷取：`test_allowlist.py` 定義 `ERROR_TEST_DEVICE_ALLOWLIST_REJECTED = "TEST_DEVICE_ALLOWLIST_REJECTED"`，判定函式 `raise RuntimeError(ERROR_TEST_DEVICE_ALLOWLIST_REJECTED, reason)`，`_run_async` 捕捉後寫 stderr `serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: <reason>\n`、exit 2（不取 lock，故無需釋放）。不得直接以 `print` 字面輸出碼（任何 sink 都看不到，P0 C03／C04 FAIL）。通過時把 `AllowlistDecision` 以建構參數 `test_allowlist=` 傳入 `SerialwrapService`。**實作偏離（P1 T13，審查修正 2026-10-17）**：`enforce_allowlist` 實際以字面值 `"TEST_DEVICE_ALLOWLIST_REJECTED"` 傳入 `RuntimeError`，未採常數 `ERROR_TEST_DEVICE_ALLOWLIST_REJECTED`——`call_arg RuntimeError` sink 的 `value_pattern` 只擷取字串常數、不解析名稱引用，以常數傳入時 extractor 0 筆。模組常數仍保留，由 `tests/test_stage0_test_allowlist_wiring.py::test_error_code_literal_matches_constant` 比對兩者一致（防漂移）。
5. 生效時：裝置來源掃描結果經 `filter_allowlist`（只影響發現與選取；`open_serial_port`、BC-04 鎖、backend 選擇完全不變）；`health.status` 在 `multi_open_detail` 之後新增 `"test_device_allowlist":{"enabled":true,"coms":[…],"source":"env"|"config"}`；未啟用時**不輸出**此鍵（現有輸出逐位元組不變）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_test_allowlist.py`
  ```python
  from __future__ import annotations
  import pytest
  from sw_core.test_allowlist import resolve_test_allowlist, filter_allowlist, AllowlistDecision
  from sw_core.device_watcher import DeviceInfo

  DEF = {"SERIALWRAP_STATE_DIR": "$HOME/.local/state/serialwrap",
         "SERIALWRAP_RUN_DIR": "/run/user/1000/serialwrap",
         "SERIALWRAP_CONFIG_DIR": "$HOME/.config/serialwrap"}
  ISO = {"SERIALWRAP_STATE_DIR": "/tmp/iso/state", "SERIALWRAP_RUN_DIR": "/tmp/iso/run",
         "SERIALWRAP_CONFIG_DIR": "/tmp/iso/cfg"}

  def test_disabled_when_unset():
      assert resolve_test_allowlist({}, None, defaults=DEF) == AllowlistDecision(False, (), "none", None)

  def test_env_takes_precedence_and_dedups():
      d = resolve_test_allowlist({**ISO, "SERIALWRAP_TEST_DEVICE_ALLOWLIST": " COM7, COM8,COM7,"},
                                 {"windows": {"test_device_allowlist": ["COM9"]}}, defaults=DEF)
      assert d == AllowlistDecision(True, ("COM7", "COM8"), "env", None)

  def test_config_used_when_env_absent():
      d = resolve_test_allowlist(ISO, {"windows": {"test_device_allowlist": ["COM9"]}}, defaults=DEF)
      assert d.coms == ("COM9",) and d.source == "config" and d.rejected_reason is None

  @pytest.mark.parametrize("drop,reason", [
      ("SERIALWRAP_STATE_DIR", "not an isolated instance: SERIALWRAP_STATE_DIR unset"),
      ("SERIALWRAP_RUN_DIR", "not an isolated instance: SERIALWRAP_RUN_DIR unset"),
      ("SERIALWRAP_CONFIG_DIR", "not an isolated instance: SERIALWRAP_CONFIG_DIR unset"),
  ])
  def test_rejected_when_not_isolated(drop, reason):
      env = {k: v for k, v in ISO.items() if k != drop}
      env["SERIALWRAP_TEST_DEVICE_ALLOWLIST"] = "COM7"
      assert resolve_test_allowlist(env, None, defaults=DEF).rejected_reason == reason

  def test_rejected_when_dir_equals_default():
      env = {**ISO, "SERIALWRAP_STATE_DIR": DEF["SERIALWRAP_STATE_DIR"], "SERIALWRAP_TEST_DEVICE_ALLOWLIST": "COM7"}
      assert resolve_test_allowlist(env, None, defaults=DEF).rejected_reason == \
          "not an isolated instance: SERIALWRAP_STATE_DIR equals default"

  def test_filter_keeps_only_listed_exact_names():
      devs = {"COM7": DeviceInfo(by_id="COM7", real_path=r"\\.\COM7"),
              "COM8": DeviceInfo(by_id="COM8", real_path=r"\\.\COM8")}
      assert list(filter_allowlist(devs, ("COM7",))) == ["COM7"]
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_invalid_config_type_rejected` | `{"windows":{"test_device_allowlist":"COM7"}}` → `rejected_reason == "invalid windows.test_device_allowlist"` |
  | `test_daemon_refuses_start_when_rejected` | （`core_portable`＋`process_rpc`）subprocess 執行 `python3 -m sw_core.daemon`：子行程 env 由測試**完整建構**——`HOME`、`XDG_STATE_HOME`、`XDG_RUNTIME_DIR`、`XDG_CONFIG_HOME`（Windows 另設 `LOCALAPPDATA`／`APPDATA`）全部指向 `tmp_path` 下目錄、移除全部 `SERIALWRAP_*_DIR`、只設 `SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM7`（conftest 已把 `SERIALWRAP_*_DIR` 設為隔離目錄，直接繼承會使判定變成「隔離」；直接移除又會落到 live 預設路徑，故必須改寫 HOME／XDG）→ exit 2、stderr 恰為 `serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: not an isolated instance: SERIALWRAP_STATE_DIR unset\n`；`tmp_path` 下未建立任何 state／run／lock 檔（判定先於 `ensure_runtime_dirs`） |
  | `test_reject_runs_before_runtime_dirs_and_lock` | 行程內：monkeypatch `daemon.ensure_runtime_dirs`、`daemon._build_lock`、`SerialwrapService` 為 spy，`_run_async` 遇拒絕回 2 且三者呼叫數皆 0 |
  | `test_rejection_code_reaches_extraction_sink` | `python3 -m tools.contracts extract` 的輸出含 `TEST_DEVICE_ALLOWLIST_REJECTED`（interface `proc`） |
  | `test_health_status_shows_allowlist_when_enabled` | 隔離 env＋allowlist → `health()["test_device_allowlist"] == {"enabled": True, "coms": ["COM7"], "source": "env"}`，鍵位於 `multi_open_detail` 之後 |
  | `test_health_status_omits_key_when_disabled` | 未設定 → `"test_device_allowlist" not in health()` |
  | `test_allowlist_does_not_change_serial_backend_or_lock` | 啟用時 `open_serial_port` 仍為原 backend，且 T10 獨占行為不變（spy 斷言 `flock` 仍被呼叫，`posix_pty`） |
  | `test_posix_by_id_allowlist_filters_symlink_paths` | POSIX by_id symlink 路徑清單只保留列出者 |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_test_allowlist.py` → 預期 `ModuleNotFoundError: No module named 'sw_core.test_allowlist'`。commit：`test(daemon): OD-11 隔離實例測試裝置 allowlist 失敗測試`
- [ ] **B1 實作**：依規格；`defaults` 由 `sw_core.constants` 新增的 `default_isolation_paths() -> dict[str, str]`（以「env 全部未設定」重新計算 `STATE_DIR`／`RUN_DIR`／`CONFIG_DIR` 預設值的純函式）提供；config 讀取使用 T15 的 `resolve_config_path()`（Stage B 中 T15 在 T13 之後，因此本 Task 先以 `os.path.join(CONFIG_DIR, "config.yaml")` 讀取，T15 實作時改呼叫 `resolve_config_path()`，T15 的測試覆蓋此替換）。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_test_allowlist.py tests/test_device_source.py tests/test_platform_backends.py tests/test_daemon_lock.py tests/test_runtime_paths.py tests/test_contracts_catalog.py tests/test_contracts_rpc_schema.py`；`python3 -m tools.contracts check`；`scripts/stage0/win-pytest.sh tests/test_stage0_test_allowlist.py`；全套依 §0 第 4 點。
- **測試資產變更**：無。
- [ ] **B3 commit**：`feat(daemon): OD-11 新增隔離實例測試裝置 allowlist，正式實例拒絕並於 status 顯示`

---

## Task T14：BC-05 Windows `remote -R/-L`、SC-27 `connect`／`benches`、SC-21 `remote stop`、跨行程檔案鎖

- **範圍**：BC-05：移除 POSIX-only 依賴（盤點 07 §8.6 清單 #1～#6、#8；#9 指出程式碼本無 pipe，BC-05 範圍以該清單為準，PLT-D-08）；驗證非阻塞轉接（ssh 子行程 stdin=NUL、stdout/stderr→log，不使用 pipe）、程序生命週期、停止、重啟。SC-27（owner 確認項 #7 預設）：`connect` 與 `benches` 在 Windows 也支援，移除其 `REMOTE_NOT_SUPPORTED`。SC-21：`remote stop <port|all>` 為 `remote close` 別名。依據：規格 §9.1 BC-05、§4.2 #17；OD-12（remote 經 ssh 的信任範圍）；盤點 07 §8、§9；arch §4.9、§7.7。
- **Test author model**：haiku-xhigh　**Implementer model**：codex-gpt-6-luna-max
- **Changelog fragment**：`changelog.d/rust-p1-bc05-windows-remote.md`（`type: feat`，`scope: remote`）：Windows 支援 `serialwrap remote -R/-L`、`connect`、`benches`：改用跨平台檔案鎖、Windows 行程存活與整棵樹終止、免 ControlMaster 的就緒判定；新增 `remote stop` 作為 `remote close` 別名。
- **BLOCKED-OWNER**：(1) Win32-OpenSSH 對真實 relay 的三拓樸驗收（需 relay 主機與金鑰，規格 §9.1 BC-05「既有三拓樸」）；(2) Windows 測試團隊工作站的 `connect <code>` 實地操作。本機可驗證的 Windows 行程生命週期以 `windows_native` 測試涵蓋。

**Files**
- Create: `sw_core/filelock.py`、`tests/test_stage0_remote_windows.py`
- Modify: `sw_core/remote_tunnel.py`、`sw_core/cli.py`（移除 `remote`／`connect`／`benches` 的 `os.name=="nt"` 擋板、bench state 鎖改用 `filelock`、`remote stop` 別名、help 文字）、`README.md`（英繁兩段：remote stop、Windows 支援與 OD-12 信任範圍說明）、`sw_core/assets/skill/SKILL_WINDOWS.md`（移除「本期不支援 remote」段落，改為使用說明；受管區段以 `python3 -m tools.contracts skill-section` 重新產生）、`contracts/behavior_changes.yaml`（BC-05、SC-21、SC-27 → `IMPLEMENTED`）、`contracts/error_codes.yaml`（`REMOTE_NOT_SUPPORTED`：維持 `reachable: true`，`exit_codes` 移除 `connect`／`benches`／`remote` 三個 context、只留 `"daemon start": 2`，`behavior_change` 保留 `[SC-27, BC-05]` 追溯；`INVALID_BENCH_CONFIG`、`TUNNEL_STATUS_ERROR`、`UNKNOWN_BENCH_CODE` 的 platforms 加入 `windows`）、`contracts/cli_schema.json`（`x-pending` 的 SC-21 `remote stop` 併入正式子命令並刪除該項）、`tests/test_remote_cli.py`、`tests/test_remote_tunnel.py`、`tests/test_contracts_cli_schema.py`、`tests/test_contracts_gap_codes.py`（測試資產變更，見下）

**測試資產變更**（BC-05／SC-27 的預期反轉，需獨立審閱，PR 註明）：
- `tests/test_remote_cli.py::test_remote_windows_returns_not_supported`（約 108～121 行）：改為斷言 `os.name="nt"` 下 `serialwrap remote` 與 `serialwrap remote u@h:7777` **不**回 `REMOTE_NOT_SUPPORTED`（以替身 `status`／`open_tunnel` 回 `ok:true`），並更名為 `test_remote_windows_supported`。
- `tests/test_remote_tunnel.py::test_guard_platform_rejects_windows`（約 442～446 行）：改為斷言 `guard_platform()` 在 `os.name="nt"` 下為 no-op（不擲 `TunnelError`），更名為 `test_guard_platform_noop_on_windows`。
- P0 的 `tests/test_contracts_catalog.py::test_ec_exit_codes_frozen`：`REMOTE_NOT_SUPPORTED` 的期望 `exit_codes` 由 `{"daemon start": 2, "connect": 1, "benches": 1, "remote": 1}` 改為 `{"daemon start": 2}`（其餘斷言不變）。
- `tests/test_remote_endpoint.py`（約 184 行，`daemon start --endpoint tcp://1.2.3.4:7777` → `REMOTE_NOT_SUPPORTED`）與 `tests/test_win_daemon_start.py:111,126`：**不改**——該碼在 `daemon start --endpoint` 路徑仍可達（`cli.py` 約 435 行）。
- `tests/test_contracts_cli_schema.py::test_every_subcommand_options_match`：`remote stop` 是 `words` positional 的手動分派入口，不會建立 argparse subparser；用 `build_parser().parse_args(["remote", "stop"])` 與帶 port 的呼叫驗證實際 parser 接受的語法，不把 schema 欄位與寫死的 schema 欄位互相比較。
- `tests/test_contracts_gap_codes.py::TestGapPosixRemoteCli`：改名為 `TestGapRemoteCli` 並標為 `core_portable`；`INVALID_BENCH_CONFIG`、`TUNNEL_STATUS_ERROR`、`UNKNOWN_BENCH_CODE` 在 `SC-27` 支援 Windows 後皆為 Windows 可達錯誤碼，並同步更新 `INTERNAL_ERROR`／`TUNNEL_STATUS_ERROR` 的 `contract_tests` 節點與 spec errata 引用。
- `tests/test_remote_tunnel.py`：移除已過期的模組層 `fcntl` skip，逐案將不依賴 POSIX 行程 API 的測試標為 `core_portable`，保留使用 `sleep`、`os.getpgid`、`os.killpg` 的行程測試為 `posix_only`，讓可攜案例可在 Windows 收集與執行。

**Interfaces**
- Consumes：T03 `win32_files.lock_file_ex`／`unlock_file_ex`。
- Produces（Python）：
  ```python
  # sw_core/filelock.py
  @contextlib.contextmanager
  def exclusive_file_lock(path: str, *, blocking: bool = True) -> Iterator[None]
      # POSIX fcntl.flock(LOCK_EX[|LOCK_NB])；Windows LockFileEx（blocking 時不帶 FAIL_IMMEDIATELY）；鎖檔 0o600、不刪除
  # sw_core/remote_tunnel.py
  def supports_control_master(os_name: str | None = None) -> bool          # (os_name or os.name) != "nt"
  def build_argv(spec: TunnelSpec, cp: str, *, control_master: bool = True) -> list[str]
      # control_master=False：省略 ControlMaster／ControlPath／ControlPersist 三組 -o，其餘順序不變
  @dataclass(frozen=True)
  class ProcIdentity:
      pid: int
      start_ticks: int | None        # POSIX /proc/<pid>/stat 第 22 欄；Windows CreationTime FILETIME
  def pid_alive(pid: int, start_ticks: int | None, *, os_name: str | None = None) -> bool
      # Windows：OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)＋GetExitCodeProcess==STILL_ACTIVE(259)＋GetProcessTimes CreationTime 比對；絕不呼叫 os.kill(pid, 0)
  def terminate_tree(pid: int, pgid: int | None, *, timeout: float = 5.0, sleep: Callable[[float], None] = time.sleep,
                         os_name: str | None = None, start_ticks: int | None = None) -> None
          # POSIX：既有 _terminate_pgid；Windows：先比對 CreationTime，再以有 10 s timeout 的 taskkill /PID <pid> /T /F；依同一 CreationTime 有界確認已退出，失敗則 raise OSError
  def make_role_probe(spec, cp, endpoint, *, runner, ping, control_master: bool = True)
          # control_master=False：connect → ping(endpoint)；expose+tcp → runner(["ssh", "-o", "BatchMode=yes", *spec.ssh_opts, spec.ssh_target, "ss", "-ltnH", f"sport = :{port}"])，無 listener／探測失敗回 False 重試，確認為非 loopback 才 raise REMOTE_BIND_UNVERIFIED；expose+remote_socket → 以 ssh test -S 驗證
  def make_ssh_check(spec, cp, *, runner, control_master: bool = True)
      # control_master=False → lambda: True（readiness 只靠 alive＋role probe）
  ```
- `open_tunnel` 的 Windows expose+tcp 若輪詢逾時仍為 `starting`，必須在 readiness try 內轉成 `REMOTE_BIND_UNVERIFIED`，走既有 fail-closed teardown；teardown 失敗時保留 `status="error"` 的 registry state，並以 readiness 原錯誤作為主要例外。Windows `close` 與 cleanup 都將 registry 的 `pid_start_ticks` 傳入 `terminate_tree`。
  Registry JSON 欄位不變；Windows 上 `pgid` 寫 `null`、`pid_start_ticks` 寫 CreationTime 整數、`control_path` 仍寫入（不建立）。`close` 在 Windows 呼叫 `terminate_tree(pid, None)`。
- Rust 對應：arch §4.9 `ProcessTunnel`（`spawn_detached` 無 pipe、`alive`、`terminate`、`supports_control_master`）、§7.7 Windows 差異（readiness 三種、registry 鎖 LockFileEx、存活 `ProcessTunnel::alive`、停止 terminate tree、argv 不帶 3 個 ControlMaster `-o`）。

**CLI 契約（凍結）**
- `serialwrap remote stop <port|all>`：解析與輸出與 `remote close` 完全相同（`{"ok":true,"closed":[…]}`，exit 0）；`remote stop` 不帶參數 → `all`。
- Windows：`remote`／`connect`／`benches` 不再回 `REMOTE_NOT_SUPPORTED`；`guard_platform()` 保留函式但在 Windows 改為 no-op（避免外部呼叫者匯入失敗）。`REMOTE_NOT_SUPPORTED` 碼仍留在 `contracts/error_codes.yaml` 且 **`reachable: true`**——`daemon start --endpoint`（POSIX 一律、Windows 非 loopback）仍輸出此碼（`cli.py` 約 435 行），標 `false` 會使 P0 驗證器在抽出該碼時 FAIL；僅把 scope／`exit_codes` 縮為 `daemon start`。
- `ssh` 子行程：`stdin=subprocess.DEVNULL`、`stdout`／`stderr` 導向 log 檔（append）；Windows `creationflags=CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW`；POSIX `start_new_session=True`（現況）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_remote_windows.py`
  ```python
  from __future__ import annotations
  import json
  import os
  import subprocess
  import sys
  import time
  import pytest
  from sw_core import remote_tunnel as rt

  SPEC = rt.TunnelSpec(role="connect", ssh_target="u@h", port=48701, remote_socket="/run/s.sock")

  def test_argv_without_control_master_drops_three_options():
      a = rt.build_argv(SPEC, "/tmp/cm-48701", control_master=False)
      joined = " ".join(a)
      assert "ControlMaster" not in joined and "ControlPath" not in joined and "ControlPersist" not in joined
      assert a[:3] == ["ssh", "-N", "-T"] and a[-1] == "u@h"
      assert "-L" in a and "127.0.0.1:48701:/run/s.sock" in a

  def test_argv_with_control_master_unchanged_from_baseline():
      assert rt.build_argv(SPEC, "/tmp/cm") == (["ssh", "-N", "-T"] + rt.default_ssh_opts("/tmp/cm")
                                                + ["-L", "127.0.0.1:48701:/run/s.sock", "u@h"])

  def test_supports_control_master_by_platform():
      assert rt.supports_control_master("posix") is True
      assert rt.supports_control_master("nt") is False

  def test_windows_pid_alive_never_calls_os_kill(monkeypatch):
      monkeypatch.setattr(os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("os.kill used")))
      monkeypatch.setattr(rt, "_win_process_state", lambda pid: (True, 1234))
      assert rt.pid_alive(42, 1234, os_name="nt") is True
      assert rt.pid_alive(42, 999, os_name="nt") is False

  def test_role_probe_connect_without_cm_uses_ping():
      probe = rt.make_role_probe(SPEC, "/tmp/cm", "tcp://127.0.0.1:48701", runner=None,
                                 ping=lambda ep: ep == "tcp://127.0.0.1:48701", control_master=False)
      assert probe() is True

  def test_remote_stop_is_alias_of_close(tmp_path, monkeypatch):
      monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
      env = {**os.environ, "SERIALWRAP_RUN_DIR": str(tmp_path)}
      a = subprocess.run([sys.executable, "-m", "sw_core.cli", "remote", "stop", "all"], capture_output=True, text=True, env=env)
      b = subprocess.run([sys.executable, "-m", "sw_core.cli", "remote", "close", "all"], capture_output=True, text=True, env=env)
      assert a.returncode == b.returncode == 0 and a.stdout == b.stdout == '{"closed":[],"ok":true}\n'

  def test_cli_remote_not_blocked_on_simulated_nt(monkeypatch, capsys):
      from sw_core import cli
      monkeypatch.setattr(cli.os, "name", "nt")
      monkeypatch.setattr(rt, "status", lambda run_dir: {"ok": True, "tunnels": []})
      rc = cli.main(["remote", "status"])
      assert rc == 0 and json.loads(capsys.readouterr().out) == {"ok": True, "tunnels": []}

  @pytest.mark.windows_native
  def test_native_spawn_alive_terminate_tree(tmp_path):
      code = ("import subprocess,sys,time; "
              "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)")
      log = tmp_path / "t.log"
      pid, pgid, ticks, _tail = rt.real_spawner([sys.executable, "-c", code], str(tmp_path / "cm"), str(log))
      assert pgid is None and isinstance(ticks, int)
      assert rt.pid_alive(pid, ticks) is True
      rt.terminate_tree(pid, None, timeout=2.0)
      time.sleep(0.5)
      assert rt.pid_alive(pid, ticks) is False
      out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
      assert str(pid) not in out
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_filelock_posix_blocks_second_holder` | 子行程持鎖時 `exclusive_file_lock(p, blocking=False)` 擲 `BlockingIOError` |
  | `test_native_filelock_windows_blocks_second_holder`（`windows_native`） | 同上，Windows 以 LockFileEx 實作 |
  | `test_bench_state_lock_uses_filelock` | `cli._remember_bench_endpoint` 呼叫 `filelock.exclusive_file_lock(<state>.lock)`（spy），nt 不再只用 threading lock |
  | `test_connect_not_blocked_on_simulated_nt` | `os.name="nt"` 下 `serialwrap connect CODE` 不回 `REMOTE_NOT_SUPPORTED`（替身 open_tunnel） |
  | `test_benches_not_blocked_on_simulated_nt` | 同上，`serialwrap benches` 回 `{"ok":true,"benches":[…]}` |
  | `test_registry_windows_fields` | Windows spawner 寫入 registry 的 `pgid is None`、`pid_start_ticks` 為 int |
  | `test_close_windows_uses_terminate_tree` | `os_name="nt"` 時 `close` 呼叫 `terminate_tree(pid, None)`，不呼叫 `os.killpg` |
  | `test_ssh_child_has_no_pipes` | `real_spawner` 的 `Popen` kwargs：`stdin is subprocess.DEVNULL`、stdout／stderr 為檔案物件（非 `PIPE`） |
  | `test_wait_ready_expose_tcp_without_cm_uses_separate_ssh` | `control_master=False` 時 role probe 的 runner argv 不含 `ControlPath` |
  | `test_readme_mentions_remote_stop` | `README.md` 英文與繁中段皆含 `remote stop` |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_remote_windows.py` → 預期 `TypeError: build_argv() got an unexpected keyword argument 'control_master'`。commit：`test(remote): BC-05 Windows remote／connect／benches 與 remote stop 失敗測試`
- [ ] **B1 實作**：
  - `filelock.py` 依 Interfaces；`remote_tunnel.Registry.lock()` 與 cli bench state 鎖改用之；`remote_tunnel` 頂層移除 `import fcntl`（改由 `filelock` 延遲 import）。
  - `real_spawner`：Windows `Popen(argv, stdin=DEVNULL, stdout=log, stderr=log, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000, close_fds=True)`；`pgid=None`；`start_ticks=_win_creation_time(pid)`（`OpenProcess`＋`GetProcessTimes`，FILETIME 合成 64-bit int）。
  - `open_tunnel`／`wait_ready`：`cm = supports_control_master()`；以 `control_master=cm` 建 argv、ssh_check、role probe；`alive` 使用 `pid_alive`。
  - `cli._run_remote`：`words[0] in ("close","stop")`；三個命令移除 nt 擋板；help 新增 `stop`。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_remote_windows.py tests/test_remote_tunnel.py tests/test_remote_cli.py tests/test_remote_endpoint.py tests/test_win_daemon_start.py tests/test_bench_connect_cli.py tests/test_bench_tools_cli.py tests/test_bench_registry.py tests/test_skill_cmd.py tests/test_contracts_cli_schema.py tests/test_contracts_catalog.py`；`python3 -m tools.contracts check`；全套依 §0 第 4 點；`scripts/stage0/win-pytest.sh tests/test_stage0_remote_windows.py tests/test_remote_tunnel.py`；Linux Docker 三拓樸 `tools/docker/remote_tunnel_test.sh`（既有，確認 POSIX 未回歸）。
- [ ] **B3 commit**：`feat(remote): BC-05 Windows 支援 remote／connect／benches 並新增 remote stop 別名`

---

## Task T15：平台管理：SC-08、SC-09、SC-10、SC-12、SC-20、DF-14

- **範圍**：SC-08（UDS 預設 `0600`、新建目錄 `0700`；明確設定 `SERIALWRAP_SOCKET_GROUP` 時 `0660`＋chgrp，owner 確認項 #3 預設）；SC-09（設定探索：`SERIALWRAP_CONFIG` → `CONFIG_DIR/config.yaml` → 系統目錄唯讀 fallback）；SC-10（lock 與 socket 路徑比較前正規化；Windows Tier 1 改 `LockFileEx`，與 `msvcrt.locking` 互斥相容）；SC-12（監管 reconcile 確認舊 daemon 已結束，ping 不通且行程消失，上限 10 s，才啟動新 daemon）；SC-20（`daemon restart`）；DF-14（daemon 啟動順序 initial scan → `prepare_dynamic_rank` → watcher.start）。依據：規格 §6.1、§6.2、ADR-04、§4.2 #14；盤點 07 §2、§5、§6.6，02 RPC-D-05～07；arch §4.4、§6.1。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max（00-master-plan §2：arbiter／gate 狀態機、可信來源跨層傳遞、平台鎖與監管停止交接屬 Codex）
- **Changelog fragment**：`changelog.d/rust-p1-platform-mgmt.md`（`type: feat`，`scope: daemon`）：socket 權限預設 0600；新增 `SERIALWRAP_CONFIG` 與系統設定目錄 fallback；lock／socket 路徑正規化並於 Windows 改用 LockFileEx；監管切換先確認舊 daemon 結束；新增 `daemon restart`；修正 watcher 與 COM 排序的啟動順序（DF-14）。
- **BLOCKED-OWNER**：`systemd-system` 模式 reconcile（需 sudo）與 `SERIALWRAP_SOCKET_GROUP` chgrp 到真實群組（需群組成員資格）的實機驗證。

**Files**
- Create: `tests/test_stage0_platform_mgmt.py`
- Modify: `sw_core/rpc_posix.py`、`sw_core/constants.py`（`resolve_config_path`、`ensure_runtime_dirs` 0700）、`sw_core/runtime_config.py`（`RuntimeConfig(read_path, write_path=None)`：讀寫路徑分離，`write_path` 預設等於 `read_path`；讀到系統 fallback 時 `write_path` 必須是使用者路徑，永不寫系統檔）、`sw_core/util.py`（`normalize_endpoint_path`）、`sw_core/lock_posix.py`、`sw_core/lock_win.py`、`sw_core/multi_open.py`（比較用正規化、提供舊 daemon 存活查詢）、`sw_core/cli.py`（跨 socket 預檢比較、`daemon restart`）、`sw_core/setup_cmd.py`（reconcile 等待、`wait_old_daemon_gone`）、`sw_core/service.py`（`start()` 順序）、`sw_core/device_watcher.py`（新增 `scan_once()`）、`README.md`（`daemon restart`、`SERIALWRAP_CONFIG`、socket 0600，英繁兩段）、`contracts/behavior_changes.yaml`（SC-08／09／10／12／20、DF-14 → `IMPLEMENTED`）、`contracts/error_codes.yaml`（setup／restart 的既有錯誤碼 context）、`contracts/daemon_logging.yaml`（同步 watcher logger 來源行）、`contracts/cli_schema.json`（SC-20、setup／restart RPC 與 exit-code 契約）、`tools/contracts/catalog.py`（新增 daemon restart CLI context）、`tests/conftest.py`（隔離系統設定路徑）、`docs/rust-refactor/plans/02-stage0-python-bc.md`（本節測試資產記錄）
- 設定讀寫點（SC-09 必須逐一改寫，只改 constructor 預設值不會生效；讀用 `resolve_config_path()`、寫用 `resolve_config_path(for_write=True)`）：
  - `sw_core/cli.py:312` `_default_runtime_config()`（讀；`_safe_runtime_config` 經由它）；
  - `sw_core/daemon.py:84` `_write_config_endpoint`（寫 endpoint；讀寫分離）；
  - `sw_core/setup_cmd.py:554` reconcile 的 `RuntimeConfig(config_path or …)`（讀寫；明確傳入 `config_path` 時維持原樣）；
  - `sw_core/device_source.py:29` `_load_exclude_coms`（讀）；
  - `sw_core/test_allowlist.py`／`daemon._run_async` 的 allowlist config 讀取（讀；T13 暫用 `CONFIG_DIR/config.yaml`，本 Task 替換）。
  實作前以 `grep -n 'config.yaml' sw_core/*.py` 確認清單完整，新增者一併處理並寫入 PR。

**Interfaces**
- Consumes：T03 `win32_files.lock_file_ex`；T18 `find_release_manifest`（不需要，僅 DF-05 在 T18）。
- Produces（Python）：
  ```python
  def resolve_config_path(*, for_write: bool = False, env: Mapping[str, str] | None = None) -> str
      # 讀：SERIALWRAP_CONFIG（strip 非空 → expanduser，原樣使用，即使檔案不存在）> CONFIG_DIR/config.yaml（存在）
      #     > 系統檔（POSIX /etc/serialwrap/config.yaml；Windows %ProgramData%\serialwrap\config.yaml；存在）> CONFIG_DIR/config.yaml
      # 寫：SERIALWRAP_CONFIG（若設定）否則 CONFIG_DIR/config.yaml；永不寫系統檔
  def normalize_endpoint_path(s: str) -> str
      # 去掉 "unix://" 前綴 → os.path.normpath(os.path.abspath(os.path.expanduser(s)))；"tcp://…" 原樣回傳
  def wait_old_daemon_gone(endpoint: str, pid: int | None, *, timeout_s: float = 10.0,
                           ping: Callable[[str], bool], alive: Callable[[int | None], bool | None],
                           sleep: Callable[[float], None] = time.sleep,
                           monotonic: Callable[[], float] = time.monotonic) -> bool
      # 成功條件（SC-12）：ping(endpoint) 為 False **且** alive(pid) 為 False（行程可證實已消失）。
      # alive 回 None＝無法證實（pid 不明且掃描不到／權限不足）→ 視為未消失，持續等待至逾時回 False。
      # 預設 alive：pid 已知 → /proc（POSIX）或 OpenProcess＋GetExitCodeProcess（Windows，沿用 T14 pid_alive）；
      # pid 為 None → multi_open 掃描同機 serialwrapd 中 argv 的 --socket 正規化後等於 endpoint 者（找到→True、
      # 確認不存在→False、掃描失敗→None）；systemd 模式另以 `systemctl [--user] is-active serialwrap` 的
      # inactive／failed 作為已停止證據。
  # sw_core/setup_cmd.py
  def reconcile(*, old_mode, target_mode, fx, home, daemon_running: bool = False,
                old_endpoint: str | None = None, old_pid: int | None = None,
                ping: Callable[[str], bool] | None = None, alive: Callable[[int | None], bool | None] | None = None,
                **既有參數) -> dict[str, Any]
      # 新參數皆為關鍵字且有預設（None＝以實際 RPC ping／預設 alive），既有呼叫端與測試不變
  # sw_core/device_watcher.py
  DeviceWatcher.scan_once(self) -> dict[str, DeviceInfo]    # 同步掃描一次並更新內部 devices，不啟動 thread
  ```
- Rust 對應：arch §3.1 `RuntimePaths`（新增 `SERIALWRAP_CONFIG` 與系統目錄 fallback，SC-09）、§4.4 `InstanceLockFactory`（Windows `LockFileEx(EXCLUSIVE|FAIL_IMMEDIATELY, offset 0, len 1)`、路徑比較正規化）、§6.1 啟動步驟 7（DF-14）。

**凍結細節**
- SC-08：bind 前以 `old = os.umask(0o177)` 建 socket、bind 後立即還原 umask（消除「先 bind 後 chmod」的權限窗口；umask 為行程全域，只在 `JsonRpcUnixServer.start()` 單一 thread 的 bind 呼叫前後切換）；`_restrict_socket_permissions`：`group` 未設定 → `os.chmod(path, 0o600)`（冪等確認）；設定 → `0o660`＋`shutil.chown(group=…)`（失敗沿用現況處理）。`ensure_runtime_dirs` 對**新建**目錄使用 `os.makedirs(d, mode=0o700, exist_ok=True)` 後 `os.chmod(d, 0o700)`（僅限本次新建者；既有目錄權限不改）。
- SC-12：`setup_cmd.reconcile` 在停止舊監管項後呼叫 `wait_old_daemon_gone(old_endpoint, old_pid, ...)`；`False` → 不啟動新 daemon，回傳既有結果 dict 並加上 `"ok": False, "error_code": "TIMEOUT", "phase": "reconcile_wait_old_exit"`（成功路徑的回傳形狀不變、不新增 `ok` 鍵），CLI exit 1。`old_pid` 取自停止前的 `daemon status.pid`；取不到時**不得**只看 ping——改由 `wait_old_daemon_gone` 的預設 `alive` 以 multi_open 掃描／監管狀態取得可驗證證據，無法證實行程消失即逾時並阻擋新啟動。
- SC-20：`serialwrap daemon restart`：systemd 模式（user／system）→ 等同 `serialwrap service restart`（輸出與 exit code 原樣）；on-demand → `daemon stop` → `wait_old_daemon_gone`（10 s）→ 以相同 endpoint `daemon start`；輸出 `{"ok":<start.ok>,"restarted":true,"stopped":<bool>,"start":<start 回應>}`（`_print` sort_keys），exit 依 start 結果（0／2）；等待逾時 → `{"ok":false,"error_code":"TIMEOUT","phase":"restart_wait_old_exit"}` exit 1。
- DF-14：`SerialwrapService.start()` 改為：`initial = self._watcher.scan_once()`（新方法，同步掃描一次並更新內部 devices）→ `prepare_dynamic_rank(list(initial))` → `update_devices(initial)` → `bootstrap_attach()` → `self._watcher.start()`。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_platform_mgmt.py`
  ```python
  from __future__ import annotations
  import os
  import stat
  import pytest
  from sw_core import constants
  from sw_core.util import normalize_endpoint_path

  def test_config_env_wins(tmp_path):
      p = tmp_path / "explicit.yaml"
      assert constants.resolve_config_path(env={"SERIALWRAP_CONFIG": str(p)}) == str(p)

  def test_user_config_then_system_fallback(tmp_path, monkeypatch):
      monkeypatch.setattr(constants, "CONFIG_DIR", str(tmp_path / "cfg"))
      sysf = tmp_path / "etc" / "config.yaml"; sysf.parent.mkdir(); sysf.write_text("supervision_mode: on-demand\n")
      monkeypatch.setattr(constants, "SYSTEM_CONFIG_PATH", str(sysf))
      assert constants.resolve_config_path(env={}) == str(sysf)
      (tmp_path / "cfg").mkdir(); (tmp_path / "cfg" / "config.yaml").write_text("")
      assert constants.resolve_config_path(env={}) == str(tmp_path / "cfg" / "config.yaml")

  def test_write_never_targets_system_file(tmp_path, monkeypatch):
      monkeypatch.setattr(constants, "CONFIG_DIR", str(tmp_path / "cfg"))
      monkeypatch.setattr(constants, "SYSTEM_CONFIG_PATH", str(tmp_path / "etc.yaml"))
      (tmp_path / "etc.yaml").write_text("")
      assert constants.resolve_config_path(for_write=True, env={}) == str(tmp_path / "cfg" / "config.yaml")

  @pytest.mark.parametrize("raw,expected_suffix", [
      ("unix:///run/x/../x/s.sock", "/run/x/s.sock"),
      ("/run//x/./s.sock", "/run/x/s.sock"),
      ("tcp://127.0.0.1:48700", "tcp://127.0.0.1:48700"),
  ])
  def test_normalize_endpoint_path(raw, expected_suffix):
      assert normalize_endpoint_path(raw).endswith(expected_suffix)

  def test_socket_default_mode_0600(tmp_path, monkeypatch):
      monkeypatch.delenv("SERIALWRAP_SOCKET_GROUP", raising=False)
      from sw_core.rpc_posix import JsonRpcUnixServer
      import asyncio
      sock = str(tmp_path / "s.sock")
      srv = JsonRpcUnixServer(sock, lambda m, p: {"ok": True}, blocking_methods=set())
      async def run():
          await srv.start(); mode = stat.S_IMODE(os.stat(sock).st_mode); await srv.stop(); return mode
      assert asyncio.run(run()) == 0o600

  def test_wait_old_daemon_gone_requires_both_conditions():
      from sw_core.setup_cmd import wait_old_daemon_gone
      t = [0.0]
      seq = iter([True, False, False, False])
      ok = wait_old_daemon_gone("unix:///s", 42, timeout_s=10.0, ping=lambda e: False,
                                alive=lambda pid: next(seq), sleep=lambda s: t.__setitem__(0, t[0] + s),
                                monotonic=lambda: t[0])
      assert ok is True
      assert wait_old_daemon_gone("unix:///s", 42, timeout_s=1.0, ping=lambda e: True, alive=lambda p: False,
                                  sleep=lambda s: t.__setitem__(0, t[0] + s), monotonic=lambda: t[0]) is False

  def test_wait_old_daemon_gone_endpoint_down_but_process_alive_times_out():
      from sw_core.setup_cmd import wait_old_daemon_gone
      t = [0.0]
      assert wait_old_daemon_gone("unix:///s", 42, timeout_s=1.0, ping=lambda e: False, alive=lambda p: True,
                                  sleep=lambda s: t.__setitem__(0, t[0] + s), monotonic=lambda: t[0]) is False

  def test_wait_old_daemon_gone_unknown_pid_unprovable_times_out():
      from sw_core.setup_cmd import wait_old_daemon_gone
      t = [0.0]
      assert wait_old_daemon_gone("unix:///s", None, timeout_s=1.0, ping=lambda e: False, alive=lambda p: None,
                                  sleep=lambda s: t.__setitem__(0, t[0] + s), monotonic=lambda: t[0]) is False
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_socket_group_mode_0660` | 設 `SERIALWRAP_SOCKET_GROUP=<目前使用者主群組名>` → socket 為 `0o660` |
  | `test_new_runtime_dirs_0700` | 不存在的 `RUN_DIR` 經 `ensure_runtime_dirs()` 後 mode 為 `0o700`；既有目錄 mode 不變 |
  | `test_lock_compares_normalized_socket` | `SingletonLock(lock, "unix:///tmp/a/../a/s")` 與既有 daemon `/tmp/a/s` 判為同一 endpoint（`DAEMON_ALREADY_RUNNING`） |
  | `test_cli_cross_socket_precheck_normalizes` | 跨 socket 預檢把 `/run/x/./s.sock` 與 `/run/x/s.sock` 視為相同，不誤報 `DAEMON_ALREADY_RUNNING_ELSEWHERE` |
  | `test_native_windows_lock_uses_lockfileex`（`windows_native`） | `WindowsSingletonLock.acquire()` 後另一行程以 `msvcrt.locking` 取同檔失敗；反之亦然 |
  | `test_reconcile_waits_and_refuses_on_timeout` | `reconcile(old_mode=…, target_mode=…, fx=FakeEffects(), home=tmp, daemon_running=True, old_endpoint="unix:///s", old_pid=42, ping=lambda e: True, alive=lambda p: True)`（替身 clock 以 monkeypatch `setup_cmd.time`）→ 結果 `ok is False`、`error_code == "TIMEOUT"`、`phase == "reconcile_wait_old_exit"`、FakeEffects 未記錄新 daemon 啟動命令 |
  | `test_reconcile_success_shape_unchanged` | 舊 daemon 立即消失 → 回傳 dict 鍵集合與實作前相同（不含 `ok`） |
  | `test_socket_bound_with_restrictive_umask` | spy `os.umask`：bind 前設 `0o177`、bind 後還原為原值 |
  | `test_runtime_config_never_writes_system_fallback` | 只存在系統檔時讀取成功，`_write_config_endpoint` 寫入使用者 `CONFIG_DIR/config.yaml`，系統檔 bytes 不變 |
  | `test_daemon_restart_on_demand_shape` | 替身 stop／start → stdout `{"ok":true,"restarted":true,"start":{…},"stopped":true}`、exit 0 |
  | `test_daemon_restart_systemd_routes_to_service_restart` | `supervision_mode=systemd-user` → 呼叫 `service_action("restart", …)` |
  | `test_service_start_scans_before_watcher_thread` | spy 順序：`scan_once` → `prepare_dynamic_rank` → `update_devices` → `bootstrap_attach` → `watcher.start` |
  | `test_runtime_config_reads_via_resolve_config_path` | `RuntimeConfig` 預設路徑來自 `resolve_config_path()`；`SERIALWRAP_CONFIG` 設定時 daemon 寫入 endpoint 到該檔 |
  | `test_readme_mentions_daemon_restart` | `README.md` 英文與繁中段皆含 `daemon restart` |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_platform_mgmt.py` → 預期 `AttributeError: module 'sw_core.constants' has no attribute 'resolve_config_path'`。commit：`test(daemon): SC-08/09/10/12/20 與 DF-14 平台管理失敗測試`
- [ ] **B1 實作**：依凍結細節；`SYSTEM_CONFIG_PATH` 常數於 `constants.py` 以 `os.name` 決定（`/etc/serialwrap/config.yaml` 或 `os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "serialwrap", "config.yaml")`），此判斷屬 Python 平台後端選擇，Rust 由 PAL `NativeTraits` 提供；`WindowsSingletonLock` 改用 `win32_files.lock_file_ex(fd)`（探測與取鎖順序維持 Windows 現況：先試連 endpoint），release 用 `unlock_file_ex`。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_platform_mgmt.py tests/test_runtime_config.py tests/test_runtime_paths.py tests/test_runtime_paths_xdg.py tests/test_daemon_lock.py tests/test_lock_win.py tests/test_setup_reconcile.py tests/test_setup_cli.py tests/test_cli_daemon_start.py tests/test_win_daemon_start.py tests/test_com_rank.py tests/test_multi_open_detect.py tests/test_rpc_tcp.py tests/test_autospawn_gate.py tests/test_rpc_blocking_offload.py tests/test_contracts_cli_schema.py`；`python3 -m tools.contracts check`；`scripts/stage0/win-pytest.sh tests/test_stage0_platform_mgmt.py tests/test_lock_win.py`；全套依 §0 第 4 點。
- **測試資產變更**（Reviewer A 的獨立審閱確認用途合理）：`tests/test_codex_fixes.py::test_rpc_socket_default_mode_is_0600`（依 SC-08 更新舊預設權限斷言）、`::test_enable_linger_includes_user`（替 SC-12 注入舊 daemon 已退出證據）；`tests/test_cli_diagnostics.py::TestCliTraceSetup::test_setup_success_trace_covers_existing_probe_rpcs_without_changing_sequence`（新增 `health.status` trace，SC-12）；`tests/test_setup_cli.py::setup_env`（替換為隔離的舊 daemon 存活替身，並提供舊 daemon PID 與 stop 後 ping 狀態，SC-12）；`tests/test_setup_reconcile.py::test_transition_stops_old_before_starting_new`、`::test_force_overrides_flashing`、`::test_config_is_written_with_custom_path_and_socket`、`::test_system_with_sudo_installs_real_unit_content`、`::test_transition_out_of_system_with_sudo_stops_before_starts`（明確注入舊 daemon 已退出證據，SC-12）；`tests/conftest.py`（固定隔離 `SYSTEM_CONFIG_PATH`，SC-09）。`tests/test_contracts_catalog.py` 與 `tests/test_stage0_test_allowlist_wiring.py` 的試探性修改已還原，不屬本次變更。
- [ ] **B3 commit**：`feat(daemon): SC-08/09/10/12/20 平台管理對齊定稿規格並修正啟動順序（DF-14）`

---

## Task T16：缺陷修正群組：DF-01、DF-06、DF-07、DF-08、DF-09、DF-10、DF-11、DF-12、DF-13、DF-16

- **範圍**：調和表 §1.2 指派於 P1 的其餘 DF（DF-02／03／04／05／14／15／17 已併入其他 Task；DF-19 併入 T20，R5-F2；DF-18 屬 P7）。依據：調和表 §1.2 與對應列（RPC-H-01、CLI-12.01～03、EVT-D-E5、EVT-D-E6、WAL-D17、PLT-O-06～08、GAP-G-05c）。
- **Test author model**：haiku-xhigh　**Implementer model**：DF-01／06／07／08／09（event engine、client、traceback 路徑）agy-gemini-3.8-flash-high；DF-10／11／12／13／16（setup 遷移、sudo 閘、reconcile、監管資產）codex-gpt-6-luna-max（00-master-plan §2：平台／監管歸 Codex）。兩組各自 B1～B3，可平行，commit 分開。
- **Changelog fragment**：`changelog.d/rust-p1-defect-fixes.md`（`type: security`，`scope: daemon`）：修正 event 規則 ID 路徑穿越（DF-01）；client 非 JSON 回應與壞 YAML 不再印 traceback（DF-06、DF-07）；`contains` 規則 flags 驗證與規則載入容錯（DF-08、DF-09）；setup 遷移目標、sudo 權限、`ran` 欄位、minicom broker wrapper 與 Windows skill link 修正（DF-10～DF-13、DF-16）。
- **BLOCKED-OWNER**：DF-11 在真實 WSL 搭配 `--with-sudo` 寫入 `/etc/wsl.conf`（需 sudo）；DF-10 在 `systemd-system` 模式下 state 實際路徑驗證（需 sudo）。

**Files**
- Create: `tests/test_stage0_defect_fixes.py`
- Modify: `sw_core/service.py`（`event.rule_delete`、`event.reset`）、`sw_core/event_engine/engine.py`、`sw_core/event_engine/registry.py`、`sw_core/event_engine/schema.py`、`sw_core/client.py`、`sw_core/cli.py`、`sw_core/setup_cmd.py`（DF-10：`_state_path_for("systemd-system", …)` 改回傳 daemon 實際使用的路徑）、`sw_core/sysenv.py`（DF-12：`SystemEffects` 記錄實際執行的命令）、`sw_core/assets/tools/minicom-broker.sh`、`contracts/behavior_changes.yaml`（各 DF → `IMPLEMENTED`）。**不修改** `sw_core/systemd_units.py`：unit 文字屬位元組級相容面，且在 unit 加 `SERIALWRAP_STATE_DIR` 會經 `_run_dir_default()`（`constants.py`：設了 `STATE_DIR` 時 `RUN_DIR=STATE_DIR`）連帶改動 WAL 目錄、lock、`dev/ttyMCU`、events 路徑，與調和表 DF-10「遷移目標改為 daemon 實際使用的 state 路徑」方向相反。

**Interfaces**
- Produces（Python）：
  ```python
  # sw_core/event_engine/registry.py
  RULE_ID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\.[a-z0-9][a-z0-9-]{0,63}\Z")
  def valid_rule_id(rule_id: object) -> bool
  ```
  其餘為既有函式的行為修正，簽章不變。
- Rust 對應：P7 event engine（DF-01、08、09）、P3 client（DF-06）、P4 setup／service（DF-07、10～13、16）承接相同凍結行為。

**修正表（凍結；每列一個測試）**

| DF | 測試名 | 輸入 | 斷言 |
|---|---|---|---|
| DF-01 | `test_rule_delete_rejects_traversal` | `event.rule_delete {"rule_id":"../../x"}`（另放一個 `x.json` 於上層目錄） | 回 `{"ok":true,"deleted":false,…}`、上層檔案仍存在 |
| DF-01 | `test_event_reset_rejects_invalid_id_without_counting` | `event.reset {"rule_id":"A/B"}` | 不建立或修改任何 counter 檔；回應中重設數為 0 |
| DF-01 | `test_valid_rule_id_still_deletes` | `rule_id="team.rule-1"` 存在 | `deleted is True` |
| DF-06 | `test_client_non_json_response_invalid_response` | stub socket 回 `b"hello\n"` | CLI stdout `{"error_code":"INVALID_RESPONSE","ok":false}`（含既有 message 鍵則保留）、exit 2、stderr 無 `Traceback` |
| DF-07 | `test_supervision_mode_bad_yaml_one_line_exit1` | `config.yaml` 內容 `":\n- ["` | `serialwrap supervision-mode` stderr 恰一行、exit 1 |
| DF-07 | `test_service_bad_yaml_internal_error_json` | 同上，`serialwrap service status` | stdout `{"error_code":"INTERNAL_ERROR","message":…,"ok":false}`、exit 1 |
| DF-07 | `test_setup_unexpected_exception_internal_error_json` | `setup` 內部擲 `RuntimeError` | 同上形狀、exit 1 |
| DF-08 | `test_contains_rule_flags_validated` | `contains` 規則帶 `flags:"q"` | `INVALID_RULE_SCHEMA`，`error` 含 `unsupported regex flag: q`；規則未落盤 |
| DF-09 | `test_load_all_value_error_is_failed_load` | 規則檔觸發 `ValueError`／`TypeError` | `load_all()` 回傳 `failed` 含該檔、不擲例外；`engine.start()` 成功 |
| DF-10 | `test_system_scope_migration_targets_daemon_state` | reconcile 到 `systemd-system`，`run_user` 的 home 為 `tmp/home_ru`（替身 `pwd.getpwnam`），呼叫者 home 不同 | 遷移目標等於 system unit（`User=<run_user>`、無 `SERIALWRAP_STATE_DIR`、systemd 不設 `XDG_STATE_HOME`）下 daemon 實際使用的 `<run_user home>/.local/state/serialwrap/state.json`；unit 文字與實作前逐位元組相同（`render_system_unit` 輸出比對基線） |
| DF-11 | `test_wsl_systemd_sudo_only_with_flag` | WSL＋目標 systemd、未帶 `--with-sudo` | FakeEffects 未執行 sudo；結果 `pending_sudo` 含寫 `/etc/wsl.conf` 的命令 |
| DF-11 | `test_wsl_systemd_sudo_with_flag_runs` | 帶 `--with-sudo` | FakeEffects 執行 sudo 命令一次 |
| DF-12 | `test_reconcile_ran_lists_executed_commands` | 以**真實 `SystemEffects`**（非 FakeEffects——`FakeEffects` 本來就有 `.calls`，現況即綠；缺陷是 `SystemEffects` 沒有 `.calls`，生產環境 `ran` 恆為 `[]`）並 monkeypatch `subprocess.run` 回成功，reconcile 執行 2 個命令 | `result["ran"] == [<cmd1>, <cmd2>]`（實際執行的指令，`applied` 語意不變）；`SystemEffects().calls` 存在且與 `ran` 一致 |
| DF-13 | `test_minicom_broker_wrapper_finds_renamed_router` | 物化後執行 `serialwrap-minicom-broker --help`（以替身 PATH） | 呼叫的是 `serialwrap-minicom`，不再 exit 2 |
| DF-16 | `test_windows_skill_link_falls_back_to_copy` | `_force_symlink` 擲 `OSError(1314)` 模擬 | skill 目錄被複製、`setup` 不擲例外 |

**Steps**
- [ ] **A1 寫失敗測試**：依修正表寫 `tests/test_stage0_defect_fixes.py`；代表性測試碼：
  ```python
  from __future__ import annotations
  import json
  from sw_core.event_engine.registry import valid_rule_id

  def test_rule_id_regex_examples():
      assert valid_rule_id("team.rule-1") is True
      for bad in ("../x", "A.b", "a", "a.b/c", "a." + "b" * 64, "-a.b", None, 3):
          assert valid_rule_id(bad) is False

  def test_rule_delete_rejects_traversal(stage0_event_service, tmp_path):
      svc, rules_dir = stage0_event_service
      victim = rules_dir.parent / "x.json"; victim.write_text("{}")
      r = svc.rpc("event.rule_delete", {"rule_id": "../x"})
      assert r["ok"] is True and r["deleted"] is False
      assert victim.exists()
  ```
  > `stage0_event_service` fixture：建 `SerialwrapService`，`SERIALWRAP_EVENTS_DIR` 指到 `tmp_path/"events"`（conftest 已隔離），回傳 `(svc, Path(EVENTS_DIR))`。
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_defect_fixes.py` → 預期 `ImportError: cannot import name 'valid_rule_id'` 與各斷言失敗。commit：`test(daemon): DF 群組缺陷修正失敗測試`
- [ ] **B1 實作**：逐列修正；DF-01 在 service 層與 engine 層各檢查一次（防禦縱深）；DF-06 在 `client.py` 的 `json.loads` 外加 `except ValueError` → 回 `{"ok":False,"error_code":"INVALID_RESPONSE"}`，CLI 以既有錯誤輸出路徑 exit 2；DF-07 的 traceback 路徑以 `try/except yaml.YAMLError`（supervision-mode）與 `except Exception`（service、setup、daemon 子命令）包住並輸出凍結形狀；DF-16 在 `os.name=="nt"` 且 symlink 失敗時 `shutil.copytree(target, link, dirs_exist_ok=True)`；DF-10 的 `_state_path_for("systemd-system", home, *, run_user=None)` 以 `pwd.getpwnam(run_user).pw_dir`（`run_user` 由既有 `_resolve_run_user` 解析）組出 `<home>/.local/state/serialwrap/state.json`，`pwd` 不可用時回退呼叫者 home 並 log WARNING；DF-12 在 `SystemEffects.run` 成功執行後 `self.calls.append(list(cmd))`（`__init__` 建 `self.calls: list[list[str]] = []`）。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_defect_fixes.py tests/test_event_rpc.py tests/test_event_registry.py tests/test_event_schema.py tests/test_event_cli.py tests/test_setup_reconcile.py tests/test_setup_materialize.py tests/test_setup_cli.py tests/test_wsl_systemd.py tests/test_systemd_units.py tests/test_minicom_router.py tests/test_cli_diagnostics.py tests/test_cli_stderr_full_error.py tests/test_sysenv.py`；`python3 -m tools.contracts check`；全套依 §0 第 4 點。
- **測試資產變更**：若既有 `tests/test_setup_reconcile.py` 斷言 `systemd-system` 遷移目標為 `/var/lib/serialwrap/state.json`，該斷言屬 DF-10 預期反轉：先列入本小節（檔名、測試名）並經獨立審閱才可改；其餘無。
- [ ] **B3 commit**：`fix(daemon): 修正 event 規則路徑穿越與 client／setup 缺陷群組（DF-01、06～13、16）`

---

## Task T17：F12 `f12-wal-path-live` 與 L3 `p0-wal-live` 改為 seq／marker／CRC oracle

- **範圍**：OD-13 核准的 oracle 修訂：F12 由 mtime 改為讀回可關聯 marker 的新增 WAL 紀錄並驗證 JSON、payload、len、CRC 與 seq 語意；rotate／reset 時重新辨識目前路徑與保留檔案；timeout、無法讀取或證據不完整不得 PASS；mtime／大小只作診斷；讀取器在 Windows 以 share-delete 開檔，不阻擋產品 rotate。調和表 TST-D-7 一併修訂 L3 `realhw/cases/p0.py::p0_wal_live`（原讀死路徑 mirror log）。依據：規格 §8.3、§10.4（測試工具變更需獨立審閱）、§15.10；OD-13；盤點 08 §3.9；CLAUDE.md 回歸 case 政策（#155）。
- **Test author model**：haiku-xhigh　**Implementer model**：agy-gemini-3.8-flash-high
- **Changelog fragment**：`changelog.d/rust-p1-f12-wal-oracle.md`（`type: change`，`scope: regression`）：F12 `f12-wal-path-live` 與 L3 `p0-wal-live` 改以讀回含 marker 的新增 WAL 紀錄並驗證 CRC 判定活性，不再依賴 mtime。
- **BLOCKED-OWNER**：(1) L2 F12 兩個 case 在擁有者 bench 實跑（TestPilot `serialwrap_regression`）；(2) L3 `p0-wal-live` 在 realhw 實跑；(3) Windows 上 F12 對常駐 handle WAL 的實機驗證（OD-08 原生 COM）。

**Files**
- Create: `realhw/wal_oracle.py`、`tests/test_stage0_f12_oracle.py`
- Modify: `regression/serialwrap_regression/cases/f12_diagnostics.py`、`realhw/cases/p0.py`、`docs/regression-plugin.md`（F12 oracle 說明與 family↔issue 對照不變）

**Interfaces**
- Consumes：產品 CLI `daemon status`（`wal_path`）、`cmd submit`／`cmd status`（`cmd_id`）；**不得** import `sw_core`（測試工具獨立性）。
- Produces（Python，stdlib only）：
  ```python
  # realhw/wal_oracle.py
  @dataclass(frozen=True)
  class WalCursor:
      path: str
      identity: tuple[int, int]        # (st_dev, st_ino)
      offset: int                      # 起始時檔案大小（只從此之後找新紀錄）
      last_seq: int | None             # 起始時最後一筆完整紀錄的 seq
  @dataclass(frozen=True)
  class OracleResult:
      ok: bool
      reason_code: str                 # "ok" | "wal_unreadable" | "wal_record_corrupt" | "wal_marker_not_found" | "wal_seq_regressed"
      matched: dict[str, Any] | None   # 命中的原始紀錄
      raw_lines: tuple[str, ...]       # 讀到的新增完整行（證據）
      diagnostics: dict[str, Any]      # size_before/after、mtime_before/after、archives_scanned
  def open_cursor(wal_path: str) -> WalCursor
  def read_shared(path: str, offset: int) -> bytes        # Windows：CreateFileW(GENERIC_READ, FILE_SHARE_READ|WRITE|DELETE)；POSIX：open("rb")
  def verify_record(rec: dict[str, Any]) -> str | None      # None=合法；否則 "schema"|"b64_invalid"|"len_mismatch"|"crc_mismatch"
  def scan_new_records(cursor: WalCursor, *, allow_reset: bool = False) -> tuple[list[dict[str, Any]], list[str], list[str]]
      # (records, raw_lines, problems)；current 檔 identity 改變或 size < offset → 依生命週期讀完所有相關檔：
      # 先找 identity 等於 cursor.identity 的歸檔（path + ".*"）從 offset 讀到尾，再依歸檔後綴（時間＋.N）升冪讀
      # 其後產生的每個中間歸檔全檔，最後讀 current 全檔；找不到 identity 相符的歸檔 → problems 記 "archive_missing"
  def wait_for_marker(cursor: WalCursor, *, marker: bytes, cmd_id: str | None, timeout_s: float,
                      allow_reset: bool = False, poll_s: float = 0.5, sleep: Callable[[float], None] = time.sleep,
                      monotonic: Callable[[], float] = time.monotonic) -> OracleResult
      # allow_reset：只有呼叫端本身在 cursor 之後執行了 `wal reset`（有操作證據）才可設 True
  ```
  `OracleResult.reason_code` 另增 `"wal_seq_discontinuous"`、`"wal_evidence_incomplete"`（見判定規則 3、8）。
- Rust 對應：無（測試工具）；同一 oracle 在 Stage 1／Slice 2.x A/B 中對 Rust artifact 執行。

**判定規則（凍結）**
1. 命中條件：新增紀錄中存在 `dir == "TX"`、`cmd_id == <submit 回傳的 cmd_id>`、`base64` 解碼後 payload 含 `marker` 的紀錄，且 `verify_record` 為 None；或（`cmd_id` 不可得時）`dir == "RX"` 且 payload 含 marker 的紀錄。背景雜訊造成的 seq 前進不算命中。
2. 任一新增完整行 JSON 解析失敗或 `verify_record` 非 None → `wal_record_corrupt`（FAIL，category `test`）。尾端不完整行（無 `\n`）不計入、下輪再讀。行尾 `\r\n`（Windows 舊檔，T02 第 2 點）先 `rstrip("\r\n")` 再解析，不視為損壞。
3. seq 語意（跨檔）：
   - 同一檔案 identity 下新增紀錄的 seq 必須嚴格遞增且大於 `cursor.last_seq`；違反 → `wal_seq_regressed`（FAIL）。
   - **rotate 不重置 seq**：跨越歸檔邊界時，下一個檔的第一筆 seq 必須等於前一個檔最後一筆 seq＋1（以 rotate 為準；重建 META 本身也消耗 seq，照實計入）；違反 → `wal_seq_discontinuous`（FAIL）。錯誤的「rotate 後 seq=1」因此不會 PASS。
   - **reset** 是唯一允許 seq 重新從 1 開始的情況，且只有 `allow_reset=True`（呼叫端有操作證據）時才接受新檔第一筆 seq＝1；`allow_reset=False` 時視同 rotate 規則。
   - `last_seq is None`（cursor 起始時讀不到最後一筆完整紀錄）時，第一個檔的嚴格遞增仍要求，跨檔連續性從第一個可讀紀錄起算。
4. `wal_path` 無法 stat／讀取 → `wal_unreadable`（FAIL，category `environment`，絕不 PASS）。
5. 期限內未命中 → `wal_marker_not_found`（FAIL，category `test`）。
6. evidence：`wal-oracle.json`（`OracleResult` 序列化：`reason_code`、`matched`、`raw_lines` 最多 200 行、`diagnostics`），保留 `daemon-status.json`、`doctor.json`、`submit.json`；`wal-mtime.txt` 改為診斷資訊寫入 `diagnostics`。
7. `p0-wal-live` 改讀 `daemon status.wal_path`（不再寫死 `~/.local/state/.../raw.mirror.log`），套用相同判定；marker 沿用 `P0WAL_<5 位亂數>`。
8. 證據不完整（`scan_new_records` 的 problems 含 `archive_missing`、歸檔讀取失敗、或兩次 rotate 之間的中間歸檔缺失）時，即使 current 檔中找到 marker 也不得 PASS → `wal_evidence_incomplete`（FAIL，category `environment`）。
9. `f12_diagnostics.py` 現況只 import `os`、`random`：本 Task 新增 `import time`，並把 `sleep=time.sleep` 明確傳給 `wait_for_marker`（測試以 monkeypatch `f12.time.sleep` 免等待）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_f12_oracle.py`
  ```python
  from __future__ import annotations
  import base64
  import json
  import os
  import sys
  import zlib
  from pathlib import Path
  from types import SimpleNamespace

  REPO = Path(__file__).resolve().parents[1]
  sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "regression"))
  from realhw import wal_oracle as wo  # noqa: E402
  from serialwrap_regression.cases import f12_diagnostics as f12  # noqa: E402

  def rec(seq, direction, payload: bytes, cmd_id=None, *, crc=None):
      return {"seq": seq, "dir": direction, "cmd_id": cmd_id, "com": "COM0", "len": len(payload),
              "crc32": crc or f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}",
              "payload_b64": base64.b64encode(payload).decode(), "loss_flag": False, "meta": {},
              "mono_ts_ns": 1, "source": "agent", "wall_ts": "2026-10-15T00:00:00+00:00"}

  def append(p: Path, *recs):
      with open(p, "a", encoding="utf-8") as fp:
          for r in recs:
              fp.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")

  def test_marker_found_with_valid_crc(tmp_path):
      p = tmp_path / "raw.wal.ndjson"; append(p, rec(1, "RX", b"boot"))
      cur = wo.open_cursor(str(p))
      append(p, rec(2, "TX", b"echo MARKER_123456\n", "c1"))
      r = wo.wait_for_marker(cur, marker=b"MARKER_123456", cmd_id="c1", timeout_s=0.1, sleep=lambda s: None)
      assert r.ok is True and r.reason_code == "ok" and r.matched["seq"] == 2

  def test_corrupt_crc_fails(tmp_path):
      p = tmp_path / "raw.wal.ndjson"; append(p, rec(1, "RX", b"x"))
      cur = wo.open_cursor(str(p))
      append(p, rec(2, "TX", b"echo MARKER_1\n", "c1", crc="00000000"))
      r = wo.wait_for_marker(cur, marker=b"MARKER_1", cmd_id="c1", timeout_s=0.1, sleep=lambda s: None)
      assert r.ok is False and r.reason_code == "wal_record_corrupt"

  def test_background_noise_is_not_enough(tmp_path):
      p = tmp_path / "raw.wal.ndjson"; append(p, rec(1, "RX", b"x"))
      cur = wo.open_cursor(str(p))
      append(p, rec(2, "RX", b"noise"), rec(3, "RX", b"more noise"))
      t = [0.0]
      r = wo.wait_for_marker(cur, marker=b"MARKER_9", cmd_id="c9", timeout_s=1.0,
                             sleep=lambda s: t.__setitem__(0, t[0] + s), monotonic=lambda: t[0])
      assert r.ok is False and r.reason_code == "wal_marker_not_found"

  def test_rotation_between_cursor_and_marker(tmp_path):
      p = tmp_path / "raw.wal.ndjson"; append(p, rec(1, "RX", b"x"))
      cur = wo.open_cursor(str(p))
      append(p, rec(2, "RX", b"pre-rotate"))
      os.replace(p, tmp_path / "raw.wal.ndjson.20261015-000000")
      append(p, rec(3, "TX", b"echo MARKER_42\n", "c42"))
      r = wo.wait_for_marker(cur, marker=b"MARKER_42", cmd_id="c42", timeout_s=0.1, sleep=lambda s: None)
      assert r.ok is True and r.diagnostics["archives_scanned"] == ["raw.wal.ndjson.20261015-000000"]

  def test_unreadable_is_environment_fail():
      try:
          wo.open_cursor("/nonexistent/raw.wal.ndjson")
      except OSError:
          pass
      r = wo.wait_for_marker(wo.WalCursor("/nonexistent/raw.wal.ndjson", (0, 0), 0, None),
                             marker=b"M", cmd_id=None, timeout_s=0.0, sleep=lambda s: None)
      assert r.reason_code == "wal_unreadable"

  def test_f12_case_passes_without_mtime_change(tmp_path, monkeypatch):
      p = tmp_path / "raw.wal.ndjson"; append(p, rec(1, "RX", b"x"))
      st = os.stat(p)
      class Sw:
          def run(self, *a):
              if a == ("daemon", "status"): return {"wal_path": str(p)}
              if a == ("doctor",): return {"checks": [{"check": "wal_dir"}]}
              raise AssertionError(a)
          def submit_and_wait(self, com, cmd):
              marker = cmd.split()[-1]
              append(p, rec(2, "TX", (cmd + "\n").encode(), "cid-1"))
              os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))     # mtime 不前進
              return {"status": "done", "cmd_id": "cid-1"}
      notes = {}
      ctx = SimpleNamespace(sw=Sw(), note=lambda n, c: notes.__setitem__(n, c) or n,
                            cfg={"boards": [{"com": "COM0"}], "timeouts": {}})
      monkeypatch.setattr(f12.time, "sleep", lambda s: None)
      result = f12.f12_wal_path_live(ctx)
      assert result.verdict == "PASS"
      assert "wal-oracle.json" in notes
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_partial_trailing_line_ignored_until_complete` | 尾端無換行的半行不造成 corrupt；補齊後命中 |
  | `test_seq_regression_same_identity_fails` | 同檔新增紀錄 seq 小於 `last_seq` → `wal_seq_regressed` |
  | `test_reset_new_file_seq_restart_allowed` | reset（identity 改變、新檔 seq 從 1）且含 marker、`allow_reset=True` → PASS |
  | `test_rotate_with_seq_reset_fails` | rotate（identity 改變、歸檔存在）後新檔 seq 從 1 且含 marker、`allow_reset=False` → `wal_seq_discontinuous` |
  | `test_double_rotate_reads_intermediate_archive` | cursor 之後連續 rotate 兩次，marker 在中間歸檔 → PASS，`diagnostics["archives_scanned"]` 依序含兩個歸檔名，seq 跨三檔連續 |
  | `test_intermediate_archive_missing_is_incomplete` | 同上但刪除中間歸檔、marker 在 current → `wal_evidence_incomplete`（不得 PASS） |
  | `test_corrupt_line_in_archive_fails` | 歸檔中新增區段有一行 CRC 錯誤 → `wal_record_corrupt` |
  | `test_crlf_lines_accepted` | 以 `\r\n` 行尾寫入的合法紀錄 → 不判為損壞，可命中 marker |
  | `test_f12_case_wal_unreadable_is_environment_fail` | `wal_path` 指向不可讀路徑 → `CaseResult` verdict `FAIL`、category `environment`、reason_code `wal_unreadable` |
  | `test_f12_case_marker_missing_fails` | submit 完成但 WAL 無 marker → `FAIL`、`wal_marker_not_found` |
  | `test_p0_wal_live_uses_status_wal_path` | `realhw.cases.p0.p0_wal_live` 讀 `daemon status.wal_path`（替身）且不存取 `Path.home()` 路徑（monkeypatch `Path.home` 擲例外） |
  | `test_wal_oracle_does_not_import_sw_core` | 以 `ast` 掃描 `realhw/wal_oracle.py` 無 `sw_core` import |
  | `test_native_reader_does_not_block_rotate`（`windows_native`） | `read_shared` 讀檔期間對同檔 `os.replace` 成功 |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_f12_oracle.py` → 預期 `ImportError: cannot import name 'wal_oracle' from 'realhw'`。commit：`test(regression): F12 WAL oracle 改為 marker／CRC 判定失敗測試`
- [ ] **B1 實作**：依 Interfaces 與判定規則；`f12_wal_path_live` 在 submit 前 `open_cursor`、submit 後 `wait_for_marker(timeout_s=ctx.cfg.get("timeouts", {}).get("wal_oracle_s", 10.0), sleep=time.sleep)`（判定規則 9），依 `reason_code` 映射 `CaseResult`；保留既有 `wal_path_missing`、`wal_path_not_found`、`doctor_wal_dir_check_missing`、`submit_precondition_failed` 前置判定與 hints（hints 改述「以讀回 marker 紀錄驗證 daemon 持續寫入」）；`issues` 維持 `("#148",)`。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_f12_oracle.py tests/test_regression_pluginfiles.py tests/test_regression_harness.py tests/test_realhw_cases.py tests/test_regression_brcm_reboot.py`；`scripts/stage0/win-pytest.sh tests/test_stage0_f12_oracle.py`。
- [ ] **B3 commit**：`test(regression): F12 與 p0-wal-live 改以讀回 marker 紀錄並驗 CRC 判定 WAL 活性`

---

## Task T18：Python 側 release manifest、`tree_sha256`、`version_pairing`、DF-05 daemon 定位

- **範圍**：規格 §9.5／OD-13：Stage 1 Rust CLI＋Python daemon 同一發佈組合版本、release manifest 分別記錄 component 身分與 artifact digest；Python 以 interpreter 執行時須另提供部署程式碼的 digest（`tree_sha256`）。Stage 1A 中 `doctor` 由 Rust CLI 委派到 Python CLI（arch §7.4），因此 Python `doctor` 必須能在 bundle 內執行 `version_pairing` 檢查；**僅在偵測到 manifest 時**加入此檢查，純 Python 安裝的 doctor 輸出逐位元組不變。DF-05：`daemon start` 定位順序 manifest launcher →（frozen 時）同層 `serialwrapd.exe`／PATH → 與 CLI 同目錄的 `serialwrapd` →（存在時）checkout 的 `serialwrapd.py` → `python -m sw_core.daemon` fallback。依據：規格 §9.4 版本列、§9.5、§12.1；arch §10.1、§10.3（本 Task 同步在 §10.3 第 3 點註明 Python doctor 對 `profiles_sha256`／`supervisor.definition_sha256` 只驗格式）；調和表 SC-25、DF-05、PLT-D-12。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：haiku-xhigh
- **Changelog fragment**：`changelog.d/rust-p1-release-manifest-pairing.md`（`type: feat`，`scope: release`）：新增 Python 側 release manifest 讀取與部署程式碼 `tree_sha256`；位於發佈組合內時 `doctor` 檢查 CLI／daemon 版本與檔案雜湊配對；`daemon start` 改依 manifest、同目錄 `serialwrapd`、checkout 腳本、`python -m sw_core.daemon` 順序定位（DF-05）。
- **BLOCKED-OWNER**：無（Stage 1 bundle 由 P3 `mkbundle.sh` 產生後才有實際 manifest；本 Task 以 fixture manifest 驗證）。

**Files**
- Create: `sw_core/release_manifest.py`、`contracts/release_manifest.schema.json`（P0 `01-stage0-contracts.md` 明定「不屬 P0、由 P1 T18 建立」）、`contracts/golden/tree_sha256.ndjson`、`tests/test_stage0_release_manifest.py`
- Modify: `sw_core/doctor_cmd.py`（條件式 `version_pairing`）、`sw_core/cli.py`（`_daemon_script_path()`（約 186 行，POSIX 路徑）與 `_daemon_spawn_argv()`（約 199 行，Windows 路徑）兩處 spawn argv 建構改呼叫 `daemon_launch_argv`）、`contracts/behavior_changes.yaml`（SC-25：`scope` 由 `rust_only` 改為 P0 `CHANGE_SCOPES` 既有值 `python_when_manifest_present`，`applies_to.python` 由 `null` 改為 `{min_version: "0.4.0"}`、`status` → `IMPLEMENTED`；DF-05 → `IMPLEMENTED`；P0 `test_bc_real_rust_only_python_null` 已允許 SC-25 取此值；`ab_participation` 依 P0 規則隨 scope 推導）、`docs/rust-refactor/plans/spec-reconciliation.md`（SC-25 範圍欄同步更新為「雙邊（Python 僅在 bundle 內偵測到 manifest 時）」；P0 已預告此變更，P2 `cases.yaml` 以 ID 引用、不受 scope 值影響）

**Interfaces**
- Produces（Python）：
  ```python
  # sw_core/release_manifest.py
  MANIFEST_NAME = "release-manifest.json"
  def file_sha256(path: str) -> str
  def tree_sha256(root: str, files: Iterable[str] | None = None) -> str
      # files 為 None：遞迴 root 下所有檔案，排除 "__pycache__" 目錄與 "*.pyc"；relpath 以 "/" 分隔；
      # 依 relpath 位元組序排序；digest = sha256("".join(f"{rel}\0{file_sha256(abs)}\n")).hexdigest()
  def package_tree_sha256() -> str                          # root = os.path.dirname(sw_core.__file__) 的上一層中 "sw_core" 子樹，relpath 以 "sw_core/" 開頭
  def find_release_manifest(env: Mapping[str, str] | None = None, argv0: str | None = None) -> str | None
      # SERIALWRAP_RELEASE_MANIFEST（strip 非空且檔案存在）> <dirname(realpath(argv0))>/../release-manifest.json（存在）> None
  def load_release_manifest(path: str) -> dict[str, Any]     # 驗 schema_version==1 與必填鍵；不符 → ValueError(f"invalid release manifest: {key}")
  def package_asset_sha256(name: Literal["skill", "skill_windows"]) -> str
      # sw_core/assets/skill/SKILL.md｜SKILL_WINDOWS.md 的 sha256（以 importlib.resources 讀，涵蓋 wheel 安裝）
  def check_version_pairing(manifest: dict[str, Any], *, manifest_dir: str, cli_version: str,
                            daemon_version: str | None, sha256_of: Callable[[str], str] = file_sha256,
                            package_tree: Callable[[], str] = package_tree_sha256,
                            asset_sha256: Callable[[str], str] = package_asset_sha256) -> dict[str, Any]
      # 回 doctor item：{"check":"version_pairing","ok":bool,"advisory":False,"detail":str,"mismatches":[…]}
  def daemon_launch_argv(*, env: Mapping[str, str] | None = None, argv0: str | None = None,
                         python: str | None = None, frozen: bool | None = None,
                         script_path: str | None = None) -> list[str]           # DF-05
      # frozen 預設 getattr(sys, "frozen", False)；script_path 預設 cli._daemon_script_path()（checkout 的 serialwrapd.py）
  ```
  manifest 必填鍵（`load_release_manifest` 與 `contracts/release_manifest.schema.json` 一致，依 arch §10.1）：`schema_version`、`bundle_version`、`stage`、`created_at`、`target`、`git_revision`、`contract_revision`、`components.cli.{implementation,version,git_revision,path,sha256}`、`components.daemon.{implementation,version,git_revision,launcher.{path,sha256},entry,runtime.{path,python_version,sha256},package.{name,version,wheel_sha256,tree_sha256}}`、`components.python_cli_delegate.{entry,tree_sha256}`、`components.assets.{skill_sha256,skill_windows_sha256,profiles_sha256}`、`components.supervisor.{default_mode,definition_sha256}`、`pairing.{rule,cli_version,daemon_version}`。
- Rust 對應：arch §10.1 manifest 欄位與 `tree_sha256` 演算法（sha256 of sorted `"<relpath>\0<sha256>\n"`）、§10.3 doctor `version_pairing`（非 advisory）；`contracts/golden/tree_sha256.ndjson` 供 Rust `sw-cli` 測試讀取比對（arch §9.1 golden 規則）。

**`version_pairing` 判定（凍結）**
1. `manifest["pairing"]["rule"] == "exact"`，且 `cli_version == pairing.cli_version == components.cli.version == bundle_version`，且 `daemon_version == pairing.daemon_version == components.daemon.version == bundle_version`（arch §10.3 第 1 點：daemon 的 `health.status.version` 亦須等於 `bundle_version`；daemon 不可達時 `daemon_version=None` → `ok:false`、mismatch `"daemon_unreachable"`，且 manifest 自身宣告不一致時另列 `"daemon_version"`）。
2. `components.cli.sha256`、`components.daemon.launcher.sha256`、`components.daemon.runtime.sha256` 與 `sha256_of(<manifest_dir>/<path>)` 相等；`components.daemon.package.tree_sha256` 與 `package_tree()` 相等（Python 由 bundle 內 interpreter 執行時，`package_tree()` 即受測程式碼）；`components.python_cli_delegate.tree_sha256` 亦與 `package_tree()` 相等（Stage 1A 實際執行 `doctor` 的是被委派的 Python CLI，arch §7.4，與 daemon 共用同一 `sw_core` 子樹）；`components.assets.skill_sha256`／`skill_windows_sha256` 與 `asset_sha256("skill")`／`asset_sha256("skill_windows")` 相等。`components.assets.profiles_sha256` 與 `components.supervisor.definition_sha256` 在 Python 側**只驗存在且為 64 位小寫 hex**（profiles 物化路徑與監管定義由 setup／P4 產生，Python doctor 無穩定的比對來源）；本 Task 同步在 arch §10.3 第 3 點註明此限縮，實際比對由 P4 的 Rust doctor 承接。
3. `mismatches` 依上列順序列出字串：`"cli_version"`、`"daemon_version"`、`"daemon_unreachable"`、`"cli_sha256"`、`"launcher_sha256"`、`"runtime_sha256"`、`"package_tree_sha256"`、`"python_cli_delegate_tree_sha256"`、`"skill_sha256"`、`"skill_windows_sha256"`、`"profiles_sha256_format"`、`"supervisor_definition_sha256_format"`；`detail` 為 `"ok"` 或 `", ".join(mismatches)`。
4. doctor 只在 `find_release_manifest()` 非 None 時附加此 item（位置：清單末端）；manifest 解析失敗 → item `ok:false`、`detail="invalid release manifest: <key>"`。
5. CLI 每次 RPC 的版本不一致警告維持現況（只警告、不阻擋，arch §10.3 第 2 點）；`f12-version-reported-consistent` 不變。

**DF-05 定位順序（凍結；保留現況 frozen 與 checkout 分支，只在其前插入 manifest 與同目錄）**：
1. manifest 存在且 `components.daemon.launcher.path` 檔案存在 → `[<abs launcher>]`；
2. `frozen`（PyInstaller）→ `sys.executable` 同層 `serialwrapd.exe` 存在則 `[<該路徑>]`，否則 `shutil.which("serialwrapd")`；都找不到 → 回 `[]`（呼叫端依現況回 `DAEMON_BINARY_NOT_FOUND`）；
3. 與 `realpath(argv0)` 同目錄存在可執行 `serialwrapd`（Windows `serialwrapd.exe`）→ `[<該路徑>]`；
4. `script_path`（原始碼 checkout 的 repo 根 `serialwrapd.py`）存在 → `[python or sys.executable, script_path]`（保留現況：source checkout 從其他 cwd 執行時，`-m sw_core.daemon` 可能找不到未安裝的 `sw_core`）；
5. 否則 `[python or sys.executable, "-m", "sw_core.daemon"]`（pipx 等安裝佈局；CLI-7.4）。
之後附加既有 `--socket`／`--lock`／`--profile-dir` 參數（既有順序）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_release_manifest.py`
  ```python
  from __future__ import annotations
  import hashlib
  import json
  from pathlib import Path
  import pytest
  from sw_core import release_manifest as rm

  def _manifest(tmp_path: Path, **over) -> Path:
      (tmp_path / "bin").mkdir(exist_ok=True)
      for rel, data in (("bin/serialwrap", b"cli"), ("bin/serialwrapd", b"launcher"), ("py/python", b"py")):
          (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True); (tmp_path / rel).write_bytes(data)
      h = lambda b: hashlib.sha256(b).hexdigest()
      m = {"schema_version": 1, "bundle_version": "0.4.0", "stage": "1A", "target": "x86_64-unknown-linux-gnu",
           "git_revision": "0" * 40, "contract_revision": "1" * 40, "created_at": "2026-12-01T03:00:00+00:00",
           "components": {
               "cli": {"implementation": "rust", "version": "0.4.0", "git_revision": "0" * 40,
                       "path": "bin/serialwrap", "sha256": h(b"cli")},
               "daemon": {"implementation": "python", "version": "0.4.0", "git_revision": "0" * 40,
                          "launcher": {"path": "bin/serialwrapd", "sha256": h(b"launcher")},
                          "entry": ["py/python", "-I", "-B", "py/serialwrapd.py"],
                          "runtime": {"path": "py/python", "python_version": "3.12.7", "sha256": h(b"py")},
                          "package": {"name": "serialwrap", "version": "0.4.0", "wheel_sha256": "2" * 64,
                                      "tree_sha256": "TREE"}},
               "python_cli_delegate": {"entry": ["py/python", "-I", "-B", "-m", "sw_core.cli"], "tree_sha256": "TREE"},
               "assets": {"skill_sha256": "S1", "skill_windows_sha256": "S2", "profiles_sha256": "3" * 64},
               "supervisor": {"default_mode": "systemd-user", "definition_sha256": "4" * 64}},
           "pairing": {"rule": "exact", "cli_version": "0.4.0", "daemon_version": "0.4.0"}}
      m.update(over)
      p = tmp_path / rm.MANIFEST_NAME; p.write_text(json.dumps(m)); return p

  def test_tree_sha256_matches_golden_vectors():
      root = Path(__file__).resolve().parents[1] / "contracts" / "golden"
      for line in (root / "tree_sha256.ndjson").read_text().splitlines():
          case = json.loads(line)
          entries = "".join(f"{rel}\0{hashlib.sha256(data.encode()).hexdigest()}\n"
                            for rel, data in sorted(case["files"].items(), key=lambda kv: kv[0].encode()))
          assert hashlib.sha256(entries.encode()).hexdigest() == case["tree_sha256"]

  def test_tree_sha256_excludes_pycache(tmp_path):
      (tmp_path / "a.py").write_text("x"); (tmp_path / "__pycache__").mkdir()
      (tmp_path / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
      expected = hashlib.sha256(f"a.py\0{hashlib.sha256(b'x').hexdigest()}\n".encode()).hexdigest()
      assert rm.tree_sha256(str(tmp_path)) == expected

  def test_pairing_ok(tmp_path):
      p = _manifest(tmp_path); m = rm.load_release_manifest(str(p))
      item = rm.check_version_pairing(m, manifest_dir=str(tmp_path), cli_version="0.4.0", daemon_version="0.4.0",
                                      package_tree=lambda: "TREE", asset_sha256={"skill": "S1", "skill_windows": "S2"}.get)
      assert item == {"check": "version_pairing", "ok": True, "advisory": False, "detail": "ok", "mismatches": []}

  def test_pairing_reports_mismatches_in_order(tmp_path):
      p = _manifest(tmp_path); m = rm.load_release_manifest(str(p))
      (tmp_path / "bin" / "serialwrap").write_bytes(b"tampered")
      item = rm.check_version_pairing(m, manifest_dir=str(tmp_path), cli_version="0.4.1", daemon_version=None,
                                      package_tree=lambda: "OTHER", asset_sha256={"skill": "S1", "skill_windows": "X"}.get)
      assert item["ok"] is False
      assert item["mismatches"] == ["cli_version", "daemon_unreachable", "cli_sha256", "package_tree_sha256",
                                    "python_cli_delegate_tree_sha256", "skill_windows_sha256"]

  def test_find_manifest_env_then_bundle_dir(tmp_path):
      p = _manifest(tmp_path)
      assert rm.find_release_manifest(env={"SERIALWRAP_RELEASE_MANIFEST": str(p)}, argv0="/usr/bin/x") == str(p)
      assert rm.find_release_manifest(env={}, argv0=str(tmp_path / "bin" / "serialwrap")) == str(p)
      assert rm.find_release_manifest(env={}, argv0="/nonexistent/bin/serialwrap") is None

  def test_daemon_launch_argv_order(tmp_path, monkeypatch):
      p = _manifest(tmp_path)
      none_py = str(tmp_path / "no-serialwrapd.py")      # 測試在 checkout 中執行，需明確停用 checkout 分支
      assert rm.daemon_launch_argv(env={"SERIALWRAP_RELEASE_MANIFEST": str(p)}, argv0="/x/serialwrap",
                                   frozen=False, script_path=none_py) == [str(tmp_path / "bin" / "serialwrapd")]
      side = tmp_path / "side"; side.mkdir(); d = side / "serialwrapd"; d.write_text("#!/bin/sh\n"); d.chmod(0o755)
      assert rm.daemon_launch_argv(env={}, argv0=str(side / "serialwrap"), frozen=False, script_path=none_py) == [str(d)]
      script = tmp_path / "serialwrapd.py"; script.write_text("")
      assert rm.daemon_launch_argv(env={}, argv0="/nonexistent/serialwrap", python="/py", frozen=False,
                                   script_path=str(script)) == ["/py", str(script)]
      assert rm.daemon_launch_argv(env={}, argv0="/nonexistent/serialwrap", python="/py", frozen=False,
                                   script_path=none_py) == ["/py", "-m", "sw_core.daemon"]
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_doctor_without_manifest_unchanged` | 無 manifest 時 `run_doctor()` 的 check 名稱清單與實作前相同（以 fixture 記錄的基線清單比對），不含 `version_pairing` |
  | `test_doctor_with_manifest_appends_pairing_last` | 設 `SERIALWRAP_RELEASE_MANIFEST` → 最後一個 item 的 `check == "version_pairing"`、`advisory is False` |
  | `test_invalid_manifest_reported` | `schema_version: 2` → item `ok is False`、`detail == "invalid release manifest: schema_version"` |
  | `test_cli_daemon_start_uses_launch_argv` | `cli._run_daemon_start` 的 spawn argv 前綴等於 `daemon_launch_argv()` 結果（替身 ProcessCtl） |
  | `test_pipx_layout_no_longer_spawns_missing_serialwrapd_py` | 模擬 pipx 佈局（無 repo 根 `serialwrapd.py`、同目錄無 `serialwrapd`）→ argv 為 `python -m sw_core.daemon`（DF-05，CLI-7.4） |
  | `test_manifest_schema_file_matches_loader_required_keys` | `contracts/release_manifest.schema.json` 的 required 與 `load_release_manifest` 驗證鍵一致（含 `python_cli_delegate`、`assets`、`supervisor`） |
  | `test_frozen_branch_preserved` | `frozen=True`、`sys.executable` 同層有 `serialwrapd.exe` → `[<該路徑>]`；同層沒有且 PATH 也沒有 → `[]` |
  | `test_missing_delegate_key_invalid` | 刪除 `components.python_cli_delegate` → `detail == "invalid release manifest: components.python_cli_delegate"` |
  | `test_profiles_supervisor_hash_format_only` | `profiles_sha256: "zz"` → mismatches 含 `"profiles_sha256_format"`；合法 64 hex 但任意值 → 不列 mismatch |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_release_manifest.py` → 預期 `ImportError: cannot import name 'release_manifest' from 'sw_core'`。commit：`test(release): Python 側 release manifest 與版本配對失敗測試`
- [ ] **B1 實作**：依 Interfaces；`contracts/golden/tree_sha256.ndjson` 至少 3 筆（單檔、巢狀目錄、含非 ASCII 檔名 `中.py`），每筆 `{"files": {relpath: 內容字串}, "tree_sha256": hex}`，由 `python3 -c` 以本模組演算法產生後 check-in；`doctor_cmd.run_doctor` 末端條件式附加；`cli._run_daemon_start` 的 argv 建構（現況 POSIX 走 `_daemon_script_path()`、Windows 走 `_daemon_spawn_argv()`）統一改呼叫 `daemon_launch_argv(argv0=sys.argv[0])`，空 list 時依現況回 `DAEMON_BINARY_NOT_FOUND`；`_daemon_script_path()` 保留供 `script_path` 預設值使用。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_release_manifest.py tests/test_doctor.py tests/test_doctor_endpoint_reachable.py tests/test_cli_daemon_start.py tests/test_win_daemon_start.py tests/test_cli_version.py tests/test_daemon_version.py tests/test_cli_version_mismatch_warning.py tests/test_packaging_entrypoints.py tests/test_contracts_catalog.py`；`python3 -m tools.contracts check`；全套依 §0 第 4 點。
- **測試資產變更**：無（現況 frozen／checkout 分支保留）。
- [ ] **B3 commit**：`feat(release): 新增 Python 側 release manifest 與版本配對檢查，daemon 定位改依 manifest（DF-05）`

---

## Task T19：Stage 0 凍結值擷取與 S1-W WAL 微基準

- **範圍**：master plan P1「Stage 0 擷取凍結值」中屬本 Phase 的部分：BC-01 後的 WAL seq 語意（rotate／reset／restart／recreate）、CRC 變體、META 空 payload、BC-03 映射、release 回應形狀，寫入 `contracts/captures/stage0-python-bc.json` 並以測試防止漂移；S1-W WAL 寫入微基準工具（OD-10：BC-01 效能以微基準與短測驗證），四流 × 10,000 筆 × 1,024 B、預填 `60/64 × R`、每流至少跨一次 rotate，輸出 p50／p95／p99 與所有 rotate 停頓；以及 OD-10(2)「BC-01 效能以微基準與**短測**驗證」的短測定義與工具。RPC 行長邊界（`RPC-H-02`）已由 P0 T8 `scripts/stage0/capture_rpc_line_limit.py` 以真實 `JsonRpcUnixServer`／`TcpRpcServer` 對 65535／65536／65537 實測並寫入 `contracts/golden/rpc/line_limit.json`，不在此重做。依據：規格 §1.1.1、§1.1.2、§1.1.3、§1.1.4、§1.2、§8.1；OD-10、OD-13；arch §5.8 熱路徑。
- **Test author model**：haiku-xhigh　**Implementer model**：agy-gemini-3.8-flash-high
- **Changelog fragment**：`changelog.d/rust-p1-stage0-captures-microbench.md`（`type: perf`，`scope: wal`）：新增 Stage 0 Python 基線凍結值擷取與 S1-W WAL 寫入微基準工具。
- **BLOCKED-OWNER**：(1) 修正後 Python 48 小時基線（Linux、Windows 各一次，bench 依序，OD-10）；(2) 端點防毒（EDR）在量測期間「無新增攔截」的查詢紀錄（OD-05、規格 §1.2「查詢權限不足不得填零」）；(3) 量測 manifest 中未列值項目（ANSI／文字／prompt 比例、暖機、取樣算法）需 Stage 0 測試負責人具體化並經獨立審閱後，才能把本工具的輸出當正式量測（規格 §1.1.2 末段）——本 Task 交付工具與本機 Linux／Windows 煙霧數據，正式量測紀錄標「待審閱口徑」。

**Files**
- Create: `scripts/stage0/capture_bc_baseline.py`、`scripts/stage0/wal_microbench.py`、`scripts/stage0/wal_shorttest.py`、`contracts/captures/stage0-python-bc.json`、`contracts/captures/stage0-python-bc.schema.json`、`tests/test_stage0_captures.py`

**Interfaces**
- Consumes：T02～T05 `WalWriter`；T06／T08 映射；T10 `device.attach` 結果表。
- Produces（Python CLI）：
  ```text
  python3 scripts/stage0/capture_bc_baseline.py --out contracts/captures/stage0-python-bc.json [--raw-out <dir>]
      # 在暫存目錄以 WalWriter／SessionManager 實跑各情境，產生兩種輸出：
      # (1) --raw-out：原始證據（不 check-in）——每情境的原始 WAL 行、回應 JSON、機器相關值原樣保留；
      # (2) --out：凍結語意投影（check-in；DISK_STABLE：sort_keys、indent=2、尾端換行），只含由實跑結果**推導**的值，
      #     schema 為 contracts/captures/stage0-python-bc.schema.json：
      # {"schema_version":1,"git_revision":…,
      #  "wal":{"rotate_bytes":67108864,"rotate_compare":">=","rotate_check":"before_write",   # 由 sw_core.constants 與實測（R-1 不 rotate、R 先 rotate）推導
      #   "crc_empty":"00000000","meta_empty_payload":{"len":0,"payload_b64":""},
      #   "seq_after_rotate":<實測：rotate 後第一筆 seq − rotate 前最後一筆 seq>,               # 期望 1（連續）
      #   "seq_after_reset":<實測 reset 後第一筆 seq>,"seq_after_restart":<實測 restart 後第一筆 seq − 重啟前 max seq>,
      #   "seq_after_external_unlink":{"meta_seq_delta":<實測>,"record_seq_delta":<實測>},
      #   "meta_recreated_example":{…正規化後…}},
      #  "bc03_mapping":[{"account":[confirmed,saw_unknown],"aborted_by_drain":bool,"expected":<error_code>}…],
      #  "release_drain_timeout_shape":{"keys":[…依序…],"error_code":…,"drain_stalled":true,"drain_timeout_epoch_type":"int"},
      #  "device_attach_results":[{"case":<T10 結果表情境名>,"expected":{"ok":bool,"error_code":str|null,"state":str}}…]}
      # 正規化（只套用於 --out）：inode／dev／FileId → "<id>"、`wall_ts` → "<wall_ts>"、`mono_ts_ns` → 0、
      #   uuid（client_id／cmd_id）→ "<uuid>"、暫存路徑前綴 → "<tmp>"；正規化欄位清單寫在 schema 的 x-normalized，經審閱後才能增減。
      #   不得把 "continues" 之類的標籤寫死成擷取結果——每個值都必須來自實跑。
  python3 scripts/stage0/wal_microbench.py --streams 4 --records 10000 --payload 1024 --rotate-bytes 67108864
         --prefill-ratio 0.9375 --out <path.json> [--wal-root <dir>] [--rate 0]
      # 輸出 {"env":{…§1.1.1 manifest 欄位…},"rotate_bytes":R,"prefill_bytes":[…],"total_records":N,
      #       "append_ns":{"p50":…,"p95":…,"p99":…,"n":ordinary_samples},"ordinary_samples":…,"rotate_samples":…,
      #       "rotate_pauses_ns":[…],"rotate_max_ns":…,"rotations_per_stream":[…]}
      # env（規格 §1.1.1／§1.2；查不到的值一律寫 "unknown"，不得填 0 或空字串）：os、os_build、python、platform、
      #   virtualization（"wsl2"｜"native"｜"vm"｜"unknown"）、cpu、cpu_count、memory_bytes、storage_device、fs_type、
      #   power（AC／電池／電源計畫）、endpoint_av（產品名與狀態；OD-05 代表機台為第三方企業 EDR）、
      #   artifacts（{"cli":…,"daemon":…}；Python 基線為 `serialwrap --version` 與 `sw_core` tree_sha256）、
      #   contract_tree（`git rev-parse HEAD:contracts`，只在證據 JSON 記錄）、tool_revision（本腳本 git blob sha1）、
      #   timing_method（"time.perf_counter_ns"）、serial（微基準不經串口 → "none"）、mix（ANSI／文字／prompt 比例常數）。
  python3 scripts/stage0/wal_shorttest.py --duration-s 3600 --rate 10240 --streams 4 --out <path.json> [--wal-root <dir>]
      # 短測（OD-10(2)）：以 S1-W 相同資料混合與 10,240 B/s/流 的目標速率持續寫入 1 小時（Linux、Windows 各一次，本機即可執行），
      # 期間至少跨 1 次 rotate（以 --rotate-bytes 預設 R 時腳本自動預填至 60/64×R）；輸出同 microbench 的 env 與分位數，另加
      # "duration_s"、"achieved_rate_bps"、"write_failures"、"recreated_count"、"loss_records"、"seq_gaps"（應為 0）；
      # 判定：write_failures==0、loss_records==0、seq_gaps==0、achieved_rate ≥ 目標 95%，否則 exit 1。分位數門檻依 OD-09 延後凍結，不在此判定。
  ```
  `git_revision` 欄位以 `git rev-parse HEAD` 取得；擷取檔在測試中比對時忽略此欄。
- Rust 對應：Rust WalWriter 以同一工具口徑（P5 另提供 Rust 版微基準）比較；`contracts/captures/` 為 P5 承接的凍結值來源（arch §0.1「待基線擷取」）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_captures.py`
  ```python
  from __future__ import annotations
  import json
  import subprocess
  import sys
  from pathlib import Path

  REPO = Path(__file__).resolve().parents[1]

  def _run(*args):
      return subprocess.run([sys.executable, *args], cwd=REPO, capture_output=True, text=True, timeout=300)

  def test_capture_is_reproducible_and_matches_checked_in(tmp_path):
      out = tmp_path / "cap.json"
      r = _run("scripts/stage0/capture_bc_baseline.py", "--out", str(out))
      assert r.returncode == 0, r.stderr
      got = json.loads(out.read_text()); want = json.loads((REPO / "contracts/captures/stage0-python-bc.json").read_text())
      got.pop("git_revision"); want.pop("git_revision")
      assert got == want

  def test_capture_contains_required_frozen_values(tmp_path):
      want = json.loads((REPO / "contracts/captures/stage0-python-bc.json").read_text())
      import jsonschema
      jsonschema.validate(want, json.loads((REPO / "contracts/captures/stage0-python-bc.schema.json").read_text()))
      assert want["wal"]["rotate_bytes"] == 67108864 and want["wal"]["rotate_compare"] == ">="
      assert want["wal"]["rotate_check"] == "before_write"
      assert want["wal"]["crc_empty"] == "00000000"
      assert want["wal"]["seq_after_reset"] == 1 and want["wal"]["seq_after_rotate"] == 1
      assert {m["expected"] for m in want["bc03_mapping"]} == {
          "COMMAND_ABORTED_IN_FLIGHT", "SEND_FAILED", "FLUSHED_BY_RECOVERY"}
      assert want["release_drain_timeout_shape"]["error_code"] == "SESSION_RECOVERING"

  def test_microbench_smoke_crosses_rotate(tmp_path):
      out = tmp_path / "bench.json"
      r = _run("scripts/stage0/wal_microbench.py", "--streams", "2", "--records", "300", "--payload", "1024",
               "--rotate-bytes", "262144", "--prefill-ratio", "0.9375", "--out", str(out), "--wal-root", str(tmp_path / "w"))
      assert r.returncode == 0, r.stderr
      d = json.loads(out.read_text())
      assert d["total_records"] == 600
      assert d["ordinary_samples"] + d["rotate_samples"] == 600       # rotate 當筆不計入 append_ns（§1.2）
      assert d["append_ns"]["n"] == d["ordinary_samples"] and d["rotate_samples"] == len(d["rotate_pauses_ns"])
      assert all(n >= 1 for n in d["rotations_per_stream"])
      for k in ("os", "python", "virtualization", "memory_bytes", "storage_device", "power", "endpoint_av",
                "artifacts", "contract_tree", "tool_revision", "timing_method", "serial"):
          assert k in d["env"] and d["env"][k] not in (0, "", None)  # 查不到寫 "unknown"，不得填零
      assert d["rotate_max_ns"] == max(d["rotate_pauses_ns"])
      assert d["append_ns"]["p50"] <= d["append_ns"]["p95"] <= d["append_ns"]["p99"]

  def test_microbench_prefill_is_parseable_wal(tmp_path):
      out = tmp_path / "bench.json"
      r = _run("scripts/stage0/wal_microbench.py", "--streams", "1", "--records", "10", "--payload", "64",
               "--rotate-bytes", "65536", "--prefill-ratio", "0.9375", "--out", str(out), "--wal-root", str(tmp_path / "w"))
      assert r.returncode == 0, r.stderr
      files = list((tmp_path / "w").rglob("raw.wal.ndjson*"))
      assert files, "預填後必須存在 WAL 檔"
      for p in files:
          for line in p.read_text(encoding="utf-8").splitlines():
              assert isinstance(json.loads(line)["seq"], int)   # 預填不得用無效 padding（§1.1.3）

  def test_shorttest_smoke(tmp_path):
      out = tmp_path / "short.json"
      r = _run("scripts/stage0/wal_shorttest.py", "--duration-s", "2", "--rate", "10240", "--streams", "2",
               "--rotate-bytes", "16384", "--out", str(out), "--wal-root", str(tmp_path / "w"))
      assert r.returncode == 0, r.stderr
      d = json.loads(out.read_text())
      assert d["write_failures"] == 0 and d["loss_records"] == 0 and d["seq_gaps"] == 0
  ```
  `test_capture_is_reproducible_and_matches_checked_in` 只比對正規化後的 `--out` 投影；原始證據（`--raw-out`）不參與比對。
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_captures.py` → 預期 `returncode == 2`（腳本不存在）與 `FileNotFoundError`。commit：`test(wal): Stage 0 凍結值擷取與 S1-W 微基準失敗測試`
- [ ] **B1 實作**：
  - `capture_bc_baseline.py`：只用 `sw_core` 公開 API 與 `tests/stage0_fakes.py` 的替身（以 `sys.path` 加入 `tests/`）；每個情境在獨立暫存目錄執行；輸出排序穩定；`rotate_*` 三欄以 `sw_core.constants.DEFAULT_WAL_ROTATE_BYTES` 與實測（檔案大小 `R-1` 不 rotate、恰 `R` 時下一筆先 rotate）推導，實測與常數不一致時 exit 1；依正規化清單產生 `--out`。
  - `contracts/captures/stage0-python-bc.schema.json`：定義上方 `--out` 結構（`bc03_mapping[*]` 必有 `account`、`aborted_by_drain`、`expected`；`device_attach_results[*]` 必有 `case`、`expected`）與 `x-normalized` 清單。
  - `wal_shorttest.py`：共用 microbench 的資料產生器與 env 蒐集；以 `time.monotonic` 節流；本機 Linux 與 Windows（經 `win-pytest.sh` 同樣的 staging 方式）各跑一次 1 小時並保存結果（不 check-in，路徑記入 PR）。realhw P1 WAL 子集的 bench 短測屬 BLOCKED-OWNER。
  - `wal_microbench.py`：每流一個獨立 `WalWriter(wal_dir=<root>/s<i>, rotate_bytes=R)`；預填以 `WalWriter.append` 寫入合法紀錄至 `prefill_ratio × R`（記錄實際起始大小）；每流一條 thread，以 `time.perf_counter_ns()` 量測每筆 append；當筆發生 rotate（append 前後歸檔數增加）計入 `rotate_pauses_ns`、不計入 `append_ns`（規格 §1.2「不含 rotate 當筆」）；資料內容依固定 seed 產生 ANSI／文字／prompt 混合（比例常數寫在檔頭並於輸出 `env.mix` 記錄，待測試負責人審閱）；目標速率 10,240 B/s 以 `--rate` 參數提供（預設 0＝不節流，正式量測時由 manifest 指定）。
  - 產生 `contracts/captures/stage0-python-bc.json` 並 check-in。
  - 本機執行並保存煙霧結果（不 check-in）：Linux `python3 scripts/stage0/wal_microbench.py … --out "$SCRATCH/s1w-linux.json"`；Windows 以 `scripts/stage0/win-pytest.sh` 同樣方式複製後 `cmd.exe /c py -3.12 scripts\stage0\wal_microbench.py … --out <LOCALAPPDATA 下路徑>`。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_captures.py`。
- [ ] **B3 commit**：`perf(wal): 新增 Stage 0 凍結值擷取與 S1-W WAL 寫入微基準工具`

---

## Task T21：Stage 0 實機回歸 case、程序 runbook 與 Spike 3 腳本（撰寫；實跑 BLOCKED-OWNER）

- **範圍**：把本 Phase 所有「需實機才驗得到」的 BLOCKED-OWNER 項目轉成**可執行交付物**（固定入口、revision、預期結果、責任人、證據位置），只有實跑結果維持 BLOCKED-OWNER。涵蓋：(1) 規格 §11.6 Stage 0 L2 出口「BC-02／03 有對應案例」與 CLAUDE.md 回歸 case 政策（#155）——在 `regression/` 新增 BC-03、BC-02、BC-04／SC-04 交接三個 case；(2) P2 `03-stage0-test-infra.md` Windows-native 清單指派給本計畫的 `win-wal-av-rotate`、`win-wal-av-unlink`、`win-wal-av-reader-handle`、`win-wal-f12-progress`（Spike 3，規格 §13 要求保存「環境、artifact、操作、觀測與退路」）；(3) release 持久化前後 kill、雙平台 3,000 ms drain 量測、Windows remote 三拓樸的程序。依據：規格 §6.2 交接、§11.6、§13、§9.1 BC-05；CLAUDE.md #155；`docs/regression-plugin.md`。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：haiku-xhigh
- **Changelog fragment**：`changelog.d/rust-p1-stage0-hw-cases.md`（`type: change`，`scope: regression`）：新增 Stage 0 行為變更的實機回歸 case（release 中止在途命令、reboot 後不回放舊按鍵、release 等 handle 關閉回條）、Windows WAL Spike 3 腳本與實機程序 runbook。
- **BLOCKED-OWNER**：本 Task 交付物的**實跑**全部需擁有者 bench／Windows host／relay（見 §2 彙總 #5、#6、#14～#17）；撰寫與 L1 自測不受阻。case 的 `issues` 欄掛擁有者為 Stage 0 開立的 issue 號；尚未開立時保留空 tuple 並在 PR 說明列為待補（不得自行開 issue、不得填假號）。

**Files**
- Create: `regression/serialwrap_regression/cases/f10_device_handoff.py`（family `F10`，破壞性、需 throwaway＋`allow_destructive`）、`scripts/stage0/spike3_win_wal.py`、`scripts/stage0/spike3_evidence.schema.json`、`scripts/stage0/drain_timing.py`、`realhw/procedures/stage0-release-kill.md`、`realhw/procedures/stage0-windows-remote-topologies.md`、`realhw/procedures/stage0-spike1-win-vcom.md`（跨計畫審查補入：規格 §13 Spike 1 原無任何計畫交付程序）、`tests/test_stage0_hw_cases.py`
- Modify: `regression/serialwrap_regression/cases/f05_console_coexist.py`（新增一個 case）、`docs/regression-plugin.md`（family↔issue 對照與新 case 說明）、`sw_core/session_manager.py`（`_save_state` 成功後的隔離實例 kill 測試鉤子，僅 T13 判定為隔離實例時生效）、`tests/conftest.py`（pop 清單加 `SERIALWRAP_TEST_KILL_AFTER_SAVE`）

**交付物（凍結）**

| ID | 位置 | 入口 | 預期結果 | 對應 |
|---|---|---|---|---|
| `f10-release-drain-receipt` | `f10_device_handoff.py` | TestPilot `serialwrap_regression`（`allow_destructive=true`） | throwaway daemon 對實板 console 持續 TX 時 `device release` → 回應 `ok:true` 只在 handle 關閉後出現（外部 `python -c "open(tty)"` 於回應後立即成功）；`device attach` 後恢復 `ATTACHING`→`READY` | BC-04、SC-04、SC-05 |
| `f10-release-aborts-inflight-command` | 同上 | 同上 | 長命令（`sleep 20; echo X`）送出後 release → `cmd status` 為 `COMMAND_ABORTED_IN_FLIGHT`，WAL 中該 cmd_id 的 TX 只出現一次（無重送） | BC-03 |
| `f5-deferred-dropped-after-reboot` | `f05_console_coexist.py` | 同 F5 既有執行方式（tmux＋minicom） | agent 命令進行中 human 鍵入 `echo STALE`，期間觸發 reboot（boot quiet 重新啟動）→ 開機後 console 不出現 `STALE` 的回放；未觸發 reboot 的對照組照常回放 | BC-02 |
| `win-wal-av-rotate`／`win-wal-av-unlink`／`win-wal-av-reader-handle`／`win-wal-f12-progress` | `scripts/stage0/spike3_win_wal.py --case <id> --evidence-dir <dir>` | Windows host `py -3.12`（經 `win-pytest.sh` 同樣 staging） | rotate：常駐 handle 下 rotate 成功、seq 連續；unlink：classic／POSIX delete 後 META 正確、`range_known` 依實況；reader-handle：無 share-delete 的外部 reader 期間 `rotation_failed:true` 且不遺失；f12-progress：T17 oracle 對常駐 handle WAL PASS。每項同時記錄 EDR 產品／狀態與攔截查詢結果（查不到寫 `unknown`） | BC-01、OD-05、P2 case_id |
| release 持久化 kill | `realhw/procedures/stage0-release-kill.md` | 手動程序（逐步命令） | 兩情境：(a) `_save_state` 完成**後**、回應送達前 kill（以 `SERIALWRAP_TEST_KILL_AFTER_SAVE=1` 測試鉤子觸發，僅隔離實例生效）→ 重啟仍 `RELEASED`；(b) 完成**前** kill → 依最後有效 state 恢復、不宣稱 release 完成 | §6.2 交接 |
| 3,000 ms drain 量測 | `scripts/stage0/drain_timing.py --device <by-id|COMx> --iterations 50 --out <json>` | Linux 與 Windows 實體 USB-UART 依序 | 輸出每次 release 的 Phase 1→回條耗時分佈與逾時次數；USB 拔線中 write 不返回情境另以 `--unplug-prompt` 互動記錄 | SC-04、規格 §5.2.5 |
| Spike 1 Windows 虛擬 COM 資格 | `realhw/procedures/stage0-spike1-win-vcom.md` | 手動程序（需管理員；侵入性驅動安裝由擁有者執行） | 在 OD-05 機台分別記錄：com0com 版本與簽章、Secure Boot／HVCI／VBS 狀態、DUT-side pair 與 Console-side pair 建立結果、Python 原生 serial backend 開埠（`dwShareMode=0`、第二次開啟 `ERROR_ACCESS_DENIED`）、DTR／RTS／baud 不跨 pair 外洩；失敗時記錄退路（Console 依 OD-01、DUT-side 改實體 USB-UART 直連依 OD-08），不得關閉安全功能後宣稱符合 | §13 Spike 1、OD-01、OD-08；P9 `S0-SPIKE1-WIN-VCOM` |
| Windows remote 三拓樸 | `realhw/procedures/stage0-windows-remote-topologies.md` | 手動程序 | 對既有三拓樸（expose tcp、expose remote_socket、connect）各記錄 open／status／stop／重啟後 registry 清理；信任範圍依 OD-12 記錄 ssh 驗證者 | BC-05、SC-21、SC-27 |

每個 runbook／腳本檔首段固定欄位：`entry`（命令）、`revision`（`git rev-parse HEAD:<path>` blob sha1，執行時記入證據）、`expected`、`owner`（擁有者實機）、`evidence_dir`（`~/b-log/rust-refactor/p1/<id>/<YYMMDD-HHMMSS>/`）、`rollback`（失敗時的復原步驟）。`spike3_evidence.schema.json` 定義 Spike 3 證據 JSON：`{"case_id","run_at","env":{…同 T19 env…},"artifact":{…},"steps":[{"op","ts","result"}],"observations":{…},"edr":{"product","status","interceptions"|"unknown"},"verdict":"PASS"|"FAIL"|"INSUFFICIENT_EVIDENCE","fallback":str}`。`SERIALWRAP_TEST_KILL_AFTER_SAVE` 鉤子只在 T13 判定為隔離實例時生效，正式實例忽略並 log WARNING（加入 conftest pop 清單）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_stage0_hw_cases.py`（`core_portable`＋`memory_only`；只驗 case 註冊、oracle 判定邏輯與檔案契約，不碰硬體）

  | 測試名 | 斷言 |
  |---|---|
  | `test_new_cases_registered_with_family_and_flags` | registry 含三個新 case id；F10 兩個 `destructive is True`；F5 新 case 的 `requires` 與既有 F5 case 一致 |
  | `test_f10_release_receipt_oracle_with_fake_ctx` | 替身 ctx：release 回應時間早於外部 open 成功 → FAIL（category `test`）；晚於 → PASS |
  | `test_f10_inflight_abort_oracle_detects_resend` | 替身 WAL 含同一 cmd_id 兩筆相同 TX payload → FAIL `resend_detected` |
  | `test_f5_stale_replay_oracle` | 替身 console 擷取含 `STALE` → FAIL；不含且對照組含 → PASS；對照組也不含 → `INSUFFICIENT_EVIDENCE`（不得 PASS） |
  | `test_runbooks_have_frozen_header_fields` | 三份 `realhw/procedures/*.md` 與三支腳本 docstring 皆含 `entry`、`revision`、`expected`、`owner`、`evidence_dir`、`rollback` |
  | `test_spike3_cases_match_p2_case_ids` | `spike3_win_wal.py --list` 輸出恰為 P2 指派給本計畫的四個 case_id |
  | `test_spike3_evidence_schema_rejects_missing_edr` | 缺 `edr` 的證據 JSON 不通過 schema |
  | `test_kill_hook_ignored_on_non_isolated_instance` | 非隔離實例設 `SERIALWRAP_TEST_KILL_AFTER_SAVE=1` → `_save_state` 後不 kill、log WARNING |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_stage0_hw_cases.py` → 預期 case id 不存在與檔案不存在的斷言失敗。commit：`test(regression): Stage 0 實機回歸 case 與 runbook 契約失敗測試`
- [ ] **B1 實作**：依交付物表；case 沿用 `regression/serialwrap_regression/harness.py` 的 `Case`／`register` 與 ThrowawayDaemon 既有工具；`docs/regression-plugin.md` 執行順序表把新 F10 case 放在既有 F10 之後（破壞性壓軸順序不變）。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_stage0_hw_cases.py tests/test_regression_pluginfiles.py tests/test_regression_harness.py tests/test_realhw_cases.py`；全套依 §0 第 4 點。實跑：BLOCKED-OWNER（PR 列出每項的入口與證據目錄）。
- **測試資產變更**：無。
- [ ] **B3 commit**：`test(regression): 新增 Stage 0 行為變更實機回歸 case、Spike 3 腳本與實機程序`

---

## 1. Phase 完成條件（Definition of Done）

- [ ] T01～T21 全部 B3 commit 完成；`python3 -m pytest -q tests/` 零失敗（新增 SKIP 僅限 `windows_native`／`posix_only`／`windows_com` 的平台條件，並列入報告的「證據不足」清單；`core_portable` 不得有平台 SKIP）；`tests/test_resource_markers.py`（P0）與 `tests/test_resource_report.py`（P2 Task 13）通過，所有新測試檔已依 P0 T14 規則以檔內 `pytestmark` 標註。
- [ ] `scripts/stage0/win-pytest.sh -m windows_native tests/`（Windows Python 3.12）全數 PASS 且 `summary.json` 中無被 skip 的 `windows_native` item、`rc == 0`、`finished` 為 true、`duplicates` 為空；若 runner 因缺依賴 exit 3，Phase 狀態為「未完成：Windows 原生證據缺」，不得宣告完成。正式 Stage 0 L1 出口（`S0-L1-BC`、`S0-L1-WIN-PYTEST`）另須 P2 Task 13 `l1_judge` 對 `l1_required.lock.json` 凍結集合判定 PASS（R5-F3）。
- [ ] `python3 -m tools.contracts check` PASS；兩份 SKILL 受管區段已以 `python3 -m tools.contracts skill-section` 重新產生且 `test_skill_md_blocks_in_sync` 通過；`python3 -m policy_check --repo .` PASS；`changelog.d/` 新增 21 個 fragment（T01、T21 為 `change`）；README 英繁兩段含 `daemon restart`、`remote stop`、`SERIALWRAP_CONFIG`、socket 預設 0600（SC-08）、設定探索順序（SC-09）、BC-04「daemon 持有裝置時不可再直接開 tty，請走 `serialwrap session console-attach` 或 `device release`」、Windows remote 與 OD-12 信任範圍說明；`SKILL.md`／`SKILL_WINDOWS.md` 同步 BC-04、SC-08、SC-09 的操作說明。
- [ ] `CLAUDE.md`（單一事實來源；`AGENTS.md` 等為 symlink）同步：BC-04 後 daemon 持有 tty 不可直接開的提醒、`scripts/stage0/win-pytest.sh` 用法與 exit code（3／4／5／124）、新測試檔 marker 規則引用 P0 T14、WAL 常駐 handle 下 rotate 順序與 Windows 行尾差異。
- [ ] `contracts/behavior_changes.yaml`：BC-01～BC-05、SC-01～SC-14、SC-16、SC-19、SC-20、SC-21、SC-25（`scope: python_when_manifest_present`）、SC-27、DF-01～DF-17、DF-19（R5-F2，T20）的 `status` 為 `IMPLEMENTED`（**不寫 `contract_revision` 鍵**；實作 commit SHA 記於 PR 描述與 `contracts/captures/` 證據）；`contracts/error_codes.yaml` 的 `COMMAND_ABORTED_IN_FLIGHT`、`TEST_DEVICE_ALLOWLIST_REJECTED` 為 `IMPLEMENTED`＋`reachable: true`＋非空 `contract_tests`；`rpc_schema.json`／`cli_schema.json` 中 `change` 屬本 Phase 已實作變更的 `x-pending` 項全部併入並刪除；`wal_meta_events.schema.json` 含 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK`（IMPLEMENTED）、`tx_outcome_unknown`、`command_aborted_in_flight`。
- [ ] `VERSION` 維持 `0.3.2`（依 P0：由 Stage 0 release PR 帶 `release:0.4.0` label bump）；PR 描述註明此點。
- [ ] PR 描述（`.github/pull_request_template.md` Policy Checklist）含：回歸 case 評估（#155：F12 已修 oracle；T21 新增 `f10-release-drain-receipt`、`f10-release-aborts-inflight-command`、`f5-deferred-dropped-after-reboot`，實跑屬 BLOCKED-OWNER；其餘以 pytest 覆蓋或說明免加）、**測試資產變更清單**（逐檔：`tests/test_bounded_memory.py::test_deferred_buffer_capped`（T09）、`tests/test_wal_missing_detection.py::test_recreated_wal_is_readable_again`（T05）、`tests/test_remote_cli.py::test_remote_windows_returns_not_supported`、`tests/test_remote_tunnel.py::test_guard_platform_rejects_windows`、`tests/test_contracts_catalog.py::test_ec_exit_codes_frozen`（T14）、`tests/test_resource_markers.py` 的 `windows_com` 斷言（T10）、F12 `f12-wal-path-live`、L3 `p0-wal-live`（T17），以及各 Task B2 期間追加者）及獨立審閱者簽核；issue 連結：擁有者已為 Stage 0 開 issue 時以 closing keyword（`Closes`）引用該 issue 號，未開 issue 時不引用 issue 號（R-17 不觸發），不另開 issue。
- [ ] 雙審（sonnet max＋codex `gpt-6.1-sol` medium）完成，Review Focus 五項皆有明確結論。

## 2. BLOCKED-OWNER 彙總（本環境無法產出，交付程序與工具，證據待擁有者）

| # | 項目 | 來源 Task | 需要的資源 |
|---|---|---|---|
| 1 | Windows host `py -3.12` 安裝 `pytest`／`PyYAML`／`pyserial`（使用者層級）之同意 | T01 | 擁有者同意 |
| 2 | 端點防毒（第三方企業 EDR）對常駐 WAL handle、rename 與量測期間的攔截紀錄 | T03、T19 | EDR 主控台查詢權限 |
| 3 | 實體 USB-UART 部分寫入、RTS/CTS 卡住與取消；Windows 原生 COM pyserial write timeout | T06 | 實機 bench、OD-08 COM gate |
| 4 | 3,000 ms drain 時限雙平台實測、USB 拔插時 write 不返回 | T07、T08 | 實機 bench（Linux＋Windows 依序） |
| 5 | L2 F10 release／attach 交接、程序中斷（持久化提交前後 kill）實機驗證；新增 `f10-release-drain-receipt` case 實跑 | T08 | TestPilot bench |
| 6 | L2 F5 console coexist 實機重跑 | T09 | TestPilot bench |
| 7 | Windows 原生 COM 第二次開啟 `ERROR_ACCESS_DENIED`（`windows_com` 測試）、Spike 1／OD-08 COM pair | T10、T13 | com0com 安裝（管理員）或實體 USB-UART 直連 Windows |
| 8 | 受限 `/proc`（`hidepid=2`）與 `CAP_SYS_ADMIN` 例外情境；Spike 5 BC-04／交接實機驗證 | T10 | root、實板 |
| 9 | MCU flash 真機程序（SC-14 認線資格） | T12 | 實板＋flasher |
| 10 | Win32-OpenSSH 對 relay 的三拓樸驗收、測試團隊工作站 `connect <code>` | T14 | relay 主機、金鑰、工作站 |
| 11 | `systemd-system` reconcile、`SERIALWRAP_SOCKET_GROUP` chgrp、WSL `--with-sudo` 寫 `/etc/wsl.conf` | T15、T16 | sudo |
| 12 | L2 F12 兩 case、L3 `p0-wal-live` 實跑；Windows F12 對常駐 handle 驗證 | T17 | TestPilot bench、realhw、OD-08 |
| 13 | 修正後 Python 48 小時基線（Linux、Windows 各一次，bench 依序）；量測口徑獨立審閱；realhw P1 WAL 子集的 bench 短測 | T19 | bench 約 5 天（OD-10）、測試負責人 |
| 14 | T21 新增 L2 case（`f10-release-drain-receipt`、`f10-release-aborts-inflight-command`、`f5-deferred-dropped-after-reboot`）實跑；case `issues` 欄的 Stage 0 issue 號 | T21 | TestPilot bench、擁有者開 issue |
| 15 | Spike 3 `win-wal-av-rotate`／`-unlink`／`-reader-handle`／`win-wal-f12-progress` 實跑與 EDR 攔截查詢 | T21 | Windows host、EDR 主控台權限、OD-08 |
| 16 | release 持久化前後 kill 程序、雙平台 3,000 ms drain 量測（`drain_timing.py`） | T21（程序）、T08 | 實機 bench（Linux＋Windows 依序） |
| 17 | Windows remote 三拓樸程序實跑 | T21（程序）、T14 | relay 主機、金鑰、Windows 工作站 |
