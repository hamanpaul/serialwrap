# OD-48 設計 v2：0.4.0 `setup` 遇到 0.3.x 舊 daemon 時自動停止後續行

> 依據：擁有者裁決 OD-48、OD-48 修訂（失敗不自動重啟）、OD-51（`reviews/rulings/s1.md`）；Stage 1 出口審查 C 的 S1X-c-1、S1X-c-2（併入）。
> v1 經三個對抗式審查者（UART 雙讀者安全、失敗還原、測試契約合規）審過，v2 已吸收其結論；審查原文保存在擁有者機台 `~/b-log/rust-refactor/exec/od48/`。
> 實作分支：`feature/od48-legacy-handoff`（起點 `origin/feature/rust-refactor`）。

## 背景事實（盤點結果，實作者須自行對照程式碼確認）

- 0.3.2 pipx 安裝沒有 `current-release.json`，從 bundle 執行 `setup` 走 `run_install_locked` → `execute_change_with_restore(op="install")`；手動降級殘留 `current-release.json` 時走 `op="update"`。
- 拒絕發生在 `stop_supervisor`：`graceful_stop_with_existing_hold(StopRpc::Handoff)` → `request_handoff` 收到 `{ok:false,error_code:"METHOD_NOT_FOUND"}` → `HandoffError::Unconfirmed{code:METHOD_NOT_FOUND, outcome:"refused"}`（`crates/sw-cli/src/graceful_stop.rs` request_handoff、`crates/sw-cli/src/rollback.rs` stop_supervisor／stop_failure_to_op）。
- 0.3.2 的 `daemon.stop`（v0.3.2:`sw_core/daemon.py` `_handle`）只設 stop_event 並立刻回 `{ok:true,stopping:true}`，不做忙碌檢查、不 drain。關機順序：server.stop（unlink socket；Python ≥3.12.1 的 wait_closed 會等所有連線結束）→ service.stop → 釋放 flock → asyncio 收尾（executor 最多約 300 s；3.10／3.11 沒有上限）→ 行程結束時核心才關 UART fd。**socket 消失、lock 可取得都不代表 UART 已釋放。**0.3.2 沒有 TIOCEXCL。
- 0.3.2 systemd unit（user／system）為 `Restart=on-failure`、`RestartSec=2`，無 ExecStop／KillMode。`daemon.stop` 正常結束（exit 0）不會被重拉，關機途中例外（exit 1）或被 SIGKILL 會在 2 s 後被重拉。systemd-system 的 daemon 固定 `--socket /run/serialwrap/serialwrapd.sock`；systemd-user 的 argv 不帶 `--socket`。
- on-demand 沒有常駐重拉者，但 0.3.2 的 `serialwrap-minicom`（`minicom_router.sh` `AUTO_START_DAEMON=1`）與人／agent 手動 `daemon start` 會起舊 daemon。0.3.2 在 pipx 下的 POSIX `daemon start` 本身會失敗（OBS-0.3.2-ONDEMAND-START）。
- Windows 0.3.2：PyInstaller one-file exe 或 pipx，只有 on-demand（可能被使用者以 NSSM／Task Scheduler 包起來），停止方式只有 RPC `daemon.stop`；one-file 有 bootloader 父行程與 Python 子行程。
- P5～P7 的 Rust daemon 在 rpc() 也以 setdefault 補 `version`，值目前同樣是 `"0.3.2"`（VERSION 檔尚未升版）。**只看版本號會把 Rust daemon 誤判為舊版。**
- `query_supervisor` 對 systemd 只用 `is-active`，`mode_enabled` 把 inactive／failed 也映成 `Some(true)`：原本 disabled 的 unit 經一次拒絕會被改成 enabled（既有缺陷，D8）。
- SC-37 已被 P5 使用、SC-38 已由 OD-21 核准給 `SERIALWRAP_SERIAL_BACKEND` 驗證（P5-T2／T3）。

## D1. 判定「舊版 0.3.x Python daemon」（全部成立才算）

