# Stage 0～3 證據索引與 ingest 程序

本程序說明出口項目錄、各 `evidence_kind` 的 `evidence ingest` 命令、entry 欄位、受測內容指紋規則與各類判定語意。證據索引位於 `docs/rust-refactor/evidence/index.json`；出口目錄為 `tests/acceptance/stage_exits.json`（schema_version 3，共 67 項）。

PASS 只能由 `evidence ingest` 從產生工具的報告推導並登錄。人工宣稱、手寫 entry、或由 target 值回填，一律不算證據。

## 1. 出口項目錄

【BLOCKED-OWNER BO-P9-14】 下表中「擁有者阻擋」為「是」的全部項目，其證據由各階段擁有者於 bench、Windows host 或指定時間產出，再以 `evidence ingest` 登錄。本程序與 Agent 不得代為產出，也不得標為 PASS。

下表由 `tests/acceptance/stage_exits.json` 產生。`擁有者阻擋` 為「是」的項目需要擁有者的硬體、時間或裁決，本計畫不得產出其 PASS（見 `stage3-runbook.md` 與計畫 §0.2）。`候選長跑` 為「是」的項目，其 OD-09 門檻必須在候選開跑前已凍結。

| 項目 ID | 階段 | 平台 | 層級 | evidence_kind | 擁有者阻擋 | 候選長跑 |
|---|---|---|---|---|---|---|
| S0-L1-BC | 0 | both | L1 | junit | 否 | 否 |
| S0-L1-WIN-PYTEST | 0 | windows | L1 | junit | 否 | 否 |
| S0-L15-BRIDGE | 0 | linux | L1.5 | bridge-selftest | 否 | 否 |
| S0-L15-FUNCTEST | 0 | linux | L1.5 | functest-report | 否 | 否 |
| S0-WIN-NATIVE-SERIAL | 0 | windows | L1.5 | functest-report | 是 | 否 |
| S0-L2-NONDESTRUCTIVE | 0 | linux | L2 | regression-report | 是 | 否 |
| S0-L2-F12-ORACLE | 0 | linux | L2 | regression-report | 是 | 否 |
| S0-L2-BC02-BC03 | 0 | linux | L2 | regression-report | 是 | 否 |
| S0-L3-P0P1 | 0 | linux | L3 | realhw-report | 是 | 否 |
| S0-L3-BASELINE-LINUX-48H | 0 | linux | L3 | gate-report | 是 | 否 |
| S0-L3-BASELINE-WINDOWS-48H | 0 | windows | L3 | gate-report | 是 | 否 |
| S0-OD09-DEFERRED-FROZEN | 0 | both | gate | od09-freeze | 是 | 否 |
| S0-SPIKE2-WINTASK-72H | 0 | windows | spike | manual-procedure | 是 | 否 |
| S0-SPIKE1-WIN-VCOM | 0 | windows | spike | manual-procedure | 是 | 否 |
| S0-SPIKE3-WIN-WAL-EDR | 0 | windows | spike | manual-procedure | 是 | 否 |
| S1A-L1-BLACKBOX-LINUX | 1A | linux | L1 | junit | 否 | 否 |
| S1A-L1-BLACKBOX-WINDOWS | 1A | windows | L1 | junit | 否 | 否 |
| S1A-L1-DELEGATION | 1A | both | L1 | junit | 否 | 否 |
| S1A-L15-DATAPLANE | 1A | linux | L1.5 | functest-report | 否 | 否 |
| S1A-L2-DATAPLANE | 1A | linux | L2 | regression-report | 是 | 否 |
| S1A-L3-P0P1 | 1A | linux | L3 | realhw-report | 是 | 否 |
| S1B-L1-20GROUPS | 1B | both | L1 | junit | 否 | 否 |
| S1B-L15-INSTALL-ROLLBACK | 1B | linux | L1.5 | functest-report | 否 | 否 |
| S1B-PERF | 1B | both | perf | manual-procedure | 否 | 否 |
| S1B-WIN-TASK-SUPERVISE | 1B | windows | supervisor | manual-procedure | 是 | 否 |
| S1B-L2-FULL | 1B | linux | L2 | regression-report | 是 | 否 |
| S1B-L3-P0P1-REMOTE | 1B | linux | L3 | realhw-report | 是 | 否 |
| S1B-L3-MIXED-48H-WINDOWS | 1B | windows | L3 | gate-report | 是 | 是 |
| S1B-ROLLBACK-HW | 1B | both | hw | drill-report | 是 | 否 |
| S21-PERF-WAL | 2.1 | both | perf | manual-procedure | 是 | 否 |
| S21-L1-CORE | 2.1 | both | L1 | junit | 否 | 否 |
| S21-L15-SAFETY | 2.1 | linux | L1.5 | functest-report | 否 | 否 |
| S21-NATIVE-SERIAL-LINUX | 2.1 | linux | L1.5 | functest-report | 是 | 否 |
| S21-NATIVE-SERIAL-WINDOWS | 2.1 | windows | L1.5 | functest-report | 是 | 否 |
| S21-L2-F1-F2-F3-F4-F12 | 2.1 | linux | L2 | regression-report | 是 | 否 |
| S21-L3-P0-CORE-P1 | 2.1 | linux | L3 | realhw-report | 是 | 否 |
| S22-PERF-CONSOLE-ECHO | 2.2 | both | perf | manual-procedure | 是 | 否 |
| S22-L15-CONSOLE | 2.2 | linux | L1.5 | functest-report | 否 | 否 |
| S22-WIN-NATIVE-CONSOLE | 2.2 | windows | L1.5 | functest-report | 是 | 否 |
| S22-L2-F5-F6-F8 | 2.2 | linux | L2 | regression-report | 是 | 否 |
| S22-L3-CONSOLE-P1 | 2.2 | linux | L3 | realhw-report | 是 | 否 |
| S23-L15-LOGIN-EVENT | 2.3 | linux | L1.5 | functest-report | 否 | 否 |
| S23-L2-F9-F10-F11 | 2.3 | linux | L2 | regression-report | 是 | 否 |
| S23-L3-LOGIN-REMOTE | 2.3 | linux | L3 | realhw-report | 是 | 否 |
| S24-L15-ALL-CONTRACTS-LINUX | 2.4 | linux | L1.5 | functest-report | 否 | 否 |
| S24-L15-ALL-CONTRACTS-WINDOWS | 2.4 | windows | L1.5 | functest-report | 是 | 否 |
| S24-L2-ALL-38 | 2.4 | linux | L2 | regression-report | 是 | 否 |
| S24-P2-1-MCU-FLASH | 2.4 | linux | manual | manual-procedure | 是 | 否 |
| S24-L3-P0P1-REMOTE | 2.4 | linux | L3 | realhw-report | 是 | 否 |
| S24-L3-LRMIXED-48H-LINUX | 2.4 | linux | L3 | gate-report | 是 | 是 |
| S24-L3-LRMIXED-48H-WINDOWS | 2.4 | windows | L3 | gate-report | 是 | 是 |
| S3-L1-RELEASE-CONTRACT-LINUX | 3 | linux | L1 | junit | 否 | 否 |
| S3-L1-RELEASE-CONTRACT-WINDOWS | 3 | windows | L1 | junit | 否 | 否 |
| S3-L15-RELEASE-AB-LINUX | 3 | linux | L1.5 | ab-report | 否 | 否 |
| S3-L15-RELEASE-AB-WINDOWS | 3 | windows | L1.5 | ab-report | 是 | 否 |
| S3-L2-FULL-AB-LINUX | 3 | linux | L2 | ab-report | 是 | 否 |
| S3-L2-AB-WINDOWS | 3 | windows | L2 | ab-report | 是 | 否 |
| S3-L3-P0P1-REMOTE-LINUX | 3 | linux | L3 | realhw-report | 是 | 否 |
| S3-L3-P0P1-REMOTE-WINDOWS | 3 | windows | L3 | realhw-report | 是 | 否 |
| S3-L3-AB-48H-LINUX | 3 | linux | L3 | ab-report | 是 | 是 |
| S3-L3-AB-48H-WINDOWS | 3 | windows | L3 | ab-report | 是 | 是 |
| S3-CANARY-LINUX | 3 | linux | canary | canary-report | 是 | 否 |
| S3-CANARY-WINDOWS | 3 | windows | canary | canary-report | 是 | 否 |
| S3-ROLLBACK-LINUX | 3 | linux | hw | drill-report | 是 | 否 |
| S3-ROLLBACK-WINDOWS | 3 | windows | hw | drill-report | 是 | 否 |
| S3-INCIDENTS-CLOSED | 3 | both | gate | canary-report | 是 | 否 |
| S3-OD09-ALL-FROZEN | 3 | both | gate | od09-freeze | 是 | 否 |

