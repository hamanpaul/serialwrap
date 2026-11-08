# realhw/acceptance — 長跑發版判定（P2-T15）

本目錄是長跑發版判定的外層套件，對應 spec §15.11.4、§14.2 與 OD-09／OD-10／OD-13。

- `longrun_acceptance.yaml`：**發版長跑門檻唯一來源**。其他前端不得另存一份數值。
- `longrun_acceptance.schema.json`：門檻檔結構契約。延後項以 `status` 一次凍結兩平台；`frozen` 時兩平台欄位必須非 null 且符合型別，`deferred` 時全部為 null。
- `gate.py`：外層 `release_gate_result` 判定器。`engine_result` 原樣保留引擎的 `lr-mixed` 結果；引擎 FAIL 不會被任何規則轉成 PASS。
- `observer.py`：唯讀長跑觀測器，只呼叫 `daemon status` 與 `session list`；Windows 另可週期性執行獨占探測。
- `walscan.py`：獨立 WAL 掃描器（stdlib-only），驗 crc32／len，計算 agent 重送、human tick 重放與禁止窗口內的 TX。
- `winexcl.py`：Windows 原生 COM 獨占開啟證據（`CreateFileW` 零 I/O 探測與 `handoff` 循環）。不呼叫任何讀寫或控制線 API。

本套件與引擎分離：引擎（`realhw/cases/longrun.py`）不得 import `gate.py`，也不得 import yaml。

執行期依賴：`gate.py` 需要 `PyYAML` 與 `jsonschema`（測試側同版本）；`observer.py`、`walscan.py`、`winexcl.py` 僅用 stdlib。

## 判定結果

| 結果 | 意義 |
|---|---|
| `PASS` | candidate 模式下所有檢查皆通過，且沒有延後項 BLOCKED |
| `FAIL` | 有明確違規（完成的長跑證據顯示錯誤率、雙重持有、未授權 TX、重啟等） |
| `INSUFFICIENT_EVIDENCE` | 證據不足（缺 WAL、觀測缺口、未釘住 CLI、無獨占證據等）；不得視為 PASS |
| `BLOCKED` | 延後項未凍結、凍結時點早於基線或晚於候選起點（僅 candidate 模式） |
| `NOT_APPLICABLE_BASELINE` | baseline 模式的結果；只代表絕對條件通過，不代表候選非退步 |

彙總順序：任一 FAIL → `FAIL`；否則任一 INSUFFICIENT → `INSUFFICIENT_EVIDENCE`；否則任一 BLOCKED → `BLOCKED`；否則 baseline 模式為 `NOT_APPLICABLE_BASELINE`、candidate 模式為 `PASS`。退出碼：PASS 與 NOT_APPLICABLE_BASELINE 為 0，FAIL 為 1，INSUFFICIENT_EVIDENCE 為 4，BLOCKED 為 5；判定器自身失敗（門檻檔無法解析、輸入無法讀取等）為 3，與 FAIL 區分，不得被誤讀為發版違規。

## 延後項凍結

`resources`、`min_effective_load`、`comparison_uncertainty` 在 Linux 與 Windows 兩組 48 h 基線完成前皆為 `deferred`。凍結只能由 P2-T16 的 `realhw.acceptance.od09_freeze freeze` 一次寫入，不得手改。凍結時點必須晚於兩平台基線的結束時間，且早於候選 run 的 `started_at_utc`。在凍結之前，candidate 判定一律為 `BLOCKED`。

## 48 h 基線程序

基線須在擁有者 bench 上依序執行（同機同板經 usbipd，不可並行）。Windows 步驟以 `py -3.12` 執行，並使用 bench lock 路徑。

1. 確認 bench lock 已取得，且沒有其他 pytest 或 testpilot 行程。
2. 關閉該實例的監管自動重啟，並把設定證據寫成 `supervision-attest.json`（例如 `systemctl show -p Restart` 的輸出；§15.11.2）。
3. 啟動 observer 於背景：
   `python3 -m realhw.acceptance.observer --serialwrap-exe <pinned> --boards COM0,COM1 --interval-s 10 --out <report>/observer.ndjson --supervision-attest supervision-attest.json`。
   Windows 另加 `--excl-dir <report>/excl --excl-probe-interval-s <workload.windows_excl_probe_interval_s>`。
3a. （Windows）執行 `winexcl handoff --phase pre --serialwrap-exe <pinned> --boards … --observer-header <report>/observer.ndjson --excl-dir <report>/excl`。exit 0 才繼續；exit 1 表示雙重持有，停止並保留現場；exit 4 表示證據不足，排除後重做 pre，不得跳過。
4. 執行 longrun：`python3 -m realhw --tier longrun --duration 48h`（使用 pinned 執行檔）。
4a. （Windows）longrun 結束後、停止 observer 前執行 `winexcl handoff --phase post`（同參數）。結果照實保存，非 0 由 gate 判定。
5. 結束後把 `daemon status` 所列 `wal_path` 所在目錄複製到 `<report>/wal/`。
6. 執行判定器：
   `python3 -m realhw.acceptance.gate --report-dir <report> --case-dir <case> --acceptance realhw/acceptance/longrun_acceptance.yaml --platform linux|windows --mode baseline --observer <report>/observer.ndjson --wal-dir <report>/wal --out <report>/release-gate.json`。
   Windows 另加 `--excl-evidence <report>/excl`。
7. 保存所有 hash，包括 `excl/*.ndjson`。判定器輸出的 `evidence_hashes` 與 `excl_evidence_hashes` 會列出這些檔案。

## 證據層級

- 本套件的判定結果依證據來源分級。Docker 拓樸（OD-15，`L1.5-docker`）與 `host-throwaway` 報告**不得**滿足 L2、L3 或 bench 項目；它們可以提早揭露缺陷，但不能取代 bench 基線，也不能作為 OD-09 凍結依據。
- Windows 獨占證據只在擁有者 Windows bench（`SW_TEST_COM_PAIR=COM20:COM21`）上取得。`holder_unknown` 只作診斷，不作判定依據。

## 已知限制

- agent worker 的預期集合由引擎 `start.workers` 宣告；缺此欄位即 INSUFFICIENT_EVIDENCE。整個 worker 未啟動也會被發現。
- `min_effective_load` 對候選缺少的鍵視為 0 完成。`error_rate_relative`、`throughput_relative` 對基線有、候選沒有的 (board, source) 判 INSUFFICIENT_EVIDENCE（`candidate_missing`），不略過。
- WAL 掃描：seq 缺號、重號或倒退、run 區間內無有效紀錄、非法 UTF-8，以及 `tx_outcome_unknown`（BC-03 結果不明寫入）一律讓 TX 相關安全檢查判 INSUFFICIENT_EVIDENCE，不得 PASS。
- observer 輸出檔若已存在且非空，拒絕覆寫（退出碼 2）；既有觀測紀錄是證據。
- 以 Python 基線 48 h 完成為前提；不完成的基線不能產生凍結值。