1. `daemon.handoff_stop` 回 `{ok:false, error_code:"METHOD_NOT_FOUND"}`；
2. 版本 `< 0.4.0`：取自該回應的 `version`，缺漏時以 `health.status.version`；
3. **正向辨識為 Python 0.3.x daemon 行程**：該 pid 的 `/proc/<pid>/exe` 解析為 python 直譯器，且 argv 為 `…/serialwrapd`（python script）或 `-m sw_core.daemon`；Windows 為 PyInstaller launcher＋Python 子行程樹或 python.exe `-m sw_core.daemon`；
4. 執行檔或 script **不在** `<install_root>/versions/` 底下（不是 bundle 管理的 daemon）。

任一不成立 → 維持現行 fail-closed。新增 Rust 測試：METHOD_NOT_FOUND 帶 version 0.3.2，但行程位於 bundle 路徑或為原生 exe 時必須 fail-closed。現有凍結節點（`update_handoff_unsupported_daemon_refuses_without_stop`、`revert_handoff_unsupported_new_daemon_keeps_journal`、`supervision_set_handoff_unsupported_refuses_without_stop`）期望不變。

## D2. 套用範圍

- 套用：`stop_supervisor`（op=install 與 op=update）；install 分支後段的 `reconcile`——legacy 停止成功後以「已停止」狀態呼叫（不得再以 `OldStop::Reconcile` 重停，也不得以 `daemon_running=true` 走舊停止路徑）；回退（`stop_for_revert_with_hold`）與 `recover_journal` 遇到 legacy daemon（例如被 minicom_router 重拉）時，以同一套 D3～D5 停止，否則 `revert_pending`。
- 不套用：`supervision-mode set`（維持 fail-closed，hint 指示先完成升級）。共用原語（`ensure_released_or_rehandoff`、`graceful_stop_with_existing_hold`）以參數區分呼叫端，不得讓 supervision-mode set 意外取得 legacy 停止能力。

## D3. 停止前檢查（全部在送出任何 stop 之前完成）

1. **偵測端點（併 S1X-c-1）**：不能只用 `local_default_endpoint`。依舊模式解析 instance endpoint：systemd-system → `/run/serialwrap/serialwrapd.sock`；Windows 模式 → `local_default_endpoint`；其餘依序 config 的 `socket_path`（可連者優先，對齊 #108 #2）再退回 `local_default_endpoint`。停舊用舊模式端點，啟新／驗證用目標模式端點。
2. **基準掃描**：記錄所有 serialwrapd 的 pid、start_ticks、uid、socket、exe。除目標外的同 uid daemon → 立即 fail-closed `DEVICE_STILL_HELD`（附 pids 與 socket）。他 uid 的行程排除在 holder 掃描之外並在報告標註。`safety_check` 在 ping 不通改掃 `/proc` 時，找到任何同 uid、socket 不符或無 `--socket` 而無對應可 RPC daemon 的 serialwrapd，或 `query_supervisor` 顯示 running 卻無可 RPC daemon → fail-closed，不得當成 daemon=None 繼續。
3. **忙碌檢查**：`mcu.status` flashing → `FLASHING_BUSY`；`session.list` busy（前景命令、in-flight、interactive、背景 capture、recovering）→ `SESSION_BUSY` 附明細。0.3.2 無 admission freeze，檢查到 stop 之間的 TOCTOU 為已接受偏離（寫進 SC）。
4. **外部監管者**：daemon 的父行程若不是預期者（Linux：該 unit 的 systemd 或 init／使用者 shell 系的一般父行程；Windows：nssm.exe、services.exe 下的服務、Task Scheduler 宿主 taskhostw／svchost Schedule）→ 停止前 fail-closed，hint 指示先停用該監管者。
5. **legacy 擷取檔**：送 stop 前寫 `install-journal.legacy.json`（獨立檔，不寫進已算雜湊的 snapshot 目錄；sha256 記進 journal），內容只含：版本、pid、start_ticks、uid、exe、argv、停止手段、舊模式與 unit 名、MainPID。**不擷取、不保存 env**。寫入並 fsync、journal 更新完成後才送 stop。

