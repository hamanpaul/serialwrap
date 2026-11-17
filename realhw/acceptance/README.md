# realhw/acceptance — 長跑發版判定（P2-T15）

本目錄是長跑發版判定的外層套件，對應 spec §15.11.4、§14.2 與 OD-09／OD-10／OD-13／OD-94。OD-94 將每平台長跑門檻改為 24 小時；實際平台、板子與排程依其後有效的擁有者裁決辦理。

- `longrun_acceptance.yaml`：**發版長跑門檻唯一來源**。其他前端不得另存一份數值。
- `longrun_acceptance.schema.json`：門檻檔結構契約。延後項以 `status` 一次凍結兩平台；`frozen` 時兩平台欄位必須非 null 且符合型別，`deferred` 時全部為 null。
- `gate.py`：外層 `release_gate_result` 判定器。`engine_result` 原樣保留引擎的 `lr-mixed` 結果；引擎 FAIL 不會被任何規則轉成 PASS。
- `observer.py`：唯讀長跑觀測器，只呼叫 `daemon status` 與 `session list`；Windows 另可週期性執行獨占探測。
- `walscan.py`：獨立 WAL 掃描器（stdlib-only），驗 crc32／len，計算 agent 重送、human tick 重放與禁止窗口內的 TX。
- `winexcl.py`：Windows 原生 COM 獨占開啟證據（`CreateFileW` 零 I/O 探測與 `handoff` 循環）。不呼叫任何讀寫或控制線 API。

本套件與引擎分離：引擎（`realhw/cases/longrun.py`）不得 import `gate.py`，也不得 import yaml。

執行期依賴：`gate.py` 需要 `PyYAML` 與 `jsonschema`（測試側同版本）；`observer.py`、`walscan.py`、`winexcl.py` 僅用 stdlib。

## 判定結果

| 結果 | 意義 |
|---|---|
| `PASS` | candidate 模式下所有檢查皆通過，且沒有延後項 BLOCKED |
| `FAIL` | 有明確違規（完成的長跑證據顯示錯誤率、雙重持有、未授權 TX、重啟等） |
| `INSUFFICIENT_EVIDENCE` | 證據不足（缺 WAL、觀測缺口、未釘住 CLI、無獨占證據等）；不得視為 PASS |
| `BLOCKED` | 延後項未凍結、凍結時點早於基線或晚於候選起點（僅 candidate 模式） |
| `NOT_APPLICABLE_BASELINE` | baseline 模式的結果；只代表絕對條件通過，不代表候選非退步 |

彙總順序：任一 FAIL → `FAIL`；否則任一 INSUFFICIENT → `INSUFFICIENT_EVIDENCE`；否則任一 BLOCKED → `BLOCKED`；否則 baseline 模式為 `NOT_APPLICABLE_BASELINE`、candidate 模式為 `PASS`。退出碼：PASS 與 NOT_APPLICABLE_BASELINE 為 0，FAIL 為 1，INSUFFICIENT_EVIDENCE 為 4，BLOCKED 為 5；判定器自身失敗（門檻檔無法解析、輸入無法讀取等）為 3，與 FAIL 區分，不得被誤讀為發版違規。

## 延後項凍結

`resources`、`min_effective_load`、`comparison_uncertainty` 在 Linux 與 Windows 兩組 24 h 基線完成前皆為 `deferred`。凍結只能由 P2-T16 的 `realhw.acceptance.od09_freeze freeze` 一次寫入，不得手改。凍結時點必須晚於兩平台基線的結束時間，且早於候選 run 的 `started_at_utc`。在凍結之前，candidate 判定一律為 `BLOCKED`。

## OD-94：24 h 基線程序

每輪均為 24 小時（OD-94）；依 OD-102／OD-103 錯開執行：Windows Python 0.4.1 基線（COM6）→ Linux Python 基線 → 凍結延後項 → RC 後 Windows 候選 → Linux 候選 → 0.5.0 正式版。OD-103 的 interactive `SESSION_BUSY` counting 修訂須先由其專屬變更整合，本項不改 counting。Windows 基線待 Windows 原生證據完成後執行；Linux 基線待四個 gate 完成、停止其他 Linux serialwrap 測試後執行。四輪皆在 Orange Pi 3 測試板上，不並行；Windows 測試設定必須以 allowlist 或 `windows.exclude_coms` 限定只開實體 COM6（serialwrap selector `COM0`）；COM3、COM4、COM5、COM24、COM25 一律不得開啟，COM20–COM23 的 com0com 測試也不得與長跑並行。Windows 輪由唯一的 Windows 測試 daemon 開 COM6，關閉其監管自動重啟並留下 attest；Linux 輪先停止 Windows serialwrapd、改由非 serialwrapd bridge 獨占 COM6，再經 localhost TCP→WSL PTY 交給隔離 Linux daemon；同時停止 WSL 正式 `serialwrap.service`，以免觀測器偵測到另一個 serialwrapd。Linux 不以 usbipd 搬動 OPI；WSL MTK 板不參與長跑。Windows 執行用 `py -3.12`，兩平台各自使用 bench lock。

