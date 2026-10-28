---
type: fix
scope: sw-pal
---

systemd autostart hold 驗證失敗的撤銷路徑（`rollback_hold`）比照 release，best-effort 移除已空的 `<unit>.service.d`（user：`remove_dir`；system：`sudo rmdir --ignore-fail-on-non-empty`）；release 的 rmdir 失敗不影響結果改以腳本化非零 rc 的測試固定。