## D4. 停止手段（不硬殺）

- 實際在跑的 daemon pid 若是某個 systemd unit 的 `MainPID`（`systemctl [--user] show -p MainPID`，user 與 system 兩種都查，不只依 config 的模式）→ 在 autostart hold 之下 `systemctl [--user] stop`（system 需 `--with-sudo`）。
- 否則 → RPC `daemon.stop`（2 s）。
- 不送 SIGKILL／TerminateProcess。systemctl stop 自身的 TimeoutStopSec 屬監管者語意，接受。

## D5. 「已停」判準

以下**同時成立並連續維持 5 s 穩定窗**才算完成：
1. D3 擷取的行程樹（含 Windows one-file 父子）全部結束；
2. Linux：沒有同 uid 行程持有該 session 的 UART tty；Windows：holder 掃描 Unsupported，以行程樹結束推定並在報告標註；
3. 與 D3 基準相比沒有新出現的 serialwrapd（以 pid＋start_ticks 判定）。出現新的 → 視為重拉：對它以其自身端點重做 D1＋D3 後再停，總共最多 3 次（沿用 MAX_HANDOFF_CHAIN），超過 → `RelaunchLoop` 失敗。

等待上限 330 s（涵蓋 0.3.2 Python 3.12 的 executor 收尾）。逾時 → 失敗 `legacy_stop_pending`：hold 與 journal 保留（keep_journal=true），報告附舊 pid＋start_ticks，**不重啟任何 daemon**；下一次 `setup` 的 `recover_journal` 先確認該 pid＋start_ticks 已結束，再依 D6 還原或繼續。StartAndVerify 前的最後閘門同樣套用 D5 的基準差分（而非只比對 target socket），並移到 `release_held_checked`／`set_enabled` 之後、實際 start 之前。

## D6. 失敗還原（依 OD-48 修訂：不自動重啟）

- 送出 legacy stop 之前失敗：沿用現行補償（release hold），舊 daemon 仍在跑。
- legacy stop 之後任一步失敗（含切換前的 confirm_released／backup／ensure_released 與切換後 revert）：還原入口、config、unit 註冊、監管 enabled 狀態（用正確的原始值，見 D8）、current 到升級前；**不啟動任何 daemon**。報告：`legacy_daemon_stopped:true`、`restored:true|false`，以及依原監管手段給的啟動指令（systemd：`systemctl --user start serialwrap` 或 `sudo systemctl start serialwrap`；on-demand：以原 argv 重新執行的提示，不保證 0.3.2 `daemon start` 可用）。
- systemd 還原順序：先還原入口與 unit，再 release hold；release 不得觸發 start。
- 還原本身失敗 → `revert_pending=true`、journal 保留，報告列出未還原項。

## D7. 成功路徑

legacy 停止成功 → 照現行 install／update 流程切換 → 因 `original.daemon.is_some()`，升級後啟動新 daemon並跑 version_pairing。報告新增 `legacy_stop:{method:"systemctl_stop"|"rpc_daemon_stop", from_version, waited_ms, relaunch_count, stability_window_ms}`。

## D8. 既有缺陷修正：systemd enabled 狀態

根因在 `query_supervisor` 只看 `is-active`。改為以 `is-enabled` 取得 enabled（disabled／masked／static 等正確映射），`release_held` 的 restore_enabled 使用 hold 取得時記錄的原值。先寫會紅的回歸（disabled 但在跑的 unit 經一次拒絕／一次成功升級後仍為 disabled），並確認不影響 Rust-to-Rust 既有凍結測試的期望；若會影響凍結節點，停下列 OWNER-BATCH。

## D9. 契約、測試與文件

