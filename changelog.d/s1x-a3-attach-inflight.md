---
type: fix
scope: device
---

修正 attach 進行中（probe／login 期間，無 drain、無 release 請求、非 RELEASED）再呼叫 `device attach` 時誤回 `SESSION_RECOVERING` 的回歸：恢復 0.3.2 的 `{"ok":true,"session":{...,"state":"ATTACHING"}}`；`SESSION_RECOVERING` 只保留給 drain／release 進行中（S1X-a-3）。
