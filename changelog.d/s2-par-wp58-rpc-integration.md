---
type: docs
scope: rust-refactor
---
補上 WP5-8 ServiceRig 正式 RPC 接線驗證：登入 attach 在途回成功、clear drain 與 release drain 在途維持完整 `SESSION_RECOVERING` 回應；同時以完整 `RPC_WIRE` 比對 clear／release 欄位與 interactive lease 清理結果。
