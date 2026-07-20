---
type: fix
scope: remote
---
`serialwrap remote`：修全分支複查發現的 merge-blocker——native Windows 下 `_run_remote` 在 `guard_platform()` 之前就先 `from . import remote_tunnel`（該模組頂層 `import fcntl`，POSIX-only），導致 `ModuleNotFoundError` 穿越 CLI 邊界變成原始 traceback（而非 `REMOTE_NOT_SUPPORTED`）；且該 import 發生於 `try:` 外，`status`／`close` 兩條路徑更是完全沒呼叫 `guard_platform()`。改為在 `_run_remote` 最頂端、`import remote_tunnel` 之前先攔截 `os.name == "nt"`，一律回 JSON `REMOTE_NOT_SUPPORTED`，涵蓋 `status`／`close`／open 三條路徑。另補一個 catch-all `except Exception`，讓非 `TunnelError` 的非預期例外（如 `SERIALWRAP_RUN_DIR` 父路徑非目錄觸發的 `NotADirectoryError`）同樣轉為 JSON `INTERNAL_ERROR`，不穿越 CLI 邊界。
