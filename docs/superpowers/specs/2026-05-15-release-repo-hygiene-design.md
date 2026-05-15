# Release 0.1.0 repo hygiene 設計

## 問題與目標

本次變更要完成 repo hygiene 與正式 release 準備。`test/reports/` 是人工或流程產出的測試報告，不應繼續出現在目前版本的 tracked tree；同時需要確認 repo 內是否仍有不該上傳的測試產物或疑似機敏資料，console login profile 例外。

本專案已部分導入 `paulsha-conventions`，本次會以 policy check 作為準繩補齊缺口，並以 `0.1.0` 產出正式 release 文件與 GitHub Release note。

## 採用方案

採用 PR-governed release 流程：

1. 在 `release/0.1.0-repo-hygiene` 分支完成變更。
2. 以一般 commit 從目前版本移除 `test/reports/`，不重寫 git history。
3. 更新 `.gitignore`，避免 `test/reports/` 與常見 report/log/capture/cache 產物再次被加入。
4. 掃描 tracked paths 與 tracked content，僅回報檔名與判斷，不在對話或文件中揭露機敏值。
5. 跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`，不得引入新失敗。
6. 開 PR；merge 到 main 後再建立 `v0.1.0` tag 與 GitHub Release。

不採用直接 main release，因為 repo policy 明確禁止直接 commit 到 `main`。也不採用 history purge，因為使用者已選擇只從目前版本移除 tracked 檔案。

## Repo hygiene 範圍

`test/reports/` 會從 tracked tree 移除並加入 `.gitignore`。`tests/` 與 `func-test/` 保留，因為它們是本專案的可執行測試與功能測試資產，不是產出報告。

敏感資料掃描以 tracked files 為主，涵蓋常見 secret pattern、private key pattern、credential-like path、log/report/cache/build artifact path。`profiles/brcm.env` 屬於使用者指定的 console login profile 例外；若掃描命中其他 profile 或測試 fixture，需依內容判斷是範例、環境變數名稱、測試假資料或真實機敏資訊。

## paulsha-conventions 對齊

現況已有 `.paul-project.yml`、四份 agent 指令、PR template、policy workflow 與 changelog 記錄。本次實作會以 `policy_check` 結果確認是否缺少必要 marker、policy version、PR checklist、CLI help reflection 或文件同步。若需修改 agent 指令，必須同步更新 `CLAUDE.md`、`AGENTS.md`、`GEMINI.md` 與 `.github/copilot-instructions.md`。

## Release 文件

`CHANGELOG.md` 會新增 `0.1.0` release 條目，涵蓋：

- repo hygiene：移除 `test/reports/` tracked 報告並加入 ignore。
- governance：確認或補齊 `paulsha-conventions` 導入。
- security hygiene：完成 tracked files 掃描，console login profile 例外。
- validation：記錄測試與 policy check 結果，若有既有失敗需明確標示。

`VERSION` 會更新為 `0.1.0`。GitHub Release note 以 changelog 為基礎整理，並在 PR merge 後建立 `v0.1.0` release。

## 驗證與失敗處理

測試命令為 `python3 -m pytest -q tests/`。Policy 命令為 `python3 -m policy_check --repo .`；若本機尚未安裝 policy engine，使用 pinned SHA 安裝後再執行。

若測試只命中 policy 文件已記載的既有失敗，會在 PR 與 release note 中標示，不視為本次新增回歸。若發現新的測試失敗、policy failure、疑似真實 secret，必須先修正或回報阻擋點，不可繼續發 release。

若 PR merge 或 GitHub Release 權限不足，本次交付會停在已推送分支、PR 與 release note 草稿，並明確說明阻擋點。
