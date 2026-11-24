# issue 262：fix(command): line 模式把以 "=> " 結尾的輸出行當成 prompt，stdout 被截斷卻回報 done

## 現象

`line` 模式的命令，只要輸出中有一行以 `> ` 結尾，就可能被當成 prompt 而提前結束。stdout 從那一行被截斷，但結果是 `status=done`、`partial=false`、`error_code=null`，呼叫端看不出資料少了。

prplOS 上 `obuspa -c get` 對空字串參數會印出 `Path => `（`=>` 後只有一個空白），很容易遇到。

## 重現方式

環境：daemon 0.3.2，session 使用 `brcm-template`，target 為 prplOS。

```bash
serialwrap cmd submit --selector COMx --source agent:repro --mode line \
  --cmd 'obuspa -c get Device.DNS.Diagnostics.'
serialwrap cmd status --cmd-id <cmd_id>
```

離線也能看到 regex 會命中這種行：

```bash
printf 'X.Timeout => 0\nX.DNSServer => ' > sample.txt
serialwrap profile test --profile brcm-template --sample sample.txt
# prompt_regex.matched = true, matched_line = "X.DNSServer => "
```

目前套件內的 `(?m)^(?:[ \t]*|.*[^>#\s])[>#][ \t]*$`、0.3.1 的 `(?m)^(?:.*[^>#\s])?[>#][ \t]*$`，以及舊的 `(?m)[>#]\s*$` 都會命中。

## 預期行為

stdout 包含到真正 shell prompt 之前的所有行，最後幾行是：

```text
Device.DNS.Diagnostics.NSLookupDiagnostics.DNSServer => 
Device.DNS.Diagnostics.NSLookupDiagnostics.HostName => 
```

## 實際行為

`status=done`、`partial=false`，耗時約 60 ms，stdout 結尾是：

```text
Device.DNS.Diagnostics.NSLookupDiagnostics.NumberOfRepetitions => 0
Device.DNS.Diagnostics.NSLookupDiagnostics.DNSServer =
```

`> `、`HostName` 行和真正的 prompt 都不見了。同一條命令接上 `| sed 's/$/ ;/'`，讓每行不再以空白結尾，就能完整取回到 `HostName` 行。

這是時機相關的問題。同一份輸出中較早的 `...Interface => ` 行沒有觸發截斷，另一次對其他空值參數的 `get` 也重現過。

## 原因（讀碼，main `c90159c`）

- 非 passthrough 的 line 模式以 `bridge.wait_for_regex_from(completion_regex or prompt_regex, pre_offset, ...)` 等待完成。
- `wait_for_regex_from` 每 50 ms 對**命令送出後的整段 RX** 做 `regex.search(snapshot)`。prompt regex 帶 `(?m)` 和 `$`，所以任一行以 `>`/`#` 加水平空白結尾就算命中，不限於 RX 尾端。
- `_extract_command_stdout` 再用 `_last_prompt_start` 在**最後一個**命中處截斷。截斷時剛好收到的最後一行若以 `=> ` 結尾，就會切在 `=` 後面，與上面的輸出相符。

推論（未驗證）：判定完成之後才到的剩餘輸出和真正的 prompt，不會算進這筆命令。若它們在下一筆命令取 `pre_offset` 之後才到，可能讓下一筆命令提前完成或混入舊輸出。

## 暫時繞法

呼叫端自帶唯一結束標記並用 `--completion-regex`，例如命令後接 `; echo __DONE_<nonce>__$?`，再以該標記判定完成。這樣不受 prompt regex 影響。

## 提案（未定）

- 完成判定只看 RX 尾端，例如 prompt 必須是 snapshot 的最後內容（`\Z`），而不是任何一行的行尾。
- 命中後加一段很短的 RX 靜默確認，期間若還有新資料就繼續等。
- 若無法確定，至少不要回報 `partial=false`。

## 驗收條件

- [ ] 輸出中有 `Path => ` 行（含剛好停在 RX 尾端、換行稍後才到的情況）時，line 模式不會提前完成，stdout 完整到真正 prompt 之前。
- [ ] 回歸測試：fake bridge 分段送出 `A => \n`、`B => `、停頓超過 poll 間隔、`\nC => 1\n`、真正 prompt，stdout 包含 A、B、C 三行。
- [ ] 既有 prompt 判定行為不變，包括 #174、#224 的樣本與登入流程。
- [ ] `--completion-regex` 的行為不變。

## 非目標

- 不改 login FSM 的 prompt 判定。
- 不處理 passthrough 模式。

## Related

- #229：producer 回應完整但缺 prompt 時等到 timeout。方向相反，是完成太晚。
- #174：prompt_regex 過鬆造成登入誤判。這次的問題在 line 模式的完成判定，收緊 regex 也擋不住合法的 `Path => ` 行。


## 留言

