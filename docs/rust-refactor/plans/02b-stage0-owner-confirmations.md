# P1-OC Stage 0：擁有者確認清單裁決（OD-14）套用到 Python 基線與 P0 契約 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `owner-decisions-2026-10-15.md` OD-14（2026-10-16）對調和表 §3 擁有者確認清單 #1～#7 的裁決，套用到 P1 交付後的 Python Stage 0 基線（`sw_core/`）與 P0 契約（`contracts/`）：#1 attach／recover 的 `error_code` 與 `last_error` 分離（新 SC-34）；#2 `platform=passthrough` 30 秒安靜自動 `READY` 並可送命令（新 SC-35）；#3 UDS socket 維持 `0660`（SC-08 撤回，撤回 P1 T15 對 socket 與新建目錄權限的收緊）；#4 release drain 逾時新增錯誤碼 `DRAIN_STALLED`（修訂 SC-04）；#5～#7（DF-02、SC-26、SC-27）確認原預設的簿記。每項先寫失敗測試再實作，讓 Python 基線先滿足新契約，作為 Rust（P5～P8）承接與 Sequential A/B 的 A 側。

**Architecture:** 只改 Python 產品碼、P0 契約資料、P0 契約工具的一條驗證規則與測試資產，不建立 Rust crate、不新增 CLI 子命令／參數／RPC method（OD-14 相容原則：外部指令格式只能新增不能修改；狀態與回應欄位內容可調整）。本 Phase **在 P1 全部 Task 完成並合併後執行**，疊在 P1 交付物之上；`02-stage0-python-bc.md` 本身不修改（其行號為進行中的 P1 工作流所依賴），與 P1 的差異一律在本計畫各 Task 的「與 P1 的調和」小節處理。

**Tech Stack:** Python 3.10+（Linux）、Windows Python 3.12（WSL interop，經 P1 T01 `scripts/stage0/win-pytest.sh`）、pytest、PyYAML、P0 `python3 -m tools.contracts`。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md` §5.2.1、§5.2.5、§5.3、§9.2、§9.3、§9.4、ADR-04、ADR-07；`docs/rust-refactor/owner-decisions-2026-10-15.md` OD-04、OD-06、OD-11、OD-14、OD-15；`docs/rust-refactor/plans/spec-reconciliation.md` §1.1（SC-04、SC-08、SC-34、SC-35）、§1.2（DF-02）、§2（RPC-D-02、RPC-D-05、ERR-7.2-b、ERR-7.3-1、SES-16.3a、SES-16.4-03、SES-16.5-02、SES-16.6-01）、§3；`docs/rust-refactor/plans/rust-architecture.md` §3.2、§4.6、§5.5、§5.12；`docs/command-completion.md`（#230 producer 完成條件）。

---

## Global Constraints

以下摘自 `00-master-plan.md` 與 `02-stage0-python-bc.md` Global Constraints，只列本 Phase 適用者。

- **執行時點**：P1（`02-stage0-python-bc.md` T01～T21）全部完成並合併到 `feature/rust-refactor` 後才開始；P5（`06`）開始前必須合併（P5 以本 Phase 結果為 A 側基線），P3 Task 8（`gen_error_codes`）開始前必須合併（`error_codes.yaml` 變動）。
- **先讀 P1 交付**：每個 Task 的 B1 前，實作者先以 `git log --oneline --grep='SC-04\|SC-08\|DF-02\|SC-11' feature/rust-refactor` 與 `git show --stat <sha>` 讀 P1 T08（SC-04 session drain、release 回應）、T11（DF-02 `trusted_human`）、T15（SC-08 socket 權限）、T20（permit）的實作 commit，定位實際函式名與行號；本計畫列的函式名取自 P1 計畫的 Produces，實作與之不同時以 P1 實際程式碼為準並在 commit 本文註明。
- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間（ADR-07、§9.3）；`CREDENTIALS_UNRESOLVED` 只屬後者。SC-34 起 `session.attach`／`session.recover` 不再把任何 `last_error` 值放進 `error_code`。
- 本 Phase 新增的錯誤碼**只有** `DRAIN_STALLED`；其餘回應只改既有碼的選用或新增欄位。不新增 WAL META 事件。
- 錯誤碼計數由 `contracts/error_codes.yaml` 資料產生，不得硬編碼（OD-06）。
- 契約同步（沿用 P1 Global Constraints 的 P0 C03／C04／C09／C16／C17 規則）：凡把某變更翻成 `IMPLEMENTED`，**同一 commit** 內：(a) 引用該變更的碼改 `status: IMPLEMENTED`、`reachable: true`、補 `contract_tests`；(b) `rpc_schema.json` 對應 `x-pending` 併入正式欄位；(c) `python3 -m tools.contracts check` 通過；(d) 以 `python3 -m tools.contracts skill-section --namespace error_code`／`--namespace session_last_error` 重新產生兩份 SKILL 受管區段。`retryable` 一律寫字串（`"no"`）；`contracts/` 內不得出現 `contract_revision` 鍵；修改 schema 檔時遞增 `x-catalog-revision`。
- 背壓、常數不改：`DRAIN_TIMEOUT_MS = 3000`（P1 T02）。新增常數 `sw_core.constants.PASSTHROUGH_READY_QUIET_S = 30.0`、`sw_core.constants.ERROR_DRAIN_STALLED = "DRAIN_STALLED"`，測試可 monkeypatch。
- `VERSION` 不 bump（維持 P1 的版號；`applies_to.python.min_version: "0.4.0"` 由 Stage 0 release PR 一次 bump）。
- pytest marker 依 P0 T14：每個新測試檔「恰一個平台 marker＋至少一個資源 marker」寫在檔內 `pytestmark`，不另註冊 marker。新測試模組不得在模組頂層 import 本 Phase 新增的名稱（`PASSTHROUGH_READY_QUIET_S`、`ERROR_DRAIN_STALLED`、新 helper），一律以函式內 `_load()` 延遲 import。
- 測試以 `python3 -m pytest -q tests/` 為準；不得引入新失敗；「已知 flaky」不得當作放行理由。**既有測試資產變更**只限各 Task「測試資產變更」小節逐檔列出者，須獨立審閱（規格 §10.4）；未列出的既有測試變紅＝實作缺陷。
- **OD-15 docker 優先驗證**：新版 serialwrap 一律先在 docker 驗證。P2（`03`）Task 17 docker 驗證環境（`scripts/docker/run-l15.sh` 假 COM PTY pair、`scripts/docker/run-systemd.sh` systemd container）已合併時，T7 的 B2 加跑 `run-l15.sh`（本 Phase 新測試＋全套）與 `run-systemd.sh`（`systemd-system` unit 帶 `Environment=SERIALWRAP_SOCKET_GROUP=dialout` 時 socket 為 `0660` 且群組為 `dialout`，T3 的整合證據），報告標 `evidence_layer: "L1.5-docker"`；尚未合併時記為「docker 驗證待補」gate，於 P2 docker 環境就緒後補跑，不阻擋本 Phase 合併。docker 結果不得計為真板 L2／實機 L3。
- **OD-15 原生 Windows COM**：擁有者 host 已通過 Spike 1 基本資格（com0com pair `CNCA0=COM20`（serialwrap 端）／`CNCB0=COM21`（bridge 端））。T4 的 Windows 原生驗證以 OD-11 隔離實例（三個隔離目錄＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`）在該 host 實跑，不再列 BLOCKED-OWNER；只有擁有者 host 不可用時才記「證據不足」。
- repo 政策：不得 commit 到 `main`；工作分支 `wt/rust-refactor/p1oc-owner-confirmations`（PR base `feature/rust-refactor`）；每個 code Task 附 `changelog.d/` direct-child fragment（frontmatter `type` 取 `feat`／`fix`／`change`／`security`）；commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；Phase 完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。對外契約變動（`DRAIN_STALLED`、attach／recover 新欄位、passthrough 可送命令）須同步 `README.md` 英文與繁中兩段與 `docs/**`（R-16、R-18）。
- 回歸 case 政策（#155）：T7 評估並於 PR 描述記錄結論（預設：SC-34／DRAIN_STALLED／socket 權限皆由 pytest 覆蓋；SC-35 的 30 s 安靜判定由 pytest 以時間注入覆蓋，真板 U-Boot／MCU 線上的安靜升 READY 新增 `regression/` case `pt-quiet-ready`，`issues` 掛 OD-14 對應 issue（無 issue 時記 `[]` 並於 PR 說明））。

## Review Focus

1. SC-34：attach／recover 所有分支（human lease、bridge 存在的其他狀態、同步 probe 之後、REPROBE 未恢復）都不得再把 `last_error` 放進 `error_code`；`last_error`／`last_error_detail` 與同一回應 `session` 內同名欄位一致；`BOOTLOADER` 分類判定改看 `last_error`；`last_error_passthrough` meta 項、抽取規則與 `@last_error_passthrough` schema 參照全部移除，`python3 -m tools.contracts check` 無殘留。
2. SC-35 狀態機：passthrough attach 零 TX（含有 ready_probe 的 uboot-template）；`quiet_since` 在進入 `ATTACHED` 與每次 RX 時更新、TX 不更新；只有 `ATTACHED`＋bridge 存在＋無 drain＋非 RELEASED＋非 flash 臨界時才升 `READY`；升 `READY` 不 TX；READY 後 RX 不降級、不 arm boot quiet、不跑 reconfirm。
3. SC-35 命令路徑：完成條件三層（`completion_regex` → 可用 `prompt_regex` → 無條件的 timeout）；passthrough 未命中時不送 `\x03`／`\x04`、不轉 `ATTACHED`、不 force recover；`file.*` 對 passthrough 一律 `PROFILE_NOT_COMMAND_CAPABLE` 且零 TX；flash 偵測候選（非 command_capable、READY／ATTACHED）不因 READY 而遺失。
4. DRAIN_STALLED：只取代 `device.release` 逾時（含 close 失敗、仍 stalled 的重複 release）的 `SESSION_RECOVERING`；drain 中的 attach／clear／bind／recover 維持 `SESSION_RECOVERING`；回條遲到仍完成 release 並持久化；`uart_tx: possible` 使 `no_auto_resend` 推導包含此碼。
5. SC-08 撤回：socket mode 與 P1 之前的基線逐位元組相同（bind 後 `chmod 0o660`、group 時 chgrp 失敗忽略），不殘留 umask 切換與目錄 0700。

---

## 0. 執行協定（TDD 兩段式）

