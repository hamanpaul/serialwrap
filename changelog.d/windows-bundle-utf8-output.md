---
type: fix
scope: release
---

修正 Windows bundle 建置腳本在 GitHub hosted Windows runner 上印中文時丟 `UnicodeEncodeError`：runner 的 Python stdout 是 cp1252，`mkbundle_windows.py validate` 已通過卻在印成功訊息時失敗。`mkbundle_windows.py` 與 `check_win_imports.py` 改為在入口把 stdout／stderr 設為 UTF-8（腳本以 `python -I` 執行，環境變數無效）。對 v0.4.0 預演補傳時發現。
