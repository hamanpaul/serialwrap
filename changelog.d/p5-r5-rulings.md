---
type: fix
scope: rust-s21
---
Rust daemon line mode 折行回顯剝除改依修正 PR（`fix/wrapped-echo-false-positive`）語意：只接受「命令原文＋終端折行插入字元」，命令字元（含空白）須依序全中，僅可跳過換行或多一個空白，插入點（第一個以首行欄位計、含殘留 prompt）彼此至少相隔 20 字元，首字前不得插入，回顯須從首行開始並止於行尾；插入字元另須有原始串流證據（BusyBox `\r\r\n`／readline `" \r"`），一般 CRLF stdout 不拼接。
