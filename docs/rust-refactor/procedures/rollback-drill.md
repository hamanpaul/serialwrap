# 實機回滾演練程序

本程序說明 `python3 -m tools.stage3 rollback-drill` 的前置條件、D0～D8 步驟、判定與人工復原。演練驗證 §12.1：「回滾驗收須包括真實串口釋放與重新取得，不只確認版本字串改變」，且 `setup --rollback` 不得只切換 CLI。

演練具破壞性：會停止並重啟 daemon、重新取得 UART。只能由擁有者在指定 bench 明確授權後執行。

## 1. 前置條件

【BLOCKED-OWNER BO-P9-10】 Linux 實機演練，bench 獨占，兩塊板皆為 READY。

【BLOCKED-OWNER BO-P9-11】 Windows 實機演練（`windows-task`），需要 Windows bench，同樣要求兩塊板 READY。

執行前確認：

- bench 獨占。同一時間不得有其他 TestPilot 套件、A/B 或長跑使用同一批板子。
- 兩塊板 READY，`session list` 顯示兩個 session 皆為 `READY`。
- 無 flash 與 file transfer 進行中。`mcu status.flashing` 必須為 false。
- 不得有 `multi_open` 或 `foreign_holders`。
- 演練安裝目錄與 Stage 3 target 內容相同，否則 ingest 會拒收（R4-F1）。

## 2. 準備來源 bundle

演練需要兩種來源，皆由擁有者準備，不得以手改檔案取代。

- **損毀來源（D7）**：使用受測 Current bundle 本身。`--corrupt-from` 指向它，演練會在 `--out/corrupt` 複製一份並篡改 manifest 宣告的 CLI digest，該副本必須被拒絕。
- **啟動失敗來源（D8）**：以較新的版號建置候選 bundle，作為 `--start-fail-from` 的來源。演練會在 `--out/startfail` 複製一份並替換 daemon launcher，該副本的新版 daemon 必須無法啟動。

Linux 的 Current bundle 為 CLI 穩定入口解析後的上兩層目錄；Windows 則為 CLI 旁 `current-release.json` 的 `dir` 欄位。演練以相同方式解析：

```bash
CURRENT_BUNDLE="$(dirname "$(dirname "$(realpath "$SERIALWRAP_CLI")")")"
```

D8 來源的建置方式如下。`--dev` 且未加 `--delegate-sentinel` 時，`mkbundle.sh` 把版本目錄寫在 `--out/versions/<版本>`，`release-manifest.json` 位於該版本目錄下，`--out` 根目錄本身沒有 manifest。`mkbundle.sh` 不會為輸出目錄加上 `-dev` 後綴。

```bash
scripts/rust/mkbundle.sh --dev --version-override "$NEWER_VERSION" --out "$START_FAIL_OUT"
START_FAIL_SRC="$START_FAIL_OUT/versions/$NEWER_VERSION"
```

`--start-fail-from` 必須指向版本目錄（`$START_FAIL_SRC`）。指向 `--out` 根目錄時，演練的來源驗證找不到 manifest，會在 D0 以 `PRECONDITION_FAILED` 拒絕，D8 不會執行。

`make-corrupted` 與 `make-start-fail` 是選用的檢視輔助命令，會把副本寫到指定目錄供人工檢查。它們**不是**演練的前置步驟，演練也不讀取它們的輸出。不得把 `make-corrupted` 的輸出當作 `--corrupt-from`：篡改兩次可能還原為原本的 digest（原 digest 末位為 0 或 1 時會發生），使 D7 的「損毀」bundle 實為有效 bundle。

演練在 `--out/corrupt` 與 `--out/startfail` 建立副本。目的地已存在時，D0 前置檢查即失敗（verdict `PRECONDITION_FAILED`、`HARNESS_DRILL_PRECONDITION`，exit 1），不執行任何 setup。因此 `--out` 必須是新目錄，或其下沒有這兩個子目錄。

## 3. 執行

```bash
python3 -m tools.stage3 rollback-drill run --cli "$SERIALWRAP_CLI" --platform linux --boards "$LINUX_BOARDS" --expect-current "$CURRENT_VERSION" --expect-previous "$PREVIOUS_VERSION" --out "$DRILL_OUT/linux" --corrupt-from "$CURRENT_BUNDLE" --start-fail-from "$START_FAIL_SRC" --i-understand-this-restarts-the-daemon
```

- `--i-understand-this-restarts-the-daemon` 為必填。缺少即 argparse 錯誤（exit 2），不會執行任何步驟。
- `--boards` 以逗號分隔，例如 `COM0,COM1`。
- `--forward-argv` 省略時，roll-forward 的命令由 D1 記錄的 Current manifest 推得（`<current>/<components.cli.path>` 加上 `setup`）。只有在需要覆寫時才提供。
- 未給 `--corrupt-from`／`--start-fail-from` 時，演練不會執行 D7／D8，結果為 INCOMPLETE。
- Windows：`--cli` 為 `serialwrap.exe`，`--corrupt-from` 為 `current-release.json` 的 `dir`。`--start-fail-from` 須由 Windows 原生流程準備（見 `stage3-runbook.md` 步驟 (10) 與 BO-P9-17），`mkbundle.sh` 目前拒絕 Windows 目標。

## 4. D0～D8 步驟

