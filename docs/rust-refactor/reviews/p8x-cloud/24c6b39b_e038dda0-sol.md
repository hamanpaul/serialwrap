> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8X-b837cfc-1 | High | 已修 cbb03497 | 0 | FIFO 暫存與撤銷凍結後重播仍保留，本輪未動相關程式。 |
| P8X-cbb0349-1 | High | 已修 9e51abd1 | 0 | login run 取消、完成事件身分檢查與回歸案例仍保留。 |
| P8X-9e51abd-1 | High | 依裁決不處理 | 2 | 已核對 `origin/docs/parity-amendments` 的 `rulings/s2.md`；維持降為 Low、出口再檢視。 |
| P8X-23ccb65-1 | High | 已修 24c6b39b | 0 | CLI 抽取、載入與判定接線仍在；缺少 run directory 的定向測試通過。 |
| P8X-23ccb65-2 | Medium | 已修 56bd8d3e | 0 | SC-33 撤回、SC-03 承接的斷言保留並通過。 |
| P8X-23ccb65-3 | Medium | 已修 24c6b39b | 0 | 無 strict xfail；相關判定測試通過。 |
| P8X-24c6b39-1 | High | 已修 e038dda0 | 0 | 原先缺少的長跑匯入與解除阻擋路徑已接通；新增驗證缺陷另列 B。 |
| P8X-24c6b39-2 | Medium | 已修 4a2a9d50 | 0 | 改讀 `ab_env.AB_ENV`，並與 harness 入口核對；定向測試通過。 |
| P8X-24c6b39-3 | Medium | 已修 e038dda0 | 0 | 基底名稱可展開必要參數；另有聯集漏判，列於 B。 |
| P8X-24c6b39-4 | Medium | 已修 e038dda0 | 0 | Cargo 僅驗 Rust；黑箱 A/B 與 Python `contract_tests` 分別保留。 |
| P8X-24c6b39-5 | Medium | 已修 e038dda0 | 0 | artifact 按平台核對；Windows 使用 runner manifest。跨平台測試通過。 |
| P8X-24c6b39-6 | Medium | 已修 e038dda0 | 0 | Rust 執行範圍已加入覆蓋工具測試；4a2a9d50 同時補上根層 plugin 註冊。 |
| P8X-24c6b39-7 | Medium | 已修 e038dda0 | 0 | MCU RPC／CLI 已映射正式呼叫測試並歸屬 P8；定向測試通過。 |

## B 新發現

本輪審查 `072bf362`、`4a2a9d50`、`e038dda0`，無 merge commit。

**P8X-e038dda-1 [High]** [scripts/rust/contract_coverage.py:1234](scripts/rust/contract_coverage.py:1234) — **長跑證據未綁定本輪候選 artifact，舊候選的 PASS 可替新候選解鎖。**  
匯入器只核對目前 contracts、cases 與門檻檔；bundle 雜湊則與輸入目錄自己的 manifest 比對，未與本輪 artifact 交叉核對。具體情境：候選 B 修改 runtime，但未改上述資料檔，匯入候選 A 的長跑結果。定向重現中，長跑 CLI／daemon 雜湊皆與當輪不同，仍得到 `identity_errors=[]`、`lr-mixed=PASS`。  
**確切修法：**將長跑的 bundle、CLI、daemon 身分綁定本輪核准候選，判定時交叉核對；不同建置設定若需視為同候選，須有可驗證的來源與建置對應，不能只比 contracts 樹。

**P8X-e038dda-2 [High]** [scripts/rust/contract_coverage.py:1127](scripts/rust/contract_coverage.py:1127) — **長跑驗證只要求六項檢查，缺少正式 gate 的安全／存活／負載判準仍可 PASS。**  
例如輸入缺少 `safety_unauthorized_tx`、`safety_cross_context_replay`、資源與相對基線等正式必要檢查，只保留六項 PASS，彙總也填 PASS，驗證仍接受。定向重現：加入 `safety_unauthorized_tx=FAIL` 時判 FAIL；移除該列並重算彙總後，其他證據不變，便得到 `accepted=true`、`lr-mixed=PASS`。缺列應使證據失效。  
**確切修法：**依正式 P2 gate 契約驗證完整必要檢查集合、唯一性及適用性，或以保存的原始證據重跑正式判定器並核對結果；補上移除安全 FAIL 列後仍不得通過的回歸測試。

**P8X-e038dda-3 [High]** [scripts/rust/contract_coverage.py:967](scripts/rust/contract_coverage.py:967) — **參數聯集中雙邊皆未通過的節點會漏判，SKIP 可被算成整案 PASS。**  
具體輸入：宣告 `[quiet]`，Rust 只有 `[quiet]=passed`；Python 有 `[quiet]=passed`，另收集 `[extra]` 但 setup skipped。聯集會加入 `[extra]`，然而 Rust 未收集、Python 未通過時，兩個分支皆不執行。定向重現得到 `covered=["case:params"]`、`missing_reasons={}`、案例 PASS。  
**確切修法：**先建立雙邊完整必要參數聯集，再逐側要求每個節點收集且通過；任一側不成立即阻擋，不能只處理一側通過的情況。

驗證：定向 pytest **46 passed、3 deselected**，另完成上述三項定向重現。使用 `--noconftest` 避開正式環境探測，排除三個會啟動巢狀 pytest 的測項；未跑完整 pytest、Docker／bundle 或硬體測試。工作樹未修改且仍乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正