1. **Stage A（測試先行）**：依序執行 T1～T7 的 A1、A2：寫測試、跑測試、確認失敗，每個 Task 一個 `test(...)` commit。A2 的預期原因以「相依 Task 已實作」為準；Stage A 當下因相依 Task 未實作而得到 `_load()` 拋出的 `ImportError`／`AttributeError` 亦屬有效紅燈，但 B2 前須確認列出的原因成立。collection error 一律先修測試。
2. **Stage A1.5（測試審核）**：每個 Task 的 A1 交付後，由 §0.1「Test reviewer」欄的模型（≠ 測試作者、≠ 實作者）逐條檢查錯誤 API／key、浮點邊界、無 sleep 等待、與規格自相矛盾或必然先綠的斷言；結論記入 `test(...)` commit 本文後才鎖定。回歸防護（既綠、不當紅燈證據）：T2 `test_attach_flashing_and_recovering_unchanged`、T4 `test_attach_during_drain_still_session_recovering`、T5 `test_non_passthrough_empty_probe_still_not_capable`。
3. **Stage B（實作）**：順序 T1 → T2 → T3 → T4 → T5 → T6 → T7。每個 B2 必跑全套 `python3 -m pytest -q tests/`；允許失敗只限尚未進入 Stage B 之 Task 的新測試檔（逐檔列於 B2 紀錄）。最後跑全套、Windows 原生 `scripts/stage0/win-pytest.sh -m windows_native tests/`、`python3 -m tools.contracts check`、`python3 -m policy_check --repo .`。
4. **測試作者與實作者**：A1.5 鎖定後測試作者不得改斷言；實作者認為測試有誤時回報審查者裁決，不得自行放寬。
5. **雙審**：Phase 結束後 sonnet max＋codex `gpt-6.1-sol` medium 雙審，重點依 Review Focus。

### 0.1 Task 總表

模型分派依 00-master-plan §2：daemon／session 狀態機與 drain 程式碼由 `codex-gpt-6-luna-max` 實作；同一 Task 的測試作者、測試審核者與實作者三者不同。

| Task | 標題 | 範圍 | Test author | Test reviewer（A1.5） | Implementer |
|---|---|---|---|---|---|
| T1 | 契約簿記：SC-34／SC-35 登記、SC-08 撤回、#5～#7 確認、pending 清空、owner_item 驗證放寬 | OD-14 全部（簿記） | haiku-xhigh | codex-gpt-6-luna-max | agy-gemini-3.8-flash-high |
| T2 | SC-34 attach／recover 回應分離 `last_error`，移除 passthrough meta 項 | SC-34 | agy-gemini-3.8-flash-high | haiku-xhigh | codex-gpt-6-luna-max |
| T3 | SC-08 撤回：socket 權限還原 0660 基線 | OD-14 #3 | haiku-xhigh | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| T4 | `DRAIN_STALLED`：release drain 逾時新錯誤碼 | SC-04（OD-14 #4） | agy-gemini-3.8-flash-high | haiku-xhigh | codex-gpt-6-luna-max |
| T5 | SC-35a passthrough 狀態機：attach 不登入／不 probe、30 s 安靜升 READY、gate、self-test／recover／attach | SC-35 | haiku-xhigh | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| T6 | SC-35b passthrough 命令路徑：完成條件、無救援按鍵、file.* 拒絕；SC-35 → IMPLEMENTED | SC-35 | agy-gemini-3.8-flash-high | haiku-xhigh | codex-gpt-6-luna-max |
| T7 | 文件、SKILL 受管區段、#155 回歸 case、docker／Windows 原生驗證收尾 | 全部 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | haiku-xhigh |

---

## Task T1：契約簿記：SC-34／SC-35 登記、SC-08 撤回、#5～#7 確認、pending 清空、owner_item 驗證放寬

- **範圍**：只改契約資料與 P0 驗證器的一條規則，不改 `sw_core/`。(1) `behavior_changes.yaml` 新增 SC-34、SC-35（`status: "APPROVED"`）；(2) 移除 SC-08 整筆（編號保留不重用，檔頭註解記錄撤回）；(3) SC-04、DF-02、SC-26、SC-27 改 `owner_confirmation: "confirmed"`、`default_if_pending: null`、`related_decisions` 加 `"OD-14"`（保留原 `owner_item` 供追溯）；(4) `pending_owner_confirmations: []`；(5) `tools/contracts/catalog.py` 的 `owner_item` 與 pending `item` 改為「null 或 ≥1 的整數」（移除 1..7 上限，供 P8 SC-33 以 item 8 登記）。依據：OD-14；調和表 §1.1、§3。
- **Test author model**：haiku-xhigh　**Implementer model**：agy-gemini-3.8-flash-high（契約資料與單一驗證規則，介面已定）
- **Changelog fragment**：`changelog.d/rust-p1oc-owner-confirmations-catalog.md`（`type: change`，`scope: contracts`）：依 OD-14 登記 SC-34、SC-35，撤回 SC-08，擁有者確認清單 #1～#7 全數裁決。
- **BLOCKED-OWNER**：無。

**與 P1 的調和**：P1 T15 已把 SC-08 改 `IMPLEMENTED`、T08 把 SC-04 改 `IMPLEMENTED`、T11 把 DF-02 改 `IMPLEMENTED`、T14 把 SC-27 改 `IMPLEMENTED`。本 Task 不動這些 `status`，只改 owner 簿記欄位；SC-08 整筆移除前確認 `error_codes.yaml`、`rpc_schema.json`、`cli_schema.json` 無任何 `behavior_change`／`change` 參照 SC-08（P1 若新增了參照，一併刪除並記入 commit 本文）。

**Files**
- Modify: `contracts/behavior_changes.yaml`、`tools/contracts/catalog.py`（`_OWNER_ITEM_RANGE` 改為下界檢查；錯誤訊息改「必須為 null 或 ≥1 的整數」）、`tests/test_contracts_catalog.py`（下列「測試資產變更」）
- Create: `tests/test_p1oc_catalog.py`

**Interfaces**
- Produces（資料）：
  ```yaml
  - id: "SC-34"
    kind: "SC"
    title: "session.attach／recover 的 error_code 與 last_error 分離"
    status: "APPROVED"
    approval: {owner: "hamanpaul", date: "2026-10-16", decision_ref: "OD-14", basis: "owner_decision"}
    owner_confirmation: "confirmed"
    owner_item: 1
    default_if_pending: null
    applies_to: {python: {min_version: "0.4.0"}, rust: {stage: "slice-2.1"}}
    approved_at_revision: <檔案 revision>
    spec_refs: ["§9.3", "ADR-07", "OD-14"]
    reconciliation_refs: ["RPC-D-02", "ERR-7.2-b", "ERR-7.3-1", "SES-16.5-02"]
    scope: "both"
    ab_participation: "diff"
    phases: ["P1", "P5", "P7"]
    case_ids: ["sc34-attach-recover-last-error-field"]
    compatibility_impact: "session.attach 失敗時 error_code 固定為 SESSION_NOT_READY（不再是 last_error 值或 NOT_READY），另以 last_error、last_error_detail 欄位回報；session.recover REPROBE 未恢復時 error_code 固定為 SESSION_NOT_READY 並附同兩欄。依 error_code 比對 last_error 值的 agent 須改讀 last_error 欄位。"
    approval_conditions: "OD-14 #1：分離；CLI／RPC method 與參數不變"
    related_decisions: ["OD-14"]
  - id: "SC-35"
    kind: "SC"
    title: "platform=passthrough 30 秒安靜自動 READY 並可送命令"
    # 欄位同 SC-34，差異如下
    owner_item: 2
    spec_refs: ["§5.3", "§5.2.1", "OD-14"]
    reconciliation_refs: ["SES-16.4-03", "SES-16.6-01"]
    phases: ["P1", "P5", "P7", "P8"]
    case_ids: ["sc35-passthrough-quiet-ready", "sc35-passthrough-command-completion"]
    compatibility_impact: "passthrough session 不再登入或送 ready_probe（含 uboot-template）；ATTACHED 連續 30 秒無 RX 自動轉 READY 並接受 command.submit；ATTACHED 時 submit 由 PROFILE_NOT_COMMAND_CAPABLE 改為 SESSION_NOT_READY＋passthrough_ready_in_s；file.push／file.pull 對 passthrough 回 PROFILE_NOT_COMMAND_CAPABLE；命令未命中完成條件時不送 CTRL_C／CTRL_D、session 維持 READY。"
    approval_conditions: "OD-14 #2：serialwrap 不介入 passthrough UART；完成條件只用既有機制"
  ```
  `approved_at_revision` 取遞增後的檔案 `revision`（本 Task 把頂層 `revision` +1）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_p1oc_catalog.py`（`pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]`；以 P0 `tools.contracts.catalog` 的 loader 讀真實契約）
  ```python
  from __future__ import annotations
  import pathlib
  import pytest

  pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]
  ROOT = pathlib.Path(__file__).resolve().parents[1]

  def _bc():
      from tools.contracts import catalog
      return catalog.load_behavior_changes(ROOT)

  def test_sc34_sc35_registered_with_owner_decision():
      changes, _, _ = _bc()
      by = {c.id: c for c in changes}
      for cid, item in (("SC-34", 1), ("SC-35", 2)):
          c = by[cid]
          assert c.scope == "both" and c.ab_participation == "diff"
          assert c.owner_confirmation == "confirmed" and c.owner_item == item
          assert c.approval.basis == "owner_decision" and c.approval.decision_ref == "OD-14"
          assert c.approval.owner == "hamanpaul" and c.approval.date == "2026-10-16"
      assert by["SC-34"].case_ids == ("sc34-attach-recover-last-error-field",)
      assert by["SC-35"].case_ids == ("sc35-passthrough-quiet-ready", "sc35-passthrough-command-completion")
      assert list(by["SC-35"].phases) == ["P1", "P5", "P7", "P8"]

  def test_sc08_withdrawn():
      changes, _, _ = _bc()
      assert "SC-08" not in {c.id for c in changes}
      assert "sc08-uds-perm-0600" not in {cid for c in changes for cid in c.case_ids}

  def test_no_pending_confirmations_remain():
      changes, confirmations, _ = _bc()
      assert confirmations == ()
      assert [c.id for c in changes if c.owner_confirmation == "pending_default"] == []
      confirmed = {c.id: c.owner_item for c in changes if c.owner_confirmation == "confirmed"}
      assert confirmed == {"SC-04": 4, "DF-02": 5, "SC-26": 6, "SC-27": 7, "SC-34": 1, "SC-35": 2}
      for cid in ("SC-04", "DF-02", "SC-26", "SC-27"):
          c = next(x for x in changes if x.id == cid)
          assert "OD-14" in c.related_decisions and c.default_if_pending is None
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_owner_item_above_seven_accepted` | 以 `catalog.validate_behavior_changes_doc`（P0 T1 的結構驗證入口；名稱以 P0 實際為準）驗一份最小合成文件：一筆 `owner_confirmation: pending_default`、`owner_item: 8`、`default_if_pending: "x"`＋`pending_owner_confirmations: [{item: 8, …}]` → 無問題 |
  | `test_owner_item_zero_rejected` | 同上但 `owner_item: 0` → 問題訊息含 `必須為 null 或 ≥1 的整數` |
  | `test_pending_item_duplicate_still_rejected` | 兩筆 pending `item: 8` → `item 重複: 8` |
  | `test_file_revision_bumped` | `behavior_changes.yaml` 頂層 `revision` ≥ SC-34／SC-35 的 `approved_at_revision` 且二者相等 |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_p1oc_catalog.py` → 預期 `KeyError: 'SC-34'`、`assert 'SC-08' not in …` 失敗、`owner_item` 8 被拒。commit：`test(contracts): OD-14 契約簿記失敗測試`
