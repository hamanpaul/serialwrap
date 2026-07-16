---
type: fix
issue: 131
scope: windows
---
原生 Windows CLI 三修（POSIX 行為逐位元組不變）：(1) CLI endpoint 解析平台感知——win backend 預設改用 `DEFAULT_ENDPOINT`（tcp loopback）；`_endpoint_alive`／`rpc_call` 於無 `AF_UNIX` 平台對 unix endpoint 回結構化錯誤／視為不可連，不再 `AttributeError` 崩潰；config.yaml 殘留 WSL unix `socket_path` 時比照 #108 語意自動改連 tcp canonical 並印 stderr 提示。(2) `daemon start` 於 Windows 可用：spawn 預設 `--socket` 改 tcp、支援 PyInstaller 凍結模式（同層 serialwrapd.exe → PATH → `-m sw_core.daemon` fallback，全落空回 `DAEMON_BINARY_NOT_FOUND`）、以 `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP` 脫離父 console、readiness 等待窗放寬至 10s、無 bash 時 env 檔載入回結構化 `ENV_FILE_SOURCE_FAILED`；`--endpoint` 於 Windows 開放 loopback tcp:// 作為本機 bind 位址（非 loopback 照舊 `REMOTE_NOT_SUPPORTED`）。(3) session payload（`session list`／`attach`／`self-test`）新增 `console_endpoint`（僅非 None 時輸出，POSIX 輸出不變），直接可見 COM ↔ human console TCP port 對應。
