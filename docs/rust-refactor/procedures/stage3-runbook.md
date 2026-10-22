# Stage 3 擁有者執行手冊

本手冊是 Stage 3 的唯一執行順序。所有 `python3 -m tools.stage3` 命令都可被 `build_parser()` 解析（`tests/test_stage3_procedures.py` 逐行驗證），每個命令寫在單一行內。

標示 `【BLOCKED-OWNER BO-P9-xx】` 的步驟需要擁有者的硬體、時間或裁決（對照計畫 §0.2 總表）。Agent 與 CI 不得代為產出這些證據，也不得把它們標為 PASS。

## 1. 範圍與不可違反的原則

- Stage 3 在 Python 產品 daemon 退役前，以 L1.5／L2／L3 Sequential A/B、Canary 帳本、實機回滾演練與出口證據索引，證明 Rust 候選可取代 Python 基線。
- `tools/stage3` 只做外層編排與判定。實際執行仍由 `func-test`、TestPilot `serialwrap_regression` 與 `realhw` 引擎負責，本工具不建立另一個排程、重試或報告引擎。
- PASS 只能經 `evidence ingest` 從產生工具的報告推導並登錄。`evidence add` 只接受 `manual-procedure` 項目。
- 證據不足、擷取失敗、`unknown`、未執行、SKIP、INFRA_ERROR 與 INSUFFICIENT 都不等於 PASS；FAIL 不會被後續 PASS 抵銷。
- OD-09 延後門檻必須在任何 Rust 候選長跑開始前凍結，最早的候選長跑是 `S1B-L3-MIXED-48H-WINDOWS`。
- Canary 不是任何 Stage 0～2.4 出口的條件。Canary 計數紀錄不得早於 `canary policy freeze`。
- Canary collector 與演練觀測不得呼叫 `session.interactive_status`（不得呼叫：會 touch Lease，規格 §10.2、§15.11.3）。
- 48h A/B 在擁有者 bench 上 Linux 與 Windows 依序執行，同機同板經 usbipd，不可並行。
- 退役範圍只限產品執行期 Python daemon。Python 測試工具、bridge、DUT 模擬器、TestPilot 與 `realhw` 引擎保留。

## 2. 入口與結束碼

```text
python3 -m tools.stage3 <群組> <子命令> [參數]
```

| 結束碼 | 意義 |
|---|---|
| 0 | PASS 或成功 |
| 1 | FAIL、INCOMPLETE、BLOCKED 或 POLICY_NOT_FROZEN／POLICY_CHANGED 等未通過判定 |
| 2 | 參數錯誤，或 `HarnessError`（stdout 為 `{"ok":false,"reason_code":…,"detail":…}`） |
| 3 | INFRA_ERROR、BASELINE_INVALID 或 ABORTED |

stdout 以緊湊 JSON 輸出，僅 `evidence report` 與 `canary report --md` 輸出 Markdown。原因碼一律為 `HARNESS_*`，定義於 `tools/stage3/stage3_reason_codes.json`，不混入 `contracts/error_codes.yaml`。

## 3. 執行順序

以下步驟依序進行。前一步未完成，後續步驟不得開始。環境變數由擁有者於每次執行前設定。

### 前置：變數定義與各階段 target

下列變數於每次執行前由擁有者設定。Stage 3 的變數取自目前的 repo checkout（同一個 `HEAD`）；Stage 0～2.4 的 revision 變數取自各階段自身的報告，不得以目前 `HEAD` 代替（見下方前置段的警告）：

