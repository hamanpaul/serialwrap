# #198 Root 最終審查（2026-09-15）

## 授權與判準

使用者明確指示「由你審，把這個 goal 跑完」。本次由 root 接替未完成的
ownership 審查並負責最終整合裁決；不是兩席 Sol 審查，也不把先前的平台中止
改記為 PASS。平台訊息沒有具體漏洞、檔案或行號，不能據此推論產品有缺陷，
也不能推論整個 ownership 範圍不可再做一般程式正確性審查。

未處置的缺陷／驗收缺口判 FAIL；已明文列管、影響有界的殘餘風險不單獨判 FAIL。
首輪沒有新實作；PR 出現的新 finding 與後續窄修列於末節。全程沒有連真 UART，
也沒有放寬 assertions。

## 候選與方法

- 基準 main：`cf2aaba9004c18a82a6a5e4e0dcc66ea7211aeb1`。
- 受審整合 HEAD：`09a0c2550bd652623d3175afcbb9e544e857b07a`。
- root 重新讀取 manager／UART bridge 的完整差異及相關方法，對照 ownership
  scenario、決定性測試與既有 arbiter／interactive 契約，不只採用 worker 自評。
- 另以反例角度核對最終差異：busy loser 的 finally、例外收尾、舊 epoch、
  raw TX 的檢查位置、deferred 回放、FLASHING 優先、共享 profile 參數接線；
  交叉檢查 transfer／CLI trace／holder 的最終程式及既有 scoped 審查處置。

## 裁決

| 邊界 | 證據與裁決 |
|---|---|
| direct command／push／pull | manager lock 內建立 operation token；競爭者回 busy 且不進傳輸／TX；正常 arbiter 多 client accepted 契約未改 |
| 收尾與 epoch | 只在 session、token、bridge、generation 全部相符時清理；resume／post-close 例外仍經 finally，舊操作不清新 busy |
| human raw 與新 console | admission 期間不授予新 human raw owner；在途 raw send 於 write 路徑重驗 gate；POSIX／TCP 皆有正反例 |
| 回放與 FLASHING | deferred 回放一次且 I/O 不持 manager lock；FLASHING 丟棄優先，不排入稍後回放 |
| interactive／secondary／grace | 忙碌時拒絕 interactive writer；失效 ID 零 TX；secondary 保留 line broker，peer grace 保留既有語意，不宣稱 generic ACL |
| 整合面 | profile budget 傳入兩向傳輸；OpenSSL 真 shell roundtrip、UTF-8 行長、checksum 失敗不落檔；trace 不增加 RPC，holder fallback 不抬升為平台排他證據 |

先前 root 重現的兩項 MAJOR（新 console raw grant、raw 檢查後延遲送出）均已修復，
本輪逐段複核與測試沒有發現新的 BLOCKER／MAJOR。
**Spec PASS、程式品質 PASS、最終整合審查 PASS（僅上述範圍）。**

## 本輪實跑

完整驗證：

```text
python3 -m pytest -q tests/
1774 passed, 16 skipped, 67 subtests passed in 96.39s (0:01:36)
exit 0
```

定向複驗：

```text
python3 -m pytest -q tests/test_refine_ownership.py tests/test_refine_flash_precedence.py tests/test_refine_transfer_integration.py tests/test_multiagent_e2e.py tests/test_arbiter_flush_on_recovery.py tests/test_suspend_resume_reentrant.py tests/test_interactive_raw.py
56 passed in 15.04s
exit 0
```

本輪 `python3 -m policy_check --repo .`：24 pass、0 fail、2 warn、0 exemption。
PR metadata 與正式 base/head 的完整 preflight 另作送出門檻，不用本項代替。

## 交付與未驗界線

以上是本地候選審查，不是遠端 CI／merge 或已安裝 runtime 的證據。
後續僅變更文件與 OpenSpec 封存時，需確認 production／tests／regression
與此候選相同；若有程式修正，必須重新測試與審查。

UART 實機回歸為 **DEFERRED — no UART environment**，發版前補驗；
native Windows、外部 process 持 tty、真板背壓、所有未盤點 callback 與長時行為
不在此次保證內。既有 1MB pull RX 視窗限制、daemon trace／reload／Cloudflare
分期仍依 [後續驗證安排](198-followup-validation.md) 追蹤。
未安裝、重啟、操作 live config/state/WAL 或真板。

## PR 201 追加審查與修補

合併前 Copilot 對 `client.rpc_call()` 提出 trace sink 例外覆蓋主 RPC 結果的缺口。
Root 以真 rpc_call、mock transport 與 raising sink 獨立重現：
主請求成功且只有一次，外層卻拋 RuntimeError。此項採納並修復，不以先前 PASS 抵銷。

- Luna RED：`TestRpcCallTraceSink` 為 2 failed、3 passed、2 subtests，0.10s，exit 1。
- 最小修補只對 trace callback 捕獲一般 Exception；不包主 RPC／retry／enrich，
  不新增 log 或重送，KeyboardInterrupt／SystemExit 仍傳出。
- Luna commit `da01a83eec38901cb00f084f9001f9420cfebe06`，
  整合為 `583eee7`；僅 client、診斷測試、CLI 交付文件與既有 fragment 四檔。
- Luna 完整 pytest：**1777 passed、16 skipped、71 subtests，98.31s，exit 0**。
- Root 整合後定向：**29 passed、11 subtests，0.30s，exit 0**。
- Root 另驗證唯讀一次 retry 後成功仍回原 dict、sink 一次且 retry_count=1；
  mutating TIMEOUT 保留既有 enrich、原 dict、主請求一次，即使 retries=3 亦不重送。
- Root 複審四檔差異及實跑：**finding ADDRESSED、Spec／Quality PASS**。
  受審與整合的 production／tests／regression 相同，ownership 未變。

最終整合仍需重新 preflight、推送後的遠端 CI 與 thread 處置，才可合併；
第一版 PR 的綠燈不能替代修補後 HEAD 的檢查。
