---
type: fix
scope: rust-daemon
---
- 新增 issue 282 停機行收尾所需的待凍結 Fence 狀態與傳輸持有者軟喚醒基礎。
- echo-paced 傳輸持有命令列並在 750 ms 內完成行尾或送 `\x15\n` 清除半行；停機對所有 session 共用最多 1250 ms，並從既有 drain 預算扣除耗時。
- 停機旗標與待凍結／凍結狀態在同一 fence 臨界區發布，避免持有者落入誤拒窗口而留下半行。
