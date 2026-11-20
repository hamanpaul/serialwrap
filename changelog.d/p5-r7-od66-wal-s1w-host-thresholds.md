---
type: feat
scope: rust-s21
---

Slice 2.1 gate 的 WAL 寫入效能判定改依主機類型選門檻（擁有者裁決 OD-66）：WSL2 證據使用 append p50 ≤ 0.050／p95 ≤ 0.100／p99 ≤ 0.200 ms、rotate 最大停頓 ≤ 100 ms，且每項不得劣於同主機同時段的 Python 基線（基線證據一併匯入）；原生 Linux／Windows 維持規格 §1.2 原門檻。判定一律以證據數值重算、可匯入多輪取最差，判定結果標明所用門檻。