`both` 項目須**每個平台各自**有 entry。索引判定（`_item_status_full`）會逐平台檢查 target 與 entry，缺任一平台即不是 SATISFIED。

## 2. evidence_kind 與 ingest 命令

每個 kind 的 `evidence ingest` 皆需要以下參數：`--index`、`--kind`、`--item`、`--platform`、`--report`、`--evidence-root`、`--environment`。`--release-manifest` 與 `--reuse-of` 依項目需要提供。

```bash
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind junit --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind functest-report --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind bridge-selftest --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind regression-report --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind realhw-report --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind gate-report --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind ab-report --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind canary-report --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind drill-report --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind od09-freeze --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind manual-procedure --item "$ITEM" --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON"
```

各 kind 的報告來源：

| kind | 報告來源 | 備註 |
|---|---|---|
| `junit` | L1 pytest 的 `junit.xml`；ingest 讀取同目錄的 `resource-report.json`（`SW_RESOURCE_REPORT`） | 依 `required_l1_set` 完整性判定，見第 5 節 |
| `functest-report` | `func-test/runner.py --report-dir DIR` 的**整個報告目錄**（不是單一 run 的檔案）。`_derive_functest` 以 `rglob("manifest.json")` 走訪每個 case run，各 run 的 `manifest.json` 須含 `artifacts.deployed_content`；不使用 `SW_RESOURCE_REPORT` | 每個適用案例都須有 run；缺案即 INSUFFICIENT |
| `bridge-selftest` | `tools/uart_stdio_bridge` 的 `uart_stdio_bridge.selftest.runner` 產生的自測報告（不屬於 func-test） | 不執行 serialwrap artifact，故不量測內容指紋；`od09-freeze` 亦同 |
| `regression-report` | TestPilot `serialwrap_regression` 的 `report.json` | 含 `meta.deployed_content` |
| `realhw-report` | `realhw` 的 `report.json`（P0＋P1＋remote 集合） | 含 `meta.deployed_content` |
| `gate-report` | `realhw.acceptance.gate` 的 `release-gate.json` | 含 `run.deployed_content`，`engine_result` 原樣保留 |
| `ab-report` | `tools/stage3` A/B 的 `ab-report.json` | 含各側 `bundle_tree_sha256`、`runtime_tree_sha256` |
| `canary-report` | `canary report --out` 的 JSON | 須含 `ledger_through`、`ledger_records` 與兩個 tree 值 |
| `drill-report` | `rollback-drill run` 產生的報告 | verdict 須為 PASS 才能給 PASS |
| `od09-freeze` | P2 `realhw.acceptance.od09_freeze freeze --out` 的 `od09-freeze.json` | 唯讀推導，不寫入門檻檔 |
| `manual-procedure` | 手動程序紀錄（欄位見下方小節） | 前後各量測一次，見第 4 節 |

