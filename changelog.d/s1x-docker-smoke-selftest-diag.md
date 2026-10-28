---
type: fix
scope: rust-refactor
---

`test_docker_smoke_two_container` 失敗時把 bridge selftest 報告的 verdict 與非 PASS 檢查附進斷言訊息（POSIX 短 basetemp 於 pytest 結束時刪除，失敗當次證據原本無法事後查閱）；判定不變。
