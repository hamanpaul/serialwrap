# #198 三路整合與 root review

日期：2026-09-12。程式基準：`cf2aaba9004c18a82a6a5e4e0dcc66ea7211aeb1`。

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
