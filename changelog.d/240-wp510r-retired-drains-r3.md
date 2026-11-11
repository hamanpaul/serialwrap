---
type: refactor
scope: rust-core
---
依據定稿設計第 5 版（S2-PAR-wp510r R3），把 actor 的 `retired_open_closes` 改寫為 `retired_drains`，承接所有退休 bridge 來源並套用收斂規則：
- 退休項目分「仍待回條」與「失敗」兩態；成功回條才移除，失敗（`handle_closed:false`）永久保留並登記未確認關閉。
- release 與 shutdown 都等 retired drain 收斂：RELEASED／DETACHED 但仍有退休 bridge 未確認關閉的 session，shutdown 不再略過、回條前不報已排空（DR-4 b）；manager 經 fence 的 `retired_drains_pending` 判讀。
- retired drain 未收斂時 `device.attach` 回 SESSION_RECOVERING 且不開埠（DR-4 c），bind 同樣回 SESSION_RECOVERING。
- 自身 bridge 停止成功、改等 retired drain 時不再清除 `drain_stalled`（轉移等待對象不清除 stalled 與 epoch，P8-c4913ae-1）；release 經在途作業收尾完成時一併清 `drain_timeout_epoch`（Python `_finish_attach_op`）。
- 單次在途（DR-8 舊回條後才重開）與 retired drain 等待期間失敗回條的 T-1 時點，因分別與凍結節點 `attach_finish_respawns_when_state_requires`、#71 衝突，本次維持現行行為並列待裁決。
