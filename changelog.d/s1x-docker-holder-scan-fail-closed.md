---
type: fix
scope: rust-cli
---

修正 docker systemd 實跑 harness 的 holder 掃描假釋放缺陷（sol 300f383，Medium）：`scripts/docker/run_systemd_container.py` 的 `_PROC_FD_SCAN` 原本只要 fd 目錄可列舉就把行程計為 `readable`，之後 `readlink()` 的 `PermissionError` 全被吞掉。兩輪掃描都無法解析某個現存 fd 時，該 PID 仍被視為已覆蓋，`_holder_pids()` 可能回傳空集合，使交接測試誤認裝置已釋放。

現行判準（fail-closed）：掃描 `scan()` 的輸出新增 `unresolved` 清單。fd 目錄列舉失敗（非 `FileNotFoundError`）、或有 fd 的 `readlink()` 失敗（非 `FileNotFoundError`）且未找到目標者，計為 `unresolved` 而非 `readable`。fd 或行程於讀取期間消失（`FileNotFoundError`）與無 fd 目錄者可忽略，不計為未解析。`_holder_pids()` 合併 root 與 daemon 使用者兩輪掃描：行程只要任一身分能完整解析其 fd 表即視為已覆蓋；兩個身分皆無法解析、行程仍存在、且屬於權限所及 uid（0／1000）時拋出 `RuntimeError`（證據不足，不回傳空集合）。判定範圍以 root 掃描時已存在的行程為準，兩輪之間新出現者（例如 `runuser` 父行程）不計入，避免誤報；其他 uid 行程仍不計入判定不足。殘留風險與原註記相同。

測試：新增 `tests/test_docker_holder_scan.py`（`posix_only` 與 `memory_only`，行程內以暫存假 `/proc` 樹執行與 harness 相同的掃描原始碼，注入 `PermissionError`／`FileNotFoundError`）。涵蓋：可列舉但 readlink 拒絕的行程不得為 readable、同行程另一 fd 命中仍判為 holder、消失的 fd 與行程可忽略、兩身分皆無法解析須 fail-closed、單一身分可解析時以其結果為準、消失的行程不 fail-closed。既有 `tests/test_docker_l15.py::test_holder_scan_fail_closes_only_on_reachable_uids` 未修改、仍通過。本變更不改動任何 L1 凍結鎖或已凍結的測試節點。
