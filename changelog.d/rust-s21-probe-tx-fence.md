---
type: fix
scope: rust-s21
---
Rust SessionManager 動態偵測的 PROBE `\n` 改經 TxWriter／Fence 送出（單一寫入者、TX WAL 由 TxWriter 寫一筆），不再直接寫 OS；PROBE 與正式 bridge 仍共用同一預開 handle。
