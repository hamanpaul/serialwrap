---
type: fix
scope: rust-console
---
Rust daemon 修正兩個 human console／interactive lease 的順序缺陷（Codex sol 補審 p6-20261028）：命令出列時若 refresh 惰性結束已過期的 agent lease 並排出 human 回放，命令改為等回放上 UART 並結算後才送出（UART 順序 Q → B，與 Python `execute_command` 先同步 `post.execute()` 一致；reboot 命令不再讓該回放失效遺失），期間遇 drain 則該命令以未送出終結；`device.release` 一律通知 ConsoleHub 關閉全部連線並遞增 close 世代，hub 在 close-all 之前 accept、事件晚於 release 到達的 TCP／com0com 連線不再復活為已關閉的 console 或幽靈 raw owner（core 拒收並要求關閉）。
與 P5 合併時一併整合：哨兵 vtty 存活判定改經 console 工廠 `slave_path_exists`，self-test 改為與前景並行的 side op 後仍於結束時恢復 human 輸入並回放。
