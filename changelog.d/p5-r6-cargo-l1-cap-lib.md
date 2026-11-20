---
type: fix
scope: rust-s21
---
`scripts/rust/cargo_l1_results.py` 的能力測試判定改以 libtest 名稱最後一段比對 `cap_<cap>__`：lib 單元測試帶模組路徑（如 `console_pty::posix::tests::cap_posix_pty__*`）先前不被 `--caps` 執行且 skip_kind 記成 other。
