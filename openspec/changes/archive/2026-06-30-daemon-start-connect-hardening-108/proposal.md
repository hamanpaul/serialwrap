## Why

#108 實證：在已有 system-service daemon 在跑時，顯式 `serialwrap daemon start` 不受監管模式 gate，會另起第二個非託管 daemon（two-reader）；而 daemon-supervision spec 既有的「auto-spawn gate」requirement 早已要求 systemd 模式 MUST NOT 自動 spawn（且點名 `minicom_router` AUTO_START 路徑）。實作上 `_run_daemon_start` 全無 mode 檢查、`should_auto_spawn()` 為 dead code，故 spec 與實作有落差。加上 `_resolve_endpoint` 對 config.yaml `socket_path` 指向不存在 socket 時無 fallback、直接 `SOCKET_ERROR`，使「daemon 健康但 CLI 連不到」難以自癒。

> 註：#108 頭號根因（POSIX daemon 啟動改寫 config.yaml）已由 #84 PORT-4 的 POSIX guard（PR #109 `ad3edbc`）修掉，本變更不重複處理；聚焦其殘留的 daemon-start 冪等性與 endpoint 解析韌性。

## What Changes

- **`serialwrap daemon start` 納入監管模式 gate（對稱於既有 `daemon stop`）**：systemd 模式下 SHALL 重導到 `service start`（不再顯式 spawn 非託管 daemon）；同時讓 `minicom_router` 的 `AUTO_START_DAEMON=1` 在 systemd 模式自動變安全（不再生 coexist daemon）——吸收 #108 建議第 3 點。
- **`daemon start` 在 on-demand 模式冪等**：spawn 前先對目標 endpoint 做 `health.ping`，已有健康 daemon 回應時回 `{ok:true, already_running:true}` no-op，不再 spawn 第二個。
- **`_resolve_endpoint` 對 dangling socket 依 `supervision_mode` fallback**：當 config.yaml 的 `socket_path` 為 unix path 且不可連時，依 `supervision_mode` 推導 canonical endpoint（`systemd-system → SYSTEM_SOCKET`；`systemd-user`/`on-demand → SOCKET_PATH`）；canonical 可連且不同於原值時改用之並於 stderr 提示，皆不可連則回原值讓既有錯誤照常浮現。**CLI 對 config.yaml 維持唯讀**（不 self-heal 改寫，避免重蹈 #108 的 CLI-寫-config 方向）。

不在本變更（結案說明，非 deferral）：`/tmp/sw-coexist-*` GC 與 dead-pid lock GC——前者為測試（`tests/test_human_agent_coexist.py`）tempdir、非 production 產生；後者已被 `SingletonLock`（flock 持有者死亡由 kernel 自動釋放 + stale socket `unlink` 回收）容忍，無需新增 production GC。

## Capabilities

### New Capabilities
<!-- 無新 capability：均屬既有 daemon-supervision 的 requirement 變更 -->

### Modified Capabilities
- `daemon-supervision`: 將 `serialwrap daemon start` 納入「service 子命令包裝 systemctl」與「auto-spawn gate」的監管模式約束（systemd 重導 service start、on-demand 冪等）；新增 CLI endpoint 解析在 config `socket_path` dangling 時依 `supervision_mode` fallback 到 canonical socket 的要求。

## Impact

- **程式**：`sw_core/cli.py`（`_run_daemon_start` 加 mode gate + 冪等探測、`daemon start` subparser 補 `--with-sudo`、`_resolve_endpoint` 加 dangling fallback 與 `_endpoint_alive` helper、`should_auto_spawn` 由 dead code 接回使用）。
- **對外契約**：`serialwrap daemon start` 在 systemd 模式回應新增 `_routed_to:"service start"`、on-demand 新增 `already_running` 旗標；行為對齊既有 `daemon stop`。README / `docs/**` 對齊（R-16/R-18）。
- **不影響**：WAL 格式、event engine、flash/MCU 路徑、Windows TCP endpoint（tcp endpoint 跳過 unix-only 的 dangling fallback）、`daemon-device-identity-hardening`（#100/#101，另案）。
