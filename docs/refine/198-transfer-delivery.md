# #166 傳輸可攜性與單行預算

## 設定

`profiles.<name>.max_console_line_chars` 是可選的正整數；未設定為 `null`，表示沿用
既有命令長度行為。`targets` 可用同名欄位覆寫 template。長度以實際送出的 shell 命令
UTF-8 byte 計算，不含最後的換行；`bool`、字串、浮點數、零與負數都會在 profile 解析時
拒絕。

```yaml
profiles:
  prpl-template:
    platform: prpl
    max_console_line_chars: 505
```

## 工具探測與 fallback

push 先用已知 payload 做真正的 base64 解碼驗證；pull 先做真正的 base64 編碼驗證。
候選失敗後再驗證 `openssl enc -base64 -d -A`／`openssl enc -base64`，而不是只看
`which` 或命令回顯。兩路都沒有實際成功 sentinel 時，分別回傳
`TARGET_DECODER_MISSING` 或 `TARGET_ENCODER_MISSING`。
若 shell probe 本身逾時或發生語法／執行環境錯誤，分別保留 `TRANSFER_TIMEOUT` 或
`TRANSFER_PROBE_FAILED`，不會降級成工具缺失。

每一段 push、checksum、mv 與 pull 的資料命令也必須回傳 execution sentinel；工具在
探測成功後失效時，不會因 prompt 回來就算成功，而會回傳明確的
`TARGET_DECODER_FAILED`／`TARGET_ENCODER_FAILED`；即使 encoder 先吐出未換行的 partial
bytes，後續 failure sentinel 黏在其後仍會辨識，真正缺 marker 則保留
`PULL_PARSE_FAILED`。pull 的遠端 md5 缺失或無法解析時回傳 `CHECKSUM_VERIFY_FAILED`，
不寫出未驗證的本地檔案；GNU `md5sum` 對含反斜線檔名的 escaped digest 格式也會解析。

## 單行成本

所有命令在 TX 前以 `len(command.encode("utf-8"))` 檢查。push 的 raw chunk 上限依兩種
decoder／append 樣板中較長的保守 shell overhead 計算；probe 選定工具後，仍會再驗證
實際 decoder 的完整命令：

```text
raw_capacity = (budget - max_shell_overhead) // 4 * 3
effective_chunk = min(user_chunk_size, raw_capacity)
```

為避免設定值放大出與檔案無關的暫存配置，完整命令 preflight 的 probe payload 長度再取
`min(effective_chunk, len(data))`；實際資料分段仍只依檔案內容送出。

目前命令樣板的代表值（暫存路徑長度 28 bytes；mv 目的路徑亦取 28 bytes）為：decoder
probe 282 bytes、encoder probe 273 bytes；空資料 chunk 為 base64 100 bytes、OpenSSL
116 bytes（`>>` append），md5 75 bytes、mv 99 bytes、cleanup 78 bytes。實際路徑較長時
以產生後的完整命令為準。

因此 `max_console_line_chars: 505` 會自動縮小使用者指定的 `chunk_size`；若 probe、固定
命令或空 chunk 都放不進預算，會回傳 `CONSOLE_LINE_LIMIT_TOO_SMALL` 且不送出資料命令。
以上述代表路徑計算，最長 OpenSSL append 樣板為 116 bytes，505 budget 的保守 raw
上限為 `(505 - 116) // 4 * 3 = 291` bytes。目前資料 chunk 使用固定長度的暫存名稱，
目的路徑較長只影響完整 mv 等控制命令的預算檢查，不會再依目的路徑縮小 chunk。
`0` 或其他無效值不是合法的「小預算」，會在 profile 解析時拒絕。

## 回歸範圍與未驗證項

pytest 以受控 shell bridge 驗證僅有 OpenSSL 時的 binary／空檔與反斜線檔名 push→pull、
checksum、命令 echo、marker、md5／mv collision、encoder 負向路徑與 505-byte 預算；
YAML target 的繼承／覆寫／null 清除也經 `load_profiles()` 驗證。F7 regression probe
也會把 OpenSSL fallback 視為可用工具鏈，不會因缺少 base64 提前 SKIP。

本變更未操作真實 UART、daemon、live config/state/WAL，也未執行 TestPilot 真板回歸。
仍須在有 prpl 505-byte 單行限制與 bcm／prpl 實際工具組合的 bench 上驗證部署 profile、
長檔 throughput 與 1MB pull 的 RX 視窗邊界。