1. 確認 bench lock 已取得，且沒有其他 pytest 或 testpilot 行程。
2. 關閉該實例的監管自動重啟，並把設定證據寫成 `supervision-attest.json`（Linux 可附 `systemctl show -p Restart`；Windows 附對應 Task Scheduler／service 設定證據；§15.11.2）。
3. 啟動 observer 於背景：
   `<platform-python> -m realhw.acceptance.observer --serialwrap-exe <pinned> --boards COM0 --interval-s 10 --out <report>/observer.ndjson --supervision-attest supervision-attest.json`（Linux 用 `python3`，Windows 用 `py -3.12`；僅測 OPI 一板，serialwrap 的 `COM0` 對應 Windows 實體 COM6，Linux COM0 是橋接 PTY）。
   Windows 另加 `--excl-dir <report>/excl --excl-probe-interval-s <workload.windows_excl_probe_interval_s>`。
3a. （Windows）執行 `winexcl handoff --phase pre --serialwrap-exe <pinned> --boards COM0 --observer-header <report>/observer.ndjson --excl-dir <report>/excl`。exit 0 才繼續；exit 1 表示雙重持有，停止並保留現場；exit 4 表示證據不足，排除後重做 pre，不得跳過。
4. 執行 longrun：`<platform-python> -m realhw --tier longrun --duration 24h`（Linux 用 `python3`，Windows 用 `py -3.12`；設定必須 pin 此輪的 CLI／daemon）。
4a. （Windows）longrun 結束後、停止 observer 前執行 `winexcl handoff --phase post`（同參數）。結果照實保存，非 0 由 gate 判定。
5. 結束後把 `daemon status` 所列 `wal_path` 所在目錄複製到 `<report>/wal/`。
6. 執行判定器：
   `<platform-python> -m realhw.acceptance.gate --report-dir <report> --case-dir <case> --acceptance realhw/acceptance/longrun_acceptance.yaml --platform linux --mode baseline --observer <report>/observer.ndjson --wal-dir <report>/wal --out <report>/release-gate.json`。Windows 把 `--platform linux` 改為 `--platform windows`，並另加 `--excl-evidence <report>/excl`。
7. 保存所有 hash，包括 `excl/*.ndjson`。判定器輸出的 `evidence_hashes` 與 `excl_evidence_hashes` 會列出這些檔案。

### OD-102／OD-103：Linux 24 h localhost bridge 操作程序

> 本程序只供擁有者在排定的 Linux 基線／候選輪次執行。本次 bridge 實作與測試**沒有**開啟 COM6、啟停正式 service、執行實機長跑或改寫 `longrun_acceptance.yaml`。Slice 2.4 的 `lr-mixed` 仍維持 `BLOCKED-OWNER`；本程序產出的獨立 `release-gate.json` 不得改寫為 Slice 2.4 PASS。

Linux 輪讓 Windows 上一個**非 `serialwrapd`** 的 pyserial 行程獨占 OPI 的 COM6，再以只綁 `127.0.0.1` 的 TCP 連接 WSL PTY。`lr-mixed` 的 case 契約要求兩個**不同的實體 board selector**；本 bridge 只提供 OPI `COM0`，`COM1` 必須是另一個擁有者明確核准、經穩定 `/dev/serial/by-id/` 或 `/dev/serial/by-path/` 連入 WSL 的裝置，且 udev 序號須和 `realhw/config.json` 的既有 COM1 `serial` 相符。沒有第二板、無法證明兩個 selector 指向不同裝置，或只有同一 PTY 時，停止，不得開跑／不得以同裝置別名湊足 `two_boards`。既有 OD-94 通用步驟 3 的 `--boards COM0` 與 `lr-mixed` 契約不一致；本 addendum 使用 `COM0,COM1`，這個既有文件落差須在正式長跑前由 owner 收斂。

整輪不使用 usbipd，不開 COM3／COM4／COM5／COM24／COM25，也不與 COM20–COM23 的測試並行。Python／Rust CLI、daemon 與所有產品資料目錄必須 pin 到本輪指定版本並隔離；不得讀取或覆寫正式設定／state。Windows 及 WSL 兩邊都不得有其他 `serialwrapd`，也不得並行 pytest／TestPilot。**執行順序**：先完成下方 WSL 步驟 1（停止正式 service、確認無 daemon 並建立 `<RUN>`），再啟動 Windows bridge，最後繼續 WSL 步驟 2 以後；不可先開 COM6 再處理 WSL 正式 service。

#### A. Windows COM bridge（由擁有者執行）