- [ ] **B1 實作**：依 Interfaces 寫入兩筆；刪 SC-08 整筆並在檔頭註解加一行「SC-08 依 OD-14 #3 撤回（UDS 維持 0660），編號保留不重用」；四筆確認項改欄位；`pending_owner_confirmations: []`；`safety_invariants` 不動（T4 處理）；`catalog.py` 放寬 `owner_item`／pending `item` 規則（保留重複檢查與 `pending_default` 時 `owner_item` 必填的規則，改為「必須為 ≥1 的整數」）。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_p1oc_catalog.py tests/test_contracts_catalog.py tests/test_contracts_validate.py`；`python3 -m tools.contracts check`；全套依 §0 第 3 點。
- [ ] **B3 commit**：`chore(contracts): 依 OD-14 登記 SC-34／SC-35、撤回 SC-08 並完成擁有者確認簿記`

**測試資產變更**（獨立審閱）

| 檔案 | 測試 | 改動 | 理由 |
|---|---|---|---|
| `tests/test_contracts_catalog.py` | `test_bc_real_sc_df_ranges` | SC 期望改 `{f"SC-{i:02d}" for i in range(1, 28)} - {"SC-08"} \| {"SC-34", "SC-35"}`；docstring 同步；DF 期望不變 | SC-08 撤回、SC-34／35 新增 |
| `tests/test_contracts_catalog.py` | `test_bc_real_pending_items` | 改為 `pending == {}` 且 `[o.item for o in confirmations] == []`；確認項對照移到 `test_p1oc_catalog.py` | OD-14 全數裁決 |
| `tests/test_contracts_catalog.py` | 任何以 1..7 上限斷言 `owner_item` 的合成案例（P0 T1 的 `owner_item: 8` 拒絕案例，若存在） | 改為斷言 `0` 被拒、`8` 接受 | 驗證規則放寬 |

---

## Task T2：SC-34 attach／recover 回應分離 `last_error`，移除 passthrough meta 項

- **範圍**：`session.attach` 在 command-capable 且 state ∉ {READY, ATTACHING, FLASHING, RECOVERING} 的所有分支回 `{"ok":false,"error_code":"SESSION_NOT_READY","last_error":<str|null>,"last_error_detail":<str|null>,"session":…}`；`session.recover` REPROBE 未恢復回 `{"ok":true,"recovering":false,"action":"REPROBE","recovered":false,"session":…,"error_code":"SESSION_NOT_READY","last_error":…,"last_error_detail":…}`（之後照舊可附 `classification`、`recommended_action`、`matched_prompt`）。契約同步移除 `last_error_passthrough`。依據：OD-14 #1；調和表 SC-34、RPC-D-02。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max（session 狀態機回應與 probe 互斥路徑）
- **Changelog fragment**：`changelog.d/rust-p1oc-sc34-attach-last-error.md`（`type: change`，`scope: session`）：`session attach`／`session recover` 失敗時 `error_code` 固定為 `SESSION_NOT_READY`，原本的 last_error 改由 `last_error`、`last_error_detail` 欄位回報。
- **BLOCKED-OWNER**：無。

**與 P1 的調和**：P1 T08 可能已在 `attach_session` 加入 drain 分支（`SESSION_RECOVERING`＋drain 欄位）、T11 在 `interactive_open` 加 RECOVERING／FLASHING 分支；這些分支不變。先以 `git grep -n "_attach_state_result\|last_error\") or" sw_core/` 找出所有把 `last_error` 當 `error_code` 的位置（基線為 `_attach_state_result` 與 `recover_session` REPROBE 兩處），P1 若新增第三處一併改。

**Files**
- Create: `tests/test_p1oc_sc34_last_error.py`
- Modify: `sw_core/session_manager.py`（`_attach_state_result`、`recover_session` REPROBE 分支）、`contracts/error_codes.yaml`、`contracts/code_extraction.yaml`、`contracts/session_last_errors.yaml`、`contracts/rpc_schema.json`、`contracts/behavior_changes.yaml`（SC-34 → `IMPLEMENTED`）、兩份 SKILL 受管區段（重新產生）、`tests/` 下列「測試資產變更」

**Interfaces**
- Produces（Python）：
  ```python
  def _attach_state_result(self, session: SessionRuntime, snap: dict[str, Any]) -> dict[str, Any]:
      # command_capable 且 state ∉ {READY, ATTACHING}：
      #   FLASHING → {"ok":False,"error_code":"FLASHING_BUSY","session":snap}
      #   RECOVERING → {"ok":False,"error_code":"SESSION_RECOVERING","session":snap}（P1 drain 欄位照舊）
      #   其餘 → {"ok":False,"error_code":"SESSION_NOT_READY",
      #           "last_error":snap.get("last_error"),"last_error_detail":snap.get("last_error_detail"),
      #           "session":snap}
      # 其餘情況 → {"ok":True,"session":snap}（不變）
  ```
  recover REPROBE 未恢復時：`payload["error_code"] = "SESSION_NOT_READY"`、`payload["last_error"] = result["session"].get("last_error")`、`payload["last_error_detail"] = result["session"].get("last_error_detail")`，依序在 `session` 之後加入；BOOTLOADER 判定 `matched is not None or payload["last_error"] == ERROR_BOOTLOADER_STUCK`。
- Produces（契約）：
  - `error_codes.yaml`：保留頂層 `passthrough: []`（loader 會無條件讀取此鍵，meta entries 全數移除）；`NOT_READY` 改 `reachable: false`、`unreachable_reason: "SC-34（OD-14 #1）起 session.attach 改回 SESSION_NOT_READY"`、`behavior_change: ["SC-34"]`（status 仍 `EXISTING`）、`contract_tests` 改為本 Task 的 unreachable 證明測試；`SESSION_NOT_READY` 的 `meaning` 補「session.attach／session.recover 失敗時附 last_error、last_error_detail」、`extra_fields` 加 `"last_error"`、`"last_error_detail"`、`behavior_change` 加 `"SC-34"`、`contract_tests` 加本 Task node。
  - `code_extraction.yaml`：刪除 `namespaces.error_code.passthrough` 的三條 `emits: last_error_passthrough` 規則（改為 `passthrough: []`）。
  - `session_last_errors.yaml`：檔頭說明 `promoted_via` 改為「該值會出現在所列方法回應頂層 `last_error` 欄位（SC-34 起不再進 `error_code`）」；各值的 `promoted_via` 資料不變。
  - `rpc_schema.json`：`session.attach` 與 `session.recover` 的 `errors` 刪 `"@last_error_passthrough"`、`"NOT_READY"`；attach 新增失敗分支 schema `x-order: ["ok","error_code","last_error","last_error_detail","session"]`；recover REPROBE 分支 `x-order` 在 `session` 後加 `"error_code","last_error","last_error_detail"`，`last_error`／`last_error_detail` 型別 `["string","null"]`；`x-catalog-revision` +1。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_p1oc_sc34_last_error.py`（`pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]`；以 `stage0_fakes.make_manager`／`make_profile` 建 manager，直接設 session 欄位與注入 bridge 替身，不開真實 UART）
  ```python
  from __future__ import annotations
  import pytest
  from tests.stage0_fakes import make_manager, make_profile

  pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

  @pytest.mark.parametrize("last_error,detail", [
      ("CREDENTIALS_UNRESOLVED", None),
      ("READY_NONCE_TIMEOUT", "tail…"),
      (None, None),
  ])
  def test_attach_state_result_separates_last_error(tmp_path, last_error, detail):
      mgr = make_manager(tmp_path, [make_profile(ready_probe="echo __READY__${nonce}")])
      s = mgr.get_session("COM0")
      s.state = "ATTACHED"; s.last_error = last_error; s.last_error_detail = detail
      r = mgr._attach_state_result(s, s.to_public_dict())
      assert list(r.keys()) == ["ok", "error_code", "last_error", "last_error_detail", "session"]
      assert r["ok"] is False and r["error_code"] == "SESSION_NOT_READY"
      assert r["last_error"] == last_error and r["last_error_detail"] == detail
      assert r["session"]["last_error"] == last_error

  def test_attach_flashing_and_recovering_unchanged(tmp_path):
      mgr = make_manager(tmp_path, [make_profile(ready_probe="echo x")])
      s = mgr.get_session("COM0")
      for state, code in (("FLASHING", "FLASHING_BUSY"), ("RECOVERING", "SESSION_RECOVERING")):
          s.state = state; s.last_error = "PROMPT_UNAVAILABLE"
          r = mgr._attach_state_result(s, s.to_public_dict())
          assert r["error_code"] == code and "last_error" not in r
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_attach_after_probe_failure_shape` | ATTACHED＋bridge 替身，`_probe_existing_bridge` monkeypatch 回 `{"ok":True,"session":{…,"state":"ATTACHED","last_error":"LOGIN_PROMPT_TIMEOUT","last_error_detail":"x"}}` → `attach_session("COM0")` 回 SESSION_NOT_READY、`last_error=="LOGIN_PROMPT_TIMEOUT"`、`last_error_detail=="x"` |
  | `test_attach_human_lease_branch_shape` | 有 `human:` lease 的 ATTACHED（不 probe）→ 同形，`last_error` 取自 session |
  | `test_attach_never_puts_last_error_in_error_code` | 對 `session_last_errors.yaml` 每個具名值（動態前綴以 `REPROBE_FAILED:x` 代表）逐一設成 `last_error` → `error_code` 恆為 `SESSION_NOT_READY` |
  | `test_recover_reprobe_not_recovered_shape` | `_probe_existing_bridge` 回 ATTACHED＋`last_error="PROMPT_UNAVAILABLE"` → 回應鍵序前段為 `["ok","recovering","action","recovered","session","error_code","last_error","last_error_detail"]`、`ok is True`、`recovered is False`、`error_code=="SESSION_NOT_READY"` |
  | `test_recover_bootloader_stuck_by_last_error` | `last_error="BOOTLOADER_STUCK"`、無 RX 證據 → `classification=="BOOTLOADER"`、`recommended_action=="recover_interactive"`、`error_code=="SESSION_NOT_READY"` |
  | `test_not_ready_code_unreachable_in_source` | `python3 -m tools.contracts extract --namespace error_code` 的輸出不含 `NOT_READY`；`error_codes.yaml` 的 `NOT_READY.reachable is False` |
  | `test_catalog_has_no_passthrough_section` | 以 P0 loader 讀 `error_codes.yaml` → `passthrough == ()`；`code_extraction.yaml` 的 `namespaces.error_code.passthrough == []`；`rpc_schema.json` 全文不含 `@last_error_passthrough` |
  | `test_rpc_attach_response_matches_schema` | 經 `SerialwrapService.rpc("session.attach", …)` 取得失敗回應，以 `rpc_schema.json` 的 attach 失敗分支（jsonschema）驗證且鍵序符合 `x-order` |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_p1oc_sc34_last_error.py` → 預期 `'CREDENTIALS_UNRESOLVED' != 'SESSION_NOT_READY'`、`KeyError: 'last_error'`。commit：`test(session): SC-34 attach／recover last_error 分離失敗測試`
