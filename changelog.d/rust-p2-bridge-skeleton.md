---
type: feat
scope: test-infra
---
建立 `tools/uart_stdio_bridge/` 獨立外部 bridge 套件骨架、依賴定義、產品邊界守衛（禁止 import `sw_core`）、`identity.tree_sha256` 演算法、`HARNESS_*` 測試原因碼、CLI 骨架與 GitHub Actions CI 自測 job。