1. 先確認本輪已獲准使用 COM6、`py -3.12` 與 pyserial 可用，且 Windows 沒有任何其他 `serialwrapd`（含 service／工作排程器所啟動者）。有殘留時停止，不要以 bridge 掩蓋或自動終止它。將 `realhw/bridge/com_tcp_bridge.py` 從本輪固定 revision 複製到 Windows 本機暫存目錄，記下 revision 與 SHA-256；輸出目錄亦使用 Windows 本機暫存，不用 UNC 作為狀態檔位置。
2. 在 PowerShell 設定本機輸出路徑、複製腳本並檢查 Windows daemon 清單；若發現任何 daemon，命令會停止，不自動終止它。

   ```powershell
   $WinRun = Join-Path $env:LOCALAPPDATA 'Temp\sw-longrun-<run-id>'
   $BridgeScript = Join-Path $WinRun 'com_tcp_bridge.py'
   New-Item -ItemType Directory -Force $WinRun | Out-Null
   Copy-Item '<Windows-visible-repo>\realhw\bridge\com_tcp_bridge.py' $BridgeScript
   $daemons = Get-CimInstance Win32_Process | Where-Object {
     ($_.Name -match '^(serialwrapd(\.exe)?|python(w)?\.exe)$') -and
     (($_.Name -match '^serialwrapd') -or
      ($_.CommandLine -match '(?i)serialwrapd\.py|(?:^|\s)-m\s+(?:sw_core\.daemon|serialwrapd)(?:\s|$)'))
   }
   if ($daemons) { $daemons | Select-Object ProcessId, Name, CommandLine; throw '仍有 serialwrapd，停止本輪' }
   py -3.12 -m pip show pyserial | Tee-Object -FilePath "$WinRun\pyserial-version.txt"
   Get-FileHash -Algorithm SHA256 $BridgeScript | Format-List | Out-File "$WinRun\bridge-script-sha256.txt"
   Start-Transcript -Path "$WinRun\bridge-console.log" -Force
   ```

3. 在 PowerShell 前景啟動以下命令；確認輸出顯示 `COM=COM6`、`listener=127.0.0.1:48701`。bridge 會以 115200 8N1、無 flow control 獨占開埠，TCP 斷線時保留同一個 COM handle 等待重連；狀態 JSON 每秒更新 PID、COM 開啟時間、連線數與雙向 byte 計數。

   ```powershell
   try {
     py -3.12 $BridgeScript --port COM6 --tcp-port 48701 --status-file "$WinRun\bridge-status.json"
   } finally {
     Stop-Transcript
   }
   ```

   不要從 Windows 另開 TCP 探測連線：bridge 僅允許一個 client，實際 WSL 連線即為連線證據。若 COM 開啟失敗、listener 不是 loopback、bridge 結束或發生 `last_error`，本輪不得計 PASS；保留輸出供判讀。

#### B. WSL 隔離 PTY 與 daemon

1. 先在本次固定 worktree 的 repo root 開啟一個 WSL shell，整輪沿用此 shell 以保存 `SERVICE_WAS_ACTIVE`。在正式輸出根目錄建立全新的 `<RUN>`（建議使用持久化的 `~/b-log/rust-refactor/od94/<run-id>`，不得放進正式設定目錄）。先保存 systemd 原始狀態；只在 `serialwrap.service` 原本為 active 時停止它，並保留 `SERVICE_WAS_ACTIVE` 供結束時精確還原。接著用 `realhw.acceptance.observer._all_serialwrapd_pids()` 做唯讀行程盤點，要求列舉成功且結果為空。若無法列舉、仍有任何 daemon、或 service 原狀不明，停止並保留前置證據。

   ```bash
   set -euo pipefail
   umask 077
   RUN="$HOME/b-log/rust-refactor/od94/<run-id>"
   REPO="$(git rev-parse --show-toplevel)"
   mkdir -m 700 -p "$RUN"
   cd "$REPO"
   systemctl --user show serialwrap.service -p ActiveState -p SubState -p MainPID \
     | tee "$RUN/systemd-before.txt"
   SERVICE_WAS_ACTIVE=0
   if systemctl --user is-active --quiet serialwrap.service; then
     SERVICE_WAS_ACTIVE=1
     systemctl --user stop serialwrap.service
   fi
   printf 'SERVICE_WAS_ACTIVE=%s\n' "$SERVICE_WAS_ACTIVE" | tee -a "$RUN/systemd-before.txt"
   if systemctl --user is-active --quiet serialwrap.service; then
     echo 'serialwrap.service 停止後仍 active，停止本輪' >&2
     exit 1
   fi
   systemctl --user show serialwrap.service -p ActiveState -p SubState -p MainPID -p Restart \
     | tee "$RUN/systemd-during.txt"
   python3 - <<'PY'
   import sys
   from realhw.acceptance.observer import _all_serialwrapd_pids
   pids = _all_serialwrapd_pids()
   print(f"serialwrapd_pids={pids}")
   if pids is None or pids:
       sys.exit(1)
   PY
   ```