- [ ] **B1 實作**：改兩處回應；契約依 Interfaces；SC-34 → `IMPLEMENTED`；重新產生 SKILL 受管區段（`passthrough` 說明段落消失、`SESSION_NOT_READY` 說明更新）。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_p1oc_sc34_last_error.py tests/test_attach_error_surface.py tests/test_contracts_gap_codes.py tests/test_contracts_extract.py tests/test_contracts_catalog.py tests/test_contracts_validate.py tests/test_contracts_rpc_schema.py tests/test_contracts_skill_section.py tests/test_bootloader_recovery.py tests/test_profile_reresolve_on_attach.py tests/test_login_recovery_lease.py tests/test_boot_quiet.py`；`python3 -m tools.contracts check`；全套。
- [ ] **B3 commit**：`feat(session): SC-34 attach／recover 失敗改回 SESSION_NOT_READY 並以 last_error 欄位回報`

**測試資產變更**（獨立審閱；B1 前以 `grep -rn "error_code.*==.*\(CREDENTIALS_UNRESOLVED\|PROMPT_UNAVAILABLE\|BOOTLOADER_STUCK\|_PROMPT_TIMEOUT\|LOGIN_\|READY_NONCE\|NOT_READY\)" tests/` 確認，逐檔逐測試列入 commit 本文；下表為已知候選，未列出者變紅＝實作缺陷）

| 檔案 | 測試 | 改動 |
|---|---|---|
| `tests/test_attach_error_surface.py` | 斷言 attach `error_code == <last_error>` 者 | 改斷言 `error_code == "SESSION_NOT_READY"` 且 `last_error == <原值>` |
| `tests/test_contracts_gap_codes.py` | `TestGapMemory::test_not_ready_attach_state_result` | 改斷言 `SESSION_NOT_READY`＋`last_error is None`；`NOT_READY` 的 contract_tests 改指 `test_not_ready_code_unreachable_in_source` |
| `tests/test_contracts_extract.py` | 真實抽取斷言含 `last_error_passthrough` 者（現行約 687 行）、真實碼清單含 `NOT_READY` 者（約 657 行） | 改斷言不含；合成設定案例（inline YAML 的 `emits: last_error_passthrough`）屬抽取器能力測試，**保留** |
| `tests/test_contracts_catalog.py` | `test_ec_passthrough_single_meta_entry` | 改名 `test_ec_no_passthrough_meta_entry`，斷言 `passthrough == ()` |
| `tests/test_contracts_catalog.py` | `test_ec_unreachable_set` | 不可達既有錯誤碼集合加入 `NOT_READY` | SC-34 將舊 attach 錯誤碼標記為不可達 |
| `tests/test_bootloader_recovery.py`、`tests/test_login_recovery_lease.py`、`tests/test_profile_reresolve_on_attach.py`、`tests/test_boot_quiet.py` | 斷言 recover／attach `error_code` 為 last_error 值者 | 改讀 `last_error` 欄位 |
| `tests/test_resilience_hotplug.py` | `TestDeviceHotplug::test_recover_session_attached_failed_reprobe_stays_attached` | 斷言 REPROBE 回 `SESSION_NOT_READY`，並分別檢查頂層及 session 內的 `last_error`／`last_error_detail` | 核對 recover REPROBE 契約欄位 |

---

## Task T3：SC-08 撤回：socket 權限還原 0660 基線

- **範圍**：還原 P1 T15 對 `sw_core/rpc_posix.py`（bind 前 `umask(0o177)`、無 group 時 `chmod 0o600`）與 `sw_core/constants.py`（`ensure_runtime_dirs` 新建目錄 `0o700`）的 SC-08 變更，回到 P1 前基線：bind 後 `os.chmod(path, 0o660)`（OSError 忽略），`SERIALWRAP_SOCKET_GROUP` strip 後非空 → `shutil.chown(path, group=…)`（`OSError`／`LookupError` 忽略）；`ensure_runtime_dirs` 回到 `os.makedirs(d, exist_ok=True)`。SC-09／SC-10／SC-12／SC-20／DF-14 不動。依據：OD-14 #3；調和表 RPC-D-05。
- **Test author model**：haiku-xhigh　**Implementer model**：codex-gpt-6-luna-max（daemon RPC listener 與執行期目錄）
- **Changelog fragment**：`changelog.d/rust-p1oc-socket-perm-0660.md`（`type: change`，`scope: daemon`）：依擁有者裁決撤回 socket 0600 變更，維持 0660 並可用 `SERIALWRAP_SOCKET_GROUP` 指定群組。
- **BLOCKED-OWNER**：無。

**與 P1 的調和**：以 `git log -p --follow sw_core/rpc_posix.py` 找到 P1 T15 的 commit，`git show <P1 前 sha>:sw_core/rpc_posix.py` 取得 `_secure_socket` 基線逐字還原（函式名若被 P1 改為 `_restrict_socket_permissions`，保留新名、還原內容）。`changelog.d/rust-p1-platform-mgmt.md` 中「socket 權限預設 0600」一句同 commit 刪除（該 fragment 尚未 collate；若已 collate 則在本 Task fragment 註明撤回）。

**Files**
- Create: `tests/test_p1oc_socket_perm.py`
- Modify: `sw_core/rpc_posix.py`、`sw_core/constants.py`、`changelog.d/rust-p1-platform-mgmt.md`、`tests/test_stage0_platform_mgmt.py`、`tests/test_codex_fixes.py`（下列「測試資產變更」）、`README.md`、`sw_core/assets/skill/SKILL.md`、`sw_core/assets/skill/SKILL_WINDOWS.md`（P1 改寫的 socket 權限說明還原為 0660，並移除已撤回的 SC-08 標記）

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_p1oc_socket_perm.py`（`pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]`）
  ```python
  from __future__ import annotations
  import asyncio, grp, os, stat
  import pytest
  from sw_core.rpc_posix import JsonRpcUnixServer

  pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

  def _mode_after_start(path: str) -> int:
      async def run() -> int:
          srv = JsonRpcUnixServer(path, lambda m, p: {"ok": True})
          await srv.start()
          try:
              return stat.S_IMODE(os.stat(path).st_mode)
          finally:
              await srv.stop()
      return asyncio.run(run())

  def test_socket_mode_0660_without_group(tmp_path, monkeypatch):
      monkeypatch.delenv("SERIALWRAP_SOCKET_GROUP", raising=False)
      assert _mode_after_start(str(tmp_path / "s.sock")) == 0o660

  def test_socket_group_chgrp(tmp_path, monkeypatch):
      g = grp.getgrgid(os.getgid()).gr_name
      monkeypatch.setenv("SERIALWRAP_SOCKET_GROUP", f"  {g}  ")
      p = str(tmp_path / "s.sock")
      async def run():
          srv = JsonRpcUnixServer(p, lambda m, q: {"ok": True}); await srv.start()
          try:
              st = os.stat(p); return stat.S_IMODE(st.st_mode), st.st_gid
          finally:
              await srv.stop()
      mode, gid = asyncio.run(run())
      assert mode == 0o660 and gid == os.getgid()

  def test_socket_group_chgrp_to_supplementary_group(tmp_path, monkeypatch):
      selected = None
      for candidate_gid in os.getgroups():
          if candidate_gid == os.getegid():
              continue
          try:
              candidate_name = grp.getgrgid(candidate_gid).gr_name
          except KeyError:
              continue
          selected = (candidate_gid, candidate_name)
          break
      if selected is None:
          pytest.skip("目前程序沒有可辨識的附加群組")
      expected_gid, g = selected
      monkeypatch.setenv("SERIALWRAP_SOCKET_GROUP", f"  {g}  ")
      p = str(tmp_path / "supplementary.sock")
      async def run():
          srv = JsonRpcUnixServer(p, lambda m, q: {"ok": True}); await srv.start()
          try:
              st = os.stat(p); return stat.S_IMODE(st.st_mode), st.st_gid
          finally:
              await srv.stop()
      mode, gid = asyncio.run(run())
      assert mode == 0o660 and gid == expected_gid

  def test_socket_group_unknown_ignored(tmp_path, monkeypatch):
      monkeypatch.setenv("SERIALWRAP_SOCKET_GROUP", "no-such-group-p1oc")
      assert _mode_after_start(str(tmp_path / "s.sock")) == 0o660
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_runtime_dirs_not_forced_0700` | `os.umask(0o022)` 下 monkeypatch `CONFIG_DIR` 等指向 tmp 不存在路徑 → `ensure_runtime_dirs()` 後目錄 mode 為 `0o755`（umask 決定，非 0o700） |
  | `test_no_umask_switch_in_start` | monkeypatch `os.umask` 記錄呼叫 → `JsonRpcUnixServer.start()` 期間呼叫次數為 0 |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_p1oc_socket_perm.py` → 預期 `0o600 != 0o660`、目錄 `0o700 != 0o755`、umask 呼叫次數 2。commit：`test(daemon): SC-08 撤回後 socket 0660 基線失敗測試`
- [ ] **B1 實作**：依「與 P1 的調和」逐字還原。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_p1oc_socket_perm.py tests/test_stage0_platform_mgmt.py tests/test_codex_fixes.py tests/test_stage0_defect_fixes.py`；全套。
- [ ] **B3 commit**：`revert(daemon): 依 OD-14 撤回 SC-08，socket 維持 0660 基線`

**測試資產變更**（獨立審閱）

