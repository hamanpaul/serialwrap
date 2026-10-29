---
type: fix
scope: rust-s21
---
Slice 2.1 sol 補審修正（`sol-backfill/p5-20261028.md`）：session executor 的開埠與 state commit 等待移出 actor 執行緒（開埠或持久化卡住時查詢仍可回覆、release 於 3 s 期限回 `DRAIN_STALLED`）；動態偵測 PROBE fence 註冊 WAL 健康通知；interactive 模式命令 TX 帶 cmd_id 並依命令帳結算部分送出；`alias.set` 同步更新 actor profile；啟動存檔失敗比照 Python traceback 後 rc 1；`serialwrapd --` 比照 argparse rc 2；capture 淘汰以 COM 精確找 owner；gate `judge` 子命令與 `assemble` 共用 expect 建構（含 parity）。
