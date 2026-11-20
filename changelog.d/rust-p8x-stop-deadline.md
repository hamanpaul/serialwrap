---
type: fix
scope: rust-core
---
P8X-fixdl：dispatcher stop 的 deadline 等待改用注入的單調時鐘，並以 `FakeClock` 確認 deadline 前不返回、期限到達後觸發 handler abort；保留 abort 後 100 ms 回收寬限與既有回報語意。停機測試不再以牆鐘排程延遲作 judge。