| 檔案 | 測試 | 改動 |
|---|---|---|
| `tests/test_stage0_platform_mgmt.py` | socket `0o600` 斷言；`test_socket_group_mode_0660`；`test_new_runtime_dirs_0700` | socket 預期改為 `0o660`；group 時的 `0o660` 斷言不改；目錄測試改名 `test_existing_runtime_dir_permissions_unchanged`，只保留既有 `CONFIG_DIR` 權限不變的斷言，新建目錄 umask 由 `test_p1oc_socket_perm.py::test_runtime_dirs_not_forced_0700` 覆蓋；docstring 移除 SC-08 |
| `tests/test_codex_fixes.py` | `test_rpc_socket_default_mode_is_0600` | 改名 `test_rpc_socket_default_mode_is_0660`，預期由 `0o600` 改為 `0o660` |
| `tests/test_p1oc_socket_perm.py` | `test_socket_group_chgrp_to_supplementary_group` | 新增真實群組變更斷言：選取非 effective GID 的可辨識附加群組，確認 socket `st_gid` 改為該 GID；沒有符合群組時 skip |

---

## Task T4：`DRAIN_STALLED`：release drain 逾時新錯誤碼

- **範圍**：`device.release` 的 drain 自停止請求起算（含 Phase 1 等鎖，R3-F3）超過 `DRAIN_TIMEOUT_MS` 仍無成功回條，或收到 `handle_closed is False` 失敗回條（此分支可立即回應）→ `{"ok":false,"error_code":"DRAIN_STALLED","drain_stalled":true,"drain_timeout_epoch":<bridge_generation>,"session":…}`；仍 stalled 時再次 release → 同形；drain 進行中未逾時的重複 release 共用首次期限；回條遲到 → daemon 自行完成 release（`RELEASED`＋持久化），之後 release 回 `already_released`。drain 中的 `device.attach`／`session.attach`／`session.clear`／`session.bind`／`session.recover` **維持** `SESSION_RECOVERING`＋drain 欄位。依據：OD-14 #4；調和表 SC-04、SES-16.3a；arch §5.5。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max（drain barrier）
- **Changelog fragment**：`changelog.d/rust-p1oc-drain-stalled.md`（`type: change`，`scope: session`）：`device release` drain 超過 3 秒或 handle 關閉回條失敗時回新錯誤碼 `DRAIN_STALLED`（附 `drain_stalled`、`drain_timeout_epoch`）；release 仍可能在背景完成，請在有限等待內以 `session get_state` 確認 `RELEASED` 後再交接；若仍為 `RECOVERING`，停止重試並交由操作員檢查，勿重開裝置。release 尚未完成時，`session attach`／`session clear` 維持回 `SESSION_RECOVERING`。
- **BLOCKED-OWNER**：無（OD-15：Windows 原生 pyserial write 的 RTS/CTS 背壓情境在擁有者 host 的 COM20／COM21 實跑，見 B2）。

**與 P1 的調和**：P1 T08 的「回應形狀（凍結）」表中 release 逾時、drain 中再次 release、close 失敗三列回 `SESSION_RECOVERING`；本 Task 只改這三列為 `DRAIN_STALLED`，「drain 中 `device.attach`」列不改。P1 T08／T20 的既有測試（`tests/test_stage0_session_drain.py::test_release_drain_timeout_shape`、`test_failed_close_receipt_keeps_recovering`、`test_release_drain_deadline_includes_lock_wait`、T20 `test_windows_guarded_write_stall_reports_drain_stalled_on_time`）列入測試資產變更。

**Files**
- Create: `tests/test_p1oc_drain_stalled.py`
- Modify: `sw_core/constants.py`（`ERROR_DRAIN_STALLED`）、`sw_core/session_manager.py`（`release_device` 逾時／去重／close 失敗分支、`session.attach` drain guard）、`scripts/stage0/drain_timing.py`（新逾時碼量測）、`contracts/error_codes.yaml`（新碼；`SESSION_RECOVERING` 的 `scope.methods` 移除 `device.release`（若 P1 已加）、`behavior_change` 保留 SC-04）、`contracts/behavior_changes.yaml`（SC-04 `compatibility_impact`／`approval_conditions` 改寫為 DRAIN_STALLED；`safety_invariants` 的 `no_auto_resend`、`no_reopen_before_handoff` 加 `DRAIN_STALLED`）、`contracts/rpc_schema.json`（`device.release.errors` 加 `DRAIN_STALLED`、`error_order` 依實作序、失敗分支 `x-order: ["ok","error_code","drain_stalled","drain_timeout_epoch","session"]`；`x-catalog-revision` +1）、`contracts/daemon_logging.yaml`、`contracts/cli_schema.json`（修正行號來源）、兩份 SKILL 受管區段、`tests/test_contracts_keep_baseline.py`（同一行號來源同步）、`tests/` 下列「測試資產變更」、`contracts/captures/stage0-python-bc.json`

**Interfaces**
- Produces（契約，`error_codes.yaml` 依字母序插入）：
  ```yaml
  - code: DRAIN_STALLED
    status: IMPLEMENTED
    reachable: true
    constant: ERROR_DRAIN_STALLED
    scope: {interfaces: [rpc], methods: ["device.release"], platforms: [posix, windows]}
    meaning: "device.release 的 drain 超過 3000 ms 仍無成功 handle 關閉回條，或收到 handle_closed=false 失敗回條（後者可立即回應）"
    side_effect: "release 已開始但未完成：session 為 RECOVERING、by_id 已列入 released 集合（阻擋自動 attach）、在途前景命令已中止（結果見 command.get）；尚無成功關閉確認時，成功回條晚到可由 daemon 完成 release（RELEASED 並持久化），失敗回條則維持 RECOVERING 並需操作員處置"
    retryable: "no"
    uart_tx: possible
    agent_action: "不要重開或讓外部工具開啟該裝置；以有界等待輪詢 session get_state，只有 state 為 RELEASED 才交接；等待期限內仍為 RECOVERING 時停止重試並交由操作員檢查 daemon／handle；再次 device release 不會加速完成"
    extra_fields: ["drain_stalled", "drain_timeout_epoch", "session"]
    contract_tests: ["tests/test_p1oc_drain_stalled.py::test_release_timeout_returns_drain_stalled", "tests/test_p1oc_drain_stalled.py::test_late_receipt_completes_release", "tests/test_p1oc_drain_stalled.py::test_close_failure_returns_drain_stalled"]
    behavior_change: ["SC-04"]
    sources: ["sw_core/session_manager.py:<release_device 逾時分支行號>"]
  ```

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_p1oc_drain_stalled.py`（`pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]`；沿用 P1 T08 測試檔的 `short_drain` fixture 與 `_ready_with_port` helper：若其定義在 `tests/test_stage0_session_drain.py` 內，本檔以相同內容複製一份局部 helper，不 import 該測試檔）
  ```python
  from __future__ import annotations
  import threading, time
  import pytest
  from tests.stage0_fakes import FakePort

  pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

  def test_release_timeout_returns_drain_stalled(tmp_path, short_drain):
      gate = threading.Event(); port = FakePort(block=gate)
      mgr, s, b = _ready_with_port(tmp_path, port)
      threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
      time.sleep(0.05)
      r = mgr.release_device("COM0")
      assert list(r.keys()) == ["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"]
      assert (r["ok"], r["error_code"], r["drain_stalled"], r["drain_timeout_epoch"]) == (False, "DRAIN_STALLED", True, 4)
      assert r["session"]["state"] == "RECOVERING" and r["session"]["drain_stalled"] is True
      r2 = mgr.release_device("COM0")
      assert r2["error_code"] == "DRAIN_STALLED" and port.close_calls == 0
      gate.set()

  def test_attach_during_drain_still_session_recovering(tmp_path, short_drain):
      gate = threading.Event(); port = FakePort(block=gate)
      mgr, s, b = _ready_with_port(tmp_path, port)
      threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
      time.sleep(0.05)
      assert mgr.release_device("COM0")["error_code"] == "DRAIN_STALLED"
      a = mgr.attach_device("COM0")
      assert a["ok"] is False and a["error_code"] == "SESSION_RECOVERING" and a["drain_stalled"] is True
      gate.set()

  def test_late_receipt_completes_release(tmp_path, short_drain):
      gate = threading.Event(); port = FakePort(block=gate)
      mgr, s, b = _ready_with_port(tmp_path, port)
      threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
      time.sleep(0.05)
      assert mgr.release_device("COM0")["error_code"] == "DRAIN_STALLED"
      gate.set()
      for _ in range(100):
          if mgr.get_session("COM0").state == "RELEASED":
              break
          time.sleep(0.02)
      assert mgr.get_session("COM0").state == "RELEASED"
      assert mgr.release_device("COM0")["already_released"] is True and port.close_calls == 1
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_close_failure_returns_drain_stalled` | `FakePort.close` 擲 `OSError` → release 回 `DRAIN_STALLED`、state `RECOVERING`、`attach_device` 不開新 handle |
| `test_duplicate_release_before_deadline_shares_deadline` | 第一個 release 於 thread 中等待；50 ms 後第二個 release → 兩者都在第一個請求起算的期限內回 `DRAIN_STALLED`（第二個的耗時 < `short_drain` 期限＋100 ms），close 只發一次 |
| `test_duplicate_release_during_attach_op_shares_first_deadline`、`test_stalled_attach_op_duplicate_keeps_diagnostics_and_expired_deadline` | attach op 尚未完成時，並行及逾時後重複 release 沿用首次期限，且不清除 `drain_stalled`／`drain_timeout_epoch` |
  | `test_clear_bind_recover_during_drain_session_recovering` | stalled 中 `clear_session`／`bind_session`／`recover_session(force=False)` → 各回 `SESSION_RECOVERING`＋drain 欄位 |
| `test_session_attach_during_stalled_release_returns_session_recovering`、`test_session_attach_probe_racing_stalled_release_returns_session_recovering` | stalled 中及既有同步 probe 遇到 stalled release 時，`session.attach` 回 `SESSION_RECOVERING`＋drain 欄位，不誤回 `released:true` |
| `test_drain_timing_counts_drain_stalled_timeout`、`test_drain_timing_does_not_count_immediate_close_failure_as_timeout` | 3,000 ms 後的 `DRAIN_STALLED` 計入逾時；早回的 close 失敗與其他錯誤不冒稱 timeout |
  | `test_drain_stalled_catalog_entry` | P0 loader 讀 `DRAIN_STALLED`：`retryable == "no"`、`uart_tx == "possible"`、`scope.methods == ("device.release",)`、`behavior_change == ("SC-04",)`；`no_auto_resend` 與 `no_reopen_before_handoff` 的 `error_codes` 含 `DRAIN_STALLED` |
  | `test_cli_device_release_drain_stalled_exit` | 以 RPC stub 回 `DRAIN_STALLED` 形狀，`serialwrap device release --selector COM0` 的 stdout JSON `error_code == "DRAIN_STALLED"`、exit code 與其他 `ok:false` RPC 回應相同（`_run_rpc` 既有規則，不新增 exit code） |
| `test_windows_guarded_write_stall_drain_stalled_native`（`windows_native`＋`windows_com`） | COM20 使用 RTS/CTS、COM21 將 RTS 拉低，實際阻塞 COM20 原生 pyserial `write` 至少 250 ms 後才開始 release；只在確認 OS write 已阻塞後停用 `cancel_pending_write`，以驗證 3,000 ms deadline；release 在 3,000 ms ± 500 ms 回 `DRAIN_STALLED`，回條前 COM20 不可被第二次開啟（`ERROR_ACCESS_DENIED`），恢復 CTS 後 `RELEASED` 且外部可開 COM20 |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_p1oc_drain_stalled.py` → 預期 `'SESSION_RECOVERING' != 'DRAIN_STALLED'`、目錄缺 `DRAIN_STALLED`。commit：`test(session): DRAIN_STALLED release drain 逾時失敗測試`
- [ ] **B1 實作**：release 逾時、去重、close 失敗三分支改用 `ERROR_DRAIN_STALLED`；其他 drain 中拒絕維持 `SESSION_RECOVERING`；契約依 Interfaces；SKILL 重新產生。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_p1oc_drain_stalled.py tests/test_stage0_session_drain.py tests/test_stage0_tx_permit.py tests/test_device_handoff.py tests/test_contracts_catalog.py tests/test_contracts_rpc_schema.py tests/test_contracts_skill_section.py`；`python3 -m tools.contracts check`；Windows 原生 `scripts/stage0/win-pytest.sh -m windows_native tests/test_p1oc_drain_stalled.py`（擁有者 host，COM20／COM21）；全套。
- [ ] **B3 commit**：`feat(session): release drain 逾時改回 DRAIN_STALLED（OD-14 #4）`

