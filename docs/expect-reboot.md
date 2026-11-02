# 明示 reboot（`cmd submit --expect-reboot`）

broker 判定「這條命令會重開機」只認單獨一條的 `reboot`、`systemctl reboot`、
`shutdown -r` 與 `echo b > /proc/sysrq-trigger`。`firstboot -y && reboot`、
`bcm_bootstate 3 && reboot`、`sysupgrade …` 這類串接或自帶重開機的命令會被當一般命令：
送出當下不會 arm 開機靜默窗。shell 之後不再回 prompt 的板子，broker 等到逾時再送
Ctrl-C／Ctrl-D，正好打在 U-Boot autoboot 倒數上；shell 會立刻重印 prompt 的板子
（prplOS 的 `reboot` 非同步）則被當成正常完成，後續命令落進正在 shutdown 的系統。

`cmd submit --expect-reboot` 讓呼叫端明示「這條命令會重開機」，不管命令文字一律走
既有 reboot 路徑，呼叫端不必再記「reboot 必須單獨一條」。

```bash
serialwrap cmd submit --selector COM0 --source agent:fw \
  --cmd 'firstboot -y && reboot' --cmd-timeout 30 --expect-reboot
```

## CLI／RPC／結果契約

- CLI：`cmd submit --expect-reboot`（旗標，省略＝不明示）。
- RPC：`command.submit` 的可選 `expect_reboot` 布林；省略／null 視同 false。
  只接受 JSON 布林，字串 `"false"`、整數等一律以 `INVALID_EXPECT_REBOOT` 零 TX 拒絕
  （不做真值轉換，避免 `"false"` 被當成 true）。
- 傳遞：CLI → RPC → service submit-time gate → arbiter queue → `execute_command`。
  只有帶旗標時才在 arbiter callback 多傳 `expect_reboot=True` keyword，未帶旗標的
  七參數 callback 契約不變（與 `completion_regex` 同一做法）。
- `command.submit` 成功回應在帶旗標時多一個 `"expect_reboot": true` 回聲；舊版 daemon
  會默默忽略未知參數，回應沒有這個欄位就代表旗標沒生效。
- `command.get.command.expect_reboot` 一律回報（true／false）。帶旗標的命令在執行前
  於 WAL 寫一筆 `META`：`event: command_expect_reboot`，供稽核「為何走 reboot 路徑」。

## 與其他參數的互動

| 情境 | 行為 |
|---|---|
| `--mode interactive` | 拒絕，`EXPECT_REBOOT_MODE_UNSUPPORTED`，零 TX。interactive 是 lease 按鍵通道，不經 reboot 路徑，旗標在該模式沒有任何效果；默默忽略會讓呼叫端誤以為已受保護 |
| `--mode line`／`background` | 都走 reboot 路徑。與單獨 `reboot` 一樣，background 不建立 capture（沒有 `result-tail`） |
| 同時帶 `--completion-regex` | reboot 優先：regex 通過驗證但不使用，也不寫 `command_completion` META；`command.get` 兩個欄位照實回報。與單獨 `reboot` 搭配 `--completion-regex` 的既有行為一致 |
| 開機靜默窗內、或 READY 尚未再確認 | 旗標**不繞過** gate：submit-time／execute-time 照常回可重試的 `AUTOBOOT_QUIET`（或不可重試的 `READY_UNCONFIRMED`），零 TX |
| `source=human:*` | 沿用既有 reboot 路徑的 `PROMOTE_HUMAN_INTERACTIVE` |

## 串接命令的時序

reboot 路徑在送出命令**當下**就 arm 開機靜默窗，然後等 shell 的 prompt 回來：

- **單獨一條 reboot**：prpl／OpenWrt 的 `reboot` 非同步，shell 毫秒內重印 prompt，等 2 秒足夠。
- **帶旗標的串接命令**（`A && reboot`）：要先跑完 `A` 才輪到 reboot，等待上限改為
  `max(--cmd-timeout, --expected-duration)`。若仍用固定 2 秒，`A` 超過 2 秒就會誤入
  recovery 分支；之後 prompt 回來（非同步 reboot）會被 recovery 迴圈當成開機完成、
  對仍活著的 shell 做 `ensure_ready` 而過早 `confirm_ready()`，session 在板子真正掉線前
  就回 `READY`、gate 放行——正是 #162 要避免的 stale-READY。

等待期間 prompt 回來，處理與單獨 `reboot` 的 prompt 回顯相同：解除 TX 靜默，**保留**
READY 再確認 pending，回 `ok`／`partial: false`，agent 後續命令由 `AUTOBOOT_QUIET` 擋到
nonce 確認 probe 成功。沒等到 prompt（同步重開、`sysupgrade` 自行重開、前段太久）則走既有
recovery：`partial: true`、`status: recovering`、`recovery_action: EXPECT_REBOOT`。

呼叫端須知：

- `--cmd-timeout` 要涵蓋 reboot 之前那一段的耗時（長的再加 `--expected-duration`）。超過時走
  recovery 分支，沿用上述過早 READY 的風險。
- 前段若可能超過開機靜默窗（180 秒），該段不受 autoboot 保護；改成兩條提交，前段單獨跑完再
  `--expect-reboot` 送最後的 reboot。
- 前段失敗而 reboot 沒執行（`firstboot -y` 失敗而 `&&` 短路）時 prompt 照樣回來，回應是
  `ok`，錯誤要看 `stdout`；session 的 gate 會由 reprobe 的 nonce 確認自動放行，不需處理。
- 送出後等 `session list` 的 `ready_reconfirm_pending` 變 false、state 為 `READY`，或直接重試
  `AUTOBOOT_QUIET`（`retry_after_s`），做法同單獨 `reboot`。

## 沒帶旗標時的非致命提示

`command.submit` 在**沒帶** `--expect-reboot`、命令在 `&&`／`||`／`;`／`&` 之後出現
broker 認得的 reboot 區段（同 `_is_reboot_command` 的認法）時，成功回應多一則提示：

```json
{"ok":true,"cmd_id":"…","warning":{"code":"CHAINED_REBOOT_WARNING","segment":"reboot","hint":"…"},"warnings":[{"code":"CHAINED_REBOOT_WARNING","…":"…"}]}
```

- 只提示，**不改變執行**：命令照常入列、照舊當一般命令跑。`_is_reboot_command` 的判定範圍沒有擴大。
- `warning` 維持單一欄位，既有 `CMD_LENGTH_WARNING` 優先；`warnings` 列出全部，僅在有此提示時才出現，
  沒有新提示的回應與舊版相同。
- 引號內的 `&& reboot`、`echo reboot && ls`（reboot 只是參數）、`interactive` 模式不提示。
- 不涵蓋 `sysupgrade` 等自帶重開機的命令（沒有 `reboot` 字樣可認），這類命令請直接帶旗標。

## 回歸驗證範圍

pytest 覆蓋旗標傳遞、路由、互動規則、串接命令的 prompt 等待（含 PTY 假 target 前段 2.4 秒後重印 prompt）
與提示。真板的 reboot 時序（shutdown 到 U-Boot banner 的間隔、askconsole、boot_done_regex 解除時點）
屬實機才驗得到，不在 pytest 範圍。