| 變數 | 定義 |
|---|---|
| `CONTRACT_REV`、`TOOL_REV`、`CASE_LIST_SHA256`（僅 Stage 3） | `CONTRACT_REV` 為 `git rev-parse HEAD:contracts`（40 位十六進位）；`TOOL_REV` 為 `git rev-parse HEAD`，與 Stage 3 A/B 報告記錄的 `tool_revision`（執行當時的 HEAD）相同；`CASE_LIST_SHA256` 為 `tests/acceptance/cases.lock.json` 的 sha256（與 P9-T14 `set_target` 的定義一致） |
| `STAGE<階段>_CONTRACT_REV`、`STAGE<階段>_TOOL_REV`、`STAGE<階段>_CASE_LIST_SHA256`（階段 0、1A、1B、2.1、2.2、2.3、2.4） | 該階段受測報告所記錄的 `contract_revision`、`tool_revision` 與 case list（A/B 報告、func-test manifest、regression／realhw meta、gate run）。報告未記錄時取該階段出口 commit：`git rev-parse <出口 commit>:contracts`、`git rev-parse <出口 commit>`、`git show <出口 commit>:tests/acceptance/cases.lock.json \| sha256sum`。不得以目前 HEAD 代替 |
| `SET_BY` | 負責設定 target 的人員名稱（非空字串） |
| `EVIDENCE_ROOT` | 擁有者指定的證據根目錄（建議放在 repo 外的獨立目錄；ingest 會把證據檔複製到其下） |
| `ENV_LINUX_JSON`、`ENV_WINDOWS_JSON` | 各平台環境 JSON，恰好 8 個欄位（見 `evidence-index.md` 第 3 節）；同一環境固定使用同一個檔案 |
| `OD09_DIR` | P2 `realhw.acceptance.od09_freeze freeze --out` 的輸出目錄，內含 `od09-freeze.json` |
| `CURRENT_BUNDLE` | Linux：`$(dirname "$(dirname "$(realpath "$SERIALWRAP_CLI")")")`；Windows：CLI 旁 `current-release.json` 的 `dir` 欄位（與演練的解析方式相同） |
| `STAGE<階段>_<平台>_BUNDLE` | 各階段受測 bundle 目錄（例如 `STAGE0_LINUX_BUNDLE`、`STAGE24_LINUX_BUNDLE`） |
| `AB_OUT`、`JUNIT_LINUX_DIR`、`JUNIT_WINDOWS_DIR`、`REALHW_*`、`CANARY_OUT`、`DRILL_OUT` | 各產生工具的輸出目錄，由擁有者指定；每次執行必須是尚不存在的新目錄（junit 目錄內同時產生 `junit.xml` 與 `resource-report.json`） |
| `SERIALWRAP_CLI`、`WINDOWS_CLI` | 受測安裝的 CLI 執行檔（Linux 與 Windows 各一） |
| `LINUX_BOARDS`、`WINDOWS_BOARDS` | 演練使用的兩塊板子，逗號分隔（例如 `COM0,COM1`） |
| `CURRENT_VERSION`、`PREVIOUS_VERSION`、`NEWER_VERSION` | 演練的預期版本（Current、Previous）與 D8 來源用的較新版本 |
| `START_FAIL_OUT`、`START_FAIL_SRC`、`START_FAIL_SRC_WINDOWS` | D8 來源的建置輸出目錄與版本目錄（Linux 見 `rollback-drill.md` 第 2 節；Windows 見步驟 (10)） |
| `WINDOWS_CURRENT_BUNDLE` | Windows Current bundle 目錄（即 `current-release.json` 的 `dir`，見上列 `CURRENT_BUNDLE`） |

```bash
# 僅 Stage 3；Stage 0～2.4 使用各自的 STAGE<階段>_* 變數
CONTRACT_REV="$(git rev-parse HEAD:contracts)"
TOOL_REV="$(git rev-parse HEAD)"
CASE_LIST_SHA256="$(sha256sum tests/acceptance/cases.lock.json | cut -d' ' -f1)"
```

各階段出口的 target 必須先於該階段的 ingest 設定，否則 ingest 以 `no target` 拒收。以下為各階段、各平台的 target 設定；`$STAGE<階段>_<平台>_BUNDLE` 為該階段受測 bundle 目錄（`--release-manifest` 指向該階段受測 bundle 的 `release-manifest.json`）。Stage 2.4 另須 `--bundle-dir`，因其長跑要供 Stage 3 `reuse_of` 沿用（`evidence-index.md` 第 4 節）。