**測試資產變更**（獨立審閱）

| 檔案 | 測試 | 改動 |
|---|---|---|
| `tests/test_stage0_session_drain.py` | `test_release_drain_timeout_shape` | release 與重複 release 的 `error_code` 改 `DRAIN_STALLED`；其中 `attach_device` 的 `SESSION_RECOVERING` 斷言不改 |
| `tests/test_stage0_session_drain.py` | `test_failed_close_receipt_keeps_recovering`、`test_release_drain_deadline_includes_lock_wait` | release 回應碼改 `DRAIN_STALLED` |
| `tests/test_stage0_session_drain.py` | `test_reject_paths_have_zero_side_effects` | 第二次 release 回應碼改 `DRAIN_STALLED`，零 TX／持久化副作用斷言保留 |
| `tests/test_stage0_uart_exclusive.py` | `TestAttachProbeDrain` release 斷言 | release 回應碼改 `DRAIN_STALLED`，`SESSION_RECOVERING` state 與不 close 斷言保留 |
| `tests/test_stage0_captures.py`、`contracts/captures/stage0-python-bc.json` | `release_drain_timeout_shape` | 凍結回應碼由 `SESSION_RECOVERING` 改為 `DRAIN_STALLED` |
| `tests/test_contracts_keep_baseline.py` | `test_last_error_logging_templates_match` | `CREDENTIALS_UNRESOLVED` 與 `TRANSPORT_STALL` 對應到 logger 呼叫目前行號，維持模板逐字比對 |
| `tests/test_p1oc_drain_stalled.py` | CLI、Windows 原生測試及本 Task 新增測試 | CLI mock 改用現存 `_resolve_endpoint_info` API，未刪除回應斷言；Windows 案例以 COM20／COM21 RTS/CTS 實際阻塞 pyserial write，不以人工 Event 暫停替代 OS 寫入；新增 attach-op 共用期限與診斷、`session.attach` race、`drain_timing.py` 逾時分類測試 |
| `tests/test_stage0_session_drain.py`、`tests/test_stage0_uart_exclusive.py`、`tests/test_p1oc_drain_stalled.py` | `clear_session` 與 attach/release guard | release stalled 時先檢查 pending drain，再判斷 RELEASED，避免 `session.clear`／`session.attach` 回 `ok:true, released:true`；拒絕回應仍帶 drain 欄位 |
| `tests/test_stage0_tx_permit.py` | `test_windows_guarded_write_stall_reports_drain_stalled_on_time` | release 回應碼改 `DRAIN_STALLED`，原時限斷言不變 |

---

## Task T5：SC-35a passthrough 狀態機：attach 不登入／不 probe、30 s 安靜升 READY、gate、self-test／recover／attach

- **範圍**：`platform == "passthrough"` 的 session：attach（初次、熱插、手動、recover 後重 attach）不跑 Login FSM、不送 ready_probe（含 uboot-template）、不做 boot quiet gate，bridge 起來即 `ATTACHED`、`last_error:null`、零 TX；`SessionRuntime.passthrough_quiet_since`（mono）在進入 `ATTACHED` 與每次 RX（`_mark_session_rx`）時更新；`reconcile_readiness`（DeviceWatcher `on_tick`）呼叫新 helper，條件成立（ATTACHED、bridge 存在、無 drain／pending stop、非 RELEASED、非 flash 臨界、`now - quiet_since >= PASSTHROUGH_READY_QUIET_S`）即轉 `READY`（零 TX）；passthrough 不 arm boot-banner quiet、不跑 READY reconfirm、不進 auto reprobe；submit-time gate：ATTACHED 的 passthrough 回 `SESSION_NOT_READY`＋`passthrough_ready_in_s`；self-test 永不 TX（READY → `PASSTHROUGH`＋`recommended_action:"none"`；ATTACHED 既有分支加 `passthrough_ready_in_s`）；recover 的 REPROBE 不 probe（依 SC-34 形狀）；attach 對 ATTACHED passthrough 回 `{"ok":true,"session":…}`。`command_capable` 定義不改。依據：OD-14 #2；調和表 SC-35、SES-16.4-03、SES-16.6-01；arch §5.12。
- **Test author model**：haiku-xhigh　**Implementer model**：codex-gpt-6-luna-max（session 狀態機、tick、gate）
- **Changelog fragment**：`changelog.d/rust-p1oc-sc35-passthrough-ready.md`（`type: feat`，`scope: session`）：passthrough session 不再登入或送 probe；連續 30 秒沒有輸出即自動進入 READY，可直接送命令。
- **BLOCKED-OWNER**：無。

**與 P1 的調和**：P1 T12（SC-14 flash probe 資格）與 T20（permit）改過 attach／probe 路徑；先讀這兩個 commit 確認 `_attach_by_id`、`_attach_by_id_dynamic`、`_probe_existing_bridge`、`_prepare_reprobe_locked`、boot quiet arm 點（banner 偵測）的現況，再在各點加 `platform == "passthrough"` 分支。P1 T11 的 `_resolve_session_id(…, trusted_human=…)` 簽章沿用。

**Files**
- Create: `tests/test_p1oc_sc35_state.py`
- Modify: `sw_core/constants.py`（`PASSTHROUGH_READY_QUIET_S = 30.0`）、`sw_core/session_manager.py`（`SessionRuntime.passthrough_quiet_since`、attach 路徑、`_mark_session_rx`、`reconcile_readiness`、boot quiet arm、reprobe 候選、`self_test`、`recover_session`、`attach_session`）、`sw_core/service.py`（`_resolve_session_id`）、`contracts/error_codes.yaml`（`SESSION_NOT_READY.extra_fields` 加 `"passthrough_ready_in_s"`；`PROFILE_NOT_COMMAND_CAPABLE.meaning` 改「session 為 ATTACHED 且非 passthrough 的 profile 不支援命令（command_capable 為 false），或 file.push／file.pull 的目標為 passthrough」）、`contracts/rpc_schema.json`（`command.submit` 失敗分支 `SESSION_NOT_READY` 加選用 `passthrough_ready_in_s`；`session.self_test` PASSTHROUGH 分支加選用 `passthrough_ready_in_s`）、`tests/` 下列「測試資產變更」

**Interfaces**
- Produces（Python）：
  ```python
  # sw_core/constants.py
  PASSTHROUGH_READY_QUIET_S: float = 30.0

  # SessionRuntime（mutable dataclass）新增，不進 to_public_dict()／state.json
  passthrough_quiet_since: float | None = None   # time.monotonic()；None＝非 passthrough 或非 ATTACHED

  SessionManager._promote_quiet_passthrough_locked(self, now: float) -> list[str]
      # 回傳本次升 READY 的 session_id；只改狀態，不 TX；由 reconcile_readiness 以 time.monotonic() 呼叫
  SessionManager.passthrough_ready_in_s(self, session: SessionRuntime, now: float) -> float | None
      # 非 passthrough 或非 ATTACHED → None；否則 round(max(0.0, Q - (now - quiet_since)), 1)
  ```
