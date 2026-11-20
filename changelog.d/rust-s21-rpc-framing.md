---
type: feat
scope: rust-s21
---
Rust RPC server：UDS 權限維持 Python 基線 0660，SERIALWRAP_SOCKET_GROUP 有值時 chgrp（SC-08 撤回，OD-14）、Windows 只綁 loopback TCP、每連線依序處理（允許 pipelining）、Python 位元組相容的 framing 早退回應與超長行斷線。
