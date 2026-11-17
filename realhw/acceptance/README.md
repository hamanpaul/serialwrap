# realhw/acceptance — 長跑發版判定（P2-T15）

本目錄是長跑發版判定的外層套件，對應 spec §15.11.4、§14.2 與 OD-09／OD-10／OD-13／OD-94。OD-94 將每平台長跑門檻改為 24 小時；實際平台、板子與排程依其後有效的擁有者裁決辦理。

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

`resources`、`min_effective_load`、`comparison_uncertainty` 在 Linux 與 Windows 兩組 24 h 基線完成前皆為 `deferred`。凍結只能由 P2-T16 的 `realhw.acceptance.od09_freeze freeze` 一次寫入，不得手改。凍結時點必須晚於兩平台基線的結束時間，且早於候選 run 的 `started_at_utc`。在凍結之前，candidate 判定一律為 `BLOCKED`。

## OD-94：24 h 基線程序

每輪均為 24 小時（OD-94）；依 OD-102／OD-103 錯開執行：Windows Python 0.4.1 基線（COM6）→ Linux Python 基線 → 凍結延後項 → RC 後 Windows 候選 → Linux 候選 → 0.5.0 正式版。OD-103 的 interactive `SESSION_BUSY` counting 修訂須先由其專屬變更整合，本項不改 counting。Windows 基線待 Windows 原生證據完成後執行；Linux 基線待四個 gate 完成、停止其他 Linux serialwrap 測試後執行。四輪皆在 Orange Pi 3 測試板上，不並行；Windows 測試設定必須以 allowlist 或 `windows.exclude_coms` 限定只開實體 COM6（serialwrap selector `COM0`）；COM3、COM4、COM5、COM24、COM25 一律不得開啟，COM20–COM23 的 com0com 測試也不得與長跑並行。Windows 輪由唯一的 Windows 測試 daemon 開 COM6，關閉其監管自動重啟並留下 attest；Linux 輪先停止 Windows serialwrapd、改由非 serialwrapd bridge 獨占 COM6，再經 localhost TCP→WSL PTY 交給隔離 Linux daemon；同時停止 WSL 正式 `serialwrap.service`，以免觀測器偵測到另一個 serialwrapd。Linux 不以 usbipd 搬動 OPI；WSL MTK 板不參與長跑。Windows 執行用 `py -3.12`，兩平台各自使用 bench lock。

1. 確認 bench lock 已取得，且沒有其他 pytest 或 testpilot 行程。
2. 關閉該實例的監管自動重啟，並把設定證據寫成 `supervision-attest.json`（Linux 可附 `systemctl show -p Restart`；Windows 附對應 Task Scheduler／service 設定證據；§15.11.2）。
3. 啟動 observer 於背景：
   `<platform-python> -m realhw.acceptance.observer --serialwrap-exe <pinned> --boards COM0 --interval-s 10 --out <report>/observer.ndjson --supervision-attest supervision-attest.json`（Linux 用 `python3`，Windows 用 `py -3.12`；僅測 OPI 一板，serialwrap 的 `COM0` 對應 Windows 實體 COM6，Linux COM0 是橋接 PTY）。
   Windows 另加 `--excl-dir <report>/excl --excl-probe-interval-s <workload.windows_excl_probe_interval_s>`。
3a. （Windows）執行 `winexcl handoff --phase pre --serialwrap-exe <pinned> --boards COM0 --observer-header <report>/observer.ndjson --excl-dir <report>/excl`。exit 0 才繼續；exit 1 表示雙重持有，停止並保留現場；exit 4 表示證據不足，排除後重做 pre，不得跳過。
4. 執行 longrun：`<platform-python> -m realhw --tier longrun --duration 24h`（Linux 用 `python3`，Windows 用 `py -3.12`；設定必須 pin 此輪的 CLI／daemon）。
4a. （Windows）longrun 結束後、停止 observer 前執行 `winexcl handoff --phase post`（同參數）。結果照實保存，非 0 由 gate 判定。
5. 結束後把 `daemon status` 所列 `wal_path` 所在目錄複製到 `<report>/wal/`。
6. 執行判定器：
   `<platform-python> -m realhw.acceptance.gate --report-dir <report> --case-dir <case> --acceptance realhw/acceptance/longrun_acceptance.yaml --platform linux --mode baseline --observer <report>/observer.ndjson --wal-dir <report>/wal --out <report>/release-gate.json`。Windows 把 `--platform linux` 改為 `--platform windows`，並另加 `--excl-evidence <report>/excl`。
7. 保存所有 hash，包括 `excl/*.ndjson`。判定器輸出的 `evidence_hashes` 與 `excl_evidence_hashes` 會列出這些檔案。

## OD-94：獨立長跑驗收報告與 RC 身分綁定

Slice 2.4 的 `slice24.json`／`slice24.md` 不匯入長跑證據；`lr-mixed` 維持 **BLOCKED-OWNER**，並引用 OD-94。每個平台的長跑驗收結果另以本模組產生的 `release-gate.json` 封存，不得把此獨立報告寫成 Slice 2.4 gate PASS。

先在 Python 基線模式產生該平台的基線報告（每個平台各自一份；不帶 `--baseline-gate`）：