- submit-time 錯誤優先序（`_resolve_session_id`）：查無 → 原樣；`RECOVERING` → `SESSION_RECOVERING`；`FLASHING` → `FLASHING_BUSY`；`ATTACHED` 且 passthrough → `{"ok":false,"error_code":"SESSION_NOT_READY","passthrough_ready_in_s":…,"session":…}`；`ATTACHED` 且非 passthrough 且 `!command_capable` → `PROFILE_NOT_COMMAND_CAPABLE`（不變）；其餘非 READY → `SESSION_NOT_READY`；READY 且 passthrough → 不套 `READY_UNCONFIRMED`／`AUTOBOOT_QUIET`；READY 非 passthrough → 不變。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_p1oc_sc35_state.py`（`pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]`；以 `make_manager`＋`make_profile(platform="passthrough", ready_probe="")` 與 uboot 變體 `make_profile(platform="passthrough", prompt_regex="(?mi)^(=>|u-boot>|cfe>) $", ready_probe="echo __READY__${nonce}")`；bridge 以 `make_bridge(tmp_path, port=FakePort())` 注入並直接設 `session.bridge`；時間以明確 `now` 參數傳入 helper）
  ```python
  from __future__ import annotations
  import pytest
  from tests.stage0_fakes import FakePort, make_bridge, make_manager, make_profile

  pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

  def _load():
      from sw_core.constants import PASSTHROUGH_READY_QUIET_S
      return PASSTHROUGH_READY_QUIET_S

  def _attached_pt(tmp_path, **kw):
      port = FakePort()
      mgr = make_manager(tmp_path, [make_profile(platform="passthrough", ready_probe="", **kw)])
      s = mgr.get_session("COM0")
      s.bridge, _ = make_bridge(tmp_path, port=port)
      s.state = "ATTACHED"; s.passthrough_quiet_since = 1000.0
      return mgr, s, port

  def test_quiet_constant_is_30():
      assert _load() == 30.0

  def test_no_ready_before_window(tmp_path):
      mgr, s, port = _attached_pt(tmp_path)
      with mgr._lock:
          assert mgr._promote_quiet_passthrough_locked(1029.9) == []
      assert s.state == "ATTACHED"
      assert mgr.passthrough_ready_in_s(s, 1029.9) == 0.1

  def test_ready_at_window_zero_tx(tmp_path):
      mgr, s, port = _attached_pt(tmp_path)
      with mgr._lock:
          assert mgr._promote_quiet_passthrough_locked(1030.0) == [s.session_id]
      assert s.state == "READY" and s.last_error is None and port.writes == []

  def test_rx_restarts_window(tmp_path, monkeypatch):
      mgr, s, port = _attached_pt(tmp_path)
      monkeypatch.setattr("sw_core.session_manager.time.monotonic", lambda: 1020.0)
      mgr._mark_session_rx(s)
      assert s.passthrough_quiet_since == 1020.0
      with mgr._lock:
          assert mgr._promote_quiet_passthrough_locked(1049.9) == []
          assert mgr._promote_quiet_passthrough_locked(1050.0) == [s.session_id]
  ```
  表格補充：

  | 測試名 | 斷言 |
  |---|---|
  | `test_tx_does_not_restart_window` | `_mark_session_tx(s)` 後 `passthrough_quiet_since` 不變 |
  | `test_blocked_states_not_promoted` | 參數化：`_draining_by_ids` 含 by_id、`state="RELEASED"`、flash 臨界（P1 T12 的 flash-critical 標記）、`bridge=None` → `now=2000` 時皆不升 |
  | `test_attach_passthrough_zero_tx_even_with_ready_probe` | uboot 變體經 P1 的 attach 入口（`_attach_by_id`，以替身 `UARTBridge.start` 不開真實裝置）→ `ATTACHED`、`last_error is None`、`port.writes == []`、未呼叫 `ensure_ready`／`probe_ready`（monkeypatch 為擲例外） |
  | `test_ready_rx_does_not_demote_or_arm_quiet` | READY passthrough 收到含 `U-Boot 2023` banner 的 RX → state 仍 `READY`、`boot_quiet_active() is False`、`ready_reconfirm_pending is False` |
  | `test_submit_attached_passthrough_session_not_ready` | `SerialwrapService.rpc("command.submit", {"selector":"COM0","cmd":"ls"})`（`time.monotonic` 固定為 1010.0）→ 鍵序 `["ok","error_code","passthrough_ready_in_s","session"]`、`error_code=="SESSION_NOT_READY"`、`passthrough_ready_in_s == 20.0` |
  | `test_non_passthrough_empty_probe_still_not_capable` | `platform="prpl"`、`ready_probe=""`、ATTACHED → `PROFILE_NOT_COMMAND_CAPABLE`（回歸防護，既綠） |
  | `test_ready_passthrough_skips_quiet_gates` | READY passthrough 且人為設 `ready_reconfirm_pending=True` → submit 被 arbiter 接受（`ok is True`，有 `cmd_id`） |
  | `test_self_test_ready_passthrough_zero_tx` | READY passthrough → `classification=="PASSTHROUGH"`、`recommended_action=="none"`、`port.writes == []` |
  | `test_self_test_attached_passthrough_has_ready_in` | ATTACHED → 既有 PASSTHROUGH 分支鍵仍在，另含 `passthrough_ready_in_s` |
  | `test_recover_passthrough_no_probe` | ATTACHED passthrough `recover_session("COM0")` → `action=="REPROBE"`、`recovered is False`、`error_code=="SESSION_NOT_READY"`、`last_error is None`、`port.writes == []` |
  | `test_attach_attached_passthrough_ok` | uboot 變體 ATTACHED `attach_session("COM0")` → `{"ok": True, "session": …}`、零 TX |
  | `test_bridge_down_returns_to_attached_and_restarts_window` | READY passthrough 觸發 `_handle_bridge_down` 並完成重 attach（替身）→ 回 `ATTACHED`，`passthrough_quiet_since` 為重 attach 時刻 |
  | `test_flash_candidates_include_ready_passthrough` | READY 的 others-template（非 command_capable）仍出現在 flash 偵測候選（P1 T12 的候選收集函式） |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_p1oc_sc35_state.py` → 預期 `ImportError: cannot import name 'PASSTHROUGH_READY_QUIET_S'`、`AttributeError: … '_promote_quiet_passthrough_locked'`、`'PROFILE_NOT_COMMAND_CAPABLE' != 'SESSION_NOT_READY'`。commit：`test(session): SC-35 passthrough 安靜升 READY 狀態機失敗測試`
- [ ] **B1 實作**：依 Interfaces 與範圍；升 READY 時同時清 `passthrough_quiet_since`、呼叫既有 `on_ready` 回呼（arbiter 註冊同一般 READY）；所有 passthrough 判斷寫成 `session.profile.platform == "passthrough"`（不依 profile 名稱）。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_p1oc_sc35_state.py tests/test_cowork_session_usability.py tests/test_profile_reresolve_on_attach.py tests/test_boot_quiet.py tests/test_ready_reconfirm_bounds.py tests/test_flash_guard.py tests/test_stage0_lease_flash_console.py tests/test_windows_claim.py tests/test_session_bind.py tests/test_login_fsm.py`；`python3 -m tools.contracts check`；全套。
- [ ] **B3 commit**：`feat(session): SC-35 passthrough 不登入、30 秒安靜自動 READY`

**測試資產變更**（獨立審閱；B1 前以 `grep -rln "passthrough\|uboot-template\|others-template" tests/` 掃描，下列為已知候選，逐測試列入 commit 本文）

| 檔案 | 測試 | 改動 |
|---|---|---|
| `tests/test_cowork_session_usability.py` | `TestCommandCapableGate::test_empty_ready_probe_not_command_capable_cmd_submit`（passthrough＋空 ready_probe） | 改斷言 `SESSION_NOT_READY`＋`passthrough_ready_in_s`；另加非 passthrough 版本仍為 `PROFILE_NOT_COMMAND_CAPABLE` |
| `tests/test_cowork_session_usability.py`、`tests/test_profile_reresolve_on_attach.py` | 斷言 uboot-template（passthrough＋ready_probe）attach 時送 probe 或經 probe 進 READY 者 | 改斷言零 TX、`ATTACHED`，READY 改由 `_promote_quiet_passthrough_locked` 驅動 |
| `tests/test_login_fsm.py` | 以 passthrough 平台驅動 session 層登入者（純 `login_fsm` 函式測試不改） | 改為非 passthrough 平台或改斷言不呼叫 |
| `contracts/error_codes.yaml` 的 `PROFILE_NOT_COMMAND_CAPABLE.contract_tests` | 指向上列 cowork 測試 | 改指非 passthrough 版本 |
| `tests/test_stage0_uart_exclusive.py` | `TestAttachProbeDrain` 兩個 release 前的 state 斷言（passthrough＋空 `ready_probe`） | `READY`→`ATTACHED`（SC-35 零 TX，停 `ATTACHED`；release 測試的 drain 前置條件不變） |

---

## Task T6：SC-35b passthrough 命令路徑：完成條件、無救援按鍵、file.* 拒絕；SC-35 → IMPLEMENTED

- **範圍**：READY passthrough 的 `line`／`background` 完成條件三層：(1) 有 `completion_regex` → #230 producer 完成；(2) 否則 profile `prompt_regex` 通過 `valid_completion_regex`（可編譯、非空、`search("") is None`）→ 既有 prompt 完成；(3) 否則（others-template `.*`）→ 沿用既有等待迴圈（有 RX 續等、連續 `min(timeout_s, 30)` 秒無新 RX 或 effective timeout 到即停）後回 `{"ok":false,"error_code":"PROMPT_TIMEOUT","partial":true,"recovery_action":"NONE"}`，capture chunks＝`clean_text(RX)`。passthrough 未命中時不送 `\x03`／`\x04`、不 `_transition_to_attached`、不 `_force_recover`，session 維持 `READY`；`_is_reboot_command` 特例不套用；completion_regex 命中且 prompt 不可用時 stdout＝`clean_text(pre_offset 起至完成標記結尾的 RX)`；interactive 模式不變。`file.push`／`file.pull` 對 passthrough（任何 state）回 `PROFILE_NOT_COMMAND_CAPABLE`＋`hint`＋`session`、零 TX。SC-35 → `IMPLEMENTED`。依據：OD-14 #2；`docs/command-completion.md`；調和表 SC-35。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：codex-gpt-6-luna-max（命令執行與 recovery 路徑）
- **Changelog fragment**：`changelog.d/rust-p1oc-sc35-passthrough-commands.md`（`type: feat`，`scope: command`）：passthrough 命令以 `--completion-regex` 或可用的 profile prompt 判定完成；沒有完成條件時以 `PROMPT_TIMEOUT` 回傳已擷取輸出，且不送 Ctrl-C／Ctrl-D、不中斷 session。
- **BLOCKED-OWNER**：無。

**與 P1 的調和**：P1 T06（TX 三態）、T08（BC-03 中止映射）、T11（`trusted_human`）、T20（`system_probe` permit 包住 recover 按鍵）都改過 `_execute_command_inner` 與 `_recover_after_failure`；以 P1 實際程式為底，在 `_recover_after_failure` 最前面（human 分支之後、quiet 判斷之前）加 passthrough 早退分支，P1 的 permit 與 BC-03 映射路徑不受影響。

**Files**
- Create: `tests/test_p1oc_sc35_commands.py`
- Modify: `sw_core/session_manager.py`（`_execute_command_inner`、`_extract_command_stdout` 呼叫點、`_recover_after_failure`、`file_push`／`file_pull` 的 manager gate）、`sw_core/service.py`（`file.push`／`file.pull` 的 `_resolve_session_id` 後檢查）、`contracts/behavior_changes.yaml`（SC-35 → `IMPLEMENTED`）、`contracts/error_codes.yaml`（`PROFILE_NOT_COMMAND_CAPABLE`、`PROMPT_TIMEOUT`、`SESSION_NOT_READY` 的 `behavior_change` 加 `SC-35` 並補 `contract_tests`）、`contracts/rpc_schema.json`（若 T5 以 `x-pending` 暫存欄位，於此併入）、兩份 SKILL 受管區段

**Interfaces**
- Produces（Python）：
  ```python
  def _passthrough_completion_pattern(profile: SessionProfile, completion_regex: str | None) -> str | None:
      # 回傳本次要等待的 regex；None＝無完成條件（第 3 層）
      # completion_regex 非 None → completion_regex；valid_completion_regex(profile.prompt_regex) → prompt_regex；否則 None
  ```
- `file.push`／`file.pull` 錯誤優先序（凍結，P8 `09` 依此承接）：`INVALID_ARGS` → `SESSION_NOT_FOUND` → **passthrough（任何 state）→ `PROFILE_NOT_COMMAND_CAPABLE`** → 其後依 P1 既有順序（`SESSION_RECOVERING`、`FLASHING_BUSY`、非 READY、gate…）。

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_p1oc_sc35_commands.py`（`pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty]`；以 PTY 假 target（`tests/test_cowork_session_usability.py` 既有 PTY helper 或 P1 T01 替身）回應；READY passthrough 由 T5 helper `_promote_quiet_passthrough_locked` 驅動）

  | 測試名 | 情境 | 斷言 |
  |---|---|---|
  | `test_completion_regex_producer_completes` | others-template READY；target 回 `out\n__RC__=0\n__END__\n`、不回 prompt；`completion_regex='(?m)^__RC__=[0-9]+\r*\n__END__\r*$'` | `ok is True`、`partial is False`、`"out" in stdout` 且 stdout 以完成標記結尾 |
  | `test_uboot_prompt_completes` | uboot 變體 READY；target 回 `version\r\nU-Boot 2023\r\n=> ` | `ok is True`、`partial is False`、stdout 為既有 `_extract_command_stdout` 結果 |
  | `test_unusable_prompt_times_out_without_recovery_keys` | others-template READY、`timeout_s=1.0`；target 回一行後靜默 | 回應 `== {"ok": False, "error_code": "PROMPT_TIMEOUT", "partial": True, "recovery_action": "NONE"}`；target 收到的 bytes 只有命令本身（無 `b"\x03"`、`b"\x04"`）；state 仍 `READY`；`command.get` capture chunks 含該行 |
  | `test_completion_miss_no_demote` | uboot 變體、target 不回 prompt、`timeout_s=1.0` | `PROMPT_TIMEOUT`、無 `\x03`／`\x04`、state `READY`、未呼叫 `_force_recover`（monkeypatch 擲例外） |
  | `test_reboot_command_not_special_on_passthrough` | others-template READY 送 `reboot` | 未呼叫 `_handle_reboot_command`（monkeypatch 擲例外）；結果同第 3 層 |
  | `test_file_push_pull_passthrough_rejected` | READY 與 ATTACHED passthrough 各送 `file.push`／`file.pull` | `error_code == "PROFILE_NOT_COMMAND_CAPABLE"`、含 `hint` 與 `session`、target 未收到任何 byte |
  | `test_background_mode_same_completion_rules` | `mode="background"` 重跑第 1、3 列 | 完成／逾時語意相同；第 3 列 capture 有內容 |
  | `test_interactive_mode_unchanged` | READY passthrough `mode="interactive"` | 回 `execution_mode == "interactive"` 與 lease id（既有語意） |
  | `test_non_passthrough_recovery_unchanged` | prpl READY 命令逾時 | 仍送 `\x03`、`\x04` 並依既有規則處理（回歸防護） |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_p1oc_sc35_commands.py` → 預期 others-template `.*` 立即「完成」使 `partial` 為 False、target 收到 `\x03`、file.push 被接受。commit：`test(command): SC-35 passthrough 命令完成條件失敗測試`
- [ ] **B1 實作**：依範圍與 Interfaces；SC-35 → `IMPLEMENTED`，引用碼同 commit 補 `contract_tests`。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_p1oc_sc35_commands.py tests/test_p1oc_sc35_state.py tests/test_command_completion*.py tests/test_cowork_session_usability.py tests/test_bootloader_recovery.py tests/test_issue24_heartbeat.py tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py`；`python3 -m tools.contracts check`；全套。
- [ ] **B3 commit**：`feat(command): SC-35 passthrough 命令完成條件與不介入恢復`

