---
type: fix
scope: rust-s2-rh-w1
---
`sw-pal` 的 `handler_spawner` 測試在 GitHub hosted Windows runner 上不再互相干擾：同檔測試依序執行，避免平行測試以 std `Command` 起的子行程繼承探針 handle 而誤判 LEAKED；期限溢位測試的子行程延長存活、偵測窗口放寬，斷言不變。
