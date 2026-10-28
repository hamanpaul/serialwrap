---
type: fix
scope: command-output
---

修正長命令回顯被終端折行拆成多行時殘留在 line mode `stdout` 的問題，同時保留命令中的有效空白。只有原始串流帶 BusyBox ash 的 `\r\r\n` 折行標記時才跨行接合；bash readline 的單行 `" \r"` 插入空白則由呼叫端只在辨識到該控制字元後處理。一般 stdout（例如 `cat file` 輸出 `catfile`，或普通換行輸出 `echo x `／`y`）不會因去除空白或跨行拼接而被誤刪。新增兩種反例回歸，既有 BusyBox／readline 折行測試仍通過。
