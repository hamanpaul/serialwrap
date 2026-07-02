# Tasks: test-state-isolation-120

## 1. P1 state_path 注入（TDD）

- [x] 1.1 RED：新增 `tests/test_state_path_injection.py`——`SessionManager(state_path=tmp)` 寫注入路徑不碰模組層；未注入 fallback 相容 setattr；`SerialwrapService(state_path=...)` 透傳；`setattr(wal,"WAL_DIR",tmp)` 後 `WalWriter()` 落 tmp（現行 def-time 凍結應 RED）
- [x] 1.2 GREEN：`session_manager.py` 加 `state_path` keyword，`_load_state`/`_save_state`/`.corrupt`/mkstemp/`os.replace` 全改 `self._state_path`；`service.py` 透傳；`wal.py` `wal_dir: str | None = None` 建構時解析
- [x] 1.3 回歸：既有 state 相關測試（test_state_persistence_atomic、test_bounded_memory、test_session_bind、test_windows_claim）全綠

## 2. P2 CLI --socket sentinel（TDD）

- [x] 2.1 RED：`_resolve_endpoint` 測試——傳入恰等於 `SOCKET_PATH` 的 `--socket` 且 config 記錄另一可連 socket 時，回傳傳入值不 fallback（現行 RED）；未傳 → config fallback 照舊；`--endpoint` 優先序不變
- [x] 2.2 GREEN：`cli.py` `--socket default=None`；`_resolve_endpoint` 改「有傳即明確」、`chosen = cfg_sock or SOCKET_PATH`；`_run_daemon_start` on-demand 路徑 `sock = args.socket or SOCKET_PATH` 等價替換（spawn 與 health ping）
- [x] 2.3 回歸：daemon start/stop 相關測試（test_cli_daemon_start、test_autospawn_gate、test_daemon_service_selector）全綠

## 3. liveguard 純函式（TDD）

- [x] 3.1 RED：新增 `tests/test_liveguard.py`——state：乾淨空覆寫 FAIL、`released`/`bindings`/`aliases` key 消失 FAIL（strict 與 warn 皆然）、byte-identical PASS、warn＋非結構變更 WARN；WAL：縮小/消失 FAIL、append PASS、shell WAL_DIR 維度任何變更 FAIL；config：任何變更 FAIL；daemon：MainPID 變更/inactive FAIL、`last_tx_at` 前進/`bridge_generation` 變更 FAIL、不可達 SKIP
- [x] 3.2 GREEN：實作 `tests/liveguard.py` 純函式（快照 dataclass＋classify 函式＋live path XDG 公式，忽略 `SERIALWRAP_*`）

## 4. conftest 三層防線

- [x] 4.1 新增 `tests/conftest.py`：top-level 先算 live 快照（state/WAL/config/daemon RPC）→ mkdtemp 硬覆寫 7 個 `SERIALWRAP_*_DIR` env＋BY_ID/BY_PATH 空目錄；function-scoped autouse fixture patch `session_manager.STATE_PATH`；sessionfinish 呼叫 liveguard 比對、FAIL 設 exitstatus=1、清 tmp
- [x] 4.2 修 `test_setup_materialize.py` 前 2 測試補 `monkeypatch.delenv("SERIALWRAP_CONFIG_DIR", raising=False)`
- [x] 4.3 驗證：完整 pytest suite 跑過，掃出並修復其他被 conftest env shadow 的同型測試（runtime-lazy/reload 讀 XDG 未管理 `SERIALWRAP_*` 者）

## 5. subprocess 測試併修

- [x] 5.1 `test_human_agent_coexist.py`：env 補 `SERIALWRAP_CONFIG_DIR`；setUp 改每項資源建立後立刻 `addCleanup`（tempdir/FakeTarget/daemon/tmux），移除 tearDown 對應邏輯
- [x] 5.2 `test_multiagent_e2e.py`：env 補 `SERIALWRAP_CONFIG_DIR`＋明確覆寫 `SERIALWRAP_WAL_DIR` 至自身 tempdir

## 6. per-file 隔離（unittest runner 防線）＋文件

- [x] 6.1 8 檔補 setUp/tearDown patch `session_manager.STATE_PATH`＋`wal.WAL_DIR`：test_session_capture、test_issue24_heartbeat、test_session_activity、test_command_guard、test_service_human_console、test_mcu_cli_rpc、test_flash_service_wiring、test_daemon_service_selector
- [x] 6.2 抽測驗證：無 conftest 環境（`python3 -m unittest tests.test_issue24_heartbeat` 等代表檔）不觸 live path
- [x] 6.3 CLAUDE.md 測試政策註記 unittest discover 無 conftest 防線、pytest 為準；README 若提及測試跑法同步

## 7. 驗收與收尾

- [x] 7.1 完整 `python3 -m pytest -q tests/` 於本機（有 production daemon）跑過：無新失敗、四 guard 全綠、live state.json byte-identical
- [x] 7.2 新增 `changelog.d/120-test-state-isolation.md` fragment（R-09）
- [x] 7.3 `python3 -m policy_check --repo .` 通過（含 PR 參數複現 CI：`--pr-title/--pr-body/--pr-base-ref/--pr-head-ref`）