```bash
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 0 --platform linux --release-manifest "$STAGE0_LINUX_BUNDLE/release-manifest.json" --contract-revision "$STAGE0_CONTRACT_REV" --tool-revision "$STAGE0_TOOL_REV" --case-list-sha256 "$STAGE0_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 0 --platform windows --release-manifest "$STAGE0_WINDOWS_BUNDLE/release-manifest.json" --contract-revision "$STAGE0_CONTRACT_REV" --tool-revision "$STAGE0_TOOL_REV" --case-list-sha256 "$STAGE0_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 1A --platform linux --release-manifest "$STAGE1A_LINUX_BUNDLE/release-manifest.json" --contract-revision "$STAGE1A_CONTRACT_REV" --tool-revision "$STAGE1A_TOOL_REV" --case-list-sha256 "$STAGE1A_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 1A --platform windows --release-manifest "$STAGE1A_WINDOWS_BUNDLE/release-manifest.json" --contract-revision "$STAGE1A_CONTRACT_REV" --tool-revision "$STAGE1A_TOOL_REV" --case-list-sha256 "$STAGE1A_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 1B --platform linux --release-manifest "$STAGE1B_LINUX_BUNDLE/release-manifest.json" --contract-revision "$STAGE1B_CONTRACT_REV" --tool-revision "$STAGE1B_TOOL_REV" --case-list-sha256 "$STAGE1B_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 1B --platform windows --release-manifest "$STAGE1B_WINDOWS_BUNDLE/release-manifest.json" --contract-revision "$STAGE1B_CONTRACT_REV" --tool-revision "$STAGE1B_TOOL_REV" --case-list-sha256 "$STAGE1B_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 2.1 --platform linux --release-manifest "$STAGE21_LINUX_BUNDLE/release-manifest.json" --contract-revision "$STAGE21_CONTRACT_REV" --tool-revision "$STAGE21_TOOL_REV" --case-list-sha256 "$STAGE21_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 2.1 --platform windows --release-manifest "$STAGE21_WINDOWS_BUNDLE/release-manifest.json" --contract-revision "$STAGE21_CONTRACT_REV" --tool-revision "$STAGE21_TOOL_REV" --case-list-sha256 "$STAGE21_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 2.2 --platform linux --release-manifest "$STAGE22_LINUX_BUNDLE/release-manifest.json" --contract-revision "$STAGE22_CONTRACT_REV" --tool-revision "$STAGE22_TOOL_REV" --case-list-sha256 "$STAGE22_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 2.2 --platform windows --release-manifest "$STAGE22_WINDOWS_BUNDLE/release-manifest.json" --contract-revision "$STAGE22_CONTRACT_REV" --tool-revision "$STAGE22_TOOL_REV" --case-list-sha256 "$STAGE22_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 2.3 --platform linux --release-manifest "$STAGE23_LINUX_BUNDLE/release-manifest.json" --contract-revision "$STAGE23_CONTRACT_REV" --tool-revision "$STAGE23_TOOL_REV" --case-list-sha256 "$STAGE23_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 2.3 --platform windows --release-manifest "$STAGE23_WINDOWS_BUNDLE/release-manifest.json" --contract-revision "$STAGE23_CONTRACT_REV" --tool-revision "$STAGE23_TOOL_REV" --case-list-sha256 "$STAGE23_CASE_LIST_SHA256" --set-by "$SET_BY"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 2.4 --platform linux --release-manifest "$STAGE24_LINUX_BUNDLE/release-manifest.json" --contract-revision "$STAGE24_CONTRACT_REV" --tool-revision "$STAGE24_TOOL_REV" --case-list-sha256 "$STAGE24_CASE_LIST_SHA256" --set-by "$SET_BY" --bundle-dir "$STAGE24_LINUX_BUNDLE"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 2.4 --platform windows --release-manifest "$STAGE24_WINDOWS_BUNDLE/release-manifest.json" --contract-revision "$STAGE24_CONTRACT_REV" --tool-revision "$STAGE24_TOOL_REV" --case-list-sha256 "$STAGE24_CASE_LIST_SHA256" --set-by "$SET_BY" --bundle-dir "$STAGE24_WINDOWS_BUNDLE"
```

> 警告一：同一階段的 target 只設定一次。對已設定 target 的階段再次執行 `evidence target`，會使該階段既有 entry 因 revision 或指紋不符而變為 STALE；確需變更時，必須重新執行該階段全部的 ingest。
>
> 警告二：自 `evidence target --stage 3` 起，到最後一筆 `ab-report` 登錄為止，不得 commit、checkout 或 rebase。`TOOL_REV` 為 HEAD，A/B 報告的 `tool_revision` 為執行當時的 HEAD，中途的 commit 會使 ingest 拒收 A/B 報告。`docs/rust-refactor/evidence/index.json` 每次 ingest 都會被修改，應於 Stage 3 全部登錄完成後 commit 一次。

### 步驟 (1)：OD-09 延後凍結核對

【BLOCKED-OWNER BO-P9-12】 OD-09 延後凍結由 P2 Task 16 於正式 Stage 0 出口執行，寫入端為 `python3 -m realhw.acceptance.od09_freeze freeze`，程序見 `od09-deferred-freeze.md`。本手冊只核對，不寫入門檻檔。

本步驟的核對命令為唯讀，exit 0 表示 run 起點前三個延後項皆已凍結且欄位完整：

```bash
python3 -m tools.stage3 od09 check --acceptance realhw/acceptance/longrun_acceptance.yaml --run-started-at "$RUN_STARTED_AT"
```

本手冊所有開跑時刻（本步驟的 `$RUN_STARTED_AT`，以及步驟 (8) 的 `$LINUX_L3_STARTED_AT` 等）必須含明確時區偏移，例如 `RUN_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)`，不得使用無時區的時間字串。無時區者 `od09 check` 以 `HARNESS_CLI_ERROR`（exit 2）拒收；`register-candidate` 目前仍會接受並寫入 ledger，之後所有 `od09-freeze` 推導會以 `HARNESS_EVIDENCE_INGEST_INVALID` 拒收，直到 ledger 被修正（ledger 為 append-only 證據，不得自行改寫）。

