---
type: fix
scope: session
---

session 自 attach 起從未收到任何 RX、probe 期間連 echo 都沒有時，`last_error` 仍是 `PROMPT_UNAVAILABLE`，但 `last_error_detail` 改附分收發方向的提示（DUT 電源與埠、DUT TX 到轉接器 RX 與 GND、host 端 USB 路徑），不再是 `null`。`session list`、`session self-test` 回傳的 session 都看得到。這種情況過去只回 `PROMPT_UNAVAILABLE`，看不出和開機窗、prompt regex 不符有什麼不同，容易被誤導去反覆 recover 或換 profile。曾經有過 RX、之後才凍結的情況照舊由 `TRANSPORT_STALL` 處理（#150）。Rust daemon 同樣附這段提示，文字與 Python 逐字相同。
