---
type: fix
issue: 131
scope: windows
---
原生 Windows CLI 三修（POSIX 行為逐位元組不變）：(1) CLI endpoint 解析平台感知——win backend 預設改用 `DEFAULT_ENDPOINT`（tcp loopback）；`_endpoint_alive`／`rpc_call` 於無 `AF_UNIX` 平台對 unix endpoint 回結構化錯誤／視為不可連，不再 `AttributeError` 崩潰；config.yaml 殘留 WSL unix `socket_path` 時比照 #108 語意自動改連 tcp canonical 並印 stderr 提示。(2) `daemon start` 於 Windows 可用：spawn 預設 `--socket` 改 tcp、支援 PyInstaller 凍結模式（同層 serialwrapd.exe → PATH → `-m sw_core.daemon` fallback，全落空回 `DAEMON_BINARY_NOT_FOUND`）、以 `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP` 脫離父 console、readiness 等待窗放寬至 10s、env 檔於 Windows 改用內建最小解析（不經 bash——Git Bash 的 `env -0` 會把整個環境 MSYS 化，spawn 出的 daemon 拿到不可用 PATH）；`--endpoint` 於 Windows 開放 loopback tcp:// 作為本機 bind 位址（非 loopback 照舊 `REMOTE_NOT_SUPPORTED`；與 `--socket` 同給且不一致回 `INVALID_ARGS`）。另：win backend 下 config 殘留死 tcp port 時 `_endpoint_alive` 改實測（lock_win 0.2s probe）使 #108 fallback 生效；`SERIALWRAP_RPC_BACKEND` 為未知值時 CLI 退回實際平台判斷不 traceback。(3) session payload（`session list`／`attach`／`self-test`）新增 `console_endpoint`（僅非 None 時輸出，POSIX 輸出不變），直接可見 COM ↔ human console TCP port 對應。
