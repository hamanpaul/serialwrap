---
type: fix
scope: rust-p7
---
`sw_proto::pyre`：`(?P` 後接非 `<`／`=` 字元時，錯誤訊息依 CPython 3.12 `sre_parse` 帶出下一字元（`unknown extension ?P>`，跳脫取兩字元 `?P\d`）；pattern 於 `(?P` 結束改報 `unexpected end of pattern`，結尾孤立反斜線報 `bad escape (end of pattern)`（審查 L2）。
