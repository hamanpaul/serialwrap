---
type: fix
scope: rust-s2-rh-k7
---
`test_stop_during_push_flushes_wal` 的 no_auto_resend 判準改以 DUT 端位元組位移檢查「重啟後才收到的 bytes 不含 chunk 命令」，取代完整行數比對；停機時寫到一半的 chunk 行被新 daemon 的換行補成完整行，不再被誤算為重送（判準仍在 DUT 端，真的重送仍會失敗）。