**測試資產變更**：無預期；若既有 #230 測試以 passthrough profile 驅動且斷言 CTRL_C 救援，逐測試列入並獨立審閱。

---

## Task T7：文件、SKILL 受管區段、#155 回歸 case、docker／Windows 原生驗證收尾

- **範圍**：(1) `README.md` 英文與繁中兩段：`session attach`／`recover` 失敗欄位（`last_error`、`last_error_detail`）、`device release` 的 `DRAIN_STALLED` 處置、passthrough 30 秒自動 READY 與完成條件建議（建議一律帶 `--completion-regex`）、socket 權限 0660＋`SERIALWRAP_SOCKET_GROUP`；(2) `docs/command-completion.md` 加「passthrough」小節（三層規則）；`CLAUDE.md`「Platform 行為差異」passthrough 一列改寫（`CLAUDE.md` 為 single source，三個 symlink 自動同步）；(3) 確認兩份 SKILL 受管區段為最新，並手動改寫未受管的「command_capable 與 READY 判定」敘述，明確說明 passthrough 即使帶 `ready_probe` 也不 probe、ATTACHED 靜默 30 秒後升 READY，以及 ATTACHED 時 `command.submit` 回 `SESSION_NOT_READY`＋`passthrough_ready_in_s`；(4) #155：新增 `regression/` case `pt-quiet-ready`（真板 U-Boot／MCU 線：attach 後 30 s 內零 TX、靜默後 READY、`cmd submit --completion-regex` 成功）並於 `docs/regression-plugin.md` 對照表登記；(5) OD-15 驗證：P2 Task 17 docker 環境已合併時以 `scripts/docker/run-l15.sh` 跑本 Phase 新測試與全套、以 `scripts/docker/run-systemd.sh` 驗 socket 群組權限，報告標 `L1.5-docker`（不計為 L2／L3）；擁有者 Windows host 以 COM20／COM21（OD-11 隔離實例＋allowlist `COM20`）跑 `windows_native` 子集；修正後 Python 基線的 docker 48h 長跑（P2 `run-longrun.sh`）於本 Phase 合併後啟動（OD-15 (6)：P1 完成、修正後基線就緒）。
- **Test author model**：agy-gemini-3.8-flash-high　**Implementer model**：haiku-xhigh（文件與資料，介面已定）
- **Changelog fragment**：`changelog.d/rust-p1oc-docs.md`（`type: change`，`scope: docs`）：同步 OD-14 對外文件（attach／recover 欄位、DRAIN_STALLED、passthrough 自動 READY、socket 0660）。
- **BLOCKED-OWNER**：`pt-quiet-ready` 真板實跑（bench）；docker 驗證若 P2 docker 環境未合併則延後（見 Global Constraints）。收尾紀錄：`scripts/docker/` 於本 Phase 執行時尚不存在（P2 Task 17 未合併），docker 驗證記為「待補」gate；擁有者 host Windows 原生結果於 PR 描述記錄。

**Files**
- Create: `tests/test_p1oc_docs.py`、`regression/`（`pt-quiet-ready` case 檔，路徑依 `docs/regression-plugin.md` SOP）
- Modify: `README.md`、`docs/serialwrap-spec.md`、`docs/command-completion.md`、`CLAUDE.md`、`docs/regression-plugin.md`、`regression/serialwrap_regression/cases/f10_login_creds.py`、兩份 SKILL（error_code／session_last_error 受管區段只經 `skill-section` 產生；「command_capable 與 READY 判定」未受管敘述另依本 Task 手動同步）

**Steps**
- [ ] **A1 寫失敗測試**：`tests/test_p1oc_docs.py`（`pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]`）

  | 測試名 | 斷言 |
  |---|---|
  | `test_readme_both_languages_mention_drain_stalled` | `README.md` 的 `English` 章與 `繁體中文` 章各自含 `DRAIN_STALLED` 與 `last_error_detail` |
  | `test_readme_passthrough_quiet_ready` | 兩章各含 `30` 秒／seconds 與 `--completion-regex` 的 passthrough 說明（以章節切片後 regex `passthrough[\s\S]{0,400}30` 比對） |
  | `test_readme_socket_0660` | 兩章含 `0660` 與 `SERIALWRAP_SOCKET_GROUP`，且不含 `0600`（socket 語境） |
  | `test_command_completion_doc_passthrough_section` | `docs/command-completion.md` 含 `passthrough` 小節與 `PROMPT_TIMEOUT`、`recovery_action` |
  | `test_skill_sections_up_to_date` | `python3 -m tools.contracts skill-section --namespace error_code` 輸出與 SKILL 檔受管區段逐位元組相同（session_last_error 同） |
  | `test_regression_case_registered` | `regression/` 存在 `pt-quiet-ready` case，`docs/regression-plugin.md` 對照表含該 case |
- [ ] **A2 執行並確認 FAIL**：`python3 -m pytest -q tests/test_p1oc_docs.py`。commit：`test(docs): OD-14 文件同步失敗測試`
- [ ] **B1 實作**：依範圍撰寫；README 兩段內容一致。
- [ ] **B2 執行並確認 PASS**：`python3 -m pytest -q tests/test_p1oc_docs.py`；全套 `python3 -m pytest -q tests/`（零失敗）；Windows 原生 `scripts/stage0/win-pytest.sh -m windows_native tests/`（擁有者 host）；docker 驗證（若可用）；`python3 -m tools.contracts check`；`python3 -m policy_check --repo .`。
- [ ] **B3 commit**：`docs: 同步 OD-14 對外契約與 passthrough 自動 READY 說明`

---

## 1. Phase 完成條件（Definition of Done）

1. T1～T7 全部 B3 commit 完成；`python3 -m pytest -q tests/` 零失敗；`python3 -m tools.contracts check` 與 `python3 -m policy_check --repo .` 通過。
2. `behavior_changes.yaml`：SC-34、SC-35 為 `IMPLEMENTED`；無 SC-08；`pending_owner_confirmations: []`；SC-04、DF-02、SC-26、SC-27、SC-34、SC-35 為 `owner_confirmation: confirmed`。
3. `error_codes.yaml`：含 `DRAIN_STALLED`（IMPLEMENTED）、無 `passthrough` meta 項（頂層保留空 `passthrough: []`，見 T2 的 `error_codes.yaml` 條目）、`NOT_READY.reachable is False`；`rpc_schema.json` 無 `@last_error_passthrough`。
4. 下游通知已寫入：P3 Task 8 需重新產生 `error_codes.rs`；P5～P8 的 A 側基線以本 Phase 合併後的 `sw_core` 為準。
5. PR 描述記錄 #155 評估結論、OD-15 docker／Windows 原生驗證結果或「待補」gate。

## 2. BLOCKED-OWNER 彙總

| 項目 | 原因 | 交付物 |
|---|---|---|
| `pt-quiet-ready` 真板實跑 | 需 bench 與真板 U-Boot／MCU 線 | T7 regression case |
| docker 驗證（若 P2 docker 環境未合併） | 依賴 P2 OD-15 Task | T7 記錄為待補 gate |
