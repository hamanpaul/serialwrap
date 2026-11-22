---
type: docs
scope: architecture
---
- 架構文件改為「架構總覽＋13 個功能頁」：總覽改釘 `12eb9a0e`（main，含 issue 282 停機行收尾），逐條重核 evidence；加上四個行程／函式庫／外部邊界（使用者端、serialwrapd、平台層 sw-pal、外部）；sw-pal 拆成序列埠、RPC transport、console 端點、實例鎖、裝置來源、檔案層／行程掃描六個平台能力節點，Linux｜Windows 差異只寫在這些節點。
- 新增 `tools/architecture_overlay.py`（與 custom-skills `architecture-fact-layer/scripts/viewer_overlay.py` 逐位元組相同）為每頁加上滾輪縮放與功能導覽列；`docs/architecture/pages.json` 為頁面清單的單一來源；`toolchain.json` 改為 schema 2，逐頁記錄 facts／IR／原版交付／最終 HTML 雜湊與加工版本。
- `tools/architecture_review.py`、`tools/architecture_browser.py` 涵蓋總覽與已整合的功能頁（含滾輪與導覽列驗證；導覽列依加工版本 2 的定位規則判定：工具列帶→頁頂到圖容器上緣的空白帶→一般區塊）；新增 `tools/architecture_integrate.py` 串起功能頁整合；`tools/architecture_feature_check.py` 加入篇幅上限、variant、自畫箭頭、phase 至少兩則訊息等規則。
