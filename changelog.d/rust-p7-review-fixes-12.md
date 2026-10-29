---
type: change
scope: rust-p7
---
Slice 2.3 審查修正第二批（sol cd7acf0_f9e90a9、f9e90a9_de7a95a）：全套 pytest gate 列的必要集合改為完整 `-q tests/` 收集（扣除 Linux 平台 skip）；s23 quiet 零 TX 窗口自送出 banner 前起算、interleave 核對到 READY nonce probe 的完整登入區間、force handoff 以 `bridge_generation` 證明實際重開；A/B 表頭記下可還原的 run_dir；依 OD-56 於 CI `--allow-removal` 追認兩個 `s21_core.linux` 舊 nodeid；依 OD-57 登記 DF-23（login 進行中 release 後 attach：Rust 回 READY、Python 落 `ATTACH_FAILED:TxIncompleteError`）並改以 DF 判定該案例。
