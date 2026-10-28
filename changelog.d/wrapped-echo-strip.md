---
type: fix
scope: command-output
---

修正長命令的回顯被終端折行拆成多行時殘留在 line mode `stdout` 的問題。serial console 的 shell 回顯打到第 80 欄會折行（BusyBox ash 插入 `\r\r\n`、bash readline 補 `" \r"`），過去 `_extract_command_stdout` 只逐行比對 `endswith(command)`，折行後每一行都只有命令的一部分，整段回顯留在輸出開頭。現在單行比對沒剝到時，把開頭幾行接回、去空白後與完整命令比對再剝；命令字樣必須從第一行開始（容許前面殘留 prompt），單行回顯已剝掉時不再重複比對。實測目標板（BusyBox 1.36.1）上 87–123 字元的命令過去 stdout 首行是被折行的回顯，修正後為實際輸出；短命令行為不變。
