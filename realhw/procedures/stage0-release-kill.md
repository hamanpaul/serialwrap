# Stage 0 release 持久化前後 kill 程序

> 對應規格 §6.2 交接、§11.6；計畫 `docs/rust-refactor/plans/02-stage0-python-bc.md` T21 交付物「release 持久化 kill」。
> 本程序為**手動、逐步命令**；實跑屬 BLOCKED-OWNER（見計畫 §2 彙總 #16），本文件只交付程序與判定標準。

| 欄位 | 內容 |
|---|---|
| entry | 見「步驟」；核心命令為 `serialwrap --socket <隔離 socket> device release`、`serialwrap --socket <隔離 socket> session list`，測試鉤子為 `SERIALWRAP_TEST_KILL_AFTER_SAVE=1`（情境 a）與 `SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE=<目錄>`（情境 b） |
| revision | `git rev-parse HEAD:realhw/procedures/stage0-release-kill.md`（blob sha1）與被測 `sw_core/session_manager.py` 之 `git rev-parse HEAD` 一併記入證據 |
| expected | (a) release 持久化落盤後、回應送達前 kill（`SERIALWRAP_TEST_KILL_AFTER_SAVE=1` 僅隔離實例生效）→ 重啟後 session 仍為 `RELEASED`，且 `state.json` 含該裝置的 `released` 項；CLI 不得回報 `ok:true`。(b) 落盤（commit）前 kill（於 `arrived` 標記後以 `kill -9`）→ 重啟後 `state.json` 不含該裝置的 `released` 項，session 回到 release 前的最後有效狀態（`READY`／`ATTACHED`／其他非 `RELEASED`），**不得**宣稱 release 已完成 |
| owner | 擁有者實機操作（TestPilot bench 或隔離主機；需 sudo 權限時由擁有者執行） |
| evidence_dir | `~/b-log/rust-refactor/p1/release-kill/<YYMMDD-HHMMSS>/`（每步命令輸出、`state.json` 快照、`daemon.log`） |
| rollback | 每個情境結束後：若裝置仍為 `RELEASED`，以隔離 socket 執行 `serialwrap --socket <隔離 socket> device attach --selector <COM>`，再以 `session list` 確認 READY；仍卡住或 daemon 無法啟動時，刪除**隔離** state 目錄（僅 `SERIALWRAP_STATE_DIR` 指向之暫存路徑）後重建；不得刪除正式 `~/.local/state/serialwrap` |

## 前置條件

1. **只在隔離實例執行**。以下變數必須全部明確設定，且各自不等於預設路徑（SC-19 `default_isolation_paths()` 判定）：
   - `SERIALWRAP_STATE_DIR`、`SERIALWRAP_RUN_DIR`、`SERIALWRAP_CONFIG_DIR`
   - 本程序一律以 `EVD` 代表本次 evidence_dir，`SBX` 代表隔離根目錄（例如 `$HOME/tmp-swrel`），三者皆不得位於正式路徑下。
2. **所有 CLI 呼叫都必須帶 `--socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock"`**。CLI 未帶 `--socket`／`--endpoint` 時會連到預設（正式）endpoint，等於對正式 daemon 下命令。
3. **測試裝置 allowlist 以 by-id 路徑列出**，不得列 COM 名稱：`SERIALWRAP_TEST_DEVICE_ALLOWLIST` 比對的是 `DeviceInfo.by_id`（POSIX 為 `/dev/serial/by-id/...` symlink 路徑），寫 `COM0` 會把所有裝置濾掉。同時建立只含該板 symlink 的隔離 by-id 目錄，並設定空的 by-path 目錄（比照 `guards.throwaway_env`）：

   ```bash
   export SBX=$HOME/tmp-swrel
   mkdir -p "$SBX/byid" "$SBX/bypath"
   BOARD_ID=usb-<板子的 by-id 名稱>            # 由 ls -l /dev/serial/by-id 取得
   ln -sfn "$(readlink -f /dev/serial/by-id/$BOARD_ID)" "$SBX/byid/$BOARD_ID"
   export SERIALWRAP_BY_ID_DIR=$SBX/byid
   export SERIALWRAP_BY_PATH_DIR=$SBX/bypath
   export SERIALWRAP_TEST_DEVICE_ALLOWLIST=/dev/serial/by-id/$BOARD_ID
   ```

4. **與正式 daemon 的衝突（BC-04）**：正式 daemon 持有 tty 時，隔離 daemon 開檔會被獨占語意擋下。因此測試板必須是正式 daemon **未管理**的備用板。若只有正式 daemon 管理的板，須先由擁有者以正式 CLI 明確執行 `serialwrap device release --selector <COM>`（記入證據），結束後以 `serialwrap device attach --selector <COM>` 收回；除此之外不得觸碰正式 daemon、`~/.config/serialwrap` 與正式 systemd unit。
5. `SERIALWRAP_TEST_KILL_AFTER_SAVE=1` 與 `SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE` 只在隔離實例生效。非隔離實例設定時，程式只寫 WARNING（`daemon.log`），不終止、不暫停。

## 情境 (a)：release 持久化落盤後、回應前 kill

1. 啟動隔離 daemon（前置條件 1、3 的變數皆已 export）：

   ```bash
   export SERIALWRAP_STATE_DIR=$SBX/state
   export SERIALWRAP_RUN_DIR=$SBX/run
   export SERIALWRAP_CONFIG_DIR=$SBX/config
   export SERIALWRAP_TEST_KILL_AFTER_SAVE=1
   nohup serialwrapd >"$EVD/daemon-a.log" 2>&1 &
   echo $! > "$EVD/daemon-a.pid"
   SOCK="$SERIALWRAP_RUN_DIR/serialwrapd.sock"
   ```

