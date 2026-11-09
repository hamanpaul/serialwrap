---
type: fix
scope: rust-refactor
---

- 佔位退休落盤失敗留下的 parked UART handle，在 `session.bind` 重試時關閉未確認，改照 Python 只告警、不再登記永久的未確認關閉阻擋；改綁後照常重新排程 attach（handle 仍被占用時回 `ATTACH_FAILED:DeviceHeldError`），之後的 `session.bind`／`device.attach` 也照常嘗試開埠。
