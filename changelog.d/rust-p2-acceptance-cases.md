---
type: feat
scope: test-infra
---
建立 Stage 0 案例承接清單 `tests/acceptance/cases.yaml`（regression、realhw、func-test、§15.7.1 情境、Windows-native 與 MCU 手動程序，共 120 筆）、欄位 schema `cases.schema.json`（單一權威）、切片凍結鎖 `cases.lock.json` 與唯一寫入者 `regen_lock.py`（`--add-only` 拒絕消失或延後既有案例）、統計檔 `cases.counts.json`，鎖檔另快照每案例的分母屬性（platforms、layer、expected 等，改動即拒絕）；新增 Stage 0 出口證據索引 `stage0_evidence_index.yaml`（逐案例、逐適用平台、逐證據檔登錄，§4 必要項目缺列即 FAIL）、其 schema 與校驗器 `check_evidence_index.py`（exit 0 僅表示索引完整且檔案完整性相符，不是 verdict PASS；目前 exit 5，未滿足前不得視為通過）；新增 MCU 韌體升級手動程序 `realhw/procedures/p2-1-mcu-flash.md`（不計入任何計數；隔離實例以 `daemon start --force-spawn` 另起）；`docs/regression-plugin.md` 的 `f4-background-result-tail-consistent` 改為預期 PASS（#159 已修），不再保留紅燈哨兵。
