## Context

`sw_core/cli.py` 的 `_run_daemon_stop` 在 systemd 模式已會重導到 `service_action("stop", ...)`（service_ctl.py），但對稱的 `_run_daemon_start` 完全沒有監管模式檢查——它無條件 `subprocess.Popen(serialwrapd --socket SOCKET_PATH ...)`。`should_auto_spawn()` 雖已定義（systemd→False、on-demand→True）卻無任何呼叫點（dead code），且 CLI 不存在 lazy-spawn-on-connect 路徑，唯一的自動 spawn 來自 `minicom_router.sh` 顯式呼叫 `serialwrap daemon start`。`SYSTEM_SOCKET = "/run/serialwrap/serialwrapd.sock"` 已是 `setup_cmd.py` 既有常數並已 import 進 `cli.py`。`_resolve_endpoint` 以 `return cfg_sock or args.socket` 結尾，對 config.yaml `socket_path` 不做任何可連性檢查。

## Goals / Non-Goals

**Goals:**
- 顯式 `serialwrap daemon start` 不再於 systemd 模式繞過 unit 管理另起非託管 daemon；on-demand 模式冪等。
- config.yaml `socket_path` 失聯時，CLI 能依 `supervision_mode` 自動連到 canonical daemon，而非直接 `SOCKET_ERROR`。
- 行為與既有 `daemon stop` 路由、`SingletonLock` 防雙開一致，最小變更面。

**Non-Goals:**
- 不處理 #108 已被 #84 PORT-4 POSIX guard（PR #109）修掉的 config.yaml 改寫根因。
- 不新增 `/tmp/sw-coexist-*` 或 dead-pid lock 的 production GC（測試產物 + 已被 flock/socket-probe 容忍）。
- 不 self-heal 改寫 config.yaml；不動 Windows TCP endpoint 路徑。

## Decisions

**D1：`daemon start` 在 systemd 模式重導 `service start`（而非「拒絕報錯」）。**
重導與既有 `daemon stop`→`service stop` 對稱、語意一致，且讓 `minicom_router` AUTO_START 自動變安全（吸收 #108 建議 #3），使用者體驗無斷裂。替代方案「systemd 模式直接回錯誤要使用者改用 service start」較生硬、且不修復 minicom 路徑。實作：`_run_daemon_start` 開頭讀 `mode()`，`mode.startswith("systemd")` → `service_action("start", mode=mode, with_sudo=args.with_sudo)`，回應加 `_routed_to`；`daemon start` subparser 補 `--with-sudo`（鏡像 stop）。`should_auto_spawn()` 由此接回使用（systemd → 走重導分支）。

**D2：on-demand 冪等用 client 端 `health.ping` 預檢，而非僅靠 `SingletonLock`。**
`SingletonLock` 已能在第二個 daemon bind 時擋下（`DAEMON_ALREADY_RUNNING`），但那是「spawn 一個註定失敗的行程再被擋」；client 端先 `health.ping`，命中即回 `already_running:true`，避免無謂 fork/exit 與誤導性輸出。替代方案「只靠 SingletonLock」會留下短命行程與雜訊。

**D3：#2 fallback 以 `supervision_mode` 推導 canonical endpoint，不掃 /proc。**
#108 實際故障模式是 `socket_path` 被指歪但 `supervision_mode` 仍正確，故 mode 是最可靠線索；查表（systemd-system→SYSTEM_SOCKET、其餘→SOCKET_PATH）確定性高、不把 CLI 熱路徑耦合到 `/proc` 掃描。替代方案「複用 #101 multi_open 掃 /proc 發現 daemon」較重、且對單純 dangling 屬殺雞用牛刀。helper `_endpoint_alive(ep)`：unix path 用 0.2s connect probe，非 unix 一律回 True（跳過）。

**D4：fallback 僅在「來源為 config-derived 且不可連」時觸發，且不覆蓋明確 `--endpoint/--socket`。**
保留既有優先序語意；明確指定者最高優先、完全不進 fallback，避免使用者明確意圖被悄悄改寫。fallback 命中時 stderr 印一行提示（stdout 仍是乾淨 JSON，不破壞既有腳本解析）。

## Risks / Trade-offs

- [systemd-system 重導需 sudo，無 `--with-sudo` 時 `service_action` 只回報待跑指令] → 與既有 `daemon stop`/`setup` 行為一致，回應已含可複製的 sudo 指令；不靜默失敗。
- [`health.ping` 預檢增加一次 RPC round-trip 與短逾時] → 僅 on-demand `daemon start` 路徑、逾時 0.5s，可忽略；命中時反而省下一次 spawn。
- [fallback 連到「另一個」daemon 的風險] → 僅依 `supervision_mode` 推 well-known canonical，不亂猜；且只在原 socket 不可連時啟用，canonical 不可連即回原值不擴大連線面。
- [stderr 提示可能干擾只看 stderr 的腳本] → 提示僅一行、stdout JSON 不變；屬可接受的診斷輸出。

## Migration Plan

純 CLI 行為強化，無持久化/格式變更。部署＝`pipx install --force` + `serialwrap setup` + 重啟服務。回滾＝還原 `sw_core/cli.py` 即可，無資料遷移。README / `docs/**` 對 `daemon start` 新行為（`_routed_to`/`already_running`、dangling fallback 提示）對齊（R-16/R-18）。

## Open Questions

無（scope 與 #2 fallback 策略已於 brainstorm 拍板：聚焦 #1+#2、依 supervision_mode 推 well-known、不 self-heal、不做 GC）。
