# serialwrap 契約治理規範（Contracts Governance）

本文件定義 serialwrap 重構與後續維護的對外契約治理標準、驗證工具與執行流程（ADR-07、OD-06）。

---

## 契約目錄架構與各檔用途

所有對外契約規格統一存放於 `contracts/` 目錄，作為系統的單一事實來源（single source of truth）。Python 基線與 Rust 實作皆直接對此目錄下的規格進行驗證。

| 檔案路徑 | 用途說明 |
|---|---|
| `contracts/error_codes.yaml` | 權威錯誤碼目錄。登錄 RPC 與 CLI 可能回傳的所有 `error_code`，包含碼值、狀態（`status`）、介面（`interfaces`）、重試性（`retryable`）、UART 發送狀態（`uart_tx`）、契約測試節點（`contract_tests`）以及 passthrough 映射條目。 |
| `contracts/session_last_errors.yaml` | session `last_error` 目錄。登錄 session 內部狀態機記錄的錯誤原因（包含 `named` 具名錯誤與 `dynamic_prefix` 動態前綴如 `ATTACH_FAILED:*`），宣告終態屬性（`terminal`）與契約測試。與 `error_codes.yaml` 為完全獨立命名空間。 |
| `contracts/diagnostic_aux.yaml` | 輔助診斷目錄。登錄次級警告碼（`warning_codes`）、錯誤細節碼（`error_details`）、錯誤分類（`classifications`）、建議處理措施（`recommended_actions`）與恢復動作（`recovery_actions`）。 |
| `contracts/behavior_changes.yaml` | 行為變更登記簿。記錄所有行為變更項目（`BC-01`～`BC-05` 破壞性變更、`SC-01`～`SC-27` 規格澄清、`DF-01`～`DF-17` 與 `DF-19` 缺陷修復），定義核准決策（OD-04、OD-11、OD-13）、安全不變量（`safety_invariants`）、待確認項目（`pending_owner_confirmations`）與 A/B 測試模式。 |
| `contracts/code_extraction.yaml` | 程式碼錯誤碼抽取規則。定義靜態 AST 抽取器如何從原始碼提取會被觸發輸出的診斷碼，宣告 `sinks`（匯入點）、`relays`（中繼點）、`passthrough`（透傳路徑）、`allow_unresolved`（允許未解析處）與 `exclude_values`（排除項）。 |
| `contracts/rpc_schema.json` | serialwrap 自訂 NDJSON 請求／回應協定（非 JSON-RPC 2.0）Draft 2020-12 JSON Schema。定義所有 RPC 方法的請求參數、結果格式（`x-methods`）、錯誤碼清單及未實作變更暫存區（`x-pending`）。 |
| `contracts/cli_schema.json` | CLI 子命令與退出碼 Draft 2020-12 JSON Schema。定義 CLI 參數、各子命令退出碼（`x-exit-codes`）、CLI 與 RPC 的混合呼叫表（`x-hybrid-rpc`）及未實作變更暫存區（`x-pending`）。 |
| `contracts/release_manifest.schema.json` | 發佈組合（bundle）`release-manifest.json` 的 Draft 2020-12 JSON Schema（P1 T18 建立）。由 `sw_core/release_manifest.py` 讀取驗證，並供 P3 `mkbundle` 與 Rust doctor 沿用。manifest 是宣告而非內容證明；實際檔案比對見 `version_pairing` 與 `tree_sha256`。 |
| `contracts/wal_record.schema.json` | Write-Ahead Log（WAL）單筆記錄的 Draft 2020-12 JSON Schema，約束 `seq`、`mono_ts_ns`、`wall_ts`、`source`、`cmd_id`、`crc32`、`payload_b64` 等結構。 |
| `contracts/wal_meta_events.schema.json` | WAL meta 事件記錄的 JSON Schema。 |
| `contracts/state_file.schema.json` | 持久化狀態檔（`state.json`）的 Draft 2020-12 JSON Schema。 |
| `contracts/daemon_logging.yaml` | daemon 執行期日誌與事件記錄規範目錄。 |
| `contracts/p0_row_coverage.yaml` | 規格調和表（spec-reconciliation）P0 列與契約凍結位置對照清單（T15 建立）。 |
| `contracts/golden/` | 位元組級黃金測試資料目錄（例如 `rpc/line_limit.json`、`last_error/transport_stall_hint.json`），提供跨語言一致性比對標準。`golden/tree_sha256.ndjson` 每行為 `{"files":{relpath:內容},"tree_sha256":hex}`，供 `tree_sha256` 演算法的 Python 與 Rust 實作比對；其格式由 P1 計畫固定，因此不含 `captured_by`／`source` 欄位（非 capture 產物）。 |

---

## 契約治理驗證工具：`python3 -m tools.contracts`

`tools/contracts` 為獨立驗證工具，僅使用 Python 標準函式庫與 PyYAML（**嚴格禁止 import `sw_core`**），保障黑箱獨立性。

### 子命令與用法