### manual-procedure 報告欄位

`manual-procedure` 的報告為 JSON 物件，由 `_derive_manual` 推導；缺少任一必要欄位或格式不符即以 `HARNESS_EVIDENCE_INGEST_INVALID` 拒收：

| 欄位 | 內容 |
|---|---|
| `procedure_path` | repo 內 `docs/rust-refactor/procedures/` 下現行程序檔路徑（可為絕對路徑，或相對於報告檔所在目錄）；ingest 會解析 symlink 後確認仍位於此目錄 |
| `procedure_sha256` | 程序檔的 sha256；ingest 時重算，與現行程序檔不符即拒收 |
| `executed_by` | 執行者（非空字串） |
| `artifacts` | 陣列；每項為檔案路徑字串，或含 `path` 的物件。所列檔案必須存在，並會被複製入證據目錄 |
| `result` | `PASS`、`FAIL`、`SKIP`、`XFAIL`、`XPASS`、`INFRA_ERROR` 或 `INSUFFICIENT` |
| `executed_at` | 含時區的 ISO 8601 時間 |
| `deployed_content` | 物件，含 `start`、`end` 兩次 `python3 -m realhw.deployed_content` 量測結果，以及 `daemon_live`（前後各一筆執行中 daemon 的映像量測），格式與收集方式見第 4 節；target 記有 tree 時不得為 `null` |