2. 建立彼此獨立的 `$RUN/home`、`config/profiles`、`state`、`run`、`wal`、`data`、`log`、`events`、`by-id`、`by-path`、`xdg-*` 目錄；`by-path` 必須保持空目錄。將 owner 核准的第二板穩定 udev link 設為 `SECOND_BOARD_LINK`（必須在 `/dev/serial/by-id/` 或 `/dev/serial/by-path/`，不得填 `/dev/ttyUSB*`），確認它存在且確為與 OPI 不同的裝置，再建立 `$RUN/by-id/sta-board` symlink。完成 WSL 步驟 1 後才啟動 Windows bridge；此時尚不建 target，因為 COM0 PTY symlink 要到下一步才建立。
3. Windows bridge 已在前景執行後，確認 WSL `127.0.0.1:48701` localhost forwarding 可用（不要另開會消耗單一連線的 socket probe）。執行 `command -v socat` 留存結果，再以前景記錄檔啟動 repo 腳本；它優先使用 socat 的 `PTY,rawer,echo=0,wait-slave`，缺少 socat 時使用已測試的 Python PTY fallback。以有界等待確認 link 是新建 symlink，target 位於 `/dev/pts/`；不要手動建立或重用舊 symlink。

   ```bash
   set -euo pipefail
   umask 077
   RUN="$HOME/b-log/rust-refactor/od94/<run-id>"
   REPO="$(git rev-parse --show-toplevel)"
   cd "$REPO"
   mkdir -m 700 -p "$RUN" "$RUN/home" "$RUN/config/profiles" "$RUN/state" \
     "$RUN/run" "$RUN/wal" "$RUN/data" "$RUN/log" "$RUN/events" \
     "$RUN/events-rt" "$RUN/by-id" "$RUN/by-path" "$RUN/xdg-config" \
     "$RUN/xdg-state" "$RUN/xdg-data" "$RUN/runtime"
   : "${SECOND_BOARD_LINK:?請設定 owner 核准的第二板 stable udev link}"
   case "$SECOND_BOARD_LINK" in
     /dev/serial/by-id/*|/dev/serial/by-path/*) ;;
     *) echo '第二板只接受 stable by-id/by-path link' >&2; exit 1 ;;
   esac
   [[ -e "$SECOND_BOARD_LINK" ]]
   ln -s "$SECOND_BOARD_LINK" "$RUN/by-id/sta-board"
   if command -v socat >"$RUN/socat-path.txt" 2>&1; then :; else
     printf 'socat 不存在；啟動腳本將使用 Python PTY fallback\n' >"$RUN/socat-path.txt"
   fi
   "$REPO/realhw/bridge/start_pty_tcp_bridge.sh" \
     "$RUN/by-id/opi-bridge" 48701 >"$RUN/pty-tcp-bridge.log" 2>&1 &
   PTY_BRIDGE_PID=$!
   for _ in $(seq 1 50); do [[ -L "$RUN/by-id/opi-bridge" ]] && break; sleep 0.1; done
   if [[ ! -L "$RUN/by-id/opi-bridge" ]]; then
     kill -TERM "$PTY_BRIDGE_PID" 2>/dev/null || true
     wait "$PTY_BRIDGE_PID" || true
     echo 'PTY symlink 未建立，停止本輪' >&2
     exit 1
   fi
   PTY_SLAVE=$(readlink "$RUN/by-id/opi-bridge")
   [[ "$PTY_SLAVE" == /dev/pts/* ]]
   [[ "$(readlink -f "$RUN/by-id/sta-board")" != "$(readlink -f "$RUN/by-id/opi-bridge")" ]]
   printf '%s\n' "$PTY_SLAVE" | tee "$RUN/pty-slave.txt"
   [[ "$(find "$RUN/by-id" -mindepth 1 -maxdepth 1 | wc -l)" -eq 2 ]]
   find "$RUN/by-id" -mindepth 1 -maxdepth 1 -type l -printf '%f -> %l\n' \
     | sort | tee "$RUN/by-id-inventory.txt"
   ```

