# #198 三路整合與 root review

日期：2026-09-12。程式基準：`cf2aaba9004c18a82a6a5e4e0dcc66ea7211aeb1`。

本檔依日期保留歷史盤點；現行裁決見末段「2026-09-15 Root 接手最終審查」。

## 交付範圍

本輪交付為盤點、可執行方案與 #198 校正，未實作新診斷／reload／ownership gate。
依 coordinator 技能採 `codex-native-multi-agent`；原生多 agent 成功啟用，未建立 coordinator job。
三個 worker 均為使用者指定的 `gpt-5.6-luna`／`max`，各自使用獨立 branch／worktree。

| 分工 | Worker 原始 commit | 整合 commit | 成果 |
|---|---|---|---|
| ownership | `5b4cf5939adf0efd66d7a7fa232330160603381c` | `6901f7e` | [ownership 盤點](198-ownership-audit.md) |
| diagnostics | `a71cf781ffcd46b01349a6da54ba658085d3a91b` | `5b30c62` | [診斷方案](198-diagnostics-plan.md) |
| backlog | `71684cef154c20922a4db6a26f97e37230bbb56b` | `6e4d893` | [優先序與 reload](198-backlog-priorities.md) |

依 change-merger-v2 quick 模式整合：三個來源僅新增不同文件，以 cherry-pick 合併，無文字衝突；root 另修正語意與介面契約。提交遵循 conventional-commit 技能。

## Root 獨立核對與裁決

- GitHub #198／#32／#33：確認錯誤引用；#32／#33 不重開。#172、#181、#189 的既有修復不列成新功能。
- `arbiter.py:195-213`：修正 worker「enqueue 在同一 lock」敘述；實際 admission／record 在鎖內，`pq.put` 在鎖外。正常 queue 序列化不外推跨 recovery epoch。
- `session_manager.py:4063-4118`、`uart_io.py:730-781`：interactive ID 與 second console 不等於 generic writer token／reader ACL；`interactive_status` 會改 lease，不能拿來額外純讀診斷。
- `client.py:107-176`、`cli.py:612-691`：開關 trace 前後沿用同一次 endpoint 決策及相同 RPC 序列，保留既有 timeout enrich／retry；errno 不能混入 health probe 或先前 retry。
- 診斷採專用不向上傳播的 logger，明定 handler 冪等、verbosity 優先序、預設 byte 相容與新 trace 欄位白名單。第一批只交付 CLI trace。
- `session_manager.py:2770-2817`：同名 pin 的內容變更目前不重物化，yaml-target 則早退；要求 reload 方案包含 revision、同名變更、explicit target、新 target、權威 generation 與失敗回復。
- 保留 #171 最小診斷為 P1，將 daemon／session trace 分期；未採用 worker 將整票降為 P2 的版本。整合排序為 #166、#199、#171、#182、#197。
- file transfer 不經 arbiter 並不免除共享 UART 仲裁責任；若補測重現邏輯傳輸交錯，必須列 P1 修復，不能僅列 scope 外。

## 驗證狀態

- Root 在合併三路文件後執行完整 `python3 -m pytest -q tests/`：**1684 passed、16 skipped、44 subtests passed，99.36 秒，exit 0**；1 個既有 `tests/test_config_profiles.py:226` 的無效 escape sequence SyntaxWarning。live guard 未輸出 FAIL／WARN／SKIP。
- `python3 -m policy_check --repo .`：**24 pass、0 fail、2 warn**；R-19 為 CI gate 解析提醒，R-22 為既有文件引用提醒。`git diff --check` 通過，整合差異僅為 `docs/refine/` 文件。
- 既有 pytest 的通過只證明此程式基準在隔離測試情境的結果；不能當作尚未實作的 trace／reload 驗收。

Root review 結論：**處理方案 PASS**。錯誤引用、首批範圍及相容／生效契約已處置；未實作的 P1 補強仍由 #198 追蹤，功能交付未宣告完成。

## 後續工作

