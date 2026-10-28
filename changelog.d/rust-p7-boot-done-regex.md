### Added

- Rust daemon 承接 #234 `boot_done_regex`：boot quiet 期間 RX 命中 profile 的開機完成訊號即提早解除 quiet，與 `login_regex`／`prompt_regex` 同列判定、受 `bootloader_prompts` 尾行例外約束、無效 regex 靜默略過；不清 `ready_reconfirm_pending`（仍由 nonce probe 確認）。
