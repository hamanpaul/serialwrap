---
type: fix
scope: rust-refactor
---

- 開埠成功但 session 不接手而退休的 bridge（例如 `session.bind` 等待開埠逾時、session 已是 RECOVERING 之後開埠才成功），在 UART handle 確認關閉前，`device.release` 不再回成功、轉 RELEASED 或落盤；收到該 bridge 的關閉回條後才完成 RELEASED。關閉失敗時 release 回 `DRAIN_STALLED`，該裝置登記為關閉未確認並阻擋重新開埠。裝置於開埠途中改指、改以新路徑重開的情形同樣等待舊 bridge 關閉。