若尚未凍結，回到 Stage 0 程序處理。P2 候選開跑 ledger 已有登記時，`freeze` 一律拒絕（`HARNESS_OD09_TOO_LATE`），不得手改 `realhw/acceptance/longrun_acceptance.yaml`。

`S0-OD09-DEFERRED-FROZEN` 於本步驟以 `od09-freeze` 報告分別對 linux 與 windows 登錄（須已設定 Stage 0 target，見前置段）。`S3-OD09-ALL-FROZEN` 須於步驟 (3) 設定 Stage 3 target 之後登錄，命令列於步驟 (3) 末尾：

```bash
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind od09-freeze --item S0-OD09-DEFERRED-FROZEN --platform linux --report "$OD09_DIR/od09-freeze.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind od09-freeze --item S0-OD09-DEFERRED-FROZEN --platform windows --report "$OD09_DIR/od09-freeze.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_WINDOWS_JSON"
```

### 步驟 (2)：Canary policy 凍結

【BLOCKED-OWNER BO-P9-06】 擁有者裁決活躍日認定、提交計數與去重、干擾／交接分類與各類型配額、各平台證據責任人後，凍結 `tests/acceptance/canary_policy.json`。凍結只能執行一次，且早於任何計數紀錄。凍結後政策檔不得改寫；`canary report` 會以 `policy_sha256` 核對，不符即 `POLICY_CHANGED`。

```bash
python3 -m tools.stage3 canary policy freeze --policy tests/acceptance/canary_policy.json --decision-id "$DECISION_ID" --frozen-by "$FROZEN_BY" --evidence-owner linux="$LINUX_OWNER" --evidence-owner windows="$WINDOWS_OWNER" --addenda docs/rust-refactor/owner-decisions-addenda.md
```

### 步驟 (3)：建置 Stage 3 候選 bundle 並設定 target

【BLOCKED-OWNER BO-P9-17】 Windows bundle 必須於自架 runner 原生 build 與 link。`retirement bundle-plan --stage 3` 應回報 `candidate` 為 true（`retired:false` 即可建置）。

```bash
python3 -m tools.stage3 retirement bundle-plan --stage 3
scripts/rust/mkbundle.sh --stage 3 --out "$LINUX_BUNDLE_ROOT"
LINUX_BUNDLE="$LINUX_BUNDLE_ROOT"
```

> 工具現況：`scripts/rust/mkbundle.sh` 的真實建置目前只支援 `--stage 1A`（搭配 `--dev`）。對 `--stage 3` 呼叫會回報 `HARNESS_BUILD_UNAVAILABLE` 並以 exit 2 結束，Stage 3 bundle 組裝尚未承接（`--plan-only` 只印退役計畫，不建置）。在該組裝能力交付前，本步驟不可執行，須回報為阻塞；不得以手工組裝的目錄代替受測 bundle。

`LINUX_BUNDLE` 與 `LINUX_BUNDLE_ROOT` 指向同一目錄：Stage 3 候選不加 `-dev` 後綴，`--out` 即輸出位置。Windows 候選於自架 runner 原生建置（BO-P9-17），輸出目錄即 `WINDOWS_BUNDLE`。

受測 bundle 目錄須保存，之後不得有任何寫入。以 `--bundle-dir` 設定 target 會同時凍結 `bundle_tree_sha256`，之後該目錄任何檔案的替換、刪除或新增都會使發佈被拒。

```bash
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 3 --platform linux --release-manifest "$LINUX_BUNDLE/release-manifest.json" --contract-revision "$CONTRACT_REV" --tool-revision "$TOOL_REV" --case-list-sha256 "$CASE_LIST_SHA256" --set-by "$SET_BY" --bundle-dir "$LINUX_BUNDLE"
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 3 --platform windows --release-manifest "$WINDOWS_BUNDLE/release-manifest.json" --contract-revision "$CONTRACT_REV" --tool-revision "$TOOL_REV" --case-list-sha256 "$CASE_LIST_SHA256" --set-by "$SET_BY" --bundle-dir "$WINDOWS_BUNDLE"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind od09-freeze --item S3-OD09-ALL-FROZEN --platform linux --report "$OD09_DIR/od09-freeze.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind od09-freeze --item S3-OD09-ALL-FROZEN --platform windows --report "$OD09_DIR/od09-freeze.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_WINDOWS_JSON"
```

### 步驟 (4)：Linux L1 契約黑箱與 L1.5 A/B

