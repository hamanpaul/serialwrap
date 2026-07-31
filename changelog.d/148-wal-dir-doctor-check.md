---
type: fix
scope: doctor
---
新增 `serialwrap doctor` 的 `wal_dir` 診斷項並修正誤導性文件（#148）：systemd 託管的
daemon 不會繼承 shell 匯出的 `SERIALWRAP_WAL_DIR`（`render_system_unit()` 產生的 unit
本就不含對應 `Environment=` 那一行，`.bashrc` 的 export 對它無效），daemon 仍照自己
解析出的預設路徑（XDG state home）持續寫入；使用者卻常誤把 `SERIALWRAP_WAL_DIR` 指到
`~/b-log`（那其實是 §10.4 agent on-demand capture 目錄，語意完全不同），造成「我以為
WAL 在 A，daemon 實際寫在 B」的落差，且舊版 `doctor` 完全沒有任何一項能揭露 daemon
實際生效的 WAL 路徑，只能用猜的。

修法：`sw_core/doctor_cmd.py` 新增 `_check_wal_dir()`——經既有 `health.status` RPC
（0.5s 短逾時、`rpc_call` 永不拋例外）讀 daemon 實際回報的 `wal_path`，與 shell 端
`SERIALWRAP_WAL_DIR`（若有顯式覆寫）比對：daemon 未在跑／連不上時降級為 informational
（`ok=True`，doctor 常在啟動 daemon 前執行，「連不到」本身不是這項要抓的錯誤）；一致
或無 shell 覆寫時 `ok=True`；不一致時 `ok=False` 並在 `fix` 附 systemd `Environment=`
與 on-demand 重啟兩種修復指引。掛入 Linux（`single_daemon` 之後、`devices` 之前）與
Windows（`daemon_endpoint` 之後、`devices` 之前）兩份檢查清單；`cli.py` 的
`_DOCTOR_ADVISORY_CHECKS`／`_DOCTOR_ADVISORY_CHECKS_WIN` 同步加入 `wal_dir`，避免
shell/daemon 不一致把整體 `doctor` 的 `ok` 拉成 `false`（WARN 語意，不擋）。

同時修正 `README.md`（英/繁中 Logs and Evidence／日誌與輸出章節＋既有誤導範例）與
`docs/serialwrap-spec.md` §10.2（過期的 `/tmp/serialwrap/wal/` 預設路徑敘述＋主動建議
把 `SERIALWRAP_WAL_DIR` 指到 `~/b-log` 的誤導範例），明確區分「WAL（權威）」與
「`~/b-log`（agent on-demand capture）」兩種用途，並說明 systemd daemon 不繼承 shell env
的行為。

回歸 plugin：`f12-wal-path-live`（`cases/f12_diagnostics.py`，F12 診斷保真 family，
非破壞性）驗證 `daemon status` 的 `wal_path` 真實存在、送一筆命令後 mtime 前進（非死
路徑），以及 `doctor` 報告含 `wal_dir` 檢查項；`docs/regression-plugin.md` 的 F12 對照
列同步補上 #148。
