# OD-09 延後項凍結程序（Stage 0 出口）

本程序對應 spec §14.2、§15.11.4 與 OD-09，由擁有者於**正式 Stage 0 出口**執行一次。它是 `realhw/acceptance/longrun_acceptance.yaml` 延後項（`resources`、`min_effective_load`、`comparison_uncertainty`）的唯一寫入路徑；不得手改門檻檔。

凍結必須早於任何 Rust 候選長跑。最早的候選長跑是 `S1B-L3-MIXED-48H-WINDOWS`（Stage 1B），因此凍結與兩平台基線核對必須在它開跑前完成。

## 前置條件

1. 兩平台 48 h Python 基線（B8）已完成，且各自的 `release-gate.json` 已登錄於 `tests/acceptance/stage0_evidence_index.yaml`：`S0-L3-BASELINE-LINUX-48H`、`S0-L3-BASELINE-WINDOWS-48H` 的 status 為 `present`，sha256 與檔案相符。
2. 基線以凍結前的同一 acceptance revision 判定（`acceptance.sha256` 與現行門檻檔一致），且 `run.ended_at_utc` 早於凍結時點。
3. `realhw/acceptance/candidate_longrun_starts.ndjson` 為空，且檔案必須存在（缺檔時 `freeze` 拒絕執行，不以空 ledger 略過時序守門）。只要有任何候選長跑登記（含無法解析的行），`freeze` 一律拒絕（`HARNESS_OD09_TOO_LATE`），以防止回退後事後重凍。

## 步驟

以下變數由擁有者設定：`OD09_DIR` 為本次凍結的工作目錄；`EVIDENCE_ROOT` 為 Stage 0 證據根目錄，與 `tests/acceptance/check_evidence_index.py --root` 為同一根目錄（預設為 repo 根目錄），索引中的 `artifact_path`（例如 `evidence/stage0/...`）一律相對於它解析；`LINUX_WAL_DIR`、`WINDOWS_WAL_DIR` 為兩平台基線保存時的 WAL 目錄（gate 的安全檢查需要；缺漏時四個 `safety_*` 檢查會 `INSUFFICIENT_EVIDENCE`）；其餘各自指向兩平台基線的 case 目錄、release manifest 與 `release-gate.json`。

1. 計算兩平台基線統計（違規即 exit 1 且不寫出統計檔，須先釐清再繼續）：

```bash
python3 -m realhw.acceptance.od09_freeze stats --case-dir "$LINUX_CASE_DIR" --platform linux --release-manifest "$LINUX_MANIFEST" --out "$OD09_DIR/stats-linux.json"
python3 -m realhw.acceptance.od09_freeze stats --case-dir "$WINDOWS_CASE_DIR" --platform windows --release-manifest "$WINDOWS_MANIFEST" --out "$OD09_DIR/stats-windows.json"
```

2. 依固定公式產生提案（公式即 `proposal_rules`，不得在看到基線後修改）：

```bash
python3 -m realhw.acceptance.od09_freeze propose --linux-stats "$OD09_DIR/stats-linux.json" --windows-stats "$OD09_DIR/stats-windows.json" --baseline-gate linux="$LINUX_BASELINE_GATE" --baseline-gate windows="$WINDOWS_BASELINE_GATE" --out "$OD09_DIR/proposal.json"
```

3. 擁有者核准後執行凍結。`freeze` 會一次寫入兩平台的值、改寫 `revision`、附加 addenda 列，並寫出報告。需要覆寫單一欄位時，以重複的 `--set <key>.per_platform.<os>.<field>=VALUE` 指定（例如 `--set resources.per_platform.linux.rss_peak_kb=60000`）；覆寫會原樣記入 `overrides`。`--now` 可省略（預設為現在 UTC）：

```bash
python3 -m realhw.acceptance.od09_freeze freeze --acceptance realhw/acceptance/longrun_acceptance.yaml --proposal "$OD09_DIR/proposal.json" --decision-id "$DECISION_ID" --approved-by "$APPROVED_BY" --stage0-index tests/acceptance/stage0_evidence_index.yaml --evidence-root "$EVIDENCE_ROOT" --addenda docs/rust-refactor/owner-decisions-addenda.md --out "$OD09_DIR/od09-freeze.json"
```

4. 以新 revision 離線重跑兩平台基線 gate，輸出到工作目錄（不覆寫已登錄的基線證據），供候選 `--baseline-gate` 引用。Task 15 的 `baseline_compatible` 不要求 revision 相同：

```bash
python3 -m realhw.acceptance.gate --report-dir "$LINUX_REPORT_DIR" --case-dir "$LINUX_CASE_DIR" --acceptance realhw/acceptance/longrun_acceptance.yaml --platform linux --mode baseline --observer "$LINUX_OBSERVER" --wal-dir "$LINUX_WAL_DIR" --out "$OD09_DIR/baseline-rerun-linux.json"
python3 -m realhw.acceptance.gate --report-dir "$WINDOWS_REPORT_DIR" --case-dir "$WINDOWS_CASE_DIR" --acceptance realhw/acceptance/longrun_acceptance.yaml --platform windows --mode baseline --observer "$WINDOWS_OBSERVER" --wal-dir "$WINDOWS_WAL_DIR" --excl-evidence "$WINDOWS_EXCL_DIR" --out "$OD09_DIR/baseline-rerun-windows.json"
```

5. 核對凍結已生效（違規即 exit 1）：

```bash
python3 -m realhw.acceptance.od09_freeze check --acceptance realhw/acceptance/longrun_acceptance.yaml --run-started-at "$RUN_STARTED_AT"
```

6. 把 `S0-OD09-DEFERRED-FROZEN` 項目改為 `status: present`，`artifact_path` 指向 `od09-freeze.json` 的登錄位置，並填入其 sha256。此後以 `tests/acceptance/check_evidence_index.py --stage 0` 核對索引。

## 候選長跑開跑登記

每一個 Rust 候選長跑在啟動 `python3 -m realhw --tier longrun` 之前，必須先登記。登記會檢查凍結時點早於 `--started-at`，並把紀錄 append 到 `realhw/acceptance/candidate_longrun_starts.ndjson`。未凍結者拒絕（`HARNESS_OD09_NOT_FROZEN`，exit 5），ledger 不變。適用項目限於 `S1B-L3-MIXED-48H-WINDOWS`、`S24-L3-LRMIXED-48H-LINUX`、`S24-L3-LRMIXED-48H-WINDOWS`、`S3-L3-AB-48H-LINUX`、`S3-L3-AB-48H-WINDOWS`：

```bash
python3 -m realhw.acceptance.od09_freeze register-candidate --acceptance realhw/acceptance/longrun_acceptance.yaml --item S1B-L3-MIXED-48H-WINDOWS --platform windows --run-id "$RUN_ID" --started-at "$RUN_STARTED_AT"
```

未登記的長跑仍會被 gate 的 `pre_frozen` 判定為 BLOCKED（凍結晚於 `meta.started_at_utc`），因此登記是開跑前的第二道防線，不得省略。

## 注意事項

- `freeze` 以 PyYAML 重寫門檻檔，檔內註解不會保留；修訂依據以 `revision`、`addenda` 列與 `od09-freeze.json` 為準。
- 凍結後不得以 `freeze` 重凍（`HARNESS_OD09_TOO_LATE`）。若需變更，須另行裁決並以新程序處理。
- 不得以手改 `realhw/acceptance/longrun_acceptance.yaml` 取代本程序。
- 凍結工具本身不判定任何數值是否合理，只檢查來源證據完整性與時序；數值的合理性由擁有者於核准時負責。
