> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-1ec1992-1 | High | 已修 `914fe072` | 0 | 本輪未改 Python 必要集合、selector 或 Rust 集合。 |
| V051-1ec1992-2 | High | 已修 `914fe072` | 0 | 既有 20 次驗收紀錄仍在提交內；本輪未重跑。 |
| V051-1ec1992-3 | Low | 已修 `914fe072` | 0 | 撤回的 Python 觀測器描述未重新加入 changelog。 |
| V051-914fe07-1 | Medium | 已修 `f06eb10b` | 0 | JUnit 缺檔或無法解析時回報 `GAP`；本輪未修改該判斷。 |
| V051-854e1bb-1 | High | 已修 `833c8fc7` | 0 | 正式案例使用 bridge action 完成時間作下界，並配對 PID、watcher TID 與目錄 handle；兩個受控反例本輪實跑通過。 |
| V051-854e1bb-2 | High | 已修 `853153af` | 0 | 三個 `crates/` 檔案已還原至 `854e1bb8` 之前的內容；撤回後的 bundle 入口問題另見 B。 |
| V051-854e1bb-3 | Medium | 已修 `833c8fc7` | 0 | 整檔與 20 次紀錄綁定提交、測試檔及 manifest／執行檔雜湊；舊版 artifact 的暫改版本建置方式有明記。指定建置入口的限制另見 B。 |
| V051-854e1bb-4 | Medium | 已修 `833c8fc7` | 0 | ownership 異常延至擷取 DUT TX、WAL、stderr 與 attach/detach 資訊後才判失敗；受控診斷測試本輪實跑通過。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 逐項處理 `V051-22685ed-2`～`-5`，提供紅燈或缺口 | 部分完成 | `854e1bb8`、`833c8fc7` 有掃描、時間與診斷修正及紅燈紀錄；`-4` 的原始 `aaaa` 根因未定，已在實作者 SPEC-GAPS 附理由。 |
| 不放寬斷言、不刪除或改名既有測試、不擴大範圍 | 完成 `853153af`、`833c8fc7`、`fb0f1c2d` | 越界產品修改已撤回；本輪鎖檔只增加三個節點。指定 bundle 入口問題見下一項及 B。 |
| finding 對照表及可追溯 commit 的驗收 | 完成 `833c8fc7` | 實作者提供對照表；update 整檔與 20 次紀錄列出提交、測試檔與 artifact 雜湊。 |
| 確認 f1 的 OD-115 撤回已在分支 | 完成 `1ec19922`、`914fe072` | Git 歷史可見撤回、`rust_only` 及 Python 必要集合調整。 |
| 依 rest brief 準備 bundle、使用指定 A/B 入口及整檔驗收 | 部分完成 | update 整檔紀錄為 **11 passed、1 deselected**，並有 20/20 紀錄；但舊版 bundle 是暫改工作樹 `VERSION` 建置，指定的乾淨工作樹 `--version-override` 入口不能得到相同 daemon 版本。此缺口未列入 SPEC-GAPS，見 B-1。 |
| Windows bridge 讀檔競態：先紅後綠，期限與斷言不變 | 完成 `854e1bb8` | polling 內處理 `PermissionError`，保留 10 ms 間隔與期限；本輪定向測試通過。 |

本輪定向執行兩個掃描反例、console 診斷反例及 Windows bridge 測試，合計 **4 passed**。未跑完整 pytest、Docker 或 bundle 測試；工作樹維持乾淨。

## B 新發現

1. **V051-fb0f1c2-1 [High]** [version.rs](crates/sw-proto/src/version.rs:7) — **撤回 hook 後，brief 指定的舊版 bundle 建置入口無法產生版本配對的 daemon。**在乾淨工作樹 `VERSION=0.5.0` 時，`mkbundle.sh --dev --stage 2 --version-override 0.4.1` 會設定 `SW_BUNDLE_VERSION=0.4.1`，但 daemon 的版本來源只讀 `VERSION`，因此 daemon 回報 `0.5.0`，預期為 `0.4.1`。本輪以相同版本模組做定向編譯，設定 override 後實際輸出仍為 `0.5.0`；daemon 正式接線見 [daemon_main.rs](crates/sw-daemon/src/daemon_main.rs:1416)。實作者的綠燈紀錄透過暫改工作樹 `VERSION` 避開此問題，不能證明指定入口可用。相同缺口在 `854e1bb8` 之前已存在，但本輪撤回版本 hook 後重新出現，直接影響本項指定的雙版本驗收。

   **修法：**在授權範圍內提供可重現的雙版本 artifact 準備方式，讓 CLI、daemon 與 manifest 的版本一致，並以乾淨來源及實際正式測試入口驗證；若必須修改產品路徑，依 brief 記錄缺口並交編排者裁決。

## 既有碼待辦（不計入結論）

- console 原始 `aaaa` 到達 DUT 的根因仍未定；A/B 壓力測試通過不能證明原始問題消失，已列 SPEC-GAPS。
- `test_od72_slice21_nodes_keep_their_assigned_owner` 的 DF-20 鎖集合問題維持待處理。

## 對裁決的異議（不計入結論）

無。

結論：需修正