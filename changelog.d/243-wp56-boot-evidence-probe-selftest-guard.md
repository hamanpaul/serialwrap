---
type: fix
scope: sw-core
---
Rust 既有 bridge probe 與 self-test 新增非同步 reboot 開機證據等待的零 TX 守衛（對齊 Python `_probe_existing_bridge` 與 `self_test`，`sw_core/session_manager.py:4746-4753`、`:7816`，#243）：手動 `session attach`／非 force `session recover` 在 ATTACHED 走既有 bridge probe 時，quiet 或證據等待有效即不送 probe、不開新登入世代、不排 backoff、不加 attempts，非 READY 誠實回報 `PROMPT_UNAVAILABLE`，READY 不覆寫 `last_error`；`console_unlock_env` 只豁免原 quiet，不豁免等待；banner 解除等待後恢復原 quiet 豁免。READY 的 self-test 於 quiet 或等待有效時回既有 `AUTOBOOT_QUIET`／`recommended_action="wait"`，不送 nonce probe，分類優先序不變。passthrough 維持 SC-35（不送 probe、不另設 gate）。共用的 `boot_quiet_blocks_probe` 維持 Python `_boot_quiet_blocks_probe` 只看 quiet 的語意，reprobe 規劃、fence 豁免旗標、attach plan 與 reboot recovery 的判定不變。