## 3. entry 欄位

`evidence add --entry JSON_PATH` 接受完整的 entry JSON，必須含以下所有欄位（`EvidenceEntry`）：

`entry_id`、`item_id`、`platform`、`result`、`artifact_id`、`runtime_digest`、`bundle_tree_sha256`、`runtime_tree_sha256`、`contract_revision`、`tool_revision`、`case_list_sha256`、`l1_set_sha256`、`workload_sha256`、`environment_id`、`produced_at`、`started_at`、`producer`、`files`（陣列，每項 `path` 與 `sha256`）、`xfail_cases`（陣列）、`reuse_of`、`note`、`ledger_through`、`ledger_records`。

重點欄位：

- `environment_id`：由環境 JSON 計算，見下方。
- `workload_sha256`：由 ingest 從長跑報告推導的 workload 指紋，長跑 gate 項目使用。
- `runtime_digest`：由 release manifest 的 runtime 組成計算。
- `bundle_tree_sha256`、`runtime_tree_sha256`：受測內容指紋，見第 4 節。
- `l1_set_sha256`：L1 項目的必要 nodeid 集合指紋，僅 `junit` 項目有值。
- `producer`：`ingest:<kind>`（ingest 產生），或 `manual`（僅 manual-procedure 可用）。

環境 JSON 必須恰好含以下 8 個欄位，缺一或多一即拒收：

```json
{"os_build":"…","cpu":"…","ram_mb":16384,"filesystem":"…","av_product":"…","drivers":"…","board_firmware":"…","toolchains":"…"}
```

```bash
python3 -m tools.stage3 evidence env-id --env "$ENV_JSON"
```

## 4. 受測內容指紋規則

受測內容指紋（`bundle_tree_sha256`、`runtime_tree_sha256`）是判定證據是否屬於目前 target 的依據。

- target 以 `evidence target` 設定。`--stage 3` 時 `--bundle-dir` 必填，並凍結兩個 tree 值。同一 manifest 若 tree 不同，`evidence target` 以 `HARNESS_ARTIFACT_MISMATCH` 拒絕，不寫入索引。
- bundle 內容改變時必須重建，舊證據隨之過期。同一 manifest 不得換內容重設 target。
- Stage 2.4 長跑要供 Stage 3 `reuse_of` 沿用時，其 target 必須以 `--bundle-dir` 設定（R3-F2）。
- ab-report、canary-report、drill-report 的指紋由產生端對**實際部署內容**量測，缺值即拒收，不以 target 值回填（R4-F1）。
- junit、functest、TestPilot、realhw、`release-gate.json` 與手動程序同樣由產生端以 `realhw.deployed_content` 在執行前後量測實際使用的安裝。跑黑箱 L1 時必須**同時**設定 `SERIALWRAP_CLI_EXE` 與 `SERIALWRAP_DAEMON_EXE`，指向受測安裝的 CLI 與 `bin/serialwrapd`。只設前者時 harness 會改用 repo 的 Python daemon，報告記 `daemon_env_missing` 而被拒收。
- 手動程序前後各執行一次量測。量測同時收集執行中 daemon 的映像（`--daemon-pid`），寫入 `<out>.live-daemon.json`。`daemon_live` 缺少時，ingest 的指紋推導為空，target 記有 tree 時以 `report lacks deployed tree` 拒收：