[#198 修訂內容](198-issue-body.md) 為外部 issue 的可審查版本，保持未完成項目與 OPEN 狀態。
[殘餘風險與回復方式](merge-risks.md) 記錄本輪未驗證邊界。

## 2026-09-13 實作階段（尚未結案）

以上 2026-09-12 為歷史盤點結果，不代表本節的 production 候選已交付。
實作計畫與共同 BASE 為 `ae1296e`，整合分支為 `feature/198-refine-delivery`。
本節依 change-merger-v2 governed 模式逐 hunk 核對共享檔案，沒有部署或重啟服務。

| 工作 | 原始候選 | 本地狀態／獨立驗證 |
|---|---|---|
| #166 transfer | 使用者批准 Luna max 接手，`3b3c793` → `6fee966`／`ed8750e` | Sol scoped 重審 PASS；root 定向 130 passed／24 subtests，整合為 `5b1deee`／`04c54ec`／`d2d1a28`；最新整合驗證見末段 |
| Ownership | Luna max `6d946ec` → `9ac9d12` | root 重現兩個 raw 競態並修正；定向 63 passed 及另一組 65 passed／9 skipped；整合為 `aa278db`／`cda607f`，指定雙審仍未完成 |
| #171 CLI-only trace | Copilot gpt-5.4 xhigh `9b7ef46` → `59cd1f9` → `1c1d98d` | Sol xhigh 兩次指出 MAJOR 並修正後 PASS；root 定向 96 passed、2 subtests；整合為 `f962fb2`、`da6f85f`、`be35aeb` |
| #199 holder stat | Antigravity gemini-3.8-flash-high／high `d6fc3f2` | Sol xhigh PASS；root holder/flash 90 passed、6 subtests；整合為 `ce16b92` |

Root 確認並處置的 CLI 缺陷：`daemon start` already-running 前置 probe 缺 trace；
以及以 `TypeError` 訊息判斷相容性而重送 mutating RPC。第二項透過已記錄請求後
拋例外的隔離測試證明原來會送兩次，修正後只送一次且原例外上拋。
最終 CLI 片段另有 15 次 baseline/quiet/verbose 子行程重播，見
[後續安排與有界成效](198-followup-validation.md)。

審查限制：Opus xhigh 同樣受 weekly limit 阻擋；ownership 的 Sol 審查由平台中止，
沒有有效裁決。未完成指定雙審，不宣稱全部 review PASS。
此階段尚未 push、建立 PR、驗證 CI、merge 或安裝；#198 保持未完成。

### Root 完整整合驗證（#166 接手前的歷史基準）

production 整合基準 `cda607f`，另含 root 新增的 FLASHING/raw gate 交叉 assertion
`tests/test_refine_flash_precedence.py`：

- `python3 -m pytest -q tests/`：**1729 passed、16 skipped、46 subtests，95.03s，exit 0**；
  沒有 live-guard FAIL／WARN／SKIP。先前 cancel fixture 的失敗未以 flaky 放行，已用
  queued barrier／terminal poll 修正並納入此綠燈。
- `openspec validate --all --strict`：**22 passed、0 failed**；未 archive 尚缺 #166 的變更。
- 帶 main／feature refs 的 `policy_check`：**24 pass、0 fail、2 warn**；R-19／R-22
  繼續列管，沒有設定豁免。這是本地 policy 檢查，不是具有 PR metadata 的 preflight
  或遠端 CI／review 證據。
- root 本輪已確認缺陷的處置與本地整合驗證通過；指定雙審、#166、PR／CI／merge
  及安裝部署均未完成。#171 的 daemon logging、#182／#197 仍按分期追蹤。

成果在隔離分支保留，可接續原計畫；Claude 指定模型額度不足時，不自行替換模型
或把已完成的三路候選當成整張 #198 結案。

### #166 Luna 接手更新

使用者明確批准 Luna max 接替尚未開工的 Sonnet；不豁免 Opus／Sol 審查要求。
初版候選 `3b3c79310426dc6fe3959e73c2705bf555af8117` 與兩個修正 commit 已保留在
`fix/166-transfer-portability`，最新 HEAD `ed8750ef7acb450b1fb97c6d15a4bab9a190f146`。
Root 先確認實際 OpenSSL `-A` 解碼、marker 換行、
md5sum 檔名碰撞及 profile 物化問題的修正；定向 116 passed／16 subtests。
受控 shell 不等同真板，且上述 1729 passed 的整合基準不涵蓋此新候選。

Sol 首輪 spec/quality FAIL 的 3 個 MAJOR（預檢記憶體配置放大、pull 負向缺測、
target override/null 缺測）均交原 Luna 修正；root 另採納 GNU md5sum 反斜線格式
回歸及 partial-output encoder 失敗辨識為同輪修復。配置放大由 root 有界重現：
1-byte 檔案曾先配置 749,913 bytes 作預檢，不以「最後 checksum 正確」放行資源問題。

fix1 全套曾出現 event counter 測試紅燈，未以單跑通過豁免：原 fixture 將 handler
marker 存在當成 counter 儲存完成，實際時序為 handler 結束後才寫 counter。
額外 `ed8750e` 只加入 dispatcher 完成等待並保留原正反斷言，不改 event production；
重跑完整 `1713 passed, 16 skipped, 60 subtests, 96.88s`。此為 worker 結果，
Sol scoped 重審已逐項 ADDRESSED、spec/quality PASS，另執行具名 10 cases 通過。

### Luna 接手後的 root 最終本地驗證

以 governed 模式納入三個 commit，沒有文字衝突；逐 hunk 核對 manager 僅新增設定
傳遞，既有 operation admission／identity cleanup 保留。Production 基準 `d2d1a28`，
另含 root 的 `tests/test_refine_transfer_integration.py` 5 個交叉測試與文件同步。

- 整合定向：**61 passed、16 subtests，1.22s**；驗 0-TX 過小預算、UTF-8 路徑、probe
  期間 competing transfer 拒絕、OpenSSL-only 在 505-byte 預算下的多 chunk／空檔傳輸。
- 完整 `python3 -m pytest -q tests/`：**1763 passed、16 skipped、62 subtests，99.94s，exit 0**；
  live guard 未回報異常。
- `openspec validate --all --strict`：**22 passed、0 failed**。
- `python3 -m policy_check --repo . --pr-base-ref main --pr-head-ref feature/198-refine-delivery`：
  **24 pass、0 fail、2 warn**。Worker `fix/` 的 R-12 問題在真正 `feature/` 整合分支
  驗為 PASS；R-19 gate 解析與 R-22 的 118 既有引用提醒仍列管，沒有豁免。
- 雙語 README 說明 target 省略繼承、explicit null 清除；F7 兩 case 已掛 #166 並保留
  真板後續驗證，不把本機 shell 或歷史 SKIP 當成實機轉綠。

Root 對 **Task 1 修正及本地整合驗證判定 PASS**；這不是 #198 全部指定審查完成。
Opus xhigh 仍受週額度限制，ownership Sol 仍缺有效裁決；未重派該平台中止審查。
因此 OpenSpec 5.2／5.3 保持未完成，未 archive、push、PR、CI、merge、安裝或重啟服務。

## 2026-09-14 補充審查修正（本地完成，整票尚未結案）

使用者批准以 Sol xhigh 接替 Opus 的補充審查席位；範圍為 Task 1／3／4，
不包含先前被平台中止、尚無有效裁決的 ownership 審查。
Sol 首輪 SCOPED FAIL 後，root 逐條重現並交單一 Luna max 修正：

- 無法轉成整數的 log-level 環境值曾使一般 RPC CLI 在送出前崩潰。
- `setup` 的兩個既有 RPC 漏接 trace；修正保留各自 try／解析、原方法順序、
  0.5 秒 timeout 及 best-effort 行為，不增加 probe 或 RPC。
- F7 將明確 `CHECKSUM_MISMATCH` 降為 SKIP；這是既有未補的驗收缺口，現在
  一律回 `FAIL/test/binary_roundtrip_mismatch`，並在跨板執行中立即停止。
- 首次完整測試另暴露已關閉 stderr capture 被 handler flush 的生命週期缺陷。
  當次為 **7 failed、1764 passed、16 skipped、65 subtests，98.92s**，沒有以
  定向綠燈放行；補真 `TemporaryFile` RED 後修正自有 handler 重建。

Luna 修正 commit 為 `9cbbf5ae229e043f41b1ff2715c070f903b11036`，Sol scoped
複審判定 R1–R4 全部 ADDRESSED、Spec／Quality PASS，無新 BLOCKER／MAJOR。
Root 依 change-merger-v2 governed 模式整合為
`8ec820e5807b8991bbaf1b292277e54c3e4bad6e`；沒有文字衝突，
`git diff --exit-code 9cbbf5a 8ec820e` 確認完整 tracked tree 相同。

### 本輪 root 獨立驗證

- 候選定向：**97 passed、1 skipped、21 subtests，0.52s**。
- 候選完整 `python3 -m pytest -q tests/`：**1772 passed、16 skipped、65 subtests，
  96.89s，exit 0**；不是重用 worker 的 94.91s 結果，live guard 未回報異常。
- 整合後 unit／integration：**69 passed、1 skipped、5 subtests，0.44s**，
  涵蓋 CLI、F7、setup、Windows daemon-start seam 及傳輸交叉測試。
- Root 的原版／候選 R4 對照：原版重設 closed-file stream 拋 ValueError；候選
  連續 3 輪各有 1 個自有 handler、1 筆輸出、propagate=False。
- `openspec validate --all --strict`：**22 passed、0 failed**；只驗證，不 archive。
- 預設及帶 main／feature refs 的 `python3 -m policy_check --repo .` 均為
  **24 pass、0 fail、2 warn**；R-19 gate 解析、R-22 的 118 既有引用繼續列管，
  沒有豁免，亦不冒稱包含 PR metadata 的 preflight 或遠端 CI 已通過。

F7 的 FAIL 表示資料完整性驗收失敗，不等於已定位其根因；`CHECKSUM_MISMATCH`
也不代表遠端檔案已搬移或整個 push／pull RPC 已成功。`MOVE_FAILED`、已列管的
1MB `PULL_PARSE_FAILED` 與缺工具等仍按原契約分流，不把所有非 ok 一律 FAIL。

以上為受審修正與本地整合驗證通過，不是 #198 全範圍審查或外部交付完成。
Ownership 指定審查仍缺有效裁決，沒有重派或繞過平台限制；OpenSpec 5.2／5.3
保持未完成。未 archive、push、建立 PR、執行遠端 CI、merge 至 main、關票、
安裝、重啟服務或操作真 UART。

## 2026-09-15 Luna 實作與獨立 Sol 雙審更新

使用者明確批准 Luna max 接手實作及兩個獨立 Sol xhigh 席次。已完成的實作不重做；
針對最新補修補齊第二席獨立審查，第二席先不讀第一席 verdict，找到 R1 尚存缺口：
`PYTHONINTMAXSTRDIGITS=0` 時，5000 位 log-level 數字仍可成為巨大 logger level，
沒有如文件承諾回到 WARNING。Root 獨立重現後採納，未以另一席 PASS 抵銷。

Luna 以 `d26c5f94e8a5ee921dfceb98532c8363870743dc` 只修 4 檔，
在整數轉換前固定拒絕超過 4300 位的數字（不計合法負號），補真正 logger.level、
warning 可見、CLI 單次 RPC、正負邊界與相容性測試；正常數值 20／5000 仍保留。
兩席各自對 BASE `9cbbf5a` → HEAD `d26c5f9` 重審，均為
**R1 ADDRESSED、Spec PASS、Quality PASS、SCOPED PASS，無 BLOCKER／MAJOR**。

Root 依最小差異方案整合為 `3906b5ee043802de68e1376f7f9e1abf73d6469a`，
無文字衝突；受審候選與整合版的 production／tests／regression 及本波文件完全相同。

### 本輪驗證與界線

- Luna RED：**2 failed、1 passed、1 subtests，0.08s，exit 1**，是預期的數字上限缺口。
- Luna 最終全套：**1774 passed、16 skipped、67 subtests，94.02s，exit 0**。
- Root 另跑全套：**1774 passed、16 skipped、67 subtests，95.44s，exit 0**；
  四檔於執行前／候選 commit 後的 SHA-256 相同，非重用 worker 日誌。
- 第一席 R1 定向：**5 passed、5 subtests，0.11s**；第二席：**4 passed、5 subtests，0.09s**。
  兩席另各自在 digit limit=0 下用真 parser/logger 複驗，不把無 INFO trace 當充分證據。
- Root 整合後 CLI／F7／setup／Windows seam：**55 passed、1 skipped、7 subtests，0.36s**。
  這個 skip 是原生 Windows detach 旗標對測；本 Linux 環境未驗該平台。
- Root policy：**24 pass、0 fail、2 warn**；警示仍為 R-19 gate 解析及 R-22 的
  118 既有懸空引用，沒有 exemption。OpenSpec strict：**22 passed、0 failed**。

以上補修雙席通過，不代表 #198 全範圍審查完成。Ownership 原席的平台中止仍無
有效裁決，與模型額度不足不同；未藉本次模型替換重派或繞過。OpenSpec 5.2／5.3
仍未完成，不提前 archive。未 push／PR／遠端 CI／merge main／關票／安裝或操作 live 系統。
Python 3.10 的 API 缺席路徑有相容寫法，但本輪沒有實際 Python 3.10／Windows／真板證據。

## 2026-09-15 無 UART 環境的驗收分期

使用者確認本機沒有 UART environment，實機測試可先延後；root 將本地開發／審查、
發版前 regression 與重大更新部署後穩定性驗收分開記錄。
這符合原計畫不操作真板及 issue 將本地／平台證據分列的範圍，不是豁免已知缺陷。
本輪完整 pytest／policy／程式審查要求不降級，真 UART 明列 DEFERRED，
不作為目前開發的先決條件；具體待驗清單與證據欄位見
[後續驗證與驗收分期](198-followup-validation.md)。

這次只調整計畫與交付紀錄，沒有修改 production／tests／regression，
也沒有重跑未變更的完整 suite。前節的 1774 passed 仍是具名歷史執行結果，不冒稱本輪新跑。
Ownership 的獨立審查仍缺有效裁決，與 UART 無環境分開列管；不提前 archive 或冒稱全案雙審完成。

## 2026-09-15 Root 接手最終審查

使用者最新指示「由你審，把這個 goal 跑完」取代尚未完成的 reviewer 席次安排。
Root 已重新讀取 ownership 完整差異、檢查反例與跨模組接線，裁決本次範圍
**PASS，無未處置 BLOCKER／MAJOR**；不把原 Sol 平台中止算作審查通過。
前述等待 reviewer 的段落均為歷史狀態，最新依 [root 最終審查](198-root-final-review.md)。

受審 HEAD 為 `09a0c2550bd652623d3175afcbb9e544e857b07a`，本輪重新執行：

- 完整 pytest：**1774 passed、16 skipped、67 subtests，96.39s，exit 0**。
- ownership／arbiter／raw／transfer integration 定向：**56 passed，15.04s，exit 0**。
- 本地 policy：**24 pass、0 fail、2 warn**；正式 PR metadata preflight 另行執行。

OpenSpec 封存只代表本地實作、審查與規格收斂；外部 PR／CI／exact-head merge
仍須取得 GitHub 證據才可把 goal 標完成。UART 回歸維持發版前 DEFERRED，
不安裝、重啟、部署或操作 live 系統。

## PR 201 追加 finding 的最小整合

第一版 PR 的三個 CI checks 全綠後，合併前發現新 review thread：
trace sink 例外會覆蓋主 RPC 結果。Root 重現後交原 Luna max 窄修，
再由 root 複審，不把既有綠燈當成免修依據。
策略採單一四檔 cherry-pick，無文字／介面衝突，不引入新的 logging 架構。

候選 `da01a83` 整合為 `583eee7`；worker full 1777 passed／16 skipped／71 subtests，
root 定向 29 passed／11 subtests，另驗 retry／TIMEOUT enrich 呼叫次數與回應身分。
Root 裁決 ADDRESSED／PASS，詳見 [最終審查追加紀錄](198-root-final-review.md)。
送出前重跑完整 preflight，送出後核對同一新 HEAD 的 CI 與 thread。
