---
type: fix
scope: rust-p7
---
Rust daemon 接上 transport stall 精煉（#150）：既有 bridge 再探、自動 reprobe 與 attach 的 probe 失敗於全程零 RX 且 RX 凍結 ≥ 30 s 時翻轉為 `TRANSPORT_STALL`（detail 帶 host 層復原提示），並一次性寫 stderr WARNING 與 WAL META `transport_stall_suspected`、RX 恢復重臂；先前 `refine_probe_failure` 未接正式路徑，stall 永遠不會被回報。
