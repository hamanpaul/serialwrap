---
type: fix
scope: realhw
---
`od09_freeze.propose()` 新增可注入的 `generated_at`（預設仍為現在 UTC）；od09 凍結測試改以固定時點產生提案，不再於 2026-11-20T09:00Z 之後因「now 早於 proposal.generated_at」全數失敗。
