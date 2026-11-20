---
type: change
scope: rust-console
---
併入 P5 第四輪（開埠移出 actor 的工作者佇列）後，TCP console listener 與哨兵 PTY 改在開埠工作者中建立並隨 `BridgeOpened` 同一事件交給 core；listener 所屬 epoch 於 `AdoptBridge` 接手（未接手即排空時同樣停止 listener）。
`tests/acceptance/regen_lock.py --add-only` 同時接受 P5 的 `--allow-defer <case_id>:<from>:<to>:OD-NN` 與 P6 的 `--authorize-deferral <case_id>:OD-<n>` 逐名延後核准；Slice 2.2 凍結檔改以 `breaker/slice_2.2.amendments.json` 記錄 OD-34 的兩筆延後（`cowork-01-agent-bad-cmd`、`l15-delayed-reply`），凍結本體不再改寫。