本環境可執行。L1 依 `tests/acceptance/l1_required.lock.json` 的 `s3_release_contract.linux`（selector `tests/contract_blackbox`、`markexpr` 為 `core_portable or posix_only`）執行，並同時設定 `SERIALWRAP_CLI_EXE` 與 `SERIALWRAP_DAEMON_EXE` 指向受測安裝；只設前者時報告會記 `daemon_env_missing` 而被拒收。`SW_RESOURCE_REPORT` 指向同目錄的 `resource-report.json`，ingest 會讀取它。L1 報告以 `junit` 登錄，`--report` 指向 `junit.xml`。

```bash
mkdir "$JUNIT_LINUX_DIR"
SERIALWRAP_CLI_EXE="$SERIALWRAP_CLI" SERIALWRAP_DAEMON_EXE="$LINUX_BUNDLE/bin/serialwrapd" SW_RESOURCE_REPORT="$JUNIT_LINUX_DIR/resource-report.json" python3 -m pytest tests/contract_blackbox -m "core_portable or posix_only" --junitxml="$JUNIT_LINUX_DIR/junit.xml"
python3 -m tools.stage3 ab verify-manifest --manifest "$AB_LINUX_L15_MANIFEST" --repo-root .
python3 -m tools.stage3 ab run --manifest "$AB_LINUX_L15_MANIFEST" --out "$AB_OUT/linux-l15" --repo-root .
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind junit --item S3-L1-RELEASE-CONTRACT-LINUX --platform linux --report "$JUNIT_LINUX_DIR/junit.xml" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind ab-report --item S3-L15-RELEASE-AB-LINUX --platform linux --report "$AB_OUT/linux-l15/ab-report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
```

`ab run` 的 `--out` 必須是尚不存在的目錄，已存在即拒絕（`HARNESS_MANIFEST_INVALID`），以免覆寫舊證據。

> 工具現況：`ab run` 的執行器為 P9-T6（`tools/stage3/ab_runner.py`）。該模組未交付前，`ab run` 會回報 `HARNESS_CLI_ERROR`（exit 2），不會產生 `ab-report.json`。此時本節的 A/B 步驟為阻塞，不得以手工寫出的報告代替。

### 步驟 (5)：Windows L1 與 L1.5 A/B

【BLOCKED-OWNER BO-P9-01】 Windows L1.5 Sequential A/B 須在擁有者 Windows host 執行（COM20／COM21，隔離實例，`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`）。本環境只能在擁有者 host 不可用時記為證據不足。

【BLOCKED-OWNER BO-P9-15】 `WindowsProbe` 於真實 COM 上的釋放判定實測，前置於本步驟的 `ab run`（`windows_com` 資源）。

Windows L1（`S3-L1-RELEASE-CONTRACT-WINDOWS`）依 `tests/acceptance/l1_required.lock.json` 的 `s3_release_contract.windows`（selector `tests/contract_blackbox`、`markexpr` 為 `core_portable or windows_native`）於 Windows host 執行，同樣同時設定 `SERIALWRAP_CLI_EXE` 與 `SERIALWRAP_DAEMON_EXE` 指向受測安裝與 `SW_RESOURCE_REPORT`（同目錄的 `resource-report.json`），產生 `$JUNIT_WINDOWS_DIR/junit.xml`。Windows 以 PowerShell 設定同名環境變數後執行下列命令。

```bash
python -m pytest tests/contract_blackbox -m "core_portable or windows_native" --junitxml="$JUNIT_WINDOWS_DIR/junit.xml"
python3 -m tools.stage3 ab verify-manifest --manifest "$AB_WINDOWS_L15_MANIFEST" --repo-root .
python3 -m tools.stage3 ab run --manifest "$AB_WINDOWS_L15_MANIFEST" --out "$AB_OUT/windows-l15" --repo-root .
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind ab-report --item S3-L15-RELEASE-AB-WINDOWS --platform windows --report "$AB_OUT/windows-l15/ab-report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_WINDOWS_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind junit --item S3-L1-RELEASE-CONTRACT-WINDOWS --platform windows --report "$JUNIT_WINDOWS_DIR/junit.xml" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_WINDOWS_JSON"
```

### 步驟 (6)：L2 A/B

【BLOCKED-OWNER BO-P9-02】 Linux L2 Sequential A/B 須涵蓋 `serialwrap_regression` 全部適用案例（含破壞集）。manifest 的 `authorization` 必須列出授權決策與限定板子，否則 `verify-manifest` 回報違規 `destructive case without authorization: <case_id>`，`ab run` 以 `HARNESS_AUTHORIZATION_MISSING` 拒絕執行。

【BLOCKED-OWNER BO-P9-03】 Windows L2 適用案例 A/B 使用明確的 bench lock 路徑。

