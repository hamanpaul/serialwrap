---
type: fix
scope: rust-s21
---
Slice 2.1 黑箱 harness：`start_daemon(share_tty_with=…)` 讓第二個 daemon 共用第一個 daemon 的 FakeTarget 與 by-id symlink（不另建、不改寫、stop 時不關閉）；`test_two_daemons_same_tty` 改用之，修正舊寫法蓋掉第一個 daemon 的 `fake-uart0` 致兩個 daemon 實際不是同一 tty 的缺陷。
