### Added

- Rust daemon 承接 SC-30（OD-35 擁有者裁決維持）：自動 login／attach probe／既有 bridge 再探／READY 再確認／reboot recovery 進行中偵測到會擋 probe 的 boot banner 時，於同一事件內取消該 run（`RxShared::cancel_login_runs` 使 run 端 `RunCancel` 轉真）並收斂——非 READY 收斂 ATTACHED＋`PROMPT_UNAVAILABLE`、READY 不變、自動重探記 attempt、reboot recovery 回等待迴圈；banner 前已取得但尚未套用的成功結果不得推 READY、不得解除 #162 gate。console unlock profile 不取消。