```bash
python3 -m tools.stage3 ab verify-manifest --manifest "$AB_LINUX_L2_MANIFEST" --repo-root .
python3 -m tools.stage3 ab run --manifest "$AB_LINUX_L2_MANIFEST" --out "$AB_OUT/linux-l2" --repo-root .
python3 -m tools.stage3 ab verify-manifest --manifest "$AB_WINDOWS_L2_MANIFEST" --repo-root .
python3 -m tools.stage3 ab run --manifest "$AB_WINDOWS_L2_MANIFEST" --out "$AB_OUT/windows-l2" --repo-root .
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind ab-report --item S3-L2-FULL-AB-LINUX --platform linux --report "$AB_OUT/linux-l2/ab-report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind ab-report --item S3-L2-AB-WINDOWS --platform windows --report "$AB_OUT/windows-l2/ab-report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_WINDOWS_JSON"
```

### 步驟 (7)：release candidate 的 L3 P0＋P1＋remote 集合

【BLOCKED-OWNER BO-P9-18】 Release candidate 的 L3 完整穩定性集合（P0＋P1＋remote，§11.5）分別在 Linux 與 Windows bench 執行，以 `realhw` 引擎產生報告，再以 `realhw-report` 登錄。

```bash
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind realhw-report --item S3-L3-P0P1-REMOTE-LINUX --platform linux --report "$REALHW_LINUX/report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind realhw-report --item S3-L3-P0P1-REMOTE-WINDOWS --platform windows --report "$REALHW_WINDOWS/report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_WINDOWS_JSON"
```

### 步驟 (8)：48h L3 A/B，Linux 再 Windows（依序）

【BLOCKED-OWNER BO-P9-16】 執行前須保留 A→B 之間的 bench 獨占，跨 TestPilot 套件與跨主機的排程由擁有者安排。

【BLOCKED-OWNER BO-P9-04】 Linux 48h A/B（Python 基線→Rust 候選）。`ab run` 在 B 側啟動前會呼叫 `od09_freeze.register_candidate_start`；未凍結即 `HARNESS_OD09_NOT_FROZEN`，B 側不執行。

```bash
python3 -m tools.stage3 od09 check --acceptance realhw/acceptance/longrun_acceptance.yaml --run-started-at "$LINUX_L3_STARTED_AT"
python3 -m tools.stage3 ab run --manifest "$AB_LINUX_L3_MANIFEST" --out "$AB_OUT/linux-l3" --repo-root .
```

【BLOCKED-OWNER BO-P9-05】 Windows 48h A/B 只能在 Linux 48h 結束且 bench 釋放後開始。

```bash
python3 -m tools.stage3 ab run --manifest "$AB_WINDOWS_L3_MANIFEST" --out "$AB_OUT/windows-l3" --repo-root .
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind ab-report --item S3-L3-AB-48H-LINUX --platform linux --report "$AB_OUT/linux-l3/ab-report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind ab-report --item S3-L3-AB-48H-WINDOWS --platform windows --report "$AB_OUT/windows-l3/ab-report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_WINDOWS_JSON"
```

### 步驟 (9)：Canary 兩平台並行累積

【BLOCKED-OWNER BO-P9-07】 Linux Canary：擁有者日常使用，14 個活躍日、≥5,000 次 `cmd submit`、≥50 次適用干擾／交接循環。collector 以 systemd user service 常駐，範本見 `canary.md`。

【BLOCKED-OWNER BO-P9-08】 Windows Canary：測試團隊單人使用的工作站真實使用（OD-12）。collector 以 `sw-canary.pyz` 於登入時執行，範本見 `canary.md`。Windows 不要求 `/dev/ttyMCU`。

```bash
python3 -m tools.stage3 canary collect --cli "$SERIALWRAP_CLI" --ledger-dir "$CANARY_LEDGER_LINUX" --policy tests/acceptance/canary_policy.json --platform linux --once
python3 -m tools.stage3 canary report --policy tests/acceptance/canary_policy.json --addenda docs/rust-refactor/owner-decisions-addenda.md --ledger "$CANARY_LEDGER_LINUX/ledger.ndjson" --platform linux --artifact-id "$LINUX_ARTIFACT_ID" --bundle-tree-sha256 "$LINUX_BUNDLE_TREE_SHA256" --runtime-tree-sha256 "$LINUX_RUNTIME_TREE_SHA256" --out "$CANARY_OUT/canary-linux.json"
```

`--bundle-tree-sha256` 與 `--runtime-tree-sha256` 取自 `evidence status` 顯示的 Stage 3 target 值，缺漏即 argparse 錯誤。Windows 報告同樣以其 target 值產生，兩平台不得合併。

### 步驟 (10)：回滾演練（含 D7、D8）

【BLOCKED-OWNER BO-P9-10】 Linux 實機回滾演練。破壞性：會停止並重啟 daemon、重新取得 UART。須擁有者授權與 bench 獨占，並以 `--i-understand-this-restarts-the-daemon` 明確確認。

