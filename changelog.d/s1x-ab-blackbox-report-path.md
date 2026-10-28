---
type: fix
scope: rust-refactor
---

`scripts/rust/ab_blackbox.sh` 在 A/B 比較器回非 0 但已寫出報告時，仍輸出報告路徑並照傳 exit code，exit driver 不再因取不到路徑使 gate 誤判「缺少 ab-report」；比較器未產生報告時不印路徑。