| 步驟 | 內容 | 失敗時 |
|---|---|---|
| D0-preflight | 只讀前置條件（板子 READY、無 flash／file transfer、無多開、版本符合 expect）。 | 不啟動任何 setup，直接失敗 |
| D1-record | 記錄 Current 安裝目錄的 manifest，並對實際部署目錄量測 `bundle_tree_sha256` 與 `runtime_tree_sha256`，寫入報告。記錄兩板 session 的 alias、`device_by_id`、`attached_real_path` 與 `last_ready_at`。 | 後續步驟不執行，報告保留 |
| D2-rollback | 以 `setup --rollback` 切回完整 Previous 組合。 | 自動嘗試復原 |
| D3-verify-previous | 確認新 daemon、版本配對（`doctor` 的 `version_pairing`）、原板綁定，以及每塊板重新 READY。UART 必須真正被釋放後重新取得。 | 失敗 |
| D4-smoke-previous | 以一次性輸出命令確認 rollback 後 UART 可實際使用。 | 失敗 |
| D5-roll-forward | 以 D1 記錄的 Current bundle 重新啟用。 | 失敗 |
| D6-verify-current | Current 版本、程序退出、UART 重新取得與實際部署樹均須與 D1 吻合。 | 失敗 |
| D7-invalid-artifact-refusal | 以 `--out/corrupt`（由 `--corrupt-from` 的受測 Current bundle 篡改 CLI digest 而成）啟用，必須被拒絕。啟用前 PID 與 `bridge_generation` 不得改變，安裝佈局不得改變。 | 失敗；未執行則記 `step1_refusal_not_exercised` |
| D8-start-failure-revert | 以 `--out/startfail`（由 `--start-fail-from` 的來源替換 daemon launcher 而成）啟用，新版 daemon 無法啟動時，必須自動回到完整 Previous 組合並再次可操作。 | 失敗；未執行則記 `step7_revert_not_exercised` |

判定重點：

- 真實 UART 的釋放與重新取得才是回滾證據。不得以版本字串變更作為唯一依據。
- 跨重啟不得以 `bridge_generation` 遞增判定重新取得。`bridge_generation` 預設為 0、不持久化，daemon 重啟後的第一個 READY 通常是 1。generation 只用於同一 pid 內的比較（D7）。
- D1 量測的內容指紋必須與 Stage 3 target 相同。否則報告的 `deployed_content` 與 target 不符，ingest 拒收。

## 5. 結果與意義

| verdict | 意義 | exit |
|---|---|---|
| PASS | D0 至 D8 皆通過，且無限制項目 | 0 |
| FAIL | 任一步驟失敗（`HARNESS_DRILL_STEP_FAILED`）。失敗後會嘗試完整復原，`restore` 欄位記錄復原結果 | 1 |
| PRECONDITION_FAILED | D0 前置條件未滿足（`HARNESS_DRILL_PRECONDITION`），未執行 setup | 1 |
| INCOMPLETE | 全部步驟通過，但 `limitations` 非空（缺少 D7／D8 的來源，或其他限制）。`reason_code` 為 `HARNESS_DRILL_INCOMPLETE` | 1 |

INCOMPLETE 不等於 PASS，不得登錄為 `S3-ROLLBACK-*` 的 PASS。`limitations` 的常見項目：

- `step1_refusal_not_exercised`：未提供 `--corrupt-from`，D7 未執行。
- `step7_revert_not_exercised`：未提供 `--start-fail-from`，D8 未執行。

完整的 `S3-ROLLBACK-*` 證據需要 D0 至 D8 全部執行且通過。

## 6. 登錄

演練報告（`drill-report.json`）以 `evidence ingest --kind drill-report` 登錄。產生端必須量測部署內容指紋，缺值或與 target 不符即拒收，規則同 `evidence-index.md`。

```bash
python3 -m tools.stage3 evidence ingest --index docs/rust-refactor/evidence/index.json --kind drill-report --item S3-ROLLBACK-LINUX --platform linux --report "$DRILL_OUT/linux/drill-report.json" --evidence-root "$EVIDENCE_ROOT" --environment "$ENV_LINUX_JSON"
```

## 7. 失敗時的人工復原

若演練中途失敗且自動復原未完成，先依安裝佈局判斷，再人工復原。恢復目標是 D1 記錄的 Current（演練前的安裝），不是 Previous。

| 安裝佈局（`serialwrap service status` 與安裝目錄） | 處置 |
|---|---|
| 指向 D1 記錄的 Current | 不需切換佈局。確認 `daemon status` 的 pid 與版本，再進行第 3 步。 |
| 指向 Previous（D2 已切換，D5 未完成） | 以 D1 記錄的 Current bundle 向前啟用：`<D1 Current bundle 目錄>/<components.cli.path> setup`，與演練的 roll-forward（`_resolve_forward_argv`）相同。之後確認 `daemon status`。 |
| 無法判定，或指向其他版本 | 停止操作，保留所有輸出與 `drill-report.json`，交由擁有者判斷。不得任意執行 `setup --rollback`。 |

人工復原步驟：

1. 查看目前服務狀態與安裝佈局：`serialwrap service status`，並依上表判斷。
2. 依上表恢復佈局。不得以 `serialwrap setup --rollback` 回到 Previous，因為演練的恢復目標是 D1 記錄的 Current。
3. 確認每塊板重新 READY：`session list`。若某板未回到 READY，先查 UART 是否被外部工具佔用，再以 `release_check` 的 probe 結果判斷。
4. 若 daemon 無法啟動，保留 `daemon.log` 與 `drill-report.json`，不得覆寫。
5. 人工復原完成後，不得以手動方式把演練結果改為 PASS。重新演練必須使用新的 `--out`，並在報告中說明前一次的中斷原因。

【BLOCKED-OWNER BO-P9-10】 人工復原也是破壞性操作，須在 bench 獨占下進行。
