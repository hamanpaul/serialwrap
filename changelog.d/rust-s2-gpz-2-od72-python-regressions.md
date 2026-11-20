---
type: fix
scope: rust-s2-gpz
---
依 OD-72 恢復 Linux slice21 凍結集合的案例歸屬，保留既有 startup 與 background reboot 案例；DF-27 黑箱改為等待精確的 Python timeout 錯誤，再核對 DETACHED 狀態；release 黑箱固定 FakeTarget 回應時序，避免舊回應在重新 attach 後抵達。