4. 在 link 建立後，從 repo 複製 profile，於**隔離副本**加入兩個不同的 target：`COM0` OPI PTY，以及 `COM1` owner 核准的第二板 udev link；`COM1` 的 profile/serial metadata 沿用本機既有 `realhw/config.json`。若 profile 設定 `env_file`，把該 profile 所需的 owner 核准 credentials 以 0600 複製到隔離 `profiles/` 下相同檔名（預設 `COM1` 為 `brcm.env`）；不得輸出或提交內容。建立 config，明確指定 `supervision_mode: on-demand` 與本輪 socket。下列命令中的 `OPI_ENV_SOURCE`、`SECOND_ENV_SOURCE`、`CLI` 必須由擁有者填成本輪核准 credentials 路徑及已 pin 的 CLI 絕對路徑；不要改正式 profile／config。

   ```bash
   cp "$REPO/sw_core/assets/profiles/default.yaml" "$RUN/config/profiles/default.yaml"
   python3 - "$RUN/config/profiles/default.yaml" "$RUN/by-id/opi-bridge" \
     "$REPO/realhw/config.json" "$RUN/by-id/sta-board" <<'PY'
   from pathlib import Path
   import json
   import sys
   import yaml

   path, opi_link = Path(sys.argv[1]), Path(sys.argv[2])
   realhw_cfg = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
   sta_link = Path(sys.argv[4])
   doc = yaml.safe_load(path.read_text(encoding="utf-8"))
   profiles = doc.get("profiles") if isinstance(doc, dict) else None
   if not isinstance(profiles, dict) or "op3-template" not in profiles:
       raise SystemExit("隔離 profile 缺少 op3-template")
   second = [board for board in realhw_cfg.get("boards", []) if board.get("com") == "COM1"]
   if len(second) != 1 or second[0].get("profile") != "brcm-template":
       raise SystemExit("本機 realhw config 必須有唯一、使用 brcm-template 的 COM1 第二板")
   if ("brcm-template" not in profiles or profiles["brcm-template"].get("env_file") != "brcm.env"
           or not opi_link.is_symlink() or not sta_link.is_symlink()):
       raise SystemExit("profile credentials 或兩個隔離 board link 不完整")
   doc["targets"] = [
       {"act_no": 1, "com": "COM0", "alias": "opi-longrun",
        "profile": "op3-template", "device_by_id": str(opi_link)},
       {"act_no": 2, "com": "COM1", "alias": str(second[0].get("alias") or "sta-longrun"),
        "profile": str(second[0]["profile"]), "device_by_id": str(sta_link)},
   ]
   path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
   PY
   : "${OPI_ENV_SOURCE:?請設定擁有者核准的 OPI env 檔路徑}"
   : "${SECOND_ENV_SOURCE:?請設定擁有者核准的第二板 env 檔路徑}"
   install -m 600 "$OPI_ENV_SOURCE" "$RUN/config/profiles/OPI.env"
   install -m 600 "$SECOND_ENV_SOURCE" "$RUN/config/profiles/brcm.env"
   printf 'supervision_mode: on-demand\nsocket_path: "%s"\n' \
     "$RUN/run/serialwrapd.sock" >"$RUN/config/config.yaml"
   CLI="<本輪固定的 serialwrap 絕對路徑>"
   test -x "$CLI"
   export HOME="$RUN/home"
   export XDG_CONFIG_HOME="$RUN/xdg-config" XDG_STATE_HOME="$RUN/xdg-state"
   export XDG_DATA_HOME="$RUN/xdg-data" XDG_RUNTIME_DIR="$RUN/runtime"
   export SERIALWRAP_CONFIG="$RUN/config/config.yaml"
   export SERIALWRAP_CONFIG_DIR="$RUN/config" SERIALWRAP_PROFILE_DIR="$RUN/config/profiles"
   export SERIALWRAP_STATE_DIR="$RUN/state" SERIALWRAP_RUN_DIR="$RUN/run"
   export SERIALWRAP_WAL_DIR="$RUN/wal" SERIALWRAP_DATA_DIR="$RUN/data"
   export SERIALWRAP_LOG_DIR="$RUN/log" SERIALWRAP_EVENTS_DIR="$RUN/events"
   export SERIALWRAP_EVENTS_RUNTIME_DIR="$RUN/events-rt"
   export SERIALWRAP_EVENTS_LOG_PATH="$RUN/events-rt/events.ndjson"
   export SERIALWRAP_BY_ID_DIR="$RUN/by-id" SERIALWRAP_BY_PATH_DIR="$RUN/by-path"
   export SERIALWRAP_TTYMCU_PATH="$RUN/run/dev/ttyMCU"
   export SERIALWRAP_ENDPOINT="$RUN/run/serialwrapd.sock"
   ```

   `SERIALWRAP_BY_PATH_DIR` 必須保持空目錄。所有 CLI、daemon、observer 子行程須沿用同一環境；不得讓設定解析 fallback 讀取 `/etc/serialwrap` 或使用者正式 home，也不得把任何路徑變數設到正式目錄。
