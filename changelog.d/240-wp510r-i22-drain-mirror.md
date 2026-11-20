---
type: fix
scope: rust-core
---

保管 token 換成退休 epoch 時持續發布待排空鏡像，避免 shutdown 在 RELEASED 快照後漏列仍未關閉的 IoWorker；只有配對成功回條結算後才清除該等待。
