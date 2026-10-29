---
type: fix
scope: rust-s21
---

修正 Slice 2.1 gate 三個間歇或恆定失敗的測試根因（皆為測試／收集器前提，產品行為不變）：`test_queue_full_backpressure` 改以 RPC 在前景命令執行期間填滿佇列（Python CLI 逐筆送出太慢、佇列永遠到不了上限），再以 CLI 驗證溢位回應；`lock_conformance_suite` 的失效 socket 改為只 bind 不 listen（避免同執行檔平行測試 fork 時複製 listener 使探測誤判仍在執行）；PTY 測試夾具改以 CLOEXEC 建立 fd，能力測試改單執行緒執行（fork 後 exec 前的子行程會暫時持有全部 fd，被 holder 掃描誤判為外部 holder）。`cargo_l1_results.py` 在測試失敗時把 libtest 失敗段轉印到 stderr，並保存完整輸出。
