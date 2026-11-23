> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-b6f1dd6-1 | High | 已修 680124d0 | 0 | 本輪仍在共同生命週期鎖內重讀模式、檢查 hold，並持鎖至 windows-task 的 `/ENABLE`／`/Run` 完成。 |
| V051-b245cbb-1 | Medium | 未修 | 1 | [BC-07](contracts/behavior_changes.yaml:154) 仍填 `OD-04`，檢查器與測試也仍要求所有 BC 使用 `OD-04`。編排者已裁定採逐項核准來源；基礎 commit `2c8e6644` 尚未進入本分支。 |

## B 新發現

**V051-be2d22e-1 [Medium]** [daemon_cmd.rs:798](crates/sw-cli/src/commands/daemon_cmd.rs:798) — 巢狀 autostart 等鎖期間若模式由 `on-demand` 切成 `windows-task`，本輪修正讓 `/Run` 可成功執行，但隨後把排程命令的 `ok=true` 當成 daemon 已就緒。該回應沒有 PID，程式便顯示 `pid=0`，立即重送原 RPC；若 daemon 尚未開始監聽，`session list` 仍回 `SOCKET_ERROR`。新增測試正是使用持續拒絕連線的 fixture，卻只斷言沒有 `TIMEOUT` 和排程命令已送出，未檢查這個結果。修法：區分排程工作已接受啟動與 daemon 已就緒；在有界等待內確認 health 後才重送 RPC，且不要虛報 PID。補測試覆蓋延遲就緒及始終未就緒。

**V051-be2d22e-2 [Medium]** [service_cmd.rs:264](crates/sw-cli/src/commands/service_cmd.rs:264) — 鎖後重讀遇到不可 JSON 序列化的監管模式時，`daemon start` 未得到與新模式下直接執行相同的結果。例如等待期間設定改為 `supervision_mode: 2020-01-01`：本輪路徑輸出 `INTERNAL_ERROR` JSON；直接執行 [daemon start](crates/sw-cli/src/commands/daemon_cmd.rs:65) 則 stdout 為空、stderr 輸出 `TypeError`，兩者 exit code 均為 1。這違反裁決要求的重新分派等效性。修法：釋鎖後依原入口處理重讀結果，保留 `service start` 與 `daemon start` 各自的錯誤輸出；補兩入口的不可序列化模式交錯測試。

定向驗證：`mgmt_daemon` 與 `mgmt_service` 的 `p277` 節點共 **14 passed**；`git diff --check` 通過，工作樹乾淨。未跑完整 pytest、Docker／bundle 或 Windows 原生測試；上述兩項失敗情境為讀碼推導。

## 既有碼待辦（不計入結論）

README 既有的 Windows task 崩潰自動重啟敘述與 OD-83 不一致；停用工作執行 `/Run` 的實際結果仍待 Windows 實機驗證。

## 對裁決的異議（不計入結論）

無。

結論：需修正