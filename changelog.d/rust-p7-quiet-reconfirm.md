---
type: feat
scope: rust-p7
---
P7 Task 4：Rust quiet gate 純函式 `boot_quiet_blocks_probe`（console unlock／passthrough 不擋 probe）、`QuietState::agent_gate_active`、`submit_gate_reject`／`execute_gate_reject`（與 Python `gate_responses` golden 位元組相同）；passthrough 的 `on_rx_text` 不 arm（SC-35）。SessionCore 的 attach／再探／reboot recovery 改以 `boot_quiet_blocks_probe` 判定 quiet；console unlock profile 的 quiet arm 不推進 fence 登入世代、不擋其登入 probe（`FenceState.probe_quiet_exempt`），在途 unlock run 的結果照常套用（parity #49，與 Python 不遞增 `login_gen` 同）。arm 時改呼叫 07 `invalidate_human_input` 一項待 P6 07 T6。