5. 以已固定的 `$CLI` 執行 `$CLI daemon start`；等待 `$CLI daemon status` 有 PID，且 `$CLI session list` 中**只有 COM0、COM1** 且兩者皆 `READY`。核對 COM0 `attached_real_path` 指向 `$RUN/by-id/opi-bridge`、COM1 指向 `$RUN/by-id/sta-board`，`readlink -f` 兩者不同，`daemon status` 的 WAL 路徑在 `$RUN/wal`。`$RUN/by-id` 只能有這兩個已核准 link，`$RUN/by-path` 必須空；任何額外 session、其他實體 `/dev/serial` 掃描結果、第二個 daemon、或 Windows bridge `connected` 未成立時都停止，不得進入長跑。
6. 從 `realhw/config.json` 讀取本機既有 bench lock／usbipd 前置檢查路徑，寫成全新的 `$RUN/realhw-config.json`（不改 repo config）：設定 OPI `COM0` 加上 owner 核准的另一塊 `COM1`，`serialwrap_exe=$CLI`、`human_frontend=tmux`、本輪唯一 `tmux_prefix`、`longrun.agent_workers=4`／`snapshot_interval_s=300`，並清空 `win_serialwrap_exe`、`cross_os_probe_argv`。usbipd 路徑只供 harness 檢查，**本流程不呼叫 usbipd**；bench lock 必須沿用指定的共用位置，不可改成 run 專屬鎖。依實際 service／daemon 狀態建立 `supervision-attest.json`，記錄 `on-demand`、`auto_restart_disabled: true` 及 systemd 前後證據，不得只複製範例宣稱已停用。

   ```bash
   python3 - "$REPO/realhw/config.json" "$RUN/realhw-config.json" "$CLI" <<'PY'
   import json
   from pathlib import Path
   import sys

   source, output, cli = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
   config = json.loads(source.read_text(encoding="utf-8"))
   second = [board for board in config.get("boards", []) if board.get("com") == "COM1"]
   if len(second) != 1:
       raise SystemExit("本機 realhw config 必須有唯一 COM1 第二板")
   config["boards"] = [{
       "com": "COM0", "alias": "opi-longrun", "serial": "opi-bridge",
       "platform": "shell", "profile": "op3-template",
   }, second[0]]
   config["serialwrap_exe"] = cli
   config["win_serialwrap_exe"] = ""
   config["human_frontend"] = "tmux"
   config["tmux_prefix"] = "od94-<run-id>"
   config["cross_os_probe_argv"] = []
   config.setdefault("longrun", {})["agent_workers"] = 4
   config["longrun"]["snapshot_interval_s"] = 300
   output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
   PY
   python3 - "$RUN/supervision-attest.json" "$SERVICE_WAS_ACTIVE" <<'PY'
   import json
   from pathlib import Path
   import sys

   path, was_active = Path(sys.argv[1]), sys.argv[2] == "1"
   evidence = (
       f"systemd-before.txt and systemd-during.txt are retained; "
       f"serialwrap.service was_active_before={was_active}, is inactive during run; "
       "candidate daemon is started directly with the pinned CLI and supervision_mode=on-demand."
   )
   path.write_text(json.dumps({
       "mode": "on-demand", "auto_restart_disabled": True, "evidence": evidence,
   }, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
   PY
   ```
7. 執行 realhw 前先做 fail-closed 檢查：目前 `collect_capabilities()` 會在 PATH 找到 Docker 時呼叫 `docker info`，本程序禁止使用 Docker，所以 Docker 可執行檔若存在即停止；不得以假 wrapper 或修改 PATH 偽造能力。確認 observer 與 report 輸出都是新路徑後，以相同隔離環境在背景啟動 observer；等到第一筆有效記錄後才繼續。

   ```bash
   if command -v docker >/dev/null 2>&1; then
     echo 'PATH 有 docker，realhw preflight 會執行 docker info；停止本輪' >&2
     exit 1
   fi
   python3 -m realhw.acceptance.observer --serialwrap-exe "$CLI" \
     --boards COM0,COM1 --interval-s 10 --out "$RUN/observer.ndjson" \
     --supervision-attest "$RUN/supervision-attest.json" \
     >"$RUN/observer.log" 2>&1 &
   OBSERVER_PID=$!
   for _ in $(seq 1 50); do [[ -s "$RUN/observer.ndjson" ]] && break; sleep 0.1; done
   [[ -s "$RUN/observer.ndjson" ]]
   ```

   在前景執行 24 小時 longrun，保存完整輸出及原始 exit code（非 0 仍須留證並繼續收尾）：

   ```bash
   set +e
   python3 -m realhw --tier longrun --only lr-mixed --duration 24h \
     --config "$RUN/realhw-config.json" --report-dir "$RUN/realhw" 2>&1 \
     | tee "$RUN/realhw.log"
   REALHW_RC=${PIPESTATUS[0]}
   set -e
   printf '%s\n' "$REALHW_RC" >"$RUN/realhw.exit-code"
   ```

   以唯讀方式監看 Windows `bridge-status.json`、WSL PTY bridge log、observer 與 daemon。bridge／TCP 中斷、PTY bridge 行程退出、daemon PID 改變、其他 `serialwrapd` 出現或任何狀態無法確認時，照實保留並由 gate 判定；不得重啟或接續同一 run 偽裝成不中斷的 24 h。Windows bridge `accepted_connections`、`bytes_serial_to_tcp`、`bytes_tcp_to_serial` 與 WSL log 是外部 bridge 證據，需連同雜湊封存；目前 P2 gate 不會自動消費該 JSON，人工核對不得改寫 gate 結果。
