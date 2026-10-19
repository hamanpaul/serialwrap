---
type: feat
scope: command
---
passthrough 命令以明確的 completion regex 或可用的 profile prompt 判定完成；沒有完成條件時，等待 RX 靜默或逾時後回傳 `PROMPT_TIMEOUT` 與已擷取輸出，不送 Ctrl-C／Ctrl-D，session 維持 `READY`。`file.push`／`file.pull` 對 passthrough 一律以零 TX 拒絕。
