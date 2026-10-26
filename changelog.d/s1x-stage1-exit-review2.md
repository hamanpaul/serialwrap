---
type: fix
scope: rust-refactor
---

修正 Stage 1 exit closure 第二輪審查缺陷：handoff holder observer 在隔離 PID namespace 逐次掃描候選行程；bundle evidence reader 與 Rust launcher 一致拒絕不安全指標；Cargo provenance 綁定 repo toolchain 與乾淨 HEAD；Spike 3 以 no-delete share 作主注入並另記 oplock 變體。修正深層 checkout 下 handoff 測試的 AF_UNIX 暫存路徑，並同步 SC-11 的 TX fence timeout 契約與 P8 實作計畫。
