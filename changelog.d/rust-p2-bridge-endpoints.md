---
type: feat
scope: test-infra
---
新增 POSIX PTY 與 Windows pySerial bridge 端點 adapter，提供 byte 保真的讀寫、端點世代、斷線與重連能力，以及依平台回報的 transport 能力。修正 PTY 關閉時尾端資料排空、EAGAIN 重試的 peer gate 與無 peer 時的忙等；同步保護 I/O 和重連，並讓斷線窗口依 timeout 回空結果。Windows adapter 驗證 pySerial 寫入長度、限制零 timeout 的不確定路徑，並遵守 peer-close 能力閘門。`open_endpoint` 現在回傳已開啟的 adapter。
