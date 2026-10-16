---
type: change
scope: tests
---
pytest 註冊 core_portable／posix_only／windows_native 三種平台 marker 與 memory_only／process_rpc／posix_pty／windows_com／physical_uart／supervisor_runtime／interactive_desktop 七種資源 marker，逐檔標註既有測試，並以 AST 稽核測試防止漏標、誤標與 core_portable 平台 SKIP。
