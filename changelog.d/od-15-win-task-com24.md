---
type: fix
scope: test-infra
---
將 Windows Task Scheduler throwaway 長跑 allowlist 限定為專用 COM24，並保留 COM20:COM21 與 COM22:COM23 供其他原生 gates 使用。
