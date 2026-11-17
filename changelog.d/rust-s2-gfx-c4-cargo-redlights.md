---
type: fix
scope: rust-s2-gfx-c4
---
修正兩支逐項 Cargo 測試的行程隔離與時序同步：PTY holder 改用呼叫者行程樹外的獨立 session；RPC bind 逾時案例等待晚到提交完成後再重試，保留 holder PID、未知狀態及拔插後開埠等原有斷言。