#### 1. 抽取診斷碼：`python3 -m tools.contracts extract`
從原始碼 AST 靜態分析提取會輸出的錯誤碼。
```bash
python3 -m tools.contracts extract --namespace <error_code|session_last_error> [--format <json|lines>] [--repo <path>]
```
- **Exit Code**：0 表示成功；1 表示載入／抽取失敗；2 表示參數錯誤（argparse）。

#### 2. 全套契約檢查：`python3 -m tools.contracts check`
執行全套契約一致性檢查（C01～C19）。
```bash
python3 -m tools.contracts check [--only <C01,C02,...>] [--repo <path>]
```
- **輸出**：標準 JSON 物件，包含 `findings`（檢查問題列表）、`ok`（布林值）、`summary`（統計摘要）。
- **Exit Code**：0 表示無任何 `FAIL` 缺失（`WARN` 缺失不影響狀態）；1 表示存在至少一筆 `FAIL` 缺失或設定錯誤。

#### 3. 統計摘要：`python3 -m tools.contracts summary`
產生純資料驅動的契約目錄統計摘要（不使用任何硬編碼常數）。
```bash
python3 -m tools.contracts summary [--repo <path>]
```
- **Exit Code**：0 表示成功；1 表示載入／抽取／摘要計算失敗；2 表示參數錯誤（argparse）。

#### 4. 生成 SKILL 受管區段：`python3 -m tools.contracts skill-section`
依據契約目錄動態生成 Markdown 處置目錄，輸出至 stdout（不直接寫入檔案）。輸出內容貼入 `sw_core/assets/skill/SKILL.md` 與 `sw_core/assets/skill/SKILL_WINDOWS.md` 兩份文件的對應 `generated-fact` 標記區塊（`error-codes` 或 `session-last-errors`），兩檔共用同一份輸出。
```bash
python3 -m tools.contracts skill-section --namespace <error_code|session_last_error> [--repo <path>]
```
- **Exit Code**：0 表示成功；1 表示目錄載入或渲染失敗；2 表示參數錯誤（argparse）。

#### 5. 列出生效碼：`python3 -m tools.contracts effective`
列出程式碼抽出值與契約目錄生效集合的交集（`抽出碼 ∩ status ∈ EFFECTIVE_STATUS`，排序每行一個，不含 passthrough ID，不檢查 `reachable`）。目前僅支援 `error_code` 命名空間。
```bash
python3 -m tools.contracts effective --namespace error_code [--repo <path>]
```
- **Exit Code**：0 表示成功；1 表示目錄載入或抽取失敗；2 表示參數錯誤（argparse）。

---

## 檢查代號表（C01～C19）

契約檢查器以標準化代號報告問題，各代號定義如下：

| 代號 | 檢查名稱 | 嚴重程度 | 判定規則說明 |
|---|---|---|---|
| C01 | 目錄載入錯誤 | FAIL | `load_catalogs` 失敗：目錄 YAML 格式錯誤、缺少必要鍵值或違反結構限制。 |
| C02 | 輸出未登錄碼 | FAIL | 程式碼抽出值（含動態前綴或 passthrough ID）未登錄在對應的契約目錄中。 |
| C03 | 已核准未實作碼輸出 | FAIL | 程式碼抽出的錯誤碼其目錄狀態為 `status: APPROVED`（尚未實作前禁止在程式中被觸發）。 |
| C04 | 目錄碼過期未輸出 | FAIL | 目錄中標記為生效且可達（`reachable: true`）的項目未在程式碼中被抽取器捕獲。 |
| C05 | （未使用） | — | 編號保留、驗證器不產生此代號 |
| C06 | proc 介面宣告缺失 | FAIL | 實作中以 `RuntimeError` 引發的程序退出碼，目錄中的 `interfaces` 卻缺少 `proc`。 |
| C07 | 未解析語法節點 | FAIL | 抽取器遇到無法靜態解析的 AST 節點，或 `allow_unresolved` 中定義的規則已過期。 |
| C08 | 過期排除項 | FAIL | `code_extraction.yaml` 的 `exclude_values` 中包含未被抽出的過期條目。 |
| C09 | 缺少契約測試 | FAIL | 生效碼缺少 `contract_tests` 參照、參照檔案不存在、測試 node 不存在或該 node 本體內未實際斷言該碼。 |
| C10 | 行為變更參照無效 | FAIL | 錯誤碼引用的 `behavior_change` ID 不存在於 `behavior_changes.yaml`，或 APPROVED 碼引用的變更未處於合法核准狀態。 |
| C11 | 命名空間污染 | FAIL | 標記為 `error_code_namespace == "forbidden"` 的 `session_last_error` 值出現在 `error_codes.yaml`。 |
| C12 | 測試前綴違法使用 | FAIL | 產品契約目錄中出現以 `HARNESS_` 開頭的測試前綴代碼。 |
| C13 | SKILL 文件覆蓋缺失 | FAIL | OD-06 規則：程式碼會輸出的診斷碼在 `SKILL.md` 或 `SKILL_WINDOWS.md` 缺少意義說明或處置指引。 |
| C14 | 受管區段內容漂移 | FAIL | R-26 規則：SKILL 文件中的 `generated-fact` 標記區塊與 `tools.contracts skill-section` 產出結果不符。 |
| C15 | Schema 代碼不一致 | FAIL | `rpc_schema.json` 或 `cli_schema.json` 宣告的錯誤碼／退出碼與契約目錄不一致，或超出 `CLI_CONTEXTS`。 |
| C16 | 狀態推進一致性違規 | FAIL | 僅套用於 `status != "EXISTING"` 的碼：引用變更全為 `PROPOSED`／`APPROVED` 時碼必須為 `APPROVED`；任一引用變更為 `IMPLEMENTED`／`VERIFIED` 時碼必須為 `IMPLEMENTED`／`VERIFIED`、`reachable: true` 且 `contract_tests` 非空。 |
| C17 | x-pending 未併入 | FAIL | Schema 的 `x-pending` 暫存項必須帶存在於 `behavior_changes.yaml` 的 `change`；當該變更狀態為 `IMPLEMENTED` 或 `VERIFIED` 時，必須併入正式欄位並自 `x-pending` 刪除。 |
| C18 | Schema 欄位不完整 | FAIL | `rpc_schema.json` 出現空物件 `{}`、缺少 `properties`、或 RPC 方法缺少 `side_effects` / `error_order`。 |
| C19 | 排除項稽核警告/失敗 | WARN / FAIL | 合法的抽取排除項回報 WARN；若排除條目嘗試隱匿目錄中既有的代碼則回報 FAIL。 |

