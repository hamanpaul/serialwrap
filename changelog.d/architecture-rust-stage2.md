---
type: docs
scope: architecture
---
流程架構圖（`docs/architecture/`）依 Stage 2 的 Rust CLI＋Rust daemon（0.5.0，來源 revision `85a16533`）重繪：26 個元件、40 條方向關係，新增 Session actor、TX Fence、ConsoleHub、serialwrapd 主流程與 sw-pal 平台後端，並以「Linux｜Windows」標出 RPC、序列埠、console、實例鎖與裝置來源的後端差異。Archify 引擎由 `a07fa1d5` 升至 v3.2.0（`208acba8`），Viewer 固定介面改為繁體中文；`tools/architecture_review.py` 重建時關閉 Archify 的連網更新檢查，`tools/architecture_browser.py` 的一屏判定改採 Archify 3.x 的定義（圖下方的節點索引側欄不計入，另直接量主 SVG 是否完整落在第一屏）。
