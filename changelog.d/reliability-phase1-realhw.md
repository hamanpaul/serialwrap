---
type: feat
scope: realhw
---
realhw 實機穩定性套件 Phase 1 擴充：`CaseResult` 增 `category`/`reason_code` 分類欄並擴到報表與既有 29 case；preflight 新增兩級判決（suite-refuse 的 benchlock / Windows daemon 診斷，以及 family-gate 的 remote/版本/docker 能力缺項執行期 SKIP）；`p1-hp-cycle` 接上 Windows 端自動救援鏈；新增 `remote` tier 7 case（rm-topo×4、rm-live×3）；`realhw.load_cfg()` 與 `win_serialwrap_exe` 組態欄位就緒，供後續 reliability plugin 消費。