```bash
DAEMON_PID="$(serialwrap daemon status | python3 -c 'import json,sys; print(json.load(sys.stdin)["pid"])')"
python3 -m realhw.deployed_content --cli "$SERIALWRAP_CLI" --daemon "$SERIALWRAP_DAEMON_EXE" --daemon-pid "$DAEMON_PID" --out "$MEASURE_BEFORE"
# 在此執行手動程序
python3 -m realhw.deployed_content --cli "$SERIALWRAP_CLI" --daemon "$SERIALWRAP_DAEMON_EXE" --daemon-pid "$DAEMON_PID" --out "$MEASURE_AFTER"
```

- 合併為 `deployed_content`：`start` 與 `end` 各為一次量測的 JSON，`daemon_live` 為前後兩筆 `<out>.live-daemon.json`。合併後的物件寫入手動程序紀錄的 `deployed_content` 欄位：

```bash
python3 - "$MEASURE_BEFORE" "$MEASURE_AFTER" > "$DEPLOYED_CONTENT.json" <<'PY'
import json
import sys

before, after = sys.argv[1], sys.argv[2]


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


merged = {
    "start": load(before),
    "end": load(after),
    "daemon_live": [load(before + ".live-daemon.json"), load(after + ".live-daemon.json")],
}
print(json.dumps(merged, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
```

- target 記有 tree 時，報告缺值、前後不一、`daemon_live` 缺漏或與 target 不符，一律拒收。不再以報告開始時刻放行。

## 5. L1（junit）完整性規則

L1 出口一律以事先凍結的必要 nodeid 集合為分母（`l1_required.lock.json` 的 `sets.<set>.<platform>`），經 `tests/acceptance/l1_judge.py` 的 `judge` 判定（唯一判定實作，`evidence ingest --kind junit` 依路徑載入）。

- 依 selector 執行，並帶 `SW_RESOURCE_REPORT`。黑箱 L1 的命令（`s3_release_contract` 為例；其他集合依 `l1_required.lock.json` 的 `selector` 與 `markexpr` 替換）：

```bash
SERIALWRAP_CLI_EXE="$SERIALWRAP_CLI" SERIALWRAP_DAEMON_EXE="$BUNDLE/bin/serialwrapd" SW_RESOURCE_REPORT="$JUNIT_DIR/resource-report.json" python3 -m pytest tests/contract_blackbox -m "core_portable or posix_only" --junitxml="$JUNIT_DIR/junit.xml"
```

  Windows 以 `-m "core_portable or windows_native"` 執行。`--report` 指向 `$JUNIT_DIR/junit.xml`，ingest 讀取同目錄的 `resource-report.json`。
- 缺案、重複、必要案例任何 SKIP／deselected／未執行、runner exit code 非 0 或未完成，一律 INSUFFICIENT。
- 不得以「測試數 > 0、無 failure」或「passed ≥ N」判定 PASS。
- 集合只增不減。集合增加後，舊 entry 的 `l1_set_sha256` 不符，過期須重跑。

## 6. 結果語意

| 結果 | 意義 |
|---|---|
| PASS | 由 ingest 從產生工具報告推導，且 target、指紋、檔案雜湊、案例 sentinel 皆相符 |
| FAIL | 產品或安全不變量失敗。目前 target 下有效的 FAIL 紀錄不可被後續 PASS 抵銷 |
| XFAIL | 只有 `cases.yaml` 中 sentinel 已核准且未過期的案例可為 XFAIL。XFAIL 由 ingest 推導並記入 entry 的 `xfail_cases`。`evidence add` 的 XFAIL 只接受 manual-procedure 項目 |
| SKIP | 案例未執行。不等於 PASS |
| INFRA_ERROR | 基礎設施失敗。不等於產品 FAIL，也不等於 PASS，且不阻擋後續有效 PASS |
| INSUFFICIENT | 證據不足（例如 L1 集合不完整）。不等於 PASS |
| XPASS | 已標為 XFAIL 的案例意外通過，須人工審查 |

