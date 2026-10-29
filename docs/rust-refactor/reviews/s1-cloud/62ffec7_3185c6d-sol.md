## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 978aa1e | 0 | 改用 bridge barrier 排空在途資料並 flush，再依 checkpoint 長度與 SHA256 比對完整 TX；新增延遲重複 TX 案例。 |
| 2 | Medium | 已修 51bd8fa | 0 | ready 改由實際受委派的孫行程寫出；父行程確認其 PID 存活後才送事件，並檢查事件後已結束。Windows 原生未重跑。 |
| 3 | Medium | 已修 ddcbdde | 0 | driver 已接上 `l15_exit_driver.py`，保留原退出碼並交叉核對 gate、原始報告及 metadata；後由 4e3ba13、2fe7750 補強逐列與 repeat 檢查。三種核准延後情境的記憶體內整合檢查通過。 |
| 4 | High | 已修 12b3a92 | 0 | 舊版 pipx 改為隔離安裝、依賴由 wheelhouse 提供；容器移除系統 PyYAML，新 bundle 複製 PyYAML。此輪未重跑完整升級。 |
| 5 | Medium | 已修 12b3a92 | 0 | 新增 `--release-bundle`，不啟用 `sw-cli/test-fakes`；正式配方已改用此模式，本輪 workflow 亦沿用。 |
| 6 | Low | 已修 12b3a92 | 0 | 前置條件已改為實際的 `VERIFIED`／受核准延後條件限制的 `IMPLEMENTED`。 |

## B 新發現

### S1-3185c6d-1

**[High] [.github/workflows/release.yml:139](.github/workflows/release.yml:139) — 正式 bundle 使用 hosted-toolcache Python，搬到支援的 Ubuntu 主機後無法啟動 daemon。**

本輪新增 `setup-python@v5` 後直接執行 mkbundle。後者在 [mkbundle.sh:279](scripts/rust/mkbundle.sh:279) 使用 `python3 -m venv --copies`，只複製直譯器及套件，沒有複製 libpython 與標準函式庫，也沒有修正 `pyvenv.cfg` 的來源路徑。GitHub 的 Ubuntu Python 建置明確使用 `--enable-shared`，並將共享庫搜尋路徑指向 toolcache；CPython venv 的 POSIX 實作不會複製這些執行期依賴。來源：[GitHub Python builder](https://github.com/actions/python-versions/blob/main/builders/ubuntu-python-builder.psm1)、[CPython 3.12.3 venv](https://github.com/python/cpython/blob/v3.12.3/Lib/venv/__init__.py)。

**具體失敗情境：**將 workflow 產出的 tarball 解到 Ubuntu 24.04 x86_64，該機有 `/usr/bin/python3.12`、glibc 2.39，但沒有 GitHub toolcache，也未安裝獨立的 `libpython3.12t64`。執行 `versions/<V>/py/bin/python3 -I -B -m sw_core.daemon --help`，預期正常輸出 help；依上述動態連結關係，實際會因找不到 `libpython3.12.so.1.0` 而在載入階段失敗。這是來源推導的反例，本輪未重打 GitHub 產物實跑。

目前 smoke 只執行 Rust CLI 的 `--version`／`--help`，其他檢查均不啟動 Python，因此仍可通過並上傳無法啟動 daemon 的 bundle。

**確切修法：**依 OD-50，明確以支援環境的 `/usr/bin/python3.12` 建立 runtime，確認其基底路徑及依賴符合宣告；或完整打包可搬移的 Python runtime。上傳前，必須將**實際 tarball**放入沒有 toolcache 的乾淨 Ubuntu 24.04 容器，驗證 Python 匯入與隔離 daemon 啟動，失敗即阻擋上傳。

本輪逐一審閱 **2 個非 merge commit，無 merge commit**。契約檢查通過；policy check 為 **25 pass、0 fail、2 warn**（非 PR 上下文）；直接呼叫的 30 項唯讀檢查及 3 項記憶體內整合檢查通過。未執行完整 pytest／Cargo、Docker 或 Windows 原生重跑；未修改檔案，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正