2. 記錄 release 前的狀態，並送出 release（必帶 `--socket`）：

   ```bash
   serialwrap --socket "$SOCK" session list > "$EVD/a-before.json"
   serialwrap --socket "$SOCK" device release --selector COM0 --source agent:swrel --reason kill-after-save \
     > "$EVD/a-release-resp.json" 2>&1 || true
   ```

3. 判定前置：鉤子觸發時 daemon 以 137 結束，CLI 無 `ok:true` 回應。確認如下，缺一即停止並記為 INSUFFICIENT_EVIDENCE：
   - `kill -0 $(cat "$EVD/daemon-a.pid")` 失敗（行程已不存在）；
   - `$EVD/daemon-a.log` 含 `SERIALWRAP_TEST_KILL_AFTER_SAVE=1：state 已落盤` 這行 WARNING；
   - `$EVD/a-release-resp.json` 不含 `"ok":true`。
4. 保存落盤後的 `state.json` 快照，去掉鉤子後重啟 daemon，確認恢復結果：

   ```bash
   cp "$SERIALWRAP_STATE_DIR/state.json" "$EVD/a-state-after-kill.json"
   unset SERIALWRAP_TEST_KILL_AFTER_SAVE
   nohup serialwrapd >>"$EVD/daemon-a.log" 2>&1 &
   serialwrap --socket "$SOCK" session list > "$EVD/a-restart.json"
   ```

5. 判定：`a-restart.json` 中 COM0 的 `state` 必須為 `RELEASED`，且 `a-state-after-kill.json` 的 `released` 項含 `/dev/serial/by-id/$BOARD_ID`。

## 情境 (b)：release 持久化 commit 前 kill

本情境以**確定性暫停點**取代「碰運氣的毫秒視窗」：隔離 daemon 在 state.json 落盤前寫入 `arrived` 標記並等待 `go` 標記，操作者在看到 `arrived` 後才 kill。

1. 啟動隔離 daemon（不設 kill 鉤子，改設暫停點）：

   ```bash
   export SERIALWRAP_STATE_DIR=$SBX/state-b
   export SERIALWRAP_RUN_DIR=$SBX/run-b
   export SERIALWRAP_CONFIG_DIR=$SBX/config-b
   export SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE=$EVD/pause
   nohup serialwrapd >"$EVD/daemon-b.log" 2>&1 &
   echo $! > "$EVD/daemon-b.pid"
   SOCK="$SERIALWRAP_RUN_DIR/serialwrapd.sock"
   ```

2. 在背景送出 release，等待暫停點抵達（上限 60 秒；逾時即 INSUFFICIENT_EVIDENCE）：

   ```bash
   serialwrap --socket "$SOCK" device release --selector COM0 --source agent:swrel --reason kill-before-save \
     > "$EVD/b-release-resp.json" 2>&1 &
   for _ in $(seq 1 1200); do [ -f "$EVD/pause/arrived" ] && break; sleep 0.05; done
   test -f "$EVD/pause/arrived" || { echo "未抵達暫停點"; exit 1; }
   ```

3. 暫停點已確認後（此時 release 尚未 commit），記錄快照並 kill -9：

   ```bash
   cp "$SERIALWRAP_STATE_DIR/state.json" "$EVD/b-state-before-kill.json"   # 不得含 released 項
   kill -9 "$(cat "$EVD/daemon-b.pid")"
   ```

4. 重啟（不設暫停點），確認恢復結果：

   ```bash
   unset SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE
   nohup serialwrapd >>"$EVD/daemon-b.log" 2>&1 &
   serialwrap --socket "$SOCK" session list > "$EVD/b-restart.json"
   cp "$SERIALWRAP_STATE_DIR/state.json" "$EVD/b-state-after-restart.json"
   ```

5. 判定：`b-state-after-restart.json` 的 `released` 不含該 by-id；`b-restart.json` 中 COM0 的 `state` 不得為 `RELEASED`，且應為 release 前的最後有效狀態（`READY`、`ATTACHED` 或其他非 RELEASED 狀態）。`b-release-resp.json` 不得含 `"ok":true`（daemon 已在回應前被殺）。若 restart 後裝置未被收回，依 rollback 執行 `device attach`。

## 判定與證據

- PASS：情境 (a) 與 (b) 的判定皆成立，且 `daemon-a.log`、`daemon-b.log` 中無 `Traceback`。
- FAIL：任一情境判定不成立（例如 (a) 重啟後非 RELEASED、(b) 重啟後為 RELEASED）；保留 `state.json` 快照、daemon 日誌與命令輸出於 evidence_dir，回報計畫 T08 owner。
- INSUFFICIENT_EVIDENCE：未抵達暫停點、鉤子未觸發、無法取得重啟後 `session list` 結果（例如 socket 無法建立）。記錄原因後依 rollback 清理，**不得**以此宣稱通過。

## 退路

- 鉤子不可用（非隔離實例拒絕生效或 daemon 未印出鉤子 WARNING）：情境 (a) 判為 INSUFFICIENT_EVIDENCE，不得以外部 `kill -9` 近似取代鉤子結果；情境 (b) 不受影響（僅依暫停點）。
- 任何情境導致裝置停留於 `RELEASED`：依 rollback 以隔離 socket 執行 `device attach --selector COM0` 收回，並保留 evidence_dir。
- 擁有者若要驗證「release 完整完成」的對照組，可於 `$EVD/pause/go` 建立後讓 release 續行（此時 state.json 應含 released 項，作為 (b) 的正向對照）。
