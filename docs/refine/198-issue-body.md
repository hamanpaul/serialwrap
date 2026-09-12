## 目的與查核邊界

serialwrap 的投入重點是共享 UART 的確定性仲裁、human／agent 共存、原始證據／WAL，以及可診斷的恢復行為。以實際 consumer 與故障需求決定新能力的範圍。

2026-09-12 重新查核 GitHub 與 main `cf2aaba9004c18a82a6a5e4e0dcc66ea7211aeb1`；由三路 `gpt-5.6-luna`（max）分工盤點，root 獨立 review。此輪交付為工作範圍校正、證據盤點及處理方案，不代表方案已實作、部署或經實機驗收。

## 更正原票依據

- 原票誤將 [#32](https://github.com/hamanpaul/serialwrap/issues/32) 當 ownership 測試票；該票實為 file pull 的 Base64 解碼失敗，已關閉。
- 原票誤將 [#33](https://github.com/hamanpaul/serialwrap/issues/33) 當 diagnostics 票；該票實為 paired-session preflight，已決定由 caller 組合既有 primitives，關閉為 won't-fix。
- 因此取消「實作沿用 #32／#33」的安排；不重開這兩票，也不把其狀態作為本票修復證據。
- [#172](https://github.com/hamanpaul/serialwrap/issues/172) 已處理 CLI stderr 丟失 message／hint；[#181](https://github.com/hamanpaul/serialwrap/issues/181) 已補 pin／fallback 的 reattach 解析。後續診斷與 reload 需扣除已交付部分。

## 核心契約與 ownership 驗證方向

1. **Agent 命令排隊**：`command.submit` 可接受多個 client 的命令並排隊；驗收應檢查實際執行序列與不交錯，不能要求所有競爭者只有一個 submit 成功。
2. **Interactive lease**：以 `interactive_id` 管理互動生命週期；不同 agent 的競爭、失效 ID、close／expiry 與 human soft preemption 要依實際 API 驗收。它不是一套已實作的 generic writer-token／read-only-role ACL。
3. **Human console**：第一個 console 可獲 raw ownership，其餘 console 可透過 line-buffer 提交；第二個 console 不能被描述為永久唯讀。Agent 操作期間的 suspend／resume、deferred input 與 FLASHING gate 是必要邊界。
4. **Recovery 與重連**：檢查舊 worker／bridge 和新 epoch 交接、被 flush 命令的終態，以及 human peer grace 的既有行為；不能只靠「每個 queue 一條 worker」推導全生命週期排他。
5. **測試證據**：先採決定性的 fake transport／clock／barrier 正反例，再保留少量 socket／PTY 整合。找不到測試只記驗證缺口，不直接判定 production defect；靜態盤點不背書全稱保證。

程式證據：[arbiter](https://github.com/hamanpaul/serialwrap/blob/cf2aaba9004c18a82a6a5e4e0dcc66ea7211aeb1/sw_core/arbiter.py)、[session manager](https://github.com/hamanpaul/serialwrap/blob/cf2aaba9004c18a82a6a5e4e0dcc66ea7211aeb1/sw_core/session_manager.py)、[UART bridge](https://github.com/hamanpaul/serialwrap/blob/cf2aaba9004c18a82a6a5e4e0dcc66ea7211aeb1/sw_core/uart_io.py)。

## 最小診斷切片：沿 #171

- 首批限既有 CLI 操作的可選 trace：同一次解析得到的 endpoint／來源、RPC method、耗時、error code、可取得的 errno。未知值明示 unknown，不從錯誤字串猜測結構化原因。
- stdout JSON 與預設 stderr 契約不變；不將 #172 已修正的 message 顯示重做為新功能。
- Trace 沿用當次呼叫的資料，不重新解析 endpoint 或額外探測；既有 timeout enrich／唯讀 retry 次數和順序維持原狀。TIMEOUT 不表示 daemon 未執行，也不表示可以安全重送寫入操作。
- `interactive_status` 會 touch lease，不能作為額外的純讀診斷。不得為了診斷 acquire、recover、reset、改變 ownership 或增加 UART TX。
- 新 trace 用白名單欄位；不輸出 params、UART body、帳密、interactive ID 或完整 response。endpoint 顯示與分享前的去識別化方式須在實作驗收釘住。
- Daemon 狀態轉移、supervision trace、落檔／rotation 為後續獨立切片；完成首批 CLI trace 不代表 #171 全票結案。

## 既有工作的建議順位

以下是本票的安排建議，不變更其他 issues 的 label／state。已確認的 ownership 缺陷一律列 P1；待補驗證不冒充已確認缺陷。

| 順位 | 工作 | 理由與交付邊界 |
|---|---|---|
| 1 | [#166](https://github.com/hamanpaul/serialwrap/issues/166)，P1 | 有兩板的 decoder 缺失與 prpl 行長上限證據；先支援可驗證的 decoder 偵測／fallback 及安全 chunk 推導，保留 checksum／echo stall 防線。約 505 字元為該板量測，不推廣成所有 prpl 的硬編碼常數。 |
| 2 | [#199](https://github.com/hamanpaul/serialwrap/issues/199)，P1 小修 | 避免 holder path 的缺 `st_rdev` 例外，保留 POSIX char-device 比對；無 `/proc` 的空結果不代表 Windows 排他已驗證。此小修可與 #166 分開交付。 |
| 3 | [#171](https://github.com/hamanpaul/serialwrap/issues/171)，P1 最小診斷 | 先做上述 CLI trace，補定位端點與失敗階段的證據；既有 #172、#173、#189 不重做。 |
| 4 | [#182](https://github.com/hamanpaul/serialwrap/issues/182)，P2 | 以不打斷無關 session 的設定更新降低現場協調成本；先定義下節的生效與拒絕契約。 |
| 5 | [#197](https://github.com/hamanpaul/serialwrap/issues/197)，P2 驗證 | remote 已有實際 consumer；沿既有工具完成有界 Cloudflare／bench E2E，列明隔離與部署前提；不增加 provider SDK。 |

此順位來自已記錄故障與實作邊界，未取得使用頻率／操作耗時統計，不宣稱量化收益。

## #182 reload 的最小契約

- 目前 daemon 只在啟動時 `load_profiles()`；#181 的 reattach 會使用記憶體裡的 templates，未實作 YAML reload。
- 以單一有版本的設定快照作權威；先完整解析／驗證 candidate，鎖內重驗 ownership 與 generation 後一次交換。解析或衝突失敗保留舊版，不能只更新一半 targets／templates。
- reload 不關閉 active bridge，不改 active lease／capture／background，不額外 TX。正在執行的操作可繼續；「不額外 TX」不是要求整台 daemon 停止輸出。
- 哪些 template／target 變更可在下次指定 session 的 clear／attach 生效，需由變更類型表與測試釘住；同名 pin 內容變更、explicit target override、無衝突的新 target 都要有明確處置。僅換 `_templates` 不足以實現此契約。
- owner 的 COM／device identity 變動、刪除或衝突先拒絕；不讓新 target 靜默接管既有 dynamic／pinned owner。reload 不能撤回 RELEASED，也不能打斷 FLASHING。
- 記錄 operator 額外指定 clear 的中斷成本；同名 template 更新、invalid rollback、其他 session 不受影響，分別以 mock／PTY 與必要真機案例驗收。

## 範圍限制

- 不新增無具體 consumer 的通用硬體 Agent OS、multi-protocol orchestrator 或裝置排程平台。
- 無真實 contention、規模或非決定性故障時，不把全設備矩陣、分散式壓測或 throughput 排名列為前置。
- 不為診斷建立大型 dashboard、預設 raw dump 或第二份硬體狀態權威。
- 既有 Windows、remote、file transfer、MCU 與 WAL 的已使用能力持續維護；STOP 不代表刪除歷史資料、撤除安全檢查或繞過 broker。
- 修復與觀察分開授權；觀察結果的 absence 不等於健康或無外部持有者。

## 結案原則

2026-09-12 root 合併三路文件後，對上述程式基準執行 `python3 -m pytest -q tests/`：**1684 passed、16 skipped、44 subtests passed，99.36 秒，exit 0**；policy check：**24 pass、0 fail、2 warn**（CI gate 解析／既有文件引用）。這是既有程式的測試證據，不是新 trace／reload 或真機驗收。

本票保留 OPEN；依下列證據逐項完成，不能因完成盤點就把可靠性修復宣告完成。

- [x] 校正 #32／#33 錯誤引用，將契約對齊實際的 queue／interactive lease／human console。
- [x] 盤點現有工作，明列最小診斷、reload 契約、優先序與 STOP 邊界。
- [ ] Ownership 補強案例的正反例與 assertion 完成，附 root 測試證據；發現缺陷則修復或另列明確工作。
- [ ] #166／#199 的相關修復附測試證據；需要平台或真板驗證者分開記錄。
- [ ] #171 最小 CLI trace 具備相容性、資料最小化與不新增副作用的驗收證據；其餘分期仍由 #171 追蹤。
- [ ] #182／#197 的後續安排與有界成效驗證記錄完成，包含故障定位時間、人工協調／重試／恢復、必要物理介入及工具維護成本。

方案、pytest、實機驗證、安裝部署與長期成效分開記錄；單次 pilot 不外推穩定收益倍數。