【BLOCKED-OWNER BO-P9-11】 Windows 實機回滾演練（`windows-task`），同樣含 D7 與 D8。

```bash
python3 -m tools.stage3 rollback-drill run --cli "$SERIALWRAP_CLI" --platform linux --boards "$LINUX_BOARDS" --expect-current "$CURRENT_VERSION" --expect-previous "$PREVIOUS_VERSION" --out "$DRILL_OUT/linux" --corrupt-from "$CURRENT_BUNDLE" --start-fail-from "$START_FAIL_SRC" --i-understand-this-restarts-the-daemon
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind drill-report --item S3-ROLLBACK-LINUX --platform linux --report "$DRILL_OUT/linux/drill-report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
python3 -m tools.stage3 rollback-drill run --cli "$WINDOWS_CLI" --platform windows --boards "$WINDOWS_BOARDS" --expect-current "$CURRENT_VERSION" --expect-previous "$PREVIOUS_VERSION" --out "$DRILL_OUT/windows" --corrupt-from "$WINDOWS_CURRENT_BUNDLE" --start-fail-from "$START_FAIL_SRC_WINDOWS" --i-understand-this-restarts-the-daemon
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind drill-report --item S3-ROLLBACK-WINDOWS --platform windows --report "$DRILL_OUT/windows/drill-report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_WINDOWS_JSON"
```

`--corrupt-from` 指向受測 Current bundle（Linux 為 `$CURRENT_BUNDLE`，Windows 為 `$WINDOWS_CURRENT_BUNDLE`；D7 的損毀副本由演練在 `--out/corrupt` 產生）。`--start-fail-from` 指向 `mkbundle.sh --dev --version-override` 建出的版本目錄 `$START_FAIL_SRC`（即 `$START_FAIL_OUT/versions/$NEWER_VERSION`，見 `rollback-drill.md` 第 2 節；D8 的副本在 `--out/startfail` 產生）。`make-corrupted`、`make-start-fail` 只是選用的檢視命令，不是前置步驟。

Windows 的 `$START_FAIL_SRC_WINDOWS` 須由擁有者於 Windows 自架 runner 以原生流程準備（`mkbundle.sh` 目前拒絕 Windows 目標，見 BO-P9-17）。未準備即為阻塞，不得以手工組裝的目錄代替。

缺少 `--corrupt-from` 或 `--start-fail-from` 時，演練結果為 `INCOMPLETE`（`limitations` 含 `step1_refusal_not_exercised` 或 `step7_revert_not_exercised`），不得登錄為 `S3-ROLLBACK-*` 的 PASS。詳見 `rollback-drill.md`。

### 步驟 (11)：事故結案

【BLOCKED-OWNER BO-P9-09】 由擁有者或證據責任人分診所有未結案事故（含 `unexplained_pid_change`、`collector_gap`、`harvest_integrity` 與自動偵測的 `safety_violation`）。未結案事故阻擋 `canary report` 的 PASS，且阻擋退役。分診表見 `canary.md`。

```bash
python3 -m tools.stage3 canary incident list --ledger "$CANARY_LEDGER_LINUX/ledger.ndjson" --policy tests/acceptance/canary_policy.json --platform linux
python3 -m tools.stage3 canary incident close --ledger-dir "$CANARY_LEDGER_LINUX" --policy tests/acceptance/canary_policy.json --id "$INCIDENT_ID" --resolution fixed_in_artifact --issue-ref "$ISSUE_REF" --note "$CLOSE_NOTE" --closed-by "$CLOSED_BY"
```

`safety_violation` 只能以 `fixed_in_artifact` 或 `duplicate` 結案，不得以環境或非產品缺陷豁免。

結案後重新產生 Canary 報告（`canary report` 只以帳本最新版本判定，見 `evidence-index.md` 第 8 節），並以同一份報告登錄 `S3-CANARY-*` 與 `S3-INCIDENTS-CLOSED`。兩平台各做一次：

