---
type: fix
scope: rust-refactor
issue: 241
---

`doctor` 的 `version_pairing` 在 release manifest 的 `components.daemon.code_fingerprint` 不是 12 位小寫十六進位時，回報該欄位自己的 key，不再落成 `unreadable`。缺欄的舊 manifest 仍略過。