item 狀態（`evidence status` 輸出）：

| 狀態 | 意義 |
|---|---|
| SATISFIED | 目前 target 下有有效 entry |
| MISSING | 沒有對應的 entry，或 L1 集合未凍結 |
| STALE | entry 的 artifact、contract、tool、指紋、L1 集合等與 target 不符 |
| HASH_MISMATCH | 登錄後證據檔案的 sha256 不符 |
| NO_TARGET | 該 stage／platform 尚未設定 target |
| FAIL | 任一平台判定為 FAIL |

階段結果：`evidence status --stage S` 的 `result` 為 PASS（所有項目皆 SATISFIED）、FAIL（任一項目 FAIL）或 BLOCKED（其他）。本 Phase 結束時預期 Stage 3 為 BLOCKED。

## 7. target 與 reuse_of

```bash
python3 -m tools.stage3 evidence target --index docs/rust-refactor/evidence/index.json --stage 3 --platform linux --release-manifest "$LINUX_BUNDLE/release-manifest.json" --contract-revision "$CONTRACT_REV" --tool-revision "$TOOL_REV" --case-list-sha256 "$CASE_LIST_SHA256" --set-by "$SET_BY" --bundle-dir "$LINUX_BUNDLE"
```

`reuse_of` 的條件：

- 被沿用的 entry 必須存在於索引。
- `runtime_digest` 必須相同，且 `runtime_tree_sha256` 必須相等（R3-F2）。
- 不同 artifact、不同內容指紋或不同平台的執行量不得自動合併（§12.2）。

```bash
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind ab-report --item S3-L3-AB-48H-LINUX --platform linux --report "$REPORT" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_JSON" --reuse-of "$ENTRY_ID"
```

## 8. canary-report 的版本判定

- canary-report 項目只以帳本版本最新的報告判定（R3-F1）。
- 較新的報告若顯示未結案事故，會使新報告退回 INCOMPLETE，並撤銷舊 PASS。
- 舊報告不得重新匯入。ingest 會拒絕比已登錄 entry 更舊的 canary 報告。
- `ledger_through`、`ledger_records` 由 `canary report --out` 產生，ingest 以它們判定版本。

## 9. 報告與 satisfying_entries

```bash
python3 -m tools.stage3 evidence report --index docs/rust-refactor/evidence/index.json --evidence-root "$EVIDENCE_ROOT"
```

`evidence report` 輸出 Markdown。每個 stage 一張表，列出項目、平台、狀態與說明。owner_blocked 且未 SATISFIED 的項目顯示為 BLOCKED-OWNER。

`evidence status` 的每個項目含 `satisfying_entries`，列出支持該判定的 entry。BC／SC／DF 與錯誤碼由 IMPLEMENTED 改為 VERIFIED 的擁有者步驟（規格 §0.3），應以 `satisfying_entries` 作為依據，由擁有者核對後執行。

## 10. add 與 ingest 的分工

- PASS 與 XFAIL 的唯一登錄入口是 `evidence ingest`。
- `evidence add` 只接受 `manual-procedure` 項目的完整 entry（`producer` 為 `manual`）。其他項目一律走 `ingest`，`add` 會以 `HARNESS_EVIDENCE_INGEST_INVALID` 拒絕。
- `add` 的 entry 必須附 `files`（PASS 與 XFAIL 必須有證據檔案）。

```bash
python3 -m tools.stage3 evidence add --index docs/rust-refactor/evidence/index.json --entry "$ENTRY_JSON"
```

## 11. 驗證

索引結構的 schema 為 `tests/acceptance/stage3_evidence_index.schema.json`。`evidence status` 會在 `--evidence-root` 下重算證據檔的 sha256，不符即 `HASH_MISMATCH`。
