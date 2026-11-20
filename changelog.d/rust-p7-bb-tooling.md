---
type: change
scope: rust-p7
---
黑箱 FakeTarget 支援自訂登入步驟（`login_steps`）、命令覆寫、回應延遲、背景雜訊、RX 凍結、開機腳本、console lock 與 daemon 相容模式（`release_slave`），預設值逐位元組維持 P3 行為；新增 `bb_p7.py`（`p7_profile_dir`、`cli_json`、`wait_state`、`P7Rig`／fixture `p7`）、`p7_profiles/` 六個 profile 樣板，`win-blackbox.ps1` 新增選用參數 `-Tests`（P7 Task 18A）。
