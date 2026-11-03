---
type: feat
scope: command-reboot
---

新增可選 `cmd submit --expect-reboot`（RPC `command.submit` 的 `expect_reboot` 布林）：明示這條命令會重開機，不管命令文字一律走既有 reboot 路徑（送出當下 arm boot quiet window、不送 Ctrl-C／Ctrl-D、由 recovery 等板子回 READY）。過去 broker 只認單獨一條的 `reboot`／`systemctl reboot`／`shutdown -r`／SysRq，`firstboot -y && reboot`、`bcm_bootstate 3 && reboot`、`sysupgrade …` 會被當一般命令、送出當下不 arm 開機靜默窗：shell 不再回 prompt 的板子等到逾時再送 Ctrl-C／Ctrl-D，打在 U-Boot autoboot 倒數上；shell 立刻重印 prompt 的板子（prplOS 非同步 reboot）則被當成正常完成，後續命令落進正在 shutdown 的系統；agent 只能記住「reboot 必須單獨一條」。旗標經 CLI、RPC、submit-time gate、arbiter queue 傳到 execute；未帶旗標時 arbiter callback 仍是既有七參數，`command.get` 一律回報 `expect_reboot`，帶旗標的命令執行前於 WAL 寫 `command_expect_reboot` META，submit 回應回聲 `expect_reboot: true` 以便發現舊版 daemon 默默忽略旗標。

- 串接命令的 prompt 等待：單獨 reboot 維持等 2 秒；帶旗標的串接命令改等 `max(--cmd-timeout, --expected-duration)`。原本固定 2 秒時，前段超過 2 秒就誤入 recovery 分支，之後 prompt 回來（prplOS 的 reboot 非同步）會被 recovery 迴圈當成開機完成、對仍活著的 shell 過早 `confirm_ready()`，session 在板子真正掉線前就回 READY，後續命令落進正在 shutdown 的系統（以 PTY 假 target 重現，修正後 gate 維持 `AUTOBOOT_QUIET`）。
- 與其他參數：`--mode interactive` 帶旗標以 `EXPECT_REBOOT_MODE_UNSUPPORTED` 零 TX 拒絕；非布林值（如字串 `"false"`）以 `INVALID_EXPECT_REBOOT` 拒絕，不做真值轉換；同時帶 `--completion-regex` 時 reboot 優先；開機靜默窗內旗標不繞過 `AUTOBOOT_QUIET`。
- 沒帶旗標、但命令在 `&&`／`||`／`;`／`&` 之後含 broker 認得的 reboot 區段時，submit 回應多一則非致命 `CHAINED_REBOOT_WARNING`（`warning` 維持單一欄位、既有 `CMD_LENGTH_WARNING` 優先，`warnings` 列出全部）。只提示，不改變執行；`_is_reboot_command` 的判定範圍沒有擴大。
- 新增回歸 case `f9-expect-reboot-chained`（真板 reboot 時序，寫成後尚未上板跑過）。
