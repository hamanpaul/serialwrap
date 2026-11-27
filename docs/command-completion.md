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

## Rust 一般 agent 命令的 prompt 身分

未指定 `completion_regex` 的非 passthrough `line`／`background` agent 命令，
仍先尋找本命令完整 echo。只有清理後行尾錨定、符合既有單一 BusyBox／readline
折行規則的 echo 可成為輸出下界；完成判定與 stdout 使用同一份 RX snapshot，
echo 前的 human 殘餘不進入一般 stdout。

- 真正 prompt 必須有 epoch 內字面證據。READY 證據只在 probe 輸出另有獨立
  nonce 行、其後緊接與探針前 prompt 相同的完整行首 prompt、且兩者都終止於
  snapshot 尾端時登錄；probe 命令自身的 echo、舊 prompt 與只符合寬鬆 regex
  的非同步輸出不算證據。另一來源只能是已確認完整 command echo 的 prompt
  前綴，且 `Path => ` 這類輸出不得冒充 `=>` prompt。`clear`、換 bridge epoch
  與 profile rebind 後舊證據立即失效；證據寫回與完成線性化都會重驗 window
  revision、profile revision、RX watermark／epoch。profile 切換只推進 completion
  snapshot revision；不改第 1 項的 human submit 計數、pending tuple、writer
  guard 或 timeout 消耗規則。
- 一般 prompt 必須位於 echo 後、最後一行行首並結束於 snapshot 尾端。
  `A => `、`Path => ` 這類只符合寬鬆 regex 的輸出不算完成；會繼續等待真正
  prompt。精確且已由 READY 或完整 echo 證實的完整 `router=> ` prompt 仍可辨識；
  `Path => ` 等輸出不可只因 regex 命中成為 evidence。
- stdout 不以換行結尾時，只有已找到本命令 echo 的情況，才可用已知且至少含
  一個字母或數字的完整 prompt 字面作同行 suffix。只移除該 suffix，所以
  `printf abc` 回 `abc`，檔案尾端的 U+0000 仍保留。`> `、`# `、`$ `、`=> `
  等短 prompt 只能在最後一行行首裁切。沒有 echo 時，已知 prompt 也必須位於
  最後一行行首；`abcroot@t:/# ` 只是同一行候選，不得完成。
- 已知短 prompt 與 stdout 黏在同一行，或尾端 regex 已命中、但 prompt 字面
  未知／變更而無可靠邊界時，等待原期限後回 `PROMPT_TIMEOUT`、
  `partial=true`、`recovery_action=NONE`；可用 stdout 保留，不重送命令、
  不額外送 Ctrl-C／Ctrl-D。可檢驗的「候選」限定為：(1) 清理後最後一行從
  行首到尾端完整符合 profile prompt regex；或 (2) snapshot 尾端完整等於
  epoch 內已知 prompt 字面，包含不能安全裁切的同行 suffix。完全兩者皆
  不成立時才是無候選，無 human 訊號的 timeout 沿用基線 rescue 結果與 TX。
- 沒有 human TX 交錯的無 echo 終端仍可由可信尾端 prompt 完成，不要求製造
  echo，而且 prompt 必須在最後一行行首。若判斷視窗已修剪掉命令 TX 後的必要早期 RX，回
  `ok:true`、`partial:true`、不帶 error_code，stdout 只含仍保留的部分；
  echo 仍在視窗內時維持 `partial=false`。
- 有 human TX／pending 人類行而找不到可辨識 echo 時，才依既有 human 交錯
  規則在原等待上限回 partial；一般無 echo 且無 human 訊號不沿用這項判準。
- silence chunk 與絕對 deadline 兩入口採同一矩陣：無 human 且無候選時維持
  9af187ca 的 timeout／recover_after_failure 與救援 TX；有候選但證據不足時
  回 `CompletionUncertain`、`PROMPT_TIMEOUT`、`partial=true`、
  `recovery_action=NONE`、保留輸出且不送救援鍵。human 路徑仍沿用第 1 項
  已定的線性化與 pending 規則。

`--completion-regex` 的 producer 路徑不要求 echo，也不加入以上 prompt 證據規則。

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
覆蓋；Cargo 與 RPC 覆蓋 `=> ` 分段輸出、無換行 `abc`、NUL、短／未知 prompt、
READY／echo 前綴證據來源、clear 失效與 RX 修剪 partial。產品 rootfs／factory
reset 的真機時序由獨立 TestPilot ST-06 case 驗證。
