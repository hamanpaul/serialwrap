# issue 271：fix(command): line 模式輸出結尾沒有換行時，prompt 接在同一行，stdout 被整行裁掉或混入 prompt，仍回報 done

## 現象

`line` 模式的命令輸出若結尾沒有換行，shell prompt 會接在同一行，例如 `printf abc` 的回應是 `abcroot@prplOS:/# `。這一行既是輸出也是 prompt，stdout 會錯，而結果仍是 `status=done`，呼叫端看不出來。

- 套件內行首錨定的 `prompt_regex`（0.4.1 brcm-template 的 `(?m)^(?:[ \t]*|.*[^>#\s])[>#][ \t]*$`，以及 #174 的 0.3.1 版）會把整行當成 prompt，從行首裁掉，**stdout 變成空字串**。
- 舊的不錨定 `(?m)[>#]\s*$` 則只裁掉 `# `，**prompt 留在 stdout**（`abcroot@prplOS:/`）。

常見的觸發命令：`printf`（不加 `\n`）、`cat /proc/device-tree/model` 這類以 NUL 結尾、沒有換行的檔案。

## 重現方式

環境：daemon 0.4.1（code `469f55aa5ccf`），prplOS target，BusyBox ash，prompt `root@prplOS:/# `。

實機（該 target 的 profile 仍是舊的不錨定 regex）：

```bash
serialwrap cmd submit --selector COMx --source agent:repro --mode line --cmd 'printf abc'
serialwrap cmd status --cmd-id <cmd_id>
# status=done，stdout="abcroot@prplOS:/"
```

WAL 中的原始 RX：`printf abc\r\nabcroot@prplOS:/# `

離線（0.4.1 套件內的 `sw_core`，直接呼叫 `_extract_command_stdout()`）：

```python
from sw_core.session_manager import SessionManager
sm = SessionManager.__new__(SessionManager)
raw = "printf abc\r\nabcroot@prplOS:/# "
sm._extract_command_stdout(raw, "printf abc", r"(?m)^(?:[ \t]*|.*[^>#\s])[>#][ \t]*$")
# -> ''
```

| prompt_regex | `echo hi` | `printf abc` | `cat` 無換行、NUL 結尾的檔案 |
|---|---|---|---|
| 0.4.1 套件版（行首錨定） | `hi` | `''` | `''` |
| 0.3.1（#174） | `hi` | `''` | `''` |
| 舊版 `(?m)[>#]\s*$` | `hi\nroot@prplOS:/` | `abcroot@prplOS:/` | `<內容>\x00root@prplOS:/` |

行首錨定版的結果是離線重現；實機上這個 target 還沒換成新 regex，尚未以套件版 profile 實跑。

## 預期行為

- `printf abc` 的 stdout 是 `abc`，不含 prompt。
- 若無法可靠分出輸出與 prompt，至少不要靜默回傳空字串（例如標成 partial 或附警告）。

## 定位

`sw_core/session_manager.py`：`_last_prompt_start()` 取最後一個 prompt match 的 `start()`，`_extract_command_stdout()` 從這個位置裁切。行首錨定時，match 的起點是這一行的行首，也就是輸出的開頭。

## 提案（未定）

- 在登入或 READY 確認時記下實際的 prompt 字面值，裁切時只移除結尾的這段字面值；或
- 裁切位置改為 match 內與已知 prompt 前綴相符之處，而不是 match 起點。

暫時的繞法：命令結尾加 `; echo`，讓 prompt 換到下一行。

## 驗收條件

- [ ] `printf abc` → stdout `abc`
- [ ] `cat` 一個沒有換行、以 NUL 結尾的檔案 → stdout 為檔案內容（NUL 的處理方式明文定義）
- [ ] 有換行的一般輸出不變（`echo hi` → `hi`）
- [ ] #262 的 `=> ` 結尾輸出案例不回歸

## Related

- #262：同類（輸出行被當成 prompt），觸發條件不同。#262 是輸出行本身以 `=> ` 結尾；這裡是真正的 prompt 接在沒有換行的輸出之後。
- #174：prompt_regex 行首錨定。


## 留言