8. 長跑結束後、Windows bridge 尚未停止時，從另一個 Windows PowerShell 視窗（不得開 TCP probe）讀取原子更新的狀態檔，驗證連線及計數並保存停機前快照；若檢查不符，照實保留證據，不得判 PASS：

   ```powershell
   $WinRun = Join-Path $env:LOCALAPPDATA 'Temp\sw-longrun-<run-id>'
   $status = Get-Content "$WinRun\bridge-status.json" -Raw | ConvertFrom-Json
   if (-not $status.connected -or $status.accepted_connections -ne 1 -or
       $status.rejected_connections -ne 0 -or $status.bytes_serial_to_tcp -le 0 -or
       $status.bytes_tcp_to_serial -le 0 -or $null -ne $status.last_error) {
     throw '停機前 COM bridge 狀態不符合不中斷長跑條件'
   }
   Copy-Item "$WinRun\bridge-status.json" "$WinRun\bridge-status-before-stop.json"
   ```

   接著保存 realhw exit code，以 SIGTERM 停止 observer 並等待結束：

   ```bash
   kill -TERM "$OBSERVER_PID"
   if wait "$OBSERVER_PID"; then OBSERVER_RC=0; else OBSERVER_RC=$?; fi
   printf '%s\n' "$OBSERVER_RC" >"$RUN/observer.exit-code"
   ```

   確認 daemon status 的 `wal_path` 位於隔離 `$RUN/wal` 後，複製至 `$RUN/realhw/wal/`。gate 的 `--report-dir` 為 `$RUN/realhw`、`--case-dir` 為 `$RUN/realhw/lr-mixed`、observer 為 `$RUN/observer.ndjson`、WAL 為 `$RUN/realhw/wal`；依上方 OD-94 的 baseline／candidate 模式及基線報告執行。封存 `release-gate.json`、observer、WAL、`report.json`、case events／snapshots、Windows bridge 停機前快照與最終 status／script hash、PTY log、supervision attestation、命令輸出與 SHA-256。無中斷長跑須由 `bridge-status-before-stop.json` 證明 Windows bridge `accepted_connections=1`、`rejected_connections=0`、`connected=true`、`last_error=null`，且雙向 byte 計數皆大於 0；任何重連、錯誤或 evidence 缺檔均不得人工豁免或把報告改為 PASS。Windows bridge 最終 status 必須在 Ctrl+C 關閉後再複製／雜湊，以保留 `stopped_at_utc`；停機後 `connected=false` 為預期狀態，不替代停機前快照。
9. 清理只限本輪隔離實例：以隔離環境下 `$CLI daemon stop` 停止候選 daemon，記錄命令結果並確認該 PID 已退出；再只對 `$PTY_BRIDGE_PID` 送 SIGTERM（若已自然退出則保留其退出狀態），等待完成並確認 OPI PTY symlink 已移除。確認 `$RUN/by-id/sta-board` 仍是指向原 `SECOND_BOARD_LINK` 的 symlink 後只 unlink 該隔離 symlink，勿碰來源裝置。接著在 Windows bridge 的 PowerShell 視窗按 Ctrl+C，等待狀態更新為 stopped，再把 Windows 本機 `bridge-status-before-stop.json`、最終 `bridge-status.json` 與 script hash 複製入 `$RUN` 並重新計算 SHA-256。最後以 `_all_serialwrapd_pids()` 確認候選 daemon 已退出；只有本輪開始前 `SERVICE_WAS_ACTIVE=1` 才重新啟動正式 `serialwrap.service`，保存 `systemd-after.txt` 並核對狀態。不得使用廣泛 `pkill`、`pkill serialwrapd` 或清除非本輪輸出。

#### 停止條件與未解限制

- **本輪不執行**這個 COM6 runbook；任何原生 Windows 實測若另獲授權，只能使用 com0com `COM20:COM21`，不得開 COM6。
- 現行 `realhw` preflight 若在 PATH 找到 `docker`，會執行唯讀 `docker info` 探測。此處明定不使用 Docker；若該探測會觸及 Docker，應停止並先取得獨立裁決，不可用假 `docker` wrapper 或修改 PATH 來偽造能力結果。此項不屬本 bridge 變更範圍。
- `slice24-17` 已記錄：長跑仍接不進 Slice 2.4 判定（48 h 門檻、candidate 身分綁定、Windows 輪證據及雙平台條件）。本 OD-102 Linux 24 h runbook 不放寬或繞過那些條件；在其正式解決前，只能把獨立 `release-gate.json` 作為長跑證據，`lr-mixed` 仍為 `BLOCKED-OWNER`。

## OD-94：獨立長跑驗收報告與 RC 身分綁定

Slice 2.4 的 `slice24.json`／`slice24.md` 不匯入長跑證據；`lr-mixed` 維持 **BLOCKED-OWNER**，並引用 OD-94。每個平台的長跑驗收結果另以本模組產生的 `release-gate.json` 封存，不得把此獨立報告寫成 Slice 2.4 gate PASS。

先在 Python 基線模式產生該平台的基線報告（每個平台各自一份；不帶 `--baseline-gate`）：

```bash
python3 -m realhw.acceptance.gate \\
  --report-dir <RUN>/report --case-dir <RUN>/case \\
  --acceptance realhw/acceptance/longrun_acceptance.yaml \\
  --platform linux --mode baseline \\
  --observer <RUN>/observer.ndjson --wal-dir <RUN>/wal \\
  --out <RUN>/baseline-release-gate.json
```