---

## 狀態推進協定（Status Progression Protocol）

為確保「已核准但未實作」與「已實作生效」的界線明確，所有涉及行為變更（BC／SC／DF）與新增錯誤碼的修改必須遵守以下協定：

1. **狀態流轉生命週期**：
   - 行為變更項目：`PROPOSED` → `APPROVED` → `IMPLEMENTED` → `VERIFIED`。
   - 錯誤碼：新碼初始狀態為 `APPROVED`，此時必須標記 `reachable: false`，且必須關聯至未實作的變更項。
2. **原子性狀態推進（同一 Commit 原則）**：
   當功能實作完成時，必須在**同一 commit** 完成以下所有變更：
   - 原始碼實作完成，可被 `tools.contracts extract` 靜態捕獲。
   - `contracts/error_codes.yaml` 中對應碼的 `status` 翻轉為 `IMPLEMENTED`，`reachable` 設為 `true`。
   - 在 `contracts/error_codes.yaml` 補齊 `contract_tests` 參照，指向斷言該碼的具體測試 node。
   - 在 `contracts/behavior_changes.yaml` 將對應變更項的 `status` 翻轉為 `IMPLEMENTED`。
   - 若涉及 Schema 擴充，將 `contracts/rpc_schema.json` 或 `contracts/cli_schema.json` 中 `x-pending` 的條目正式合併至主體 Schema，並自 `x-pending` 清單刪除。
3. **禁止測試硬編碼**：
   所有測試禁止以字面 `status == "APPROVED"` 凍結「核准未實作」狀態，必須依據變更項目與碼的關聯性動態推導；驗證器內部則依資料比對狀態值。

---

## 黃金基準（Golden Assets）慣例

1. **用途與定位**：
   `contracts/golden/` 目錄存放真實位元組輸出檔案，作為跨語言（Python 基線 vs. Rust 實作）黑箱比對的黃金標準（ADR-07）。
2. **捕捉與維護腳本**：
   黃金基準檔必須由確定性腳本捕捉，捕捉腳本統一存放於 `scripts/stage0/`，例如：
   - `scripts/stage0/capture_rpc_line_limit.py` → `contracts/golden/rpc/line_limit.json`
   - `scripts/stage0/capture_transport_stall_hint.py` → `contracts/golden/last_error/transport_stall_hint.json`
3. **格式與目錄慣例**：
   - 檔案配置為 `contracts/golden/<領域>/<名稱>.{json,ndjson}`。
   - 每個檔案由 `scripts/stage0/capture_*.py`（或指定 capture 腳本）對真實 Python 基線產生，內含 `"captured_by"`（腳本相對路徑）與 `"source"`（被量測的程式位置）。
   - 文字與 JSON 一律採 UTF-8 編碼並以 LF 結尾。
   - 以 `DISK_STABLE` 序列化（`ensure_ascii=False, sort_keys=True, separators=(",",":")` 加 `\n` 單行）。
   - 黃金基準檔嚴禁手動修改；重新擷取必須在 commit body 註明原因與差異。
4. **獨立審閱原則（§10.4）**：
   修改 `contracts/`（含黃金基準檔）、比較器或哨兵的 PR 必須有獨立審閱；產品與契約同時修改（如狀態推進協定的同一 commit）時，PR 描述須分別呈現變更理由及既有基線對照；不得以刪案例、改分母、放寬正規化或把 FAIL 改 SKIP 取得全綠。
