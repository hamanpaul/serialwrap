---
type: fix
scope: rust-s2-gpz
---
Windows loopback TCP connect timeout 時，補查同使用者的 serialwrapd 行程並回報其 PID；掃描沒有行程證據時仍以 TIMEOUT fail-closed。
