---
type: change
scope: rust-console
---
Slice 2.2 出口 JSON 新增來源身分：受測 HEAD、serialwrapd SHA-256、release 建置命令／退出碼／建置當下 SHA，以及 `dry_run` 與 `identity_problems`。只有實際產物、release 快照及 HEAD 身分自檢完整且一致時才輸出可供 P7 綁定的 `identity.daemon_sha256`；既有 S22 result 判定不變。
