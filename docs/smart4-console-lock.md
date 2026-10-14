# Smart4 隱藏 console lock

內建 `smart4R-profile` 使用 Linux shell prompt `root@Speedport Smart 4 Typ A/B:...#`
與獨立的 `__READY__<nonce>` 輸出確認可執行命令。Typ A/B 是 prompt 辨識範圍；產品
版本、rootfs 驗證策略與漏洞是否存在，仍須由實機測試各自證明。

## 設定與來源

模板已包含於套件的 `sw_core/assets/profiles/default.yaml`。既有安裝的 setup
不覆蓋使用者修改過的 profile；更新套件後請將此模板同步到實際 daemon 使用的
profile 檔，再重新載入。不要另放一份只含 Smart4 的 YAML 於共享 profile-dir：
目前 loader 的 dynamic template 清單採最後一個 YAML 檔，應在原有模板集合內增加。

```yaml
profiles:
  smart4R-profile:
    platform: shell
    prompt_regex: '(?m)^root@Speedport Smart 4 Typ [AB]:[^\r\n]*# ?$'
    login_regex: '$^'
    password_regex: '$^'
    console_unlock_env: SMART4R_CONSOLE_UNLOCK
    console_unlock_idle_s: 5
    ready_probe: 'echo __READY__${nonce}'
    timeout_s: 120
    hard_timeout_s: 240
    bootloader_prompts: ['(?m)^CFE>\s*$']
targets:
  - com: COM0
    alias: smart4
    profile: smart4R-profile
    device_by_id: /dev/serial/by-id/REPLACE_WITH_EXACT_DEVICE
```

`device_by_id` 須換成現場確認過的實體裝置。模板的解鎖值只能從 daemon 環境或
私有 `env_file` 解析；不接受 profile 內的 literal secret。`console_unlock_idle_s`
必須是有限正數，target 可覆寫兩個欄位。缺少已宣告的來源時 session 停在
`CREDENTIALS_UNRESOLVED`，不送空帳密、不自動重試；補齊來源後手動 attach/recover。

隱藏 lock 無法透過一般 prompt 偵測取得，因此 dynamic detection 跳過此模板。
請使用 explicit target 或先 pin 到 `smart4R-profile`，避免一般 dynamic probe
在 autoboot 階段送出空行。

## 操作與時序

1. 每輪解鎖流程先被動等待至少設定的秒數；任何真實 UART RX 都重設靜默時間。
2. 目前尾行是 CFE prompt 時立即拒絕送字元。歷史 Linux prompt 不作為目前狀態。
3. 已有目前 shell prompt 時直接驗 nonce。新 attach 尚未收到 prompt 時，靜默
   滿額後才送空行辨識，避免把解鎖值當成已解鎖 shell 的命令。
4. 空行沒有取得 shell prompt 時，再等待滿額 RX 靜默，才以 `send_secret` 送出
   解鎖值與 Enter。TX 不進 WAL；此流程適用於不回顯解鎖值的 console lock。
5. 清除舊 buffer 後，要求獨立的 nonce 輸出行與 shell prompt；命令 echo 不算 READY。

```bash
serialwrap session attach --selector smart4
serialwrap session self-test --selector smart4
serialwrap cmd submit --selector smart4 --cmd reboot
```

一般 reboot 與精確的 `echo b > /proc/sysrq-trigger` 都由 broker 進入 recovery。
請分開提交 `sync` 與 reboot，不把 reboot 包在其他 shell 命令或 sentinel 中。
解鎖 profile 的 recovery 可在 generic 180 秒 boot quiet 期間進入**被動** FSM，
但所有寫入仍受實際 RX 靜默與 bootloader 判斷限制；其他 profile 的 gate 不變。

啟用 daemon INFO 日誌時，每次送 secret 前會記錄以下安全 metadata，完全不含值：

```text
console unlock attempt com=COM0 rx_idle_s=5.012 required_idle_s=5.000
```

`CONSOLE_UNLOCK_IDLE_TIMEOUT` 表示在有界等待內沒有取得靜默，
`CONSOLE_UNLOCK_BOOTLOADER` 表示目前仍在 CFE，
`CONSOLE_UNLOCK_PROMPT_TIMEOUT` 表示送 unlock 後沒有取得 shell prompt。
成功的 attach/recover 必須同時取得 READY 與 nonce 證據。
