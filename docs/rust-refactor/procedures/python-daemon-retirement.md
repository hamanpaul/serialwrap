# Python daemon 退役切換程序

本程序說明如何在 Stage 3 證據 PASS 後，把 `packaging/python_daemon_retirement.json` 改為退役生效，並以受測 bundle 原樣發佈。退役範圍只限產品執行期 Python daemon（`sw_core` 的 daemon 執行路徑）。Python 測試工具、bridge、DUT 模擬器、TestPilot 與 `realhw` 引擎保留。

## 1. 退役前置條件

- `evidence status --stage 3` 為 PASS（見 `evidence-index.md`）。
- 兩平台 Stage 3 target artifact 已設定，且 `bundle_tree_sha256` 已於 `evidence target --bundle-dir` 凍結。
- Canary 兩平台報告 PASS，所有事故已結案（`S3-CANARY-*`、`S3-INCIDENTS-CLOSED`）。
- 回滾演練兩平台 PASS（`S3-ROLLBACK-*`）。
- 48h A/B 與 OD-09 全部凍結（`S3-OD09-ALL-FROZEN`）。

【BLOCKED-OWNER BO-P9-13】 以上皆為 PASS 之後，才由擁有者裁決退役，並寫入 `packaging/python_daemon_retirement.json`。

## 2. 受測 bundle 的保存

受測 bundle 是 `mkbundle.sh --stage 3` 建出並經 `evidence target --bundle-dir` 凍結的目錄。保存規則：

- 保存位置應與工作目錄分開，例如 `$ARCHIVE_ROOT/stage3-bundles/<platform>/`。
- 保存期間，受測 bundle 目錄不得有任何寫入。`bundle_tree_sha256` 於 `evidence target` 凍結，任何替換、刪除或新增檔案都使發佈被拒。
- 不得為了發佈而重新建置 bundle。重新建置會產生新的 `bundle_tree_sha256`，所有依賴舊 target 的證據都會過期。
- 「manifest 相符不等於內容相符」。`release-manifest.json` 的雜湊相符，只代表 manifest 本身未變；CLI、daemon 或資產被替換、或多放了旁載檔，manifest 仍相符。因此發佈時必須重算每個 component 與整個檔案樹。

## 3. 修改退役裁決檔

`packaging/python_daemon_retirement.json` 的欄位：

| 欄位 | 填寫內容 |
|---|---|
| `retired` | 核准後改為 `true`。建置用的 Stage 3 bundle 必須維持靜態標記，不得含核准欄位 |
| `approved_at`、`approved_by`、`decision_ref` | 擁有者核准的時間、人員與裁決編號。`decision_ref` 應對應 `docs/rust-refactor/owner-decisions-addenda.md` 中的一列 |
| `effective_bundle_version` | 退役生效後的 bundle 版本 |
| `evidence_index`、`evidence_index_sha256` | 證據索引路徑與其 sha256。索引變更後必須更新 |
| `stage3_target_artifact_ids.linux`、`.windows` | 兩平台 Stage 3 target 的 artifact id（即 `release-manifest.json` 的 sha256） |

核准只寫在這個檔案。`retired` 改為 `true` 之前，`retirement verify --mode release` 會以 `HARNESS_RETIREMENT_NOT_APPROVED` 拒絕，exit 2。

## 4. 核對與發佈

以下命令於 `.github/workflows/release.yml` 的兩個獨立 job 中執行，每個 job 每個命令的 exit code 非 0 即中止發佈：`publish-stage3-bundle-linux`（self-hosted linux runner）只處理 Linux archive，`publish-stage3-bundle-windows`（self-hosted windows runner）只處理 Windows archive。兩個 job 各自讀取同一組受測 bundle 目錄（`$SW_STAGE3_BUNDLE_DIR` 下的 `linux/` 與 `windows/`）。

