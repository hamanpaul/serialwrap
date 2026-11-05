---
type: change
scope: tests
---
收緊正式 bundle 的 `test-fakes` 守門（cargo argv 的 `--features`）、L1.5 等待測試（LF 寫入前不得返回），以及 changelog fragment 的 policy_check 解析。README 的 0.3.x 升級步驟改為 OD-48 已可在 daemon 執行中升級；可觀察變更表更正 Rust CLI 已接受 `--expect-reboot`。
