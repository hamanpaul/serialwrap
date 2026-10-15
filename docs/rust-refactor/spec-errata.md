# 定稿規格與盤點勘誤表（spec-errata.md）

本文件記錄 serialwrap 重構至 Rust 期間發現的規格（`serialwrap-rust-spec-final.md`）與盤點文件（`docs/rust-refactor/inventory/*.md`）勘誤。
依主計畫與政策規範，`serialwrap-rust-spec-final.md` 定稿本文維持唯讀，所有與 Python 基線事實不符之處統一於本文件彙整，供擁有者於下一個 spec revision 審查併入。

- 建立者：P0-T7
- 規範依據：`docs/rust-refactor/plans/01-stage0-contracts.md` §Task T7、§Task T15、`docs/rust-refactor/plans/spec-reconciliation.md`

---

## 盤點勘誤（Inventory Errata）

本節記錄在撰寫基線特徵化測試（P0-T7）與契約比對期間發現的盤點文件描述差異。所有行為皆以 Python 實際執行結果為準。

### INV-ERR-01：`TUNNEL_STATUS_ERROR` 觸發路徑

- **涉及檔案**：`docs/rust-refactor/inventory/03-errors.md` §3.4 L190、`docs/rust-refactor/inventory/01-cli.md` §6.11 步驟 3、§6.12 步驟 4、§6.13
- **盤點原文描述與歧義**：盤點內文 `01-cli.md` §6.11 步驟 3 明載 `serialwrap remote status` 直接印 `rt.status(run_dir)` 且 exit 0，§6.12 步驟 4 與 §6.13 將 `TUNNEL_STATUS_ERROR` 歸於 `connect --close` 與 `benches`。但在 `03-errors.md` §3.4 L190 表格簡述「remote status 回應異常」易被誤讀為 `remote status` 子命令，且計畫 `01-stage0-contracts.md` §Task T7 準備表誤寫為 patch status 後執行 `remote status`。
- **Python 基線事實**：`serialwrap remote status` 為直接透傳且 exit 0；真正經 `_status_tunnels` 檢查並在狀態損毀（如回應非 dict、ok 為 false、或缺少 `tunnels` 清單）時拋出 `TunnelError("TUNNEL_STATUS_ERROR")` 的 CLI 命令為 `serialwrap benches`（`_run_benches`）與 `serialwrap connect --close`。
- **特徵化測試**：`tests/test_contracts_gap_codes.py::TestGapPosixRemoteCli::test_tunnel_status_error_malformed` 透過 `cli.main(["benches"])` 驗證此碼之觸發與錯誤輸出。

### INV-ERR-02：`DEVICE_NOT_BOUND` 正常路徑不可達（僅防禦性分支）

- **涉及檔案**：`docs/rust-refactor/inventory/03-errors.md` §3.1、`sw_core/session_manager.py:717-719, 2146`
- **Python 基線事實**：`SessionManager.__init__` 對未設 `device_by_id` 之 profile 直接略過不建立 session；建立 session 之處（`:722`、`:986`）與所有 attach/rebind 路徑（`:720`、`:2077`、`:2561`、`:2820`）皆帶有非空 `device_by_id`，且系統無 unbind 途徑。正常執行下 `s.profile.device_by_id is None` 分支不可達，目錄記載之 agent_action「先以 session.bind 綁定裝置」無從適用。特徵化測試需以合成替換 profile 之方式觸發。

### INV-ERR-03：`TUNNEL_STATUS_ERROR` 在未注入異常下不可達

- **涉及檔案**：`docs/rust-refactor/inventory/03-errors.md` §3.4、`sw_core/remote_tunnel.py:500-530`、`sw_core/cli.py:1262-1277`
- **Python 基線事實**：`remote_tunnel.status` 於正常無例外時恆回傳 `{"ok": True, "tunnels": [...]}` 字典；`_status_tunnels` 之非 dict、not ok、缺 tunnels 三分支在無外部狀態檔損毀或測試替身注入時無法直接觸發，與 `SSH_NOT_FOUND` 同屬防禦性健全檢查。

### INV-ERR-04：`UNKNOWN_BENCH_CODE`、`INVALID_BENCH_CONFIG`、`TUNNEL_STATUS_ERROR` 在 Windows 基線不可達

- **涉及檔案**：`docs/rust-refactor/inventory/01-cli.md` §6.11-6.13、`sw_core/cli.py:1334, 1464`、`sw_core/remote_tunnel.py:10`
- **Python 基線事實**：Python 基線在 Windows 下 `_run_connect` 與 `_run_benches` 均在最開頭檢查 `os.name == "nt"` 並直接回傳 `REMOTE_NOT_SUPPORTED`（exit 1），且 `remote_tunnel.py` 頂層依賴 POSIX 的 `fcntl`。因此在 Python 基線之 Windows 環境此三碼不可達，特徵化測試歸入 `posix_only`。

---

## 規格勘誤（Spec Errata）

> 本節由 P0-T15 依調和表（`spec-reconciliation.md`）指派之列填寫。

### T8-ERR-01：`device.list` 非副作用方法（計畫 T8 前提錯誤）

- **來源**：`docs/rust-refactor/plans/01-stage0-contracts.md` Task T8 的 `test_side_effects_declared_for_readonly_whitelist` 列與「`device.list` 的 prune」前提。
- **事實**：`SessionManager.list_devices()`（`sw_core/session_manager.py`）僅於鎖內對 `_devices` 做排序快照，不 prune、不寫狀態；`_dispatch` 的 `device.list` 分支直接回傳該結果。因此 `device.list` 無副作用。
- **處置**：`contracts/rpc_schema.json` 的 `x-methods.device.list.side_effects` 記為 `[]`，不自行發明效果。
- **待計畫擁有者確認**：`test_side_effects_declared_for_readonly_whitelist` 的 `device.list` 斷言由「非空且 idempotent」改為「`side_effects == []`」；此列屬契約測試，修改需獨立審閱（§10.4）。
