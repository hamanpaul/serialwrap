---
type: fix
issue: 123
scope: cli
---
修復 host 過載／長操作下 CLI 假性 TIMEOUT（daemon 端 recover/attach/self-test 為同步長操作，執行時間結構性超過 CLI 舊預設 5s socket timeout；實測 recover CLI 5.08s 報 TIMEOUT、daemon 其實健康且 ~7-8s 後成功）。三段修法：(1) 全域 `--timeout` 預設改 None，未顯式指定時長操作（session.recover/attach/self_test，對照 daemon 端 BLOCKING_RPC_METHODS）自動採 ≥30s 的 timeout floor（由 recover 路徑成本推導：CTRL_C 2s + CTRL_D 2s + force 硬輪詢 10s + attach/probe 裕度；recover 另隨 `--timeout` 參數 +25s 成長、self-test 隨 `--probe-timeout` +15s），一般方法維持 5s、顯式指定一律照用；(2) TIMEOUT 錯誤 enrich——逾時後以新連線補 1s `health.ping` 輕量探測，錯誤 JSON 附 `daemon_reachable`（bool），可達時再附 `daemon_busy`（`health.status` 的 in-flight commands/sessions 計數），供呼叫端分辨「daemon 死亡/斷線」與「daemon 忙碌、操作仍在跑」（探測總預算 ≤2s，既有欄位不動）；(3) 新增全域 `--retries N`（預設 0、行為不變），僅對冪等唯讀方法白名單（session list、health.*、device list 等查詢類）在 TIMEOUT/連線失敗時做指數退避重試（0.5s 起 ×2），寫入類絕不自動重送。