```bash
python3 -m tools.stage3 canary report --policy tests/acceptance/canary_policy.json --addenda docs/rust-refactor/owner-decisions-addenda.md --ledger "$CANARY_LEDGER_LINUX/ledger.ndjson" --platform linux --artifact-id "$LINUX_ARTIFACT_ID" --bundle-tree-sha256 "$LINUX_BUNDLE_TREE_SHA256" --runtime-tree-sha256 "$LINUX_RUNTIME_TREE_SHA256" --out "$CANARY_OUT/canary-linux.json"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind canary-report --item S3-CANARY-LINUX --platform linux --report "$CANARY_OUT/canary-linux.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind canary-report --item S3-INCIDENTS-CLOSED --platform linux --report "$CANARY_OUT/canary-linux.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
python3 -m tools.stage3 canary report --policy tests/acceptance/canary_policy.json --addenda docs/rust-refactor/owner-decisions-addenda.md --ledger "$CANARY_LEDGER_WINDOWS/ledger.ndjson" --platform windows --artifact-id "$WINDOWS_ARTIFACT_ID" --bundle-tree-sha256 "$WINDOWS_BUNDLE_TREE_SHA256" --runtime-tree-sha256 "$WINDOWS_RUNTIME_TREE_SHA256" --out "$CANARY_OUT/canary-windows.json"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind canary-report --item S3-CANARY-WINDOWS --platform windows --report "$CANARY_OUT/canary-windows.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_WINDOWS_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind canary-report --item S3-INCIDENTS-CLOSED --platform windows --report "$CANARY_OUT/canary-windows.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_WINDOWS_JSON"
```

### 步驟 (12)：全部出口項以 ingest 登錄，確認 Stage 3 為 PASS

【BLOCKED-OWNER BO-P9-14】 Stage 0～2.4 所有 `owner_blocked:true` 出口項的證據產出與登錄，由各階段的擁有者證據完成。登錄後逐階段確認狀態：

```bash
python3 -m tools.stage3 evidence status --index docs/rust-refactor/evidence/index.json --stage 0 --evidence-root "$EVIDENCE_ROOT"
python3 -m tools.stage3 evidence status --index docs/rust-refactor/evidence/index.json --stage 1A --evidence-root "$EVIDENCE_ROOT"
python3 -m tools.stage3 evidence status --index docs/rust-refactor/evidence/index.json --stage 1B --evidence-root "$EVIDENCE_ROOT"
python3 -m tools.stage3 evidence status --index docs/rust-refactor/evidence/index.json --stage 2.1 --evidence-root "$EVIDENCE_ROOT"
python3 -m tools.stage3 evidence status --index docs/rust-refactor/evidence/index.json --stage 2.2 --evidence-root "$EVIDENCE_ROOT"
python3 -m tools.stage3 evidence status --index docs/rust-refactor/evidence/index.json --stage 2.3 --evidence-root "$EVIDENCE_ROOT"
python3 -m tools.stage3 evidence status --index docs/rust-refactor/evidence/index.json --stage 2.4 --evidence-root "$EVIDENCE_ROOT"
python3 -m tools.stage3 evidence status --index docs/rust-refactor/evidence/index.json --stage 3 --evidence-root "$EVIDENCE_ROOT"
python3 -m tools.stage3 evidence report --index docs/rust-refactor/evidence/index.json --evidence-root "$EVIDENCE_ROOT"
```

Stage 3 的 `evidence status` 須為 PASS 才能進入步驟 (13)。本 Phase 結束時的預期結果為 BLOCKED，PR 描述須附該輸出，不得以手動方式改為 PASS。

### 步驟 (13)：退役裁決

【BLOCKED-OWNER BO-P9-13】 Stage 3 證據 PASS 後，擁有者將 `packaging/python_daemon_retirement.json` 改為 `retired:true`，並記錄兩平台 target artifact id。欄位與核對方式見 `python-daemon-retirement.md`。

```bash
python3 -m tools.stage3 retirement github-output
```

### 步驟 (14)：release workflow 原樣發佈受測 bundle

release workflow 以 `retirement verify --mode release` 核對 manifest、逐 component 與整個 bundle 檔案樹，再以 `retirement pack` 封裝，並以 `retirement verify-archive` 從最終 archive 位元組重驗、產生 `SHA256SUMS`。只上傳通過驗證的那份位元組，不重建受測物。完整命令與理由見 `python-daemon-retirement.md`。

Windows 自架 runner 的 bundle job 屬 BO-P9-17，於步驟 (3) 已先行處理。

## 4. 證據有效性規則

- 不得直接合併不同 artifact、不同內容指紋或不同平台的執行量。
- 同一 manifest 不得換內容重設 target。bundle 內容改變時必須重建，舊證據過期。
- `reuse_of` 只在 `runtime_digest` 相同且 `runtime_tree_sha256` 相等時可用。Stage 2.4 長跑供 Stage 3 沿用時，其 target 必須以 `--bundle-dir` 設定。
- 報告的內容指紋由產生端於執行前後量測，ingest 缺值即拒收，不以 target 值回填。
- 每次重跑使用新的 `run_id` 與新的 `--out`。不得改 `release.timeout_s` 後重跑同一 run_id。

各步驟的判定細節：A/B 見 `sequential-ab.md`，Canary 見 `canary.md`，回滾演練見 `rollback-drill.md`，證據索引與 ingest 見 `evidence-index.md`，退役與發佈見 `python-daemon-retirement.md`。
