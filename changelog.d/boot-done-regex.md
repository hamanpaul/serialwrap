---
type: feat
scope: boot-quiet-window
---

新增 profile 選配欄位 `boot_done_regex`（開機完成訊號），boot quiet window 期間 RX 命中即提早解除，與 `login_regex`／`prompt_regex` 同列判定、同樣受 `bootloader_prompts` 尾行例外約束，不參與登入流程；target 可覆寫，`serialwrap profile test` 一併回報命中。出貨 `prpl-template` 設為 askconsole 的「Please press Enter to activate this console」：prplOS 開機停在 askconsole 時既不匹配 login 也不匹配 prompt，過去只能等滿 180s 才恢復探測；實測 askconsole 在 U-Boot banner 後約 18s 出現，READY 可提早約 2 分半。#162 的 READY 再確認不變（解除 quiet 不清 `ready_reconfirm_pending`，仍由 nonce probe 確認並順帶消耗啟用 banner）。線上 `~/.config/serialwrap/profiles/*.yaml` 不會被 setup 覆寫，既有安裝需手動把此欄位補進對應 template。
