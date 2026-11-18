---
type: fix
scope: contract-blackbox
---

黑箱 L1.5 Python 基線 daemon 前置失敗時，將 session、daemon／bridge 診斷、PTY symlink 與世代、prompt 前置狀態及同路徑開埠 errno 附入失敗訊息，便於定位 attach 失敗根因。
