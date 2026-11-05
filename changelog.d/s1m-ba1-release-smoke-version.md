---
type: fix
scope: release
---

修正 Linux release bundle smoke 的版本檢查：只接受 Python 基線的純版本行或附 12 位小寫十六進位程式碼指紋的整行，避免正式 Rust bundle 因合法指紋被拒絕；錯版、格式錯誤與多餘輸出仍會失敗。
