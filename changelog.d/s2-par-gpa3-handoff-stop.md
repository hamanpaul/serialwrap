---
type: fix
scope: sw-cli
---

修正更新交接黑箱回歸的路徑：帶 `test-fakes` 且明確設定隔離 socket 時，停止監管步驟經正式 daemon 範圍掃描產生交接回報；正式建置不包含這條測試路徑。
