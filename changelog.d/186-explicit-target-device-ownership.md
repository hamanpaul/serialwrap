---
type: fix
issue: 186
scope: session-manager
---
explicit `targets` binding 的 `device_by_id` 現在對其他 target 排他：restart 後不再受
profile 檔案載入順序（`self._sessions` insertion order）擺佈而把裝置指派給錯的
session。

- **根因**：`SessionManager.__init__` 套用持久化 `_binding_overrides`（`session.bind`
  或 `_attach_by_id` 的 DETACHED-rebind fallback 留下的紀錄）時，未檢查該值是否與
  「另一個 explicit target 自己在 YAML 宣告」的 `device_by_id` 衝突。實測（TI XDS110
  探棒兩個 CDC-ACM port）：`00-com2-prpl.yaml` 綁 `COM2` 到一顆未插著的 CH340；另一份
  profile 檔把 `COM3`/`COM4` explicit 綁到 XDS110 的 `-if00`/`-if03`（皆在線）。
  `systemctl restart serialwrap` 後 `COM2` 佔用了 `-if00`、`COM3` 反而 `DETACHED`；
  把 XDS110 的 profile 檔改名成排序上先載入即可讓結果反過來——即裝置指派實際上是
  first-come-by-load-order。
- **修法**：`__init__` 建 session 前先收集本次載入的所有 explicit target `device_by_id`
  宣告集合；套用某 session 的持久化 override 時，若該值與**另一個** target 自己宣告
  的值衝突，視為過期殘留紀錄，改用該 target 自己的 YAML 宣告值，並清掉這筆過期
  override，避免下次載入再誤用。修復後「哪個 target 拿到哪顆裝置」不再依賴 profile
  檔案載入順序。
- 刻意不動 `_attach_by_id` 的 DETACHED-rebind fallback 本體：那條路徑同時也是
  `test_session_bind.py::test_auto_bind_on_device_attach` 涵蓋的既有功能（`targets`
  用佔位符 `device_by_id`、讓任意上線裝置依 `act_no` 自動填入指定 COM slot），與本次
  要修的「明確裝置被搶走」不是同一件事，收窄修復範圍避免波及該功能。
- **文件**：`README.md` 新增「範例：debug probe（如 TI XDS110）passthrough explicit
  target」小節，附可直接套用的 profile 範例，並記錄兩個 bench 排查坑：探棒韌體版本
  內嵌在 by-id 字串裡（升級韌體後綁定會安靜失效）、UART baud 常非模板預設的
  115200（本例實測 921600）。

**regression-case 評估**：`tests/test_explicit_target_device_ownership.py` 新增兩個
pytest（重建 restart 情境本身、驗證結果與 profile 傳入順序無關），修復前皆可重現地
FAIL、修復後 PASS；純 in-process session-state 邏輯，unit/mock 已完整覆蓋，不需另加
`regression/`（TestPilot real-hw）case。