- `contracts/behavior_changes.yaml`：新增一個 SC（下一個**未被保留**的編號；SC-37 為 P5、SC-38 為 OD-21 保留，實作者須掃 OD-21 與 P5～P7 分支確認），寫明 D1 判定、D3 TOCTOU 已接受偏離、D4 不硬殺、D5 判準、D6 不自動重啟；同步 `test_contracts_catalog` 的精確集合斷言。**同時依 OD-51** 補正 SC-01、SC-02 的 `compatibility_impact` 涵蓋 `file.push`／`file.pull` 並在 related_decisions 加 OD-51。
- 不新增錯誤碼（p4 鎖禁止）。若新增 phase 名稱，依規定重生 SKILL 受管區段與內嵌資產雜湊。
- Rust 測試（TDD 先紅）：(a) op=install 帶 version 0.3.2 的 Python legacy stub：systemd MainPID 相符 → systemctl stop、on-demand → daemon.stop → 切換成功、新 daemon 由 setup 啟動；(b) METHOD_NOT_FOUND 但非 Python 行程／在 bundle 路徑 → fail-closed；(c) flashing／session busy → 拒絕、未送 stop；(d) stop 後 330 s 未結束 → legacy_stop_pending、未重啟任何 daemon、journal 與 hold 保留；(e) stop 後被重拉 → 再停，超過 3 次 RelaunchLoop；(f) legacy stop 後切換前／切換後失敗 → 安裝還原、無 daemon 被啟動、報告含啟動指令；(g) MainPID 不符 → RPC；(h) 其他同 uid serialwrapd 存在 → 停止前 fail-closed；(i) systemd-system 固定 socket 的 0.3.2（S1X-c-1）update 與 first-install 都被偵測、flashing 時拒絕；(j) D8 回歸；(k) 穩定窗內出現重拉 → 不算已停；(l) 中斷注入：「擷取後 stop 前」「stop 後切換前」兩點，recover_journal 正確處理。**legacy 專用的 crash point 另立清單與獨立迴圈測試，不得加進現有凍結的 CRASH_POINTS 迴圈。**新 L1 節點以 `tests/acceptance/regen_lock.py --l1-add-only` 登記。PAL／support 若缺 MainPID 查詢、exe／argv／start_ticks／uid／父行程辨識能力，在 `crates/sw-pal` 補（platform cfg 只在 sw-pal）並補 fake。
- Docker（`scripts/docker/upgrade_checks.py`、`run_upgrade_container.py`、`tests/test_docker_upgrade.py`）：新增檢查 id（不改舊 id 語意）：「0.3.2 daemon 執行中直接 setup → 成功切換、舊 daemon 行程樹結束、新 daemon 由 setup 啟動（driver 不得代啟，需能偵測 setup 沒啟動時判 FAIL）、config／profiles 逐位元組保留、無兩個 daemon 同時持有 tty」；保留「先停再升級」路徑（OBS-UPGRADE-NOT-STARTED）。原 `REFUSAL_LEGACY_HANDOFF` 檢查改為只在「非 legacy 不支援交接」情境使用。
- 文件：plan 05 Task 12 相關段落、`docs/rust-refactor/spec-reconciliation.md` SC-23 列、`release-0.4.0-plan.md` §4.1、§5.1（含 S1X-c-2：升級時明確指定監管模式；不加旗標時與 0.3.2 相同會自動選擇，可能改變模式與 config.yaml）、§6（OBS-UPGRADE-LEGACY-HANDOFF 改為已依 OD-48 解決；OBS-UPGRADE-ROLLBACK-PIPX／STALE-CURRENT 依 OD-49 列已知限制）、§7 release notes；`owner-decisions-2026-10-15.md` 新增 OD-48（含修訂）～OD-51 列（OD-50 由 install.sh 分支處理時若衝突以本分支為準合併）；README 若提到「升級前先 daemon stop」一併更新；`changelog.d/` 新增 fragment。

## D10. 審查 C 的 S1X-c-1

已併入 D3.1、D3.2、D9(i)。
