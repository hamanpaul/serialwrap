# #198 三路整合與 root review

日期：2026-09-12。程式基準：`cf2aaba9004c18a82a6a5e4e0dcc66ea7211aeb1`。

本檔保留 2026-09-12 方案盤點；最新實作與驗證見末段「2026-09-13 實作階段」。

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
| #166 transfer | 無 | Sonnet xhigh 啟動即 weekly limit；未改檔、未擅自換模型 |
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

### Root 完整整合驗證

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
