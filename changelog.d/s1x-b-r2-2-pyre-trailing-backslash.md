---
type: fix
scope: rust-cli
---

修正 Rust CLI 的 `pyre`：樣式以單獨的 `\` 結尾時，比照 CPython `sre_parse` tokenizer 的預讀語意，取走倒數第二個 token 的當下即報 `bad escape (end of pattern)`，優先於該 token 之後才判定的錯誤（如 `[z-a\`、`[\d-z\` 的 bad character range、`[z\A\` 的 bad escape、`a\8\` 的 invalid group reference、`(?P<\` 的 unterminated name、`a{2,1}\` 的 min repeat）；原本通過的 `(?x)a#c\`（註解內讀到結尾）現亦正確報錯。在此之前已判定的錯誤（如 `++\` 的 nothing to repeat）與偶數個反斜線結尾者不變。`profile test` 的 `checks.<regex>.error` 因此與 Python 3.12.3 逐字一致（S1-2a65438-2、S1X-b-X1）。`pyre.tsv` golden 新增 25 筆語料（add-only）。
