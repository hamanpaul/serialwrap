# 每命令完成條件

部分 console 持續輸出非同步 log，命令已完成卻沒有可辨識的 shell prompt。
`cmd submit --completion-regex` 讓呼叫端為單一 `line`／`background` 命令指定完成
條件；其他命令、省略此參數的呼叫與 profile 設定均維持原行為。

```bash
serialwrap cmd submit --selector COM0 \
  --cmd 'echo value; rc=$?; printf "__RUN_RC__=%s\n__RUN_END__\n" "$rc"' \
  --completion-regex '(?m)^__RUN_RC__=[0-9]+\r*\n__RUN_END__\r*$'
```

範例標記須由呼叫端替換為每次唯一的 nonce。Regex 只比對本命令送出後的新 RX，
不接受 buffer 內歷史完成標記。完成條件應同時要求行首錨定的 numeric RC 與獨立
END 行，避免 shell 回顯含有字面標記的命令被誤當成執行結果；`\r*` 可容忍板端
ONLCR 產生的多個 CR。呼叫端仍須驗證唯一 payload、BEGIN／END 與實際 RC 值，
broker 的非 partial 完成不代表 target 命令成功或測試通過。

## CLI／RPC／結果契約

- CLI：`cmd submit --completion-regex <pattern>`。
- RPC：`command.submit` 的可選 `completion_regex` 字串；省略／null 使用 profile
  prompt。
- 檢查：最多 1024 UTF-8 bytes、可用 Python `re` 編譯、非空且不匹配空串。
  CLI、RPC、arbiter 與直接 `execute_command` 均於排隊／UART TX 前拒絕無效值，
  錯誤碼為 `INVALID_COMPLETION_REGEX`。
- `command.get.command.completion_regex` 保留指定值，未指定為 null。正常命令
  執行前的 WAL META 帶 `event: command_completion`、`completion_regex` 與 cmd_id。
- 等待完成改用此 regex；stdout 擷取仍使用 profile prompt，但僅裁切完成標記
  之後的 prompt。前置 prompt／echo redraw 不會裁掉其後的 producer 結果，完成
  標記保留於 stdout。
  超時、partial、recovery 與結果擷取保留既有語意。
- `reboot`／精確 SysRq reboot 仍優先走既有 recovery；此參數不取代開機／READY
  驗證。Interactive 流程仍使用自己的 lease／按鍵契約。

既有未指定參數的七參數 arbiter callback 維持相容；只有指定完成 regex 時才透過
新增的可選 keyword 傳到 execute。

## Passthrough 完成條件（SC-35）

`platform=passthrough` 的 session（含 `others-template`）一經 explicit target、`session pin`
或 sticky 選定，attach 時不 login、不送 `ready_probe`，停在 `ATTACHED`（零 TX）；連續 30 秒
沒有 RX 自動升 `READY`，之後才可下 `line`／`background` 命令。動態偵測（僅在沒有 template
匹配時才選中 passthrough）與 `fallback` session 的每次 re-attach 仍會先由獨立的 PROBE bridge
送一個 `\n` 偵測探測；要避免請 pin 該 profile 或使用 explicit target。passthrough 命令的完成條件依序判定：

1. 有 `completion_regex`（`cmd submit --completion-regex`）→ 以該 regex 完成，即上
   方的 producer 完成條件。
2. 否則 profile 的 `prompt_regex` 通過 `valid_completion_regex`（可編譯、非空、不匹配
   空字串）→ 以 prompt 完成。
3. 前兩層皆無可用條件（如 `others-template` 的 `.*`），或已指定的 regex／prompt 始終未命中 → 沿用既有等待迴圈：有新 RX 就續等；連續
   `min(timeout_s, 30)` 秒沒有新 RX，或達到 effective timeout 即停，回傳

   ```json
   {"ok": false, "error_code": "PROMPT_TIMEOUT", "partial": true, "recovery_action": "NONE"}
   ```

   已擷取的 RX（經 `clean_text`）保留在 background capture chunks，應以 `command.result_tail`（`serialwrap cmd result-tail --cmd-id`）讀回；`command.get` 的 `stdout` 為空，不含此段輸出。

passthrough 未命中完成條件時，不送 Ctrl-C（`\x03`）與 Ctrl-D（`\x04`）、不轉
`ATTACHED`、不 force recover，session 維持 `READY`；`reboot` 命令不套用特例。
`file push`／`file pull` 對 passthrough（任何 state）回 `PROFILE_NOT_COMMAND_CAPABLE`
且零 TX。`mode=interactive` 行為不變，仍使用 lease 與按鍵契約。

建議：console 不送 prompt 時，每次 `cmd submit` 都帶 `--completion-regex`，不要依賴第 3
層的逾時判定。

## 回歸驗證範圍

PTY 測試使用 producer 先回顯命令、再輸出 numeric RC／END 並刻意不送 shell
prompt，驗證 line／background 非 partial 完成與完整 stdout。另覆蓋 echo-only、
歷史 marker、無效 regex 零 TX、預設 prompt、CLI→RPC→queue→execute 傳遞、結果
metadata、前置 prompt 不截斷 stdout 與 reboot 不覆寫。此 broker 行為可由 pytest
覆蓋；產品 rootfs／factory
reset 的真機時序由獨立 TestPilot ST-06 case 驗證。
