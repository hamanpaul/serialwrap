---
type: fix
scope: command-output
---

修正 #237 折行回顯剝除的誤判：舊判定把命令與輸出都去掉所有空白後比對，echo 關閉時若輸出剛好等於命令去掉空白（例如 `cat file` 印出 `catfile`），整行有效 stdout 會被當成回顯刪掉，不需要發生折行就會觸發。現在只接受「命令原文加上終端折行插入的字元」：命令本身的每個字元（含空白）都必須依序出現，只有換行（BusyBox ash）或多出的一個空白（bash readline）可以跳過，插入點彼此至少相隔 20 個字元，且回顯須結束在行尾。既有 BusyBox／readline 折行與帶 prompt 前綴的案例行為不變。