```bash
LINUX_BUNDLE="$SW_STAGE3_BUNDLE_DIR/linux"
WINDOWS_BUNDLE="$SW_STAGE3_BUNDLE_DIR/windows"
python3 -m tools.stage3 retirement verify --mode release --evidence-root "$EVIDENCE_ROOT" --bundle-manifest linux="$LINUX_BUNDLE/release-manifest.json" --bundle-manifest windows="$WINDOWS_BUNDLE/release-manifest.json"
python3 -m tools.stage3 retirement pack --bundle-dir "$LINUX_BUNDLE" --out "$RELEASE_DIR/serialwrap-$TAG_NAME-linux.tar.gz"
python3 -m tools.stage3 retirement verify-archive --platform linux --archive "$RELEASE_DIR/serialwrap-$TAG_NAME-linux.tar.gz" --sha256sums "$RELEASE_DIR/SHA256SUMS-linux"
python3 -m tools.stage3 retirement pack --bundle-dir "$WINDOWS_BUNDLE" --out "$RELEASE_DIR/serialwrap-$TAG_NAME-windows.zip"
python3 -m tools.stage3 retirement verify-archive --platform windows --archive "$RELEASE_DIR/serialwrap-$TAG_NAME-windows.zip" --sha256sums "$RELEASE_DIR/SHA256SUMS-windows"
```

順序的意義：

1. `retirement verify --mode release`：檢查退役已核准；證據索引的 Stage 3 狀態為 PASS；`--bundle-manifest` 所列的 manifest 與 `stage3_target_artifact_ids` 的 sha256 相等；逐 component 重算（`verify_bundle`）；並以 `bundle_tree_sha256` 比對實際 bundle 的完整檔案樹。
2. `retirement pack`：把受測 bundle 封裝為 archive。Linux 為 `.tar.gz`，Windows 為 `.zip`。
3. `retirement verify-archive`：從最終 archive 的位元組解開，重算同一組 digest。通過後寫出 `SHA256SUMS-<平台>`（格式 `<sha256>  <archive 檔名>`）。違規時 exit 1 且不寫出 `SHA256SUMS-<平台>`。

只上傳通過 `verify-archive` 的那份 archive 位元組。不得上傳 pack 之前的檔案，也不得在 pack 後重新打包。上傳前再重驗一次：Linux 在 archive 所在目錄以 `sha256sum -c SHA256SUMS-linux` 核對（`release.yml` 的 `Verify Linux archive checksum` 步驟）；Windows 以 `Get-FileHash` 比對 `SHA256SUMS-windows`（`Verify Windows archive checksum` 步驟）。兩者皆通過後，才以 `gh release upload` 上傳 archive 與其 `SHA256SUMS-<平台>`，不上傳 bundle 目錄。

封裝與驗證的 archive 只能由 `retirement pack` 建立，不得以手動壓縮命令另行產生。若需要調整格式，必須修改 `retirement.py` 並重新驗證。

【BLOCKED-OWNER BO-P9-17】 Windows 的 archive 由自架 runner 原生 build 的 bundle 封裝。

## 5. release workflow 的 github-output

退役狀態可由 `github-output` 輸出，供 workflow 判斷：

```bash
python3 -m tools.stage3 retirement github-output
```

輸出為單行 `retired=true` 或 `retired=false`。`retired=false` 時 release workflow 不得執行退役相關的 Python 移除步驟。

## 6. 保留清單與不刪除 sw_core 的理由

`retirement verify` 會檢查下列路徑必須存在（`KEEP_PYTHON_TEST_TOOLS`）：

- `realhw`、`regression`、`reliability`、`func-test`
- `tools/uart_stdio_bridge`、`tests/contract_blackbox`、`tests/acceptance`
- `tools/stage3`、`sw_core`

`sw_core` 不得刪除，理由：

- 退役範圍只限執行期 daemon。退役後 release bundle 不再包含 Python daemon（manifest 以 `python_daemon: removed` 標記），但 `sw_core` 原始碼保留。
- Python 測試與 A/B 基線仍以 `sw_core` 為行為參照（規格 §12.2），刪除會使 Stage 3 之後的重跑與核對失去依據。

## 7. 回復

退役生效後若需撤回，必須以新的裁決處理：

- 把 `retired` 改回 `false` 須有新的 `decision_ref`，並在 addenda 中記錄撤回理由。
- 不得刪除已發佈的 archive 或 `SHA256SUMS`。撤回後以新版本發佈。
- 不得以手改 `release-manifest.json` 取代重新發佈。

【BLOCKED-OWNER BO-P9-13】 撤回退役同樣需要擁有者裁決。
