---
type: change
scope: rust-s2
---
新增 P7 reboot recovery stale-input、S24 rollback `--force` 在 `after_probe` 時拒絕交接，以及 Stage 2 Rust daemon rollback 至 Stage 1A Python daemon 的跨階段黑箱覆蓋；`mkbundle.sh` 新增 Stage 2 dev manifest 支援，預設 Stage 1A 輸出維持原樣。新增節點以 add-only 登記 L1。登入期間 human bytes 的 P7 interleave 案依 R5 停止，等待後續裁決。