候選 RC 長跑完成後，使用同平台的基線 gate 結果比較；Windows 還必須附上原生獨占證據目錄：

```bash
python3 -m realhw.acceptance.gate \\
  --report-dir <RUN>/report --case-dir <RUN>/case \\
  --acceptance realhw/acceptance/longrun_acceptance.yaml \\
  --platform linux --mode candidate \\
  --observer <RUN>/observer.ndjson --wal-dir <RUN>/wal \\
  --baseline-gate <BASELINE>/baseline-release-gate.json \\
  --out <RUN>/release-gate.json

# Windows 將 platform 改為 windows，並加：
# --excl-evidence <RUN>/excl
```

輸入契約：`--report-dir` 必須含 `report.json`（及 `snapshots.ndjson`）；`--case-dir` 必須含 `events.ndjson`；另提供唯讀 `observer.ndjson`、WAL 目錄、此版本 `longrun_acceptance.yaml`，candidate 模式再提供同平台、PASS 的 Python baseline `release-gate.json`。Windows 的 `excl/` 含 `probes.ndjson` 與 `handoff.ndjson`。命令輸出的 `release-gate.json`（含 `acceptance.sha256`、`run.contract_revision`、`run.cli_identity`、`run.deployed_content` 與證據檔雜湊）是獨立驗收報告。

RC 身分不得只用版本字串表示：候選長跑固定使用 gate 後切出的 RC bundle，封存 `release-manifest.json`、`SHA256SUMS` 與該 bundle tree SHA-256；可用以下命令獨立重測已部署內容（Windows 將執行器改成 `py -3.12`；`--cli` 指向實際 launcher，`--daemon` 指向 RC version 目錄內的 daemon image；長跑前後各執行一次並分別封存）：

```bash
python3 -m realhw.deployed_content \\
  --cli <installed-cli-entry> --daemon <RC-version-dir>/bin/serialwrapd \\
  --daemon-pid <serialwrapd-pid> --out <RUN>/deployed-start.json
```

`realhw` 本身會在 `report.json` 記錄 `cli_identity` 及 `deployed_content` start/end/live-daemon；也封存量測輸出及其 `.live-daemon.json` sidecar。核對獨立量測與報告都一致：`release-gate.json.acceptance.sha256` 等於本輪 `longrun_acceptance.yaml` SHA-256，`run.contract_revision` 等於 RC HEAD 的 `contracts` tree，`run.deployed_content.start/end.bundle_tree_sha256` 與 `release_manifest_sha256` 分別等於封存的 RC bundle tree 與 `release-manifest.json` 雜湊，start/end 的 `cli_sha256` 等於 RC `bin/serialwrap[.exe]` 雜湊，`run.cli_identity.pinned` 為 true 且 `sha256` 相同；`run.deployed_content.daemon_live[].in_install` 為 true 且 `image_sha256` 等於 RC `bin/serialwrapd[.exe]` 雜湊。start/end 身分須一致。任何身分不符、缺檔或報告非 PASS 都照實保留，不得改成 PASS；`release-gate.json`、RC bundle 身分資料和全部輸入證據一併封存於獨立長跑目錄，不匯入 `slice24.json`。

## 證據層級

- 本套件的判定結果依證據來源分級。Docker 拓樸（OD-15，`L1.5-docker`）與 `host-throwaway` 報告**不得**滿足 L2、L3 或 bench 項目；它們可以提早揭露缺陷，但不能取代 bench 基線，也不能作為 OD-09 凍結依據。
- Windows 獨占證據只在擁有者 Windows bench（`SW_TEST_COM_PAIR=COM20:COM21`）上取得。`holder_unknown` 只作診斷，不作判定依據。

## 已知限制

- agent worker 的預期集合由引擎 `start.workers` 宣告；缺此欄位即 INSUFFICIENT_EVIDENCE。整個 worker 未啟動也會被發現。
- `min_effective_load` 對候選缺少的鍵視為 0 完成。`error_rate_relative`、`throughput_relative` 對基線有、候選沒有的 (board, source) 判 INSUFFICIENT_EVIDENCE（`candidate_missing`），不略過。
- WAL 掃描：seq 缺號、重號或倒退、run 區間內無有效紀錄、非法 UTF-8，以及 `tx_outcome_unknown`（BC-03 結果不明寫入）一律讓 TX 相關安全檢查判 INSUFFICIENT_EVIDENCE，不得 PASS。
- observer 輸出檔若已存在且非空，拒絕覆寫（退出碼 2）；既有觀測紀錄是證據。
- 以 Python 基線每平台 24 h 完成為前提；不完成的基線不能產生凍結值。OD-103 對 interactive 模式 `SESSION_BUSY` 的 counting 修訂依其專屬變更辦理；本 OD-94 slice 項目不改 counting 判準。