```bash
python3 -m realhw.acceptance.gate \\
  --report-dir <RUN>/report --case-dir <RUN>/case \\
  --acceptance realhw/acceptance/longrun_acceptance.yaml \\
  --platform linux --mode baseline \\
  --observer <RUN>/observer.ndjson --wal-dir <RUN>/wal \\
  --out <RUN>/baseline-release-gate.json
```

候選 RC 長跑完成後，使用同平台的基線 gate 結果比較；Windows 還必須附上原生獨占證據目錄：

```bash
python3 -m realhw.acceptance.gate \\
  --report-dir <RUN>/report --case-dir <RUN>/case \\
  --acceptance realhw/acceptance/longrun_acceptance.yaml \\
  --platform linux --mode candidate \\
  --observer <RUN>/observer.ndjson --wal-dir <RUN>/wal \\
  --baseline-gate <BASELINE>/baseline-release-gate.json \\
  --out <RUN>/release-gate.json

# Windows 將 platform 改為 windows，並加：
# --excl-evidence <RUN>/excl
```

輸入契約：`--report-dir` 必須含 `report.json`（及 `snapshots.ndjson`）；`--case-dir` 必須含 `events.ndjson`；另提供唯讀 `observer.ndjson`、WAL 目錄、此版本 `longrun_acceptance.yaml`，candidate 模式再提供同平台、PASS 的 Python baseline `release-gate.json`。Windows 的 `excl/` 含 `probes.ndjson` 與 `handoff.ndjson`。命令輸出的 `release-gate.json`（含 `acceptance.sha256`、`run.contract_revision`、`run.cli_identity`、`run.deployed_content` 與證據檔雜湊）是獨立驗收報告。

RC 身分不得只用版本字串表示：候選長跑固定使用 gate 後切出的 RC bundle，封存 `release-manifest.json`、`SHA256SUMS` 與該 bundle tree SHA-256；可用以下命令獨立重測已部署內容（Windows 將執行器改成 `py -3.12`；`--cli` 指向實際 launcher，`--daemon` 指向 RC version 目錄內的 daemon image；長跑前後各執行一次並分別封存）：

```bash
python3 -m realhw.deployed_content \\
  --cli <installed-cli-entry> --daemon <RC-version-dir>/bin/serialwrapd \\
  --daemon-pid <serialwrapd-pid> --out <RUN>/deployed-start.json
```

`realhw` 本身會在 `report.json` 記錄 `cli_identity` 及 `deployed_content` start/end/live-daemon；也封存量測輸出及其 `.live-daemon.json` sidecar。核對獨立量測與報告都一致：`release-gate.json.acceptance.sha256` 等於本輪 `longrun_acceptance.yaml` SHA-256，`run.contract_revision` 等於 RC HEAD 的 `contracts` tree，`run.deployed_content.start/end.bundle_tree_sha256` 與 `release_manifest_sha256` 分別等於封存的 RC bundle tree 與 `release-manifest.json` 雜湊，start/end 的 `cli_sha256` 等於 RC `bin/serialwrap[.exe]` 雜湊，`run.cli_identity.pinned` 為 true 且 `sha256` 相同；`run.deployed_content.daemon_live[].in_install` 為 true 且 `image_sha256` 等於 RC `bin/serialwrapd[.exe]` 雜湊。start/end 身分須一致。任何身分不符、缺檔或報告非 PASS 都照實保留，不得改成 PASS；`release-gate.json`、RC bundle 身分資料和全部輸入證據一併封存於獨立長跑目錄，不匯入 `slice24.json`。

## 證據層級

- 本套件的判定結果依證據來源分級。Docker 拓樸（OD-15，`L1.5-docker`）與 `host-throwaway` 報告**不得**滿足 L2、L3 或 bench 項目；它們可以提早揭露缺陷，但不能取代 bench 基線，也不能作為 OD-09 凍結依據。
- Windows 獨占證據只在擁有者 Windows bench（`SW_TEST_COM_PAIR=COM20:COM21`）上取得。`holder_unknown` 只作診斷，不作判定依據。

## 已知限制

- agent worker 的預期集合由引擎 `start.workers` 宣告；缺此欄位即 INSUFFICIENT_EVIDENCE。整個 worker 未啟動也會被發現。
- `min_effective_load` 對候選缺少的鍵視為 0 完成。`error_rate_relative`、`throughput_relative` 對基線有、候選沒有的 (board, source) 判 INSUFFICIENT_EVIDENCE（`candidate_missing`），不略過。
- WAL 掃描：seq 缺號、重號或倒退、run 區間內無有效紀錄、非法 UTF-8，以及 `tx_outcome_unknown`（BC-03 結果不明寫入）一律讓 TX 相關安全檢查判 INSUFFICIENT_EVIDENCE，不得 PASS。
- observer 輸出檔若已存在且非空，拒絕覆寫（退出碼 2）；既有觀測紀錄是證據。
- 以 Python 基線每平台 24 h 完成為前提；不完成的基線不能產生凍結值。OD-103 對 interactive 模式 `SESSION_BUSY` 的 counting 修訂依其專屬變更辦理；本 OD-94 slice 項目不改 counting 判準。
