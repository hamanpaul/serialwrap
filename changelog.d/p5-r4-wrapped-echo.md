---
type: fix
scope: rust-s21
---
Rust daemon 的 line mode stdout 承接長命令折行回顯剝除（PR 237 修正後語意）：BusyBox `\r\r\n` 與 readline `" \r"` 折行的命令回顯會被剝除；只接受命令原文加上換行或單一空白的插入（插入點間隔至少 20 字元、自第一行開始並止於行尾），輸出剛好等於命令去空白（`cat file`→`catfile`）等情況不剝。Slice 2.1 黑箱案例依 OD-36～OD-38 修正前置與判準（DF-20／DF-21 逐列標記、METHOD_NOT_FOUND 為 Rust 專屬）。
