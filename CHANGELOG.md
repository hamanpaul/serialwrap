# Changelog

本檔案依照 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/) 格式維護，版本依照 [Semantic Versioning](https://semver.org/lang/zh-TW/) 編號。

## [Unreleased]

## [0.4.0] - 2026-11-06

### Added
- Rust bundle manifest 新增同 bundle Python daemon 的 `sw_core` 程式碼指紋；沿用 Python 基線演算法，並保留舊 manifest 與無 manifest 的相容行為。
- Rust CLI 的 setup 對齊 Python #241 的 `--no-restart` 預設值與 help；stale 指紋且 systemd 服務 active 時會重啟 daemon、比對重啟後指紋，並依結果輸出 action、after 與 hint。
- Rust CLI `doctor` 會以 bundle manifest 的 Python daemon 指紋比對 `health.ping` 回報；指紋不同或舊 daemon 未回報時列為 advisory，不改變既有 `version_pairing` 判定。
- Rust setup reconcile 新增可選的同模式 systemd daemon restart 與重啟後程式碼指紋輪詢；system scope 未帶 sudo 時將 restart 列入待執行指令。
- Rust CLI 的 `--version` 改從同 bundle manifest 輸出 Python daemon 程式碼指紋；共用警告邏輯依 Python 基線先判版本差異，再判程式碼指紋差異。
- Rust CLI 新增可獨立測試的 setup daemon 指紋評估與重啟動作選擇，依 Python 基線保留評估欄位、判斷順序及 flash 中不重啟的護欄。
- Rust CLI `setup` 使用既有 `health.ping` 回應與安裝 manifest 比對 daemon 程式碼指紋；無法比較時省略 `daemon_code`。
- 新增 profile 選配欄位 `boot_done_regex`（開機完成訊號），boot quiet window 期間 RX 命中即提早解除，與 `login_regex`／`prompt_regex` 同列判定、同樣受 `bootloader_prompts` 尾行例外約束，不參與登入流程；target 可覆寫，`serialwrap profile test` 一併回報命中。出貨 `prpl-template` 設為 askconsole 的「Please press Enter to activate this console」：prplOS 開機停在 askconsole 時既不匹配 login 也不匹配 prompt，過去只能等滿 180s 才恢復探測；實測 askconsole 在 U-Boot banner 後約 18s 出現，READY 可提早約 2 分半。#162 的 READY 再確認不變（解除 quiet 不清 `ready_reconfirm_pending`，仍由 nonce probe 確認並順帶消耗啟用 banner）。線上 `~/.config/serialwrap/profiles/*.yaml` 不會被 setup 覆寫，既有安裝需手動把此欄位補進對應 template。
- 看得出 daemon 跑的是哪份程式碼，且 `serialwrap setup` 升級後會讓 systemd 服務的 daemon 換新碼。版本字串只來自 `VERSION`，release、分支 build、main 全印 `0.3.2`；`pipx install --force` 加 `serialwrap setup --user` 之後，已在跑的 systemd 服務不會被重啟（同模式刷新只重寫 unit，轉換路徑的 `systemctl start` 對 active 服務是 no-op），daemon 靜默沿用舊碼，agent 只能靠檔案時間猜，曾因此判斷錯誤。

  - 程式碼指紋：`sw_core/**/*.py` 的 sha256 前 12 碼（不含 assets、`__pycache__`；每個行程只算一次）。`serialwrap --version` 印 `0.3.2 (code 1a2b3c4d5e6f)`（僅被問時才算）；daemon 啟動時算一次，`daemon status` 回 `code_fingerprint` 與 `code_path`，每個 RPC 回應也帶 `code_fingerprint`。
  - client↔daemon 比對：版本相同但指紋不同時，CLI 在 stderr 印一行並建議 `serialwrap service restart`；exit code 與 stdout JSON 不變。舊 build 的 daemon 沒有此欄位時靜默。
  - `serialwrap setup`：同模式的 systemd 服務 active 且 daemon 指紋與剛安裝的不同（或 daemon 完全不回報指紋）時，刷新 unit 後自動 restart 並核對重啟後的指紋，結果放在輸出的 `daemon_code`；指紋相同、daemon 連不上、服務非 active、flash 進行中、on-demand 一律不動。system scope 未帶 `--with-sudo` 只把 `sudo systemctl restart serialwrap` 列入 `pending_sudo`。新增 `--no-restart` 略過重啟。
  - `serialwrap doctor` 新增 advisory 檢查 `daemon_code_current`。
  - 相容性：`serialwrap --version` 輸出由 `serialwrap 0.3.2` 變成 `serialwrap 0.3.2 (code …)`，以 regex 取 `\d+\.\d+\.\d+` 的既有消費者（realhw、regression preflight）不受影響。實測 CLI 每次呼叫約多 1.6 ms（約 59 ms 的 2.7%）。
  - 規格見 `docs/serialwrap-spec.md` §8.7。
- 新增可選 `cmd submit --expect-reboot`（RPC `command.submit` 的 `expect_reboot` 布林）：明示這條命令會重開機，不管命令文字一律走既有 reboot 路徑（送出當下 arm boot quiet window、不送 Ctrl-C／Ctrl-D、由 recovery 等板子回 READY）。過去 broker 只認單獨一條的 `reboot`／`systemctl reboot`／`shutdown -r`／SysRq，`firstboot -y && reboot`、`bcm_bootstate 3 && reboot`、`sysupgrade …` 會被當一般命令、送出當下不 arm 開機靜默窗：shell 不再回 prompt 的板子等到逾時再送 Ctrl-C／Ctrl-D，打在 U-Boot autoboot 倒數上；shell 立刻重印 prompt 的板子（prplOS 非同步 reboot）則被當成正常完成，後續命令落進正在 shutdown 的系統；agent 只能記住「reboot 必須單獨一條」。旗標經 CLI、RPC、submit-time gate、arbiter queue 傳到 execute；未帶旗標時 arbiter callback 仍是既有七參數，`command.get` 一律回報 `expect_reboot`，帶旗標的命令執行前於 WAL 寫 `command_expect_reboot` META，submit 回應回聲 `expect_reboot: true` 以便發現舊版 daemon 默默忽略旗標。

  - 串接命令的 prompt 等待：單獨 reboot 維持等 2 秒；帶旗標的串接命令改等 `max(--cmd-timeout, --expected-duration)`。原本固定 2 秒時，前段超過 2 秒就誤入 recovery 分支，之後 prompt 回來（prplOS 的 reboot 非同步）會被 recovery 迴圈當成開機完成、對仍活著的 shell 過早 `confirm_ready()`，session 在板子真正掉線前就回 READY，後續命令落進正在 shutdown 的系統（以 PTY 假 target 重現，修正後 gate 維持 `AUTOBOOT_QUIET`）。
  - 與其他參數：`--mode interactive` 帶旗標以 `EXPECT_REBOOT_MODE_UNSUPPORTED` 零 TX 拒絕；非布林值（如字串 `"false"`）以 `INVALID_EXPECT_REBOOT` 拒絕，不做真值轉換；同時帶 `--completion-regex` 時 reboot 優先；開機靜默窗內旗標不繞過 `AUTOBOOT_QUIET`。
  - 沒帶旗標、但命令在 `&&`／`||`／`;`／`&` 之後含 broker 認得的 reboot 區段時，submit 回應多一則非致命 `CHAINED_REBOOT_WARNING`（`warning` 維持單一欄位、既有 `CMD_LENGTH_WARNING` 優先，`warnings` 列出全部）。只提示，不改變執行；`_is_reboot_command` 的判定範圍沒有擴大。
  - 新增回歸 case `f9-expect-reboot-chained`（真板 reboot 時序，寫成後尚未上板跑過）。
- 0.4.0 `setup` 遇到 0.3.x 舊 daemon（`daemon.handoff_stop` 回 `METHOD_NOT_FOUND`）時不再一律拒絕（OD-48）：確認版本低於 0.4.0、正向辨識為 Python 0.3.x daemon 行程且不在 bundle 的 `versions/` 底下後，先做停止前檢查（其他同使用者 daemon、flash／session 忙碌、外部監管者），把行程資訊寫進 `install-journal.legacy.json` 並 fsync，再於自動啟動 hold 之下停止舊 daemon（該 pid 為 systemd unit 的 MainPID 時用 `systemctl stop`，否則 RPC `daemon.stop`；不硬殺）。舊行程樹全部結束、沒有同使用者行程持有 UART、且沒有新出現的 serialwrapd，連續維持 5 秒才算停止（上限 330 秒；被重拉時對新行程重做檢查後再停，最多 3 次）。成功後照常切換並由 setup 啟動新版 daemon，報告新增 `legacy_stop`。舊 daemon 停止後若任何一步失敗，自動還原安裝狀態但不重啟任何 daemon，報告附 `legacy_daemon_stopped`、`restored` 與啟動指令（OD-48 修訂）；等待逾時回 `legacy_stop_pending`，保留 journal 與 hold，下一次 `setup` 先確認舊行程已結束再還原。`supervision-mode set` 維持拒絕。

  同時修正 `setup` 更新／首次安裝與 `supervision-mode set` 一律以 `local_default_endpoint` 探測舊 daemon 的問題（S1X-c-1）：改依舊模式解析 daemon instance endpoint（systemd-system 固定 `/run/serialwrap/serialwrapd.sock`、Windows 模式用預設 endpoint、其餘優先取 config 中可連線的 unix `socket_path`），啟新／驗證改用目標模式的 endpoint；endpoint 不可 RPC 時，掃到任何同使用者的 serialwrapd 一律以 `DEVICE_STILL_HELD` 拒絕，systemd 監管顯示 running 卻連不上 daemon 時亦同。修正 systemd enabled 狀態誤判（D8）：enabled 改以 `systemctl is-enabled` 判定，撤銷自動啟動 hold 時還原 hold 取得當下的原值，原本 disabled 但正在執行的 unit 不再因一次升級或拒絕被改成 enabled。
- release workflow 新增 `publish-linux-bundle` job（RELEASE-TODO-01）：tag push 時於 `publish` 之後以 `cargo build --release` 與 `mkbundle.sh --release-bundle --profile release` 建出 Linux x86_64 Rust bundle，打包為 `serialwrap-<VERSION>-linux-x86_64.tar.gz` 並產生 `SHA256SUMS`，解開後 smoke（`--version` 等於 `VERSION`、`--help`、release manifest 版本一致、內層雜湊核對、不含 test-fakes 故障注入鉤子）通過才以 `gh release upload` 附加；tag、`VERSION` 與 Cargo workspace 版號不一致即失敗。既有 wheel 與 PyInstaller exe job 不變；Windows bundle 由擁有者 runner 建置後附加（見 `docs/rust-refactor/release-0.4.0-plan.md` RELEASE-TODO-01）。
- 新增 `contracts/behavior_changes.yaml`：登記 OD-04 核准的 BC-01～BC-05（核准者、日期、目標版本、核准時 catalog revision、案例 ID、A/B 參與方式、相容性影響、核准條件），以及依定稿規格推導的 SC-01～SC-27 與缺陷修正 DF-01～DF-17、DF-19（self-test nonce probe 提交點 quiet 重驗；DF-18 保留給 P7），另含 §11.3 六項安全不變量對照與 7 項待擁有者確認事項及預設值。
- 新增 `contracts/cli_schema.json`：凍結全域選項、20 個頂層群組（含群組層選項與互斥組）的子命令與選項、每個子命令送出的 RPC 與參數組裝、stdout／stderr 格式、exit code 總表、重試白名單、預設與硬編逾時、env 變數、`--version` 與版本不符警告，以及 Stage 1A 委派集合，並以測試對照 `build_parser()` 與實際 RPC 呼叫防漂移。
- 新增宣告式 AST 抽取器以追蹤 `sw_core` 的 error_code 與 session last_error，涵蓋 scope 正確的預設值、重綁參數、字典查找、callback、tuple 傳遞、例外欄位與動態前綴，並以有界 memoization 保留未解析原因（OD-06）。
- 新增 `tools/contracts` 契約目錄 loader，逐條驗證 `error_codes`／`session_last_errors`／`behavior_changes`／`diagnostic_aux` 的結構與 YAML 1.1 型別陷阱（Rust 重構 P0）。
- 新增 `python3 -m tools.contracts`（extract／check／summary／skill-section／effective）：比對程式抽出的碼與契約目錄、檢查契約測試（解析到 test node）、行為變更交叉參照與狀態一致性、`x-pending` 併入協定、命名空間分離、排除項稽核與 SKILL 文件覆蓋；所有計數由資料產生。
- 新增 `contracts/error_codes.yaml`：依 Python 基線凍結全部 RPC／CLI error_code 的意義、副作用、可重試性、Agent 處置、介面與附帶欄位，last_error 外流以單一 passthrough 項表示，BC-03 與 SC-19 新碼以 APPROVED（未生效）登錄。
- 新增 `contracts/session_last_errors.yaml`（31 個具名值＋3 個動態前綴的終態性、自動重探、手動恢復、記錄與 detail 規則）與 `contracts/diagnostic_aux.yaml`（warning、detail、self-test classification 與 recovery action 目錄），與 RPC error_code 分屬不同命名空間。
- 新增 `contracts/rpc_schema.json`：凍結 NDJSON framing、早退回應位元組、實測的請求行長上限（`contracts/golden/rpc/line_limit.json`）、54 個 RPC method 的參數轉型、selector 別名鏈、成功回應 schema、錯誤碼與判定先後、副作用與重試／阻塞／長逾時旗標，以及 SessionPublic 等共用子結構的欄位順序；CI 安裝 jsonschema 供契約測試使用。
- `SKILL.md` 與 `SKILL_WINDOWS.md` 新增自動產生的「錯誤碼與 last_error 處置目錄」，列出每個 error_code 與 session last_error 的意義、介面、可重試性與 Agent 處置（OD-06）。
- 新增 `contracts/wal_record.schema.json`（WAL 行欄位、型別、crc32 格式、排序序列化、rotate 門檻與 seq 語意）、`contracts/wal_meta_events.schema.json`（META 事件唯一 schema，涵蓋 credentials_unresolved、transport_stall_suspected、command_completion 與 BC-01 核准的重建事件與缺口範圍規則）與 `contracts/state_file.schema.json`（state.json 五個頂層 key 與列結構），並以實際 WalWriter／SessionManager 輸出驗證。
- 偵測 WAL 被外部刪除或替換，以有界重試重建並先寫入 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 缺口 META。重建失敗時標示 WAL 不健康並停止新增的系統自動 TX。
- 支援 Windows 上的 `serialwrap remote -R/-L`、`connect` 與 `benches`，並新增 Windows 行程存活判定、整棵行程樹終止及跨行程檔案鎖；`remote stop` 新增為 `remote close` 的別名。
- 新增隔離實例專用的測試裝置 allowlist（`SERIALWRAP_TEST_DEVICE_ALLOWLIST` 或 `windows.test_device_allowlist`，只接受明確列出的 COM 名稱）：僅在 `SERIALWRAP_STATE_DIR`、`SERIALWRAP_RUN_DIR`、`SERIALWRAP_CONFIG_DIR` 三者皆明確設定且不等於預設路徑時生效，正式實例設定時拒絕啟動（`TEST_DEVICE_ALLOWLIST_REJECTED`，exit 2）；生效時 `daemon status` 顯示 `test_device_allowlist`。隔離實例下若 `config.yaml` 存在但無法解析且未以 env 提供 allowlist，同樣拒絕啟動（`invalid config.yaml`）；正式實例的啟動行為不變。不影響獨占鎖與 serial backend。
- 新增 `SERIALWRAP_CONFIG` 與系統設定檔唯讀 fallback，正規化 lock／socket 路徑並讓 Windows singleton lock 使用 `LockFileEx`。監管切換與 `daemon restart` 會先確認舊 daemon 已結束；daemon 啟動時先掃描裝置、配好 COM rank，再啟動 watcher。
- 新增 Python 側 release manifest 讀取與部署程式碼 `tree_sha256`（`sw_core/release_manifest.py`，契約見 `contracts/release_manifest.schema.json`）：位於發佈組合（bundle）內時，`doctor` 新增非 advisory 的 `version_pairing` 檢查，比對 CLI／daemon 版本、launcher／runtime／套件樹與 SKILL 資產雜湊；純 Python 安裝的 `doctor` 輸出不變。`daemon start` 改依 manifest launcher、同目錄 `serialwrapd`、checkout 腳本、`python -m sw_core.daemon` 順序定位 daemon（DF-05），修正 pipx 安裝下 spawn 不存在的 `serialwrapd.py`（CLI-7.4）。同機多開偵測（`doctor` 的 `single_daemon`、`daemon start` 防線）同步計入 `python -m sw_core.daemon` 形式的 daemon。
- release、clear、bind、斷線與關機改用 drain barrier，期間提供 drain 診斷並等 handle 關閉回條；已確認送出至少 1 byte 後中止的命令回報 `COMMAND_ABORTED_IN_FLIGHT`。新增 `daemon.handoff_stop`，原子凍結新操作後再有序關機並同步 WAL。
- passthrough 命令以明確的 completion regex 或可用的 profile prompt 判定完成；沒有完成條件時，等待 RX 靜默或逾時後回傳 `PROMPT_TIMEOUT` 與已擷取輸出，不送 Ctrl-C／Ctrl-D，session 維持 `READY`。`file.push`／`file.pull` 對 passthrough 一律以零 TX 拒絕。
- passthrough session 不再登入或送 probe；連續 30 秒沒有輸出即自動進入 READY，可直接送命令。
- 建立 Stage 0 案例承接清單 `tests/acceptance/cases.yaml`（regression、realhw、func-test、§15.7.1 情境、Windows-native 與 MCU 手動程序，共 120 筆）、欄位 schema `cases.schema.json`（單一權威）、切片凍結鎖 `cases.lock.json` 與唯一寫入者 `regen_lock.py`（`--add-only` 拒絕消失或延後既有案例）、統計檔 `cases.counts.json`，鎖檔另快照每案例的分母屬性（platforms、layer、expected 等，改動即拒絕）；新增 Stage 0 出口證據索引 `stage0_evidence_index.yaml`（逐案例、逐適用平台、逐證據檔登錄，§4 必要項目缺列即 FAIL）、其 schema 與校驗器 `check_evidence_index.py`（exit 0 僅表示索引完整且檔案完整性相符，不是 verdict PASS；目前 exit 5，未滿足前不得視為通過）；新增 MCU 韌體升級手動程序 `realhw/procedures/p2-1-mcu-flash.md`（不計入任何計數；隔離實例以 `daemon start --force-spawn` 另起）；`docs/regression-plugin.md` 的 `f4-background-result-tail-consistent` 改為預期 PASS（#159 已修），不再保留紅燈哨兵。
- 新增 `uart_stdio_bridge` bridge 核心：TX／RX 獨立 worker、有界背壓、DUT stderr 持續排空、loopback 權杖控制通道、checkpoint 與 DUT／傳輸故障動作。資料以 binary bytes 轉送並依實際接受長度留證；不確定寫入與證據失敗會 fail-stop。
- 新增 POSIX PTY 與 Windows pySerial bridge 端點 adapter，提供 byte 保真的讀寫、端點世代、斷線與重連能力，以及依平台回報的 transport 能力。修正 PTY 關閉時尾端資料排空、EAGAIN 重試的 peer gate 與無 peer 時的忙等；同步保護 I/O 和重連，並讓斷線窗口依 timeout 回空結果。Windows adapter 驗證 pySerial 寫入長度、限制零 timeout 的不確定路徑，並遵守 peer-close 能力閘門。`open_endpoint` 現在回傳已開啟的 adapter。
- 補齊 `uart_stdio_bridge` bridge.v1 證據契約（P2-T2）：新增 `tests/acceptance/` 的 `bridge_record`／`bridge_control`／`correlation`／`l15_manifest` 四份 JSON Schema（draft 2020-12，`comparison` schema 補 `$id`）、CRC golden vectors 與紀錄／writer 單元測試。紀錄建構改為拒絕未知 observation／META event、空 payload 與 generation／seq <1；`wall_ts` 固定符合 WAL 的 UTC 格式。writer 的 flush／close 有期限、保留失敗碼、在可行時記錄 `evidence_failure` META，並拒絕生命週期外的寫入；Windows `platform_info()` 由 bridge manifest 與 selftest 傳入 endpoint、COM driver 與安裝方式，查詢不足列入 `evidence_insufficient`。
- 實作 `uart_stdio_bridge` §15.4.3 / Spike 4 自測項目（ST-01～ST-14）、核准 revision 治理與 gate 資格判定報告。
- 建立 `tools/uart_stdio_bridge/` 獨立外部 bridge 套件骨架、依賴定義、產品邊界守衛（禁止 import `sw_core`）、`identity.tree_sha256` 演算法、`HARNESS_*` 測試原因碼、CLI 骨架與 GitHub Actions CI 自測 job。
- 新增 OD-15 Docker L1.5 驗證環境：兩容器假 UART PTY 拓樸、systemd PID 1 驗證、長跑與監管 runner，以及會拒收 Docker／throwaway 假冒真板證據的層級標記與索引檢查。Docker 證據保留三個 image digest 與套件樹摘要；48 h／>72 h runner 僅在 P1 與 P1-OC 門檻通過後啟動。
- 新增 DUT Docker 執行模式（`-i` 無 `-t`，固定 python:3.12-slim digest 映像檔）與 local-process 隔離模式，包含映像檔建置指令稿及命令列參數組裝。
- 自 `FakeTarget` 抽離 sans-IO 的 DUT 行為模型 `DutModel` 與 `ScenarioPlan`，支援開機 banner、命令回應、登入狀態機、動態提示符、串行延遲、雜訊以及故障注入。新增 `uart_stdio_bridge.dut` 模組，提供標準串流模擬器（`run_stdio`）與純二進位回顯模式（`run_echo`），並實作基於目錄檔案的控制通道（`ControlFileChannel` 與 `DutControlClient`）。改寫 `func-test/lib/fake_target.py` 內部委派至 `DutModel`，並新增安全匯出模組 `target_config.py`，保持位元組層級完全等價。
- 實作 `uart_stdio_bridge` WAL↔wire 比較器、correlation 窗口判定、中途 checkpoint 驗證、竄改拒絕與敏感資訊遮罩（redact）。
- 新增 func-test 外部 bridge 模式、隔離 harness 與 artifact/deployed-content 證據；補上 correlation、settle/quiesce、Windows Telnet console，以及修復對應的案例 oracle。
- 實作 §15.7.1 外部情境腳本與協調器新動作：新增 15 個 L1.5 故障注入與邊界情境、3 個 Windows-native COM 情境，擴充 runner 動作表（op/bridge/dut/wal/wire/hold 等 16 個動作）、不變量執行者約束與衍生變數展開，並嚴格校驗 meta.scenario。
- 新增跨平台 PID／RSS 觀測、Windows 共用 bench lock、平台能力式 case SKIP 與 Telnet human frontend；realhw、regression、reliability 均使用 pinned CLI 與 client／daemon 版本 gate，回歸報告記錄 deployed content，長跑即時收斂 background 結果並記錄 worker 存活事件。
- 新增長跑發版判定套件 `realhw/acceptance/`：`longrun_acceptance.yaml` 為發版長跑門檻唯一來源（延後項待 OD-09 凍結），外層 `release_gate_result` 判定器原樣保留引擎 `engine_result` 且不把引擎 FAIL 轉為 PASS；新增唯讀長跑觀測器（只呼叫 `daemon status`、`session list`）、獨立 WAL 掃描器（驗 crc32、重送與禁止窗口），以及 Windows 原生 COM 獨占開啟證據（`winexcl`，零 I/O 的 `CreateFileW` 探測與 handoff 循環）。
- 新增 OD-09 延後項凍結工具 `realhw/acceptance/od09_freeze.py`（stats、propose、freeze、check、register-candidate）作為延後項唯一寫入者；基線統計與違規檢查、固定公式提案、一次凍結兩平台並附加 owner-decisions addenda，候選長跑開跑前須先登記，未凍結者拒絕。
- 新增 pytest 資源報告外掛 `tests/resource_report_plugin.py`（`SW_RESOURCE_REPORT=<path>` 寫出 `l1-results.v1` 報告，含每案 outcome、收集與取消收集清單、`deployed_content` 量測）與純函式 `tests/resource_report.py`；`core_portable` 項目若帶平台條件 SKIP 即 exit 1（以 marker 結構判定，不看 reason 文字）；Windows collection 前排除模組層 `posix_only` 測試檔；新增 L1 必要案例集合鎖 `l1_required.lock.json`（`regen_lock.py --l1-add-only` 只增、`--check --base` 禁止基準 nodeid 消失）與唯一判定實作 `tests/acceptance/l1_judge.py`（單一 pass、required skip、重複、deselected、未執行、exit 非零、未完成、junit 不符皆不得 PASS）；`check_evidence_index.py` 以 `l1_set` 判定 `S0-L1-BC`、`S0-L1-WIN-PYTEST`；`.github/workflows/tests.yml` 新增 hosted `pytest-windows`（免驅動 Windows L1）與自架 `windows-com-selfhosted`（label `run-windows-com` 或 workflow_dispatch，runner 未註冊時不視為通過）。
- 新增 socat PTY＋EXEC 接線可行性 spike 腳本（§15.4.1、P2-T9），驗證 raw 轉接、binary 保真、no-echo 與子程序結束時 PTY EOF 語意與延遲。
- 新增 Windows host 的隔離 throwaway 排程監管程序與 WhatIf XML 預覽；補上 Spike 1 剩餘控制線隔離、Console-side pair、peer-close 實測工具及報告 schema，並登錄對應 Stage 0 證據項目。
- 新增 Rust Cargo workspace 骨架與平台 cfg 邊界檢查。
- 實作 pyjson 解析與 Python json.dumps 等價序列化。
- 實作 Python 相容語意層：`int`／`float` 字串解析、`repr(str)`、`str.splitlines`／`strip`、UTF-8 解碼錯誤字串、`posixpath`／`ntpath` 路徑操作與 `OSError` 字串格式，並以 CPython 3.12.3 產生的 golden 驗證。
- 新增 Python re 相容層：解析 CPython 3.12.3 regex 語法與錯誤位置，依 Unicode 15.0.0 表格轉譯字元類及 IGNORECASE，並實作 code point 搜尋、超大字面重複項搜尋及 CLI／daemon completion regex 驗證入口。
- 實作 `sw-proto::yaml11` 與 `config_types`：以 `yaml-rust2` 事件流支援 PyYAML 1.1 SafeLoader 子集解析（含 YAML 1.1 型別判定、tag 建構與 SafeLoader 子集相容），並依 Python 基準實作 profile 目錄載入、template 解析與 `RuntimeConfig` 存取。
- 實作 sw-proto 資料型別：常數、執行期路徑（含 SC-09 設定檔解析）、RPC endpoint 解析、版本資訊、RPC 方法表、WAL 紀錄序列化與 release manifest 驗證。
- 新增 `scripts/rust/gen_error_codes.py`，由 `contracts/error_codes.yaml`（依 YAML 順序）與 `contracts/session_last_errors.yaml` 生成 sw-proto 的 `ErrorCode`（含 `META`）與 `SessionLastError` 列舉，`--check` 可作為生成物同步閘門；並於 sw-cli 實作唯一的錯誤回應入口 `output::error_resp(ErrorCode, Option<&str>)`。生成器不產生任何 passthrough 變體（SC-34）。
- 新增 `sw-pal` client 側環境查詢、檔案讀取、RPC 傳輸、跨平台行程委派、時鐘與可腳本化測試替身。
- 新增 CPython 3.12.3 argparse 相容解析、help／usage／錯誤格式、20 個 CLI 群組規格，以及保留非 UTF-8 argv 的委派路由分類。
- 新增 Rust CLI 的 stdout JSON（sort_keys、緊湊分隔符）、stderr 錯誤行與版本不一致警告的輸出函式，以及 `-v`／`SERIALWRAP_LOG_LEVEL` 的 RPC trace 層級解析、endpoint 摘要與 trace 行序列化，行為與 Python 基線位元組一致；並新增 `scripts/rust/goldens/gen_cli_trace.py` 與 `contracts/golden/cli_trace.tsv` 作為 trace 解析的 golden 基準。
- 實作 Rust CLI endpoint 解析（--endpoint、--socket、--bench、config.yaml、預設 local endpoint）、#108 dangling fallback 機制與 benches.state.json 唯讀讀取。
- 新增 RPC client 的唯讀白名單重試、指數退避與 TIMEOUT 後 daemon 探測。
- 實作 Rust CLI 資料面 RPC 子命令（device、session、alias、cmd、stream、log、file、wal、mcu）的參數組包、wire 格式與有效 timeout，並接上 `main` 分派；RPC 失敗、endpoint 解析失敗與 stdout 寫入失敗的 stdout／stderr 與 exit code 對齊 Python 基線（exit 0／1／2／120）。非 UTF-8 argv 依 Q-19 處理：路徑型值（`--socket`、`--endpoint`）以原始位元組連線，進入 RPC 請求者於送出前失敗（daemon 收 0 筆），`--bench` 回顯者 strict 失敗（Q-19(c)，登記為與 Python 的非契約差異）。
- 實作 Rust CLI 的 `event` 群組（10 個子指令的 RPC 組包、`add --file` 讀檔與 JSON 解析、非 UTF-8 參數與 timeout 例外的 INTERNAL_ERROR 訊息）、離線 `profile test`（regex 命中行、`UNKNOWN_PROFILE`、`SAMPLE_READ_FAILED` 與 Python 未攔截例外的 exit 1）與內嵌 `skill` 操作指南輸出（Windows 上 `\n` 轉 `\r\n`）；stdout、stderr 與 exit code 對齊 Python 基線。
- 新增 Stage 1A 管理指令的 manifest 固定入口透明委派、遞迴防護與完整開發 bundle。
- Windows Rust bundle 新增預設不安裝、需 UAC 同意的實驗性 com0com 選用元件，可用 `setup --with-com0com` 安裝及 `setup --remove-com0com` 移除；並附 GPLv2 授權與原始碼提供資訊，基本安裝不需管理員權限。
- Rust CLI 原生承接 `connect` 與 `benches`，Windows 亦可使用（移除 `REMOTE_NOT_SUPPORTED`）；bench endpoint 記憶改以 `benches.state.json.lock` 跨行程鎖與原子寫入維護，並沿用 remote 隧道核心。
- Rust CLI 原生承接 daemon start／stop／status／restart，加入 graceful stop、多開預檢與 Windows on-demand 首次需求啟動。
- Rust CLI 原生承接 doctor 全部檢查（Linux 18 項、Windows 12 項），並新增 release manifest 版本配對檢查 version_pairing（SC-25）。
- 新增版本目錄安裝佈局、穩定入口原子切換與回復、release manifest／SHA256SUMS 驗證及 CLI／daemon 版本配對檢查。開發 bundle 會記錄 Python state schema 與 Windows task XML 樣板摘要。
- 新增 Rust daemon launcher、PyYAML 相容輸出與 bench registry，並讓開發 bundle 內含 Python runtime、daemon、套件、版本 manifest 與檔案雜湊清單。
- 補齊 PAL 管理面環境查詢、子行程執行與 Windows SERIALCOMM／BTHENUM 登錄讀取。
- 新增 POSIX daemon 背景啟動、非阻塞輪詢與前景執行；Windows 以 `DETACHED_PROCESS` 啟動 daemon，並保留完整 DWORD 結束碼。加入保守處理不可讀 `/proc` 狀態的 daemon 與 zombie／PID 世代掃描；以 Toolhelp32 掃描 Windows daemon，合併 launcher 與其 Python 或 launcher 子行程。
- 實作 POSIX 與 Windows 的分離行程通道、行程身分核對及終止；補齊 PAL 檔案原子寫入、目錄複製與刪除、symlink、跨行程鎖等寫入操作。POSIX 終止行程群組於 SIGKILL＋回收後即視為完成（同 Python `_terminate_pgid`），不再因 PID 1 不回收的 zombie 把已終止的隧道標為 error。
- Rust CLI 原生管理 SSH -R／-L 隧道，支援狀態查詢、關閉及 remote stop 別名；Windows 路徑使用非 ControlMaster readiness 探測、行程身分核對與 fail-closed bind 驗證。Docker 拓樸驗收可唯讀掛載並執行指定 CLI bundle。
- Rust CLI 原生承接 `service`（start／stop／restart／status）與 `supervision-mode` 查詢，不再委派 Python；`config.yaml` 寫入改為位元組相容的 PyYAML 輸出，壞 YAML 改為明確錯誤（DF-07）。系統唯讀設定檔不會被寫入。
- 實作 systemd、Windows 工作排程器與 Windows 服務的 PAL 監管控制，並加入交接期間持久停用自動拉起及釋放還原流程。修正 held 重新註冊、具名 hold 釋放、systemd 家目錄解析與安裝失敗回報，並補齊對應回歸案例。
- Rust CLI 原生承接 setup 資產物化、WSL systemd 準備與監管 reconcile，並驗證 supervision-mode set 參數。
- Stage 1B 出口驗證工具：新增 CLI 暖快取往返量測（`bench_cli_roundtrip.py`，nearest-rank 分位數與 §1.2 gate；WSL、容器、平台不符一律 `NOT_STANDARD_ENV`，`--env-manifest` 不得覆寫工具自動量測的身分欄位）、單檔解壓檢查（`check_no_onefile_extract.py`，Linux inotify、Windows `ReadDirectoryChangesW`；共用系統暫存根的事件判 `INSUFFICIENT`，不得判 PASS）、P4 必要案例凍結與完成度檢查（`check_p4_cases.py`；L1 判定唯一委派 P2 `l1_judge`，BLOCKED-OWNER 以 exit 2 分流）。
  狀態：全群組 A/B 兩輪執行器、L1.5 黑箱案例、Windows 原生 cargo L1 報告與 `s1b_*` L1 集合登記仍為 BLOCKED-PREREQ（需併入 `feature/rust-refactor`），以明確失敗呈現，未以 xfail 交付。
  黑箱 Rust 輪的 `setup`／`supervision-mode` `--help` 改以 Rust-only golden 為 oracle（SC-22／SC-23／SC-24／SC-36 為 absolute_only，與 Python 必然不同），`capture_cli_help.py --rust-only` 擷取寬度擴至與平台 golden 相同的 20～120 九種欄寬。
- Rust CLI 原生支援首次安裝、版本更新、`setup --rollback` 與中斷復原，並為 `supervision-mode set` 加入 fail-closed 交接護欄。
- Stage 1 出口 gate 讀取 Python daemon 基線缺陷登記（OD-75）。符合平台、狀態與失敗訊息特徵的案例維持證據 FAIL，另列「基線已知缺陷（不擋）」；未登記、只有 B 輪失敗、特徵不符，以及 A/B 兩輪都失敗尚未分類者仍擋出口。`l15_exit_driver.py --rejudge` 可用既有證據重判，不重跑測試。OD-30～OD-32 的擁有者延後列行為不變。初始登記為空。
- Rust CLI 跟進 #234 `boot_done_regex`：`sw-proto` profile 載入新增 `boot_done_regex`（template 載入與 target 覆寫語意與 Python 一致），`serialwrap profile test` 回報 `checks.boot_done_regex` 是否命中，CLI help golden 依 Python 變更重產。
- 新增 0.3.2 正式版 → 0.4.0 候選的 Linux 升級驗證（OD-33）：`scripts/docker/run-upgrade.sh` 在 OD-16 (5) 的 ubuntu:24.04 systemd 容器（`tools/docker/upgrade.Dockerfile`，不連網、不帶裝置）內，對 systemd-user 與 on-demand 兩種監管模式各自執行 pipx 安裝 0.3.2 → bundle 升級 → `setup --rollback` → 手動降級 → 重新升級，檢查入口取代、config／profiles 保留、alias／binding 相容、監管收斂、doctor 與全程無 two-reader，結果寫 `upgrade-report.json`。並新增 `docs/rust-refactor/release-0.4.0-plan.md`（發佈內容、升級／降級程序、已知升級問題與 release notes 草稿）。

  升級驗證判準收緊（雲端審查 e270e89#1～#3）：基線必須實際含重綁後的 `shellboard:COM7` by-id（binding 相容檢查不得空轉）；兩個拒絕型檢查除現場未改動外，拒絕原因也須與規格相符（舊 daemon 執行中為 `METHOD_NOT_FOUND`＋`daemon.handoff_stop`、回滾為 `INVALID_ARGS`＋`rollback_unavailable`）；`setup --rollback` 若成功，CLI 與 daemon 皆須回到 0.3.2 且設定逐位元組保留。
- 追蹤 [#229](https://github.com/hamanpaul/serialwrap/issues/229)，驗證紀錄見 [每命令完成條件驗證](../docs/229-command-completion-validation.md)。

  - 新增可選 `cmd submit --completion-regex`，經 RPC、仲裁佇列與命令記錄傳到執行層；noisy console 無 shell prompt 時可依本命令的新 producer RX 完成，只裁切完成標記之後的 profile prompt、保留完整 stdout 與既有 reboot recovery。
  - 在所有提交／執行入口檢查有界、可編譯且不匹配空串的 regex，無效值以 `INVALID_COMPLETION_REGEX` 零 TX 拒絕；命令記錄與 WAL META 提供完成設定稽核。
  - PTY 覆蓋 numeric RC／END、echo-only、歷史標記、原 prompt fallback、CLI/RPC 傳遞及七參數 callback 相容性。
- 新增 `smart4R-profile`、`console_unlock_env` 與 `console_unlock_idle_s`，在實際 UART RX 靜默滿額後處理隱藏 console lock；已有 shell prompt 不送 secret，CFE prompt 不送 probe，缺來源以 `CREDENTIALS_UNRESOLVED` 停止。
- Attach、recover、自動 reprobe 與 reboot recovery 支援只需 console unlock 的憑證；以獨立 nonce 輸出確認 READY，提供不含 secret 的解鎖時序日誌。
- 精確識別 `echo b > /proc/sysrq-trigger` 的立即 reboot，其他 profile 保持既有 boot quiet 行為；以 PTY 覆蓋實際 RX 靜默與各 recovery 入口。

### Changed
- Rust CLI 的 `cmd submit`、`file push`、`file pull`、`device release` 與 `session interactive-open` 對齊 #240 來源檢查：拒絕 daemon 保留的 `system` 前綴，接受 bare `human` 並保留原始 RPC 參數，空白判定遵循 Python `str.strip()`；錯誤輸出與 Python CLI 逐位元組一致。
- Rust CLI 的 `cmd submit` 新增 `--expect-reboot`，互動模式拒絕訊息與 Python CLI 對齊，並只在旗標啟用時向 `command.submit` 傳送 `expect_reboot: true`。
- `install.sh` 改為安裝 Rust bundle（OD-50）：預設從 GitHub release 下載 `serialwrap-<版本>-linux-x86_64.tar.gz` 與 `SHA256SUMS` 並驗證（未給 `--version` 取最新 release；雜湊不符或缺列即失敗、不執行 setup），或以 `--bundle <目錄或 .tar.gz>` 離線安裝，解到暫存目錄後執行 `<bundle>/bin/serialwrap setup` 並回傳其 rc。安裝前檢查 x86_64、glibc ≥ 2.39 與 `/usr/bin/python3.12`，不符時 rc=2 且不改動任何東西；偵測到舊 pipx 安裝時提示可 `pipx uninstall serialwrap`（不自動移除）。pipx 安裝路徑退役，README 安裝章節改以 `install.sh`／bundle 為主要方式。
- `mkbundle.sh` 接受呼叫端指定的 `CARGO_TARGET_DIR`，並只在 `--dev` bundle 啟用 `sw-cli/test-fakes` 更新測試鉤子；正式 bundle 不含該 feature。Cargo L1 凍結清單排除 ignored 與 `cap_*` 測試，完整執行報告仍保留 ignored 結果；cfg 邊界檢查器不設原生平台測試例外，POSIX／Windows 測試改由 pytest 明確驅動。
- 修正 Rust 重構 P0 階段盤點出的文件與註解勘誤，行為不變：`SKILL.md` 的 `file push` 預設 chunk 改為 512、`file pull` 省略 `--local` 時寫入 daemon 工作目錄並附 `local_path`、event counter 與 event log 改述為 `STATE_DIR/events`（非 tmpfs）；`README.md` 的事件規則僅讀 `*.json`、stdin payload 欄位更正為 `selector`／`matched_at`；`docs/design-event-trigger.md` 改述飽和時丟棄新 fire、`match_skipped` 的 4 種 reason、`matched_line` 與 `bridge_generation` 的實際值；`CLAUDE.md` 補上 CLI `sort_keys=True`、動態 session 不套用 `defaults.log_dir`、passthrough 的 probe 由 `ready_probe` 決定，並補充隔離實例需同時設 `SERIALWRAP_BY_ID_DIR` 與 `SERIALWRAP_BY_PATH_DIR`；`docs/serialwrap-spec.md` 更正 Windows 走 TCP loopback。同時新增 `contracts/p0_row_coverage.yaml` 與規格勘誤節，並以 `tests/test_contracts_doc_errata.py` 防止勘誤回退與 P0 列漏登。
- 為盤點時沒有任何測試斷言的 error_code 與 session last_error 補上 Python 基線特徵化測試（framing 位元組、alias、console、file transfer、bench、service、lock 等），使每個生效碼都有契約測試。
- 新增 `contracts/daemon_logging.yaml`（daemon logging 去向、等級、格式與全部訊息模板）與 `contracts/golden/last_error/transport_stall_hint.json`（TRANSPORT_STALL detail 位元組級 golden），並以特徵化測試凍結 config 載入的已知 quirks（錯誤分流、login_regex 預設值、templates 只取最後一檔、字串 "false" 的 truthiness），供 Rust 重構對照。
- `.project-policy.yml` 宣告 OD-06 診斷碼文件覆蓋設定（抽取命令、說明文件、條目格式）、以 R-26 同步 SKILL 受管區段、將 `contracts/**` 納入 code_paths，並在 preflight 加入 `python3 -m tools.contracts check`；CLAUDE.md 新增契約治理政策段。
- pytest 註冊 core_portable／posix_only／windows_native 三種平台 marker 與 memory_only／process_rpc／posix_pty／windows_com／physical_uart／supervisor_runtime／interactive_desktop 七種資源 marker，逐檔標註既有測試，並以 AST 稽核測試防止漏標、誤標與 core_portable 平台 SKIP。
- 新增 C13 與 R-27 共用的語意對等 fixture 與測試，確保契約驗證器與 paulsha-conventions 規則具有一致的覆蓋判定行為。
- WAL 與 mirror 改為常駐 handle 並逐筆 flush，關閉時同步後釋放。mirror 寫入失敗只重試 mirror，不再重複寫入同一 seq 的 WAL 行。Windows 新寫入 WAL 行尾與 mirror 內的換行由 CRLF 改為 LF；`health.status.wal` 新增 `unhealthy_reason`。
- F12 `f12-wal-path-live` 與 L3 `p0-wal-live` 改以讀回含 marker 的新增 WAL 紀錄並驗證 CRC 判定活性，不再依賴 mtime。
- 新增 Stage 0 Python 基線凍結值擷取與 S1-W WAL 寫入微基準工具。
- 新增 Stage 0 行為變更的實機回歸 case（F10 `f10-release-drain-receipt`、`f10-release-aborts-inflight-command`，F5 `f5-deferred-dropped-after-reboot`；擁有者開立 issue 前以 `pending_issue` 標示待補），並新增 Windows WAL／EDR Spike 3 腳本、release 持久化前後 kill、Windows remote 三拓樸與 Spike 1 虛擬 COM 資格的實機程序 runbook；隔離實例下 `SERIALWRAP_TEST_KILL_AFTER_SAVE` 測試鉤子僅於 release 持久化落盤後生效（建構子不觸發），`SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE` 提供 commit 前的確定性暫停點；非隔離實例只寫警告。
- 新增 Stage 0 共用測試替身、TX 結果純型別、平台條件 skip，以及 Windows 原生 pytest runner 與 JUnit 摘要。
- 同步 OD-14 對外文件：`session attach`／`session recover` 失敗時 `error_code` 固定為 `SESSION_NOT_READY`，原診斷值改由頂層 `last_error`、`last_error_detail` 回報；`device release` drain 逾 3 秒改回 `DRAIN_STALLED`（RECOVERING，不得交接）；`platform=passthrough` 不 login、不 probe，靜默 30 秒自動 READY 並建議以 `--completion-regex` 指定完成條件；UDS socket 維持 `0660`（可用 `SERIALWRAP_SOCKET_GROUP` 指定群組）。同步 README（英文與繁體中文）、`docs/command-completion.md`、CLAUDE.md、兩份 SKILL 的未受管段落，並新增 passthrough 實機回歸 case `pt-quiet-ready`（待擁有者開立 issue）。regression case 對帳密解析終態改以 `last_error` 判定。
- `device release` drain 超過 3 秒或 handle 關閉回條失敗時回新錯誤碼 `DRAIN_STALLED`，並附上 `drain_stalled` 與 `drain_timeout_epoch`。release 仍可能在背景完成；請在有限等待內以 `session get_state` 確認狀態為 `RELEASED` 後再交接。等待期限內仍為 `RECOVERING` 時停止重試並交由操作員檢查；期間勿重開裝置。release 尚未完成時，`session attach`／`session clear` 仍回 `SESSION_RECOVERING`。
- 依 OD-14 登記 SC-34、SC-35，撤回 SC-08，擁有者確認清單 #1～#7 全數裁決；放寬 `tools/contracts/catalog.py` 的 `owner_item` 驗證為 ≥1 的整數。
- `session.attach` 與 `session.recover` 失敗時固定以 `SESSION_NOT_READY` 作為 `error_code`，原有診斷值改由 `last_error`、`last_error_detail` 回報。
- 依 OD-14 #3 撤回 P1 的 SC-08 權限變更：POSIX Unix socket 維持 `0660`，可用 `SERIALWRAP_SOCKET_GROUP` 指定群組；新建執行期目錄沿用 process umask。
- 新增可指向 Python 或 Rust artifact 的 CLI 黑箱契約測試框架，BB-proc 套件改讀受測 artifact 選擇器。
- 新增 Windows GNU interop 執行器、逐案 Cargo L1 報告、匯入 DLL 檢查、黑箱 A/B 執行與 100 次啟動檢查；黑箱基線支援固定 CPython 3.12.3。
- 新增 A/B 比較器（ab_compare.py）、A/B 黑箱執行腳本（ab_blackbox.sh）、Stage 1A gate 判定器（stage1a_gate.py）與驗收鎖檔（cases.lock.json、l1_required.lock.json），並更新 README 相關建置與驗證說明。
- 新增 Rust CI workflow（Linux 原生與 Windows GNU 交叉檢查、GitHub 託管 Windows L1、自架 COM bench 限 label 或手動、排程觸發）；tests.yml 的 Python 釘為 CPython 3.12.3。
- 新增 Agent 權限模型文件（§10.3）、Windows 安裝更新回滾文件與權限阻斷驗證工具（verify_agent_perms.py），並同步 README 雙語段落與 SKILL 錯誤碼受管區段。
- 建立 Stage 1B（P4）管理面的介面骨架與測試替身：`sw-pal` 的 `Platform`／`Capabilities` 超集（`ClientPlatform`、`ClientCaps` 保留別名）、`ServiceCtl`／`ProcScan`／`ProcessTunnel`／`RegistryView` 簽章、`fakes` 組裝點替身與 `TestEnv` 測試支援模組；`sw-cli` 的 8 個委派群組（`daemon`、`service`、`supervision-mode`、`remote`、`connect`、`benches`、`setup`、`doctor`）補上宣告式 parser 與 Rust-only 旗標（`supervision-mode set`、`setup --windows-task／--windows-service／--rollback`）及其 help golden；新增黑箱 stub、委派哨兵、P4 案例綁定映射表與 `check_p4_cases.py`。執行期路由仍由 `DELEGATED_GROUPS` 決定，行為不變。
- 新增 windows-task Spike 2 程序、腳本、證據 schema 與判定器，補齊 Windows Task Scheduler 監管與遠端 Live E2E 驗證程序。
- 清空 Stage 1A 委派集合（20 個頂層群組全部由 Rust 原生承接）並承接終端類操作（console-attach／detach 及 serialwrap-minicom 端到端驗證）。
- Stage 1 出口 gate 的會計與來源修正（S1X）：擁有者機台相依的 cargo 列（`cap_*` 與 `windows_switch_running_exe_rename`）不論是否在凍結鎖內，未執行時一律列為 BLOCKED-OWNER，不再靜默消失；pytest JUnit 必須帶 HEAD、`SW_REQUIRE_CARGO=1`、`git_clean=1`（工作樹含未追蹤檔乾淨，排除 `target/`）、以 pytest 實際生效設定（`effective`：keyword、markexpr、deselect、ignore、maxfail、collectonly、lf、ff，已涵蓋 `addopts`、`PYTEST_ADDOPTS` 與合併短旗標）判定未縮小範圍，以及涵蓋 `tests/` 的 argv provenance（`scripts/rust/junit_provenance.py`）；spike3 證據須 `artifact.git_clean=true`（`spike3_win_wal.py` 產出，schema 新增 `git_head`／`git_clean`）；spike3 Windows WAL 證據（`win-wal-av-*`）依 schema 與 git_head 讀入，EDR 未知不得 PASS。`func-test/runner.py` 的 `compare: none` 以斷言步驟全數 PASS 定義 gate_eligible，平台不適用的 step（`platforms`）記為 not_applicable 且不計 PASS；結果輸出結構化 `not_applicable_steps`、`na_only_gap`，gate 僅將「缺口純為 n/a」的列排除於阻擋之外，PASS 須由其他平台列 PASS＋eligible 提供；INSUFFICIENT／SKIP 對映 evidence-insufficient，不再記為 FAIL。`capture_cli_help.py --out` 不再改寫受版控的 `contracts/cli_help/_meta.json`。計畫 04／05／03 的 L1.5 指令改為 `--mode bridge`（提議待 OD-S1X-03），docker Rust-CLI L1.5 宣告為 BLOCKED-PREREQ。
- 依 OD-17（擁有者裁決，2026-10-22）將 mkbundle 相關的 `#[ignore]` cargo L1 列改由 pytest 替換登記：新增 L1 集合 `s1_pytest_full`（linux，selector 為 Stage 1 pytest 全套 `-q tests/ -p junit_provenance`），收錄 `tests/test_rust_mkbundle.py::test_mkbundle_respects_cargo_target_dir_override` 與 `tests/test_rust_release_manifest_schema.py::test_manifest_schema_accepts_new_optional_fields` 兩個替換案例（兩者內含 `--ignored --exact` 驅動並斷言 `1 passed`），再逐名移除 `s1a_cargo`／linux、`s1b_cargo`／linux、`s1b_cargo`／windows 中對應的 `real_mkbundle_dev_output_verifies` 與 `mkbundle_respects_cargo_target_dir_override` 列（共 4 處、2 個唯一 nodeid）。`tests/acceptance/regen_lock.py --l1-add-only` 全程 add-only，移除以 `--allow-removal` 逐名核准；`.github/workflows/tests.yml` 與 `tests/test_resource_report.py::test_workflow_ci_l1_gates` 的核准清單同步。

  Stage 1 出口 gate（`scripts/rust/stage1a_gate.py`）新增 `s1_pytest_full`／linux 列：報告讀 `<--l1-results>/linux/pytest-full/resource-report.json`，junit 取 `--pytest-junit-report`，每個凍結 nodeid 須在通過 provenance 驗證的 JUnit 中恰出現一次且為 passed（CHG-S1X-OD17-GATE-01，收緊，未新增放行規則）；缺報告、集合不存在、INSUFFICIENT 或 INVALID 一律為證據不足，FAIL 仍為 FAIL，未新增放行規則。`check_p4_cases.py` 未接線，以避免循環依賴。尚未執行 Stage 1 pytest 全套，故該列在產生報告前不得標為 PASS。
- 依 OD-18 收斂 Stage 1 P4 證據閘門：平台不適用的（案例, 平台, 節點）組合不再要求該平台 rust PASS，但僅限 `check_p4_cases.py` 內 15 列核准清單（`APPROVED_NOT_APPLICABLE`），實算不適用列與清單不一致即 FAIL（待擁有者核准）；每個必要案例仍須至少一個平台有 rust PASS，不適用節點出現紀錄仍計為 FAIL。Windows dev bundle 的 `current-release.json` 指標 `dir` 一律以正斜線 `versions/<V>` 表示（launcher 拒收含反斜線之 dir），計畫 04 Task 17 與 `win-blackbox.ps1` 同步修訂；讀取端仍相容舊反斜線指標。
- 登錄擁有者 OD-34～OD-44，新增 DF-20～DF-22，並使 Slice 2.4 計畫的承接率斷路器使用切片開始時凍結的三週觀測窗口。
- S1X G5（sc20-daemon-restart）：`tests/contract_blackbox/test_cli_mgmt_daemon.py::test_daemon_restart_ab` 補上 P0 T14 規定的唯一平台 marker `posix_only` 與資源 marker `process_rpc`（依賴 `/proc` 與 `sh`，Windows 由 cargo `mgmt_daemon::restart_*` 承接 sc20）。該節點因此被 Linux `s1b_20groups` selector（`-m "core_portable or posix_only"`）收集，原先為「兩平台凍結 selector 皆不收集」而一律 FAIL 的狀態解除。

  L1 凍結鎖 `tests/acceptance/l1_required.lock.json` 以 `regen_lock.py --l1-add-only` 於 `s1b_20groups`／linux 新增該 nodeid（新增 1 筆、移除 0 筆）；收集輸入限定為既有鎖定節點加該節點，逐字取自實際 `--collect-only` 輸出。windows 集合未變動。未放寬任何 judge、check 或斷言。

  同步調整 `tests/test_check_p4_cases.py::test_pass_for_node_outside_every_selector_is_rejected`：原以 `test_daemon_restart_ab` 作為「兩平台皆不收集」之例，該前提因本變更不再成立，改用仍無平台 marker 之 `test_daemon_start_ab_conflict`，斷言不變。
- Stage 1B 20groups 委派 sentinel bundle 改為扁平佈局（PENDING-OD-S1X-20GROUPS-LAYOUT 選項 (i)）：`scripts/rust/mkbundle.sh --delegate-sentinel` 的組成與 dev bundle 相同（`bin/serialwrap`、`bin/serialwrapd`、`py/sw_core`、`py/serialwrapd.py`、`SHA256SUMS`、`release-manifest.json`），僅將 `python_cli_delegate.entry` 改為 P4 sentinel（非委派守門：任何漏網委派都寫入 `DELEGATED` marker 並 exit 97）；stage 仍為 `rust-1b-dev`。組裝程序抽為 `assemble_bundle` 由 dev 與 sentinel 共用。`SHA256SUMS` 涵蓋包含 `.mkbundle-owner` 在內的全部檔案（僅排除 `release-manifest.json` 與 `SHA256SUMS` 本身；本段原述之排除已由 `rust-s1x-sentinel-checksum-owner-marker.md` 取代）。

  測試端：`tests/contract_blackbox/test_cli_mgmt_doctor.py::test_doctor_ab_version_pairing_in_bundle` 標 `pidns_rig`，host 上其他 serialwrapd 造成的 pid 計數不穩由隔離 namespace 排除（測試斷言不變）。plan 05 Task 16 Step 4 的 20groups 命令補 `SERIALWRAP_DAEMON_EXE` 並於執行前移走既有 ab-suite 輸出。整段重跑證據尚未取得（見 `owner-decisions-addenda.md` CHG-S1X-OD16-OPEN-01 (e)）。
- Stage 1 出口驅動腳本與計畫命令修正（S1X G2）：新增 `scripts/rust/stage1_exit_driver.sh` 作為 Stage 1A＋1B 一併出口的可重現入口。自測報告改以 `tools/uart_stdio_bridge/APPROVED_REVISION.json` 的核准 tree（c4c22c0e）挑選（`selftest-linux-20261022`、`selftest-windows-20261022`），找不到即非 0 結束，不再退回 tree 9eaed6de 的舊報告；`SW_BUNDLE` 明確 export；完整 pytest 改為唯一一次，並於 1B 證據登記與 `check_p4_cases` 之後執行（`SW_RESOURCE_REPORT` 寫入 `l1/linux/pytest-full/`，供 gate 第 7 列）；register 帶入 Windows CLI 與 junit；Windows L1.5 改接 G3 wrapper `win-func-test.sh`（帶 `--bundle <OutDir>/bundle` 與 `--selftest-report`，不帶 `--case`）、spike3 改接 `win-spike3.sh`（`--evidence-dir <run>`，單次執行四案，證據寫入 `<run>/spike3/<case>.json`），gate 帶 `--l15-report windows=` 與 `--spike3-evidence`；未設 `SW_EDR_*` 時 spike3 為 evidence-insufficient（整體 FAIL），非 BLOCKED-OWNER，驅動啟動時警示；既有 `target/p4-evidence` 與 `p4-bench-*.json` 於開頭整個搬到證據目錄（搬不刪），並以 mtime 檢查各報告產生於本次開跑之後（`cp -p` 保留來源時間）；報告路徑一律取工具輸出的 run_id，不以 `ls -t` 猜測；補跑 plan 05 Step 4 的兩步 bench（隔離 throwaway serialwrapd 與 bb_stubd）；移除重複的 `pytest_final` 與同一 HEAD 的重複 cargo 測試。plan 04 Task 19 Step 5 與 plan 05 Task 16 Step 4 已附 S1X 修訂說明。
- Stage 1 systemd docker 實跑基底 pin 變更（OD-16 (5)，解除 BLOCKED-PREREQ-S1X-04）：`tools/docker/systemd.Dockerfile` 的基底由 `debian:12-slim`（glibc 2.36）改為 `ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac`（glibc 2.39，與 host 建置一致；Python 3.12.3）。digest 取自 `docker buildx imagetools inspect ubuntu:24.04` 的 index digest。apt 來源改為 Docker 官方 `ubuntu noble` 套件庫，`docker-ce-cli` 釘為 `5:29.8.2-1~ubuntu.24.04~noble`；新增 `python3-yaml`（bundle 的 py 執行環境以 `--system-site-packages` 建立，daemon 以 `-I` 啟動，須由系統 site 提供 PyYAML，與 host 的 `python3-yaml` 同源）；image 內預設 UID 1000 的 `ubuntu` 使用者先移除，以容納測試使用者 `swtest`（UID 1000）。原因：`test_update_systemd_exit1_not_relaunched_l15` 於 debian 基底下因 host 建置的 Rust 二進位需 `GLIBC_2.39` 而失敗（`GLIBC_2.39 not found`）。本 pin 同時影響 `scripts/docker/run-l15.sh`（其 build 步驟亦建置 systemd image）與 `run-systemd.sh`；`tools/docker/serialwrap.Dockerfile` 未變更。

  同輪於 `scripts/docker/run_systemd_container.py` 的 `_run_update_exit1` 修正 harness 缺陷（非測試斷言變更；未放寬任何判定）：(1) 初次 `setup --user` 後以 `service restart` 啟動受監管 daemon 再等待 READY，與 `systemd_container.initialize_supervised` 相同（跨模式 setup 只註冊 unit）；(2) bridge 建立的 PTY slave 交給 swtest（`serve()` 既有做法）；(3) `_read_daemon_status` 的 `REPO` 改用 `systemd_container.REPO`（原為未定義名稱）；(4) `_holder_pids` 改為 root 與 daemon 使用者兩輪 `/proc` fd 掃描取聯集，仍無法讀取的現存行程拋出 RuntimeError（fail-closed）。原掃描以 root 執行，容器 root 無 CAP_SYS_PTRACE，讀不到 swtest 行程 fd 表，`OSError` 被吞為「無持有者」，會使 holder 判定恆真。

  實跑證據：`tests/contract_blackbox/test_cli_mgmt_update.py::test_update_systemd_exit1_not_relaunched_l15` 1 passed（report verdict PASS、`evidence_layer: "L1.5-docker"`、producer `run-systemd.sh`、old daemon exit 1、NRestarts 0、holder marks 成對、current release 0.4.1）；`scripts/docker/run-systemd.sh --scenario hold` 亦 PASS。
- 新增 Windows 原生 CPython 3.12.3（embeddable）擷取的 argparse help／usage／錯誤 golden：`contracts/cli_help/windows/` 共 14 筆情境（`--help`、`profile test --help`、10 個錯誤情境，以及 `test_platform_oracle_exists_and_boundary_values_are_native` 強制要求的 2 個平台 oracle 邊界值 `boundary_socket_connect`、`boundary_endpoint_connect`）與 `_meta.json`（`python_version` 3.12.3、擷取時的 git revision）。計畫字面為 12 筆；多出的 2 筆由該測試與凍結 lock 直接要求，須由審查者確認。`scripts/rust/capture_cli_help.py` 補 Windows 擷取可攜性：以擷取用直譯器執行 CLI、子行程輸出以 UTF-8 解碼、golden 一律寫為 LF、固定 env 與黑箱測試 `_env` 一致（`/golden/...`），新增 `--git-revision`，且 Windows 擷取不再改寫共享的 Linux `contracts/cli_help/_meta.json`；Linux golden 位元組不變。

  oracle 變更：新增 Windows cli_help oracle（規格 §0.4），須由非實作者的審查者核可後方可凍結進 `s1a_blackbox.windows`。
- Stage 1A 出口 G3 的 Windows 證據產生器：新增 `scripts/rust/win-func-test.sh`（以 WSL interop 在 Windows 本機 staging 執行 `func-test/runner.py --mode bridge --scenarios --daemon-com COM20 --bridge-com COM21 --transport-mode virtual_com0com`，產出 `--l15-report windows=` 來源）、`scripts/rust/win-spike3.sh`（以同一套 staging 執行 spike3 四案 `win-wal-av-rotate`／`win-wal-av-unlink`／`win-wal-av-reader-handle`／`win-wal-f12-progress`，產出 `--spike3-evidence` 來源），以及共用的 `scripts/rust/win-stage-lib.sh`（HEAD 子集 staging、以 PID＋起始時間監管 Windows 子行程）。EDR 參數只透傳呼叫者提供的值，wrapper 不自填；COM 僅限 COM20:COM21。新增 `tests/test_rust_win_func_test.py` 以靜態與 `--print-argv` 斷言 argv、COM 範圍、EDR 透傳與拒絕路徑。本變更不改 runner、spike3 工具與 gate 的判定規則。
- 收緊正式 bundle 的 `test-fakes` 守門（cargo argv 的 `--features`）、L1.5 等待測試（LF 寫入前不得返回），以及 changelog fragment 的 policy_check 解析。README 的 0.3.x 升級步驟改為 OD-48 已可在 daemon 執行中升級；可觀察變更表更正 Rust CLI 已接受 `--expect-reboot`。
- `uart_stdio_bridge` 核准 tree 更新為 `dde967aa…`（外層第 4 輪兩位非實作者審查皆 `BRIDGE: APPROVE`）；`s1x-bridge-review-3.md` 標明來源無法核對的舊核准原文不採信，新增第 4 次送審審查原文作為 `review_ref`。
- 收緊 L1 凍結鎖的移除核准粒度（行為變更僅限測試閘門工具）：`tests/acceptance/regen_lock.py` 的 `--allow-removal` 改為 `set_id:platform:nodeid` 三元組，裸 nodeid 與非法格式以用法錯誤（exit 2）拒絕，`--check --base` 與 `--l1-add-only` 皆逐三元組比對。CI 之 `--allow-removal` 清單與釘定清單同步改為 545 筆三元組，逐筆對應真實移除事件（OD-16 (1)(2)、OD-17、既有 Linux 占位與舊裁決），不新增任何放行。`tests/test_rust_bench_tools.py` 之三個 pid namespace／`/proc` 測試移至 `tests/test_rust_bench_tools_posix.py`（模組層 `posix_only`，函式名稱不變）以修正平台分類。
- Stage 1 出口 G5 黑箱 L1.5 oracle 修正（test oracle 變更；審查狀態：待兩位非實作者審查，未放寬任何斷言）：

  - `tests/contract_blackbox/test_cli_mgmt_l15.py`：`test_restart_no_reopen_before_handoff`、`test_restart_no_stale_console_replay` 的 holder 交接判準改為 restart 期間背景取樣（`bb_l15.HolderObserver`），斷言取樣中新舊 holder 不得同時存在，且舊 holder 最後一次釋放不晚於新 holder 首次開啟（`bb_l15.assert_holder_handoff`）。Before：操作完成後才以 `wait_holder` 探測並依測試自身的呼叫順序寫入 `closed`／`opened` 標記再比較，因此即使新舊 holder 重疊，`old_close < new_open` 仍必然成立（假綠）。After：取樣自操作開始前即進行，取樣起點須已見舊 holder，重疊即 FAIL；`control.ndjson` 的 `opened`／`closed` 標記改為依取樣邊緣回放，僅供留存、不作判準。
  - `tests/contract_blackbox/bb_l15.py`：`run_nodeid_in_pidns` 的子 pytest exit status 非零一律回 `failed`，診斷保留 exit code 與 stderr／stdout 尾段。Before：`del returncode` 丟棄 exit status，僅憑單筆 JUnit 判定，子行程以 exit 1／3 結束而 JUnit 為 passed 時會被判為 PASS（例如 conftest live guard 以 `session.exitstatus=1` 回報的失敗）。
  - 新增 `tests/test_bb_l15_holder_observer.py`（含刻意重疊的負向測試，oracle 必須拒絕）與 `tests/test_bb_l15_pidns_exit.py`（stub 子行程回歸）。
  - 限制：取樣為 polling（單次掃描約 15–30 ms，觀測期間約佔一個 CPU 核心的大部分），短於一次掃描週期的瞬時重疊無法被觀測；判準為「已取樣到的重疊不得存在」，不宣稱完備。
  - 案例名稱與數量不變；`s1b_20groups` 之 nodeid 未改動。新增 nodeid 之 add-only 登記另由 G8 處理。
- OD-19（2026-10-23）端點防毒驗收改為與防毒產品無關（S1X）：Windows WAL 的 rotate 與 unlink 遇共用違規（ERROR_SHARING_VIOLATION）或存取拒絕（ERROR_ACCESS_DENIED）時改為有上限的退避重試（10／50／200 ms），仍失敗者進入延遲佇列（1 s 間隔、非阻塞，於後續 append 或 `tick()` 重試直到完成），寫入路徑不持鎖睡眠且不因輪替阻塞；`health.status` 的 `wal` 新增 `rotate_retry`、`rotate_deferred`、`deferred_delete_pending` 三個整數觀測欄位（不改 `healthy`）。新增 `scripts/stage0/av_sim.py` 模擬防毒持有（不含 FILE_SHARE_DELETE、seed 可重現、10～500 ms）作為可重現的故障注入；spike3 改以 av_sim 注入與 `binary_integrity`（執行檔前後存在、sha256 不變、啟動未被拒）判定，防毒產品名與狀態改為工具唯讀查詢的資訊欄位，`--edr-*` 自填參數與 `SW_EDR_*` 已移除，`stage1a_gate` 的 spike3 判準隨之改為 binary_integrity。

  接線與收緊（2026-10-23 審查修正）：`SerialwrapService` 的 DeviceWatcher 週期回呼改為先 `WalWriter.tick()` 再 `reconcile_readiness()`，使延後輪替在無新寫入時仍會完成；tick 驅動的現行檔輪替完成時寫入 `WAL_ROTATED_AFTER_DEFERRAL` META（維持現行檔存在與重啟後 seq 續接）。`AdmissionGate` 僅撤銷由交接自身建立的凍結。spike3 gate 收緊為逐 subject 重算 sha256、要求 `grabs ≥ 1`、`launch_error == none`，且 oplock 變體未實作（`not_implemented`）時不得 PASS（`oplock_not_covered`）。producer 現記錄每次 `WalWriter.append` 毫秒耗時；缺少有效樣本不得 PASS，上限未經 owner 裁決時維持 BLOCKED-OWNER。WAL 輪替持續 deferred 時每 60 秒最多再告警一次。
- `file.push`／`file.pull` 傳輸中遇 drain、bridge 世代過期或取消時，改依擁有者裁決 OD-62（比照 BC-03）回應：本次傳輸尚未寫出任何 byte 回 `FLUSHED_BY_RECOVERY`（可在 READY 後重送），已寫出 ≥1 byte 回 `COMMAND_ABORTED_IN_FLIGHT`（不得自動重送），push 附 `chunks_sent`／`chunks_total`；0.3.2 在這些情境回 `TRANSFER_TIMEOUT`。「已寫出」以整段傳輸共用的操作 permit 累計。同步 `contracts/rpc_schema.json` 兩方法 `errors`／`error_order`、`contracts/error_codes.yaml` 兩碼 scope、BC-03／SC-04 `compatibility_impact` 與 SKILL 受管區段（S1X-a-4 第 2 輪）。
- 契約補記 OD-63 佔位 session 的兩個既有行為（編排者裁決 S1X-a 第 3 輪 SPEC-GAP 1、2），程式不變：`contracts/rpc_schema.json` 的 `session.bind` result 比照 `session.attach` 的 RELEASED 分支補 `released`、`recommended_action` 屬性與 `x-order`，side_effects 的一般路徑一筆加 x-condition 排除佔位，另補一筆佔位早退項（佔位不離開 RELEASED、無其他副作用），`x-catalog-revision` 依 P0 規則遞增（rpc_schema 14→15；file.* META 說明同輪修改的 wal_meta_events 4→5）；DF-04 的 compatibility_impact、`openspec/specs/device-handoff/spec.md` 的 OD-63 情境與 README（中英）補述佔位 `session bind` 的回應，以及佔位開埠失敗且無 passthrough fallback template 時不留列（`device attach` 回 `SESSION_NOT_FOUND`）、`_pending_com` 保留原 COM 至裝置拔除或 daemon 重啟，之後依 dynamic 規則重新分配 COM。
- 補上游 agent skill（`sw_core/assets/skill/SKILL.md`）三處過時或缺漏，並同步 README／`docs/serialwrap-spec.md` 中同樣不準的句子；不改程式行為，每個數字與行為皆對照 `session_manager.py`、`config.py`、`setup_cmd.py` 核實。

  - `cmd submit` 的期限語意：總期限是 `--cmd-timeout`（命令一直有輸出也一樣會到點進恢復）、靜默上限是 `min(--cmd-timeout, 30s)`；`--expected-duration` 只延長總期限、不延長靜默上限，舊文「暫停 prompt timeout」不成立，已更正。逾時後非 human 來源依序送 Ctrl-C、Ctrl-D（各在 `min(--cmd-timeout, 2s)` 內比對 prompt），Ctrl-D 打在已登入的 shell 等於登出，開機靜默窗內不送。建議長時間或會靜默的命令在板上以 `setsid` 丟背景再輪詢；`--mode background` 與 `line` 共用同一段等待邏輯，不是長命令出口。
  - boot quiet window 補 `boot_done_regex`：命中 `login_regex`、`prompt_regex`、`boot_done_regex` 任一即解除（尾行為 `bootloader_prompts` 例外），三者都不中才等滿 180s；prplOS askconsole 要靠此欄位。`serialwrap setup` 不覆寫既有 profile，舊安裝需手動補欄位並重啟 service。
  - `--completion-regex`：指向 `docs/command-completion.md`，補要點（行首錨定、數字 RC 加獨立 END 行、每次換 nonce、上限 1024 bytes、不匹配空串）；它只換掉「等什麼」，總期限、靜默上限、逾時恢復語意不變，reboot 仍走既有 recovery。
  - README 長命令範例原把全域 `--timeout 300`（RPC 等待秒數）寫在 `cmd submit` 之後，argparse 回 `unrecognized arguments` 直接拒絕，改為 `--cmd-timeout 30 --expected-duration 300`；`docs/serialwrap-spec.md` §6.5／§14 同步更正。

### Fixed
- 修正 `brcm-template` 漏配帶前導 space／tab 的 BDK `>`／`#` 提示符，避免正確帳密下仍發生自動登入與 READY 確認逾時；保留行首錨定及 banner 裝飾線排除，並新增直接載入出貨 YAML 的 regex、登入流程與自動偵測回歸測試，以及 TestPilot 實機重開機自動登入 case。
- 修正 #224 TestPilot 實機 case 的 WAL 分頁判定，並讓 Windows TestPilot 可在既有 serialwrap broker 上執行單一 BRCM 重開機登入回歸 case；保留版本、doctor、WAL、READY 與 benchlock 前置檢查，其餘 POSIX 專用 case 在 Windows 明確略過。
- 修正 console unlock 空行回應晚於一秒、於第二次 RX 靜默等待到達時仍送出解鎖值的問題；送 secret 前重新檢查此次空行後的新 RX，包含 prompt 後緊接非同步 log，已解鎖時只驗 nonce。加入真 PTY 延遲回覆回歸測試（#229）。
- bundle CLI 被複製離開 `release-manifest.json` 時，仍以編譯期印入的 Python daemon 程式碼指紋回報 `--version` 與 setup `daemon_code`，與 Python 基線一致。開發建置維持占位、不謊報指紋；manifest 缺欄位或損壞時不改用編譯期值。
- `doctor` 的 `version_pairing` 在 release manifest 的 `components.daemon.code_fingerprint` 不是 12 位小寫十六進位時，回報該欄位自己的 key，不再落成 `unreadable`。缺欄的舊 manifest 仍略過。
- Rust CLI 的一般 RPC、event 與 daemon status 警告路徑接入共用 release manifest 程式碼指紋來源，在 daemon 程式碼指紋不符時輸出警告並保留既有錯誤行與 exit code。
- 補齊 Rust CLI 正式 setup 入口的指紋重啟跳過回歸，鎖住 Python 基線的 action、原文 hint 與判定優先序，並驗證 flash 未帶 force 時在物化前拒絕，以及 --no-restart 仍保留模式轉換的 stop／start、不得追加 restart 或指紋輪詢。既有產品接線已符合這些行為，本次僅新增測試。
- 升級驗證改依 Python 基線指認 `--version`：純版本或附 12 位程式碼指紋都算該版本，版本號仍須整段相等，入口切換與 pipx 備份條件不變。
- 修正非同步 reboot（prplOS／OpenWrt 的 `reboot` 在 shell 重印 prompt 後才慢慢關機）後，關機中的舊 shell 回應 READY 確認 probe 的 nonce，讓 READY 在板子重開前就被確認（stale-READY）、#162 防線被繞過的問題。過去 `_handle_reboot_command` 在 2 秒內 prompt 回來時只清 TX 靜默、保留 `ready_reconfirm_pending`，但 quiet 一清，readiness reprobe 在 RX 閒置 3 秒後就送 `\n`＋`echo __READY__<nonce>`，舊 shell 照常應答，`confirm_ready()` 清掉 pending，agent 命令因此通過 gate、落進關機中的系統（讀到舊 boot_id 或被重開打斷）；IB8 實機 8 次單獨 `reboot` 的 WAL 都在 +3.1～+6.3s 發生，而 U-Boot banner 要到 +16.9～+25.9s 才出現。現在判定為非同步 reboot 後，確認 probe 要等 RX 出現 boot banner（沿用既有 `detect_boot_banner`，不寫死平台字串）才送；寬限 `BOOT_EVIDENCE_GRACE_S`（60s，約最慢觀測值的 2.3 倍）內都沒看到 banner 則視為板子其實沒重開，照常確認，不卡死。等待期間 agent 命令維持可重試的 `AUTOBOOT_QUIET`，`session list` 新增 `boot_evidence_wait_remaining_s`；`READY_RECONFIRM_MAX_S` 的有界上限、`READY_UNCONFIRMED` 終態、自發重開機與 human 來源的 reboot 路徑不變。
- 將 Windows Task Scheduler throwaway 長跑 allowlist 限定為專用 COM24，並保留 COM20:COM21 與 COM22:COM23 供其他原生 gates 使用。
- 修正 Docker 升級驗證 driver（`scripts/docker/run_upgrade_container.py`）在 on-demand 模式的預熱競態：0.3.2 的 `daemon stop` 先回應、行程稍後才結束，driver 立即 `daemon start` 會得到 `DAEMON_ALREADY_RUNNING_ELSEWHERE`，之後沒有 daemon，使 `seed_user_data` FAIL、`legacy_handoff_direct_setup` INSUFFICIENT。現在每個 `daemon stop` 之後（基線預熱、停舊後 setup、install.sh 前、手動降級、重新升級），先有界等待舊 daemon 行程樹結束、socket 移除，才執行下一步；逾時該步驟判 FAIL，等待結果記錄於 `detail.stop_wait`。判定與必要檢查不變。
- Docker 升級驗證（`scripts/docker/run-upgrade.sh`）自建的 0.4.0 bundle 改比照 release workflow 的 `publish-linux-bundle`：先確認主機 `/usr/bin/python3.12` 可執行、實體位於 `/usr/bin` 且版本符合 `contracts/cli_help/_meta.json`，以它建立 build venv 後執行 `mkbundle.sh --release-bundle`；自建或 `--bundle` 提供的 bundle 皆須 `py/pyvenv.cfg` 為 `home = /usr/bin` 且 `include-system-site-packages = false`。任一不符以 rc=3 拒絕，不回退其他 python（release 前置第 2 輪 OWNER-BATCH #2）。合入 OD-48 後新出現的 `--scenario install-sh --mode systemd-system` 組合改為 rc=2 明確拒絕。
- OD-48 第 2 輪修正：首次以 bundle 升級 pipx 安裝（沒有 `current-release.json`）時，若在寫完舊 daemon 擷取檔、尚未送出 stop 前中斷，下一次 `setup` 的 journal 復原改以行程辨識確認 endpoint 上仍是 0.3.x 舊 daemon，不再因「沒有 current 卻有 daemon」回 `restore_verify_failed` 並卡住 journal（S1-4baca8c-1）。跨監管模式升級（舊 endpoint 與目標 endpoint 不同）在啟動新版 daemon 後中斷時，journal 會記錄目標模式與 endpoint，復原先停止目標 endpoint 上的新版 daemon並確認釋放後才還原，避免新版 daemon 繼續持有 UART（S1-4baca8c-2）。Linux 改以 `/proc/<pid>/cgroup` 判定舊 daemon 是否由外部監管者管理：位於 serialwrap 以外的 systemd service unit 或其他監管者 scope 時，在送出任何 stop 之前以 `DEVICE_STILL_HELD`（`external_supervisor`）拒絕，父行程清單保留為輔助判定。舊 daemon 停止後還原本身失敗時，報告以 `unrestored` 逐項列出未還原的項目（`entries`／`config`／`unit`／`enabled`／`current`）。
- release workflow 的 `publish-linux-bundle` job 改為 `runs-on: ubuntu-24.04`，以系統 `/usr/bin/python3.12` 建立 bundle 內附的 Python runtime（`pyvenv.cfg` 為 `home = /usr/bin`，對齊 OD-50 的使用者主機），不再使用 `actions/setup-python` 的 hosted toolcache Python——後者建出的 runtime 依賴 `/opt/hostedtoolcache` 的 libpython 與標準函式庫，搬到使用者主機後 daemon 無法啟動（S1-3185c6d-1）。smoke 新增 `pyvenv.cfg` 可攜性檢查，並在乾淨 `ubuntu:24.04` 容器（`--network none`、只唯讀掛載解開的 bundle）內以 bundle 的 `bin/serialwrap daemon start`／`status`／`stop` 啟停 on-demand daemon，失敗即阻擋上傳。
- 出口 driver 只有在完整 Windows L1.5 原始報告、執行 metadata 與 gate 列逐筆一致，且所有非通過結果皆為核准的 `BLOCKED-OWNER` 延後項時，才略過 Windows producer 的非零結束碼。
- 長跑收尾輪詢背景命令時，將單次 RPC timeout 限制在設定的總期限內，避免浮點捨入使逾時略超上限。
- `tests/test_human_agent_coexist.py` 的 FakeTarget 對空行重印 prompt（比照真實 shell 與 `test_multiagent_e2e.py`），消除 prompt probe 依賴開機殘留 prompt、被 `clear_rx_buffer()` 清掉後 20s 內到不了 READY 的競態（Rust 重構 P0 gate）。
- `python3 -m tools.contracts` 入口固定 stdout／stderr 為 UTF-8＋LF，原生 Windows（locale 編碼如 cp950、CRLF 換行）下讀取端不再解碼失敗；C13／C14 子行程的 `PYTHONPATH` 改以 `os.pathsep` 串接（Rust 重構 P0 gate）。
- 原生 Windows 執行 `-m core_portable` 時，四個頂層 `import pty` 的 posix_only 測試模組（agent defer TX、flash pump、human／agent 共存、multi-agent E2E）改以既有的模組層 POSIX 守衛略過，不再因缺 termios 中斷整次收集；契約測試的 `SessionPublic` 鍵順序依平台判定 `console_endpoint` 條件欄，真實 repo 契約檢查測試明示以 UTF-8 解碼 CLI 輸出。
- WAL 與 mirror 輪替及重設前先同步並關閉常駐 handle；歸檔名稱碰撞時加上數字後綴，避免覆蓋既有資料。檔案系統不支援原子 no-replace 改名時，採相容降級方式維持輪替與重設。
- Windows WAL 與 mirror 改用含 share-delete 權限的常駐檔案 handle，讓外部工具能在寫入期間改名或刪除檔案；新增 DeletePending、FileId 與 LockFileEx 包裝。
- human 延遲輸入超過上限或遇重開機、recovery、release、flash 時整段丟棄，不再截頭留尾，也不會在上下文失效後回放舊按鍵。
- TX 區分未提交、已確認 N bytes、結果不明；TX WAL 只記確認寫出的 bytes 並依實際寫入順序。寫入等待可被取消，RX 觀察者取得該 chunk 的精確 WAL seq。
- 修正 RECOVERING 與 FLASHING 狀態下送命令及開啟 interactive 時的錯誤碼；已排隊命令與檔案傳輸遇到 autoboot quiet 時附上 `retry_after_s`；已取消但 worker 仍執行中的命令計入 pending 上限；RPC 偽造的 `human:` 來源不再繞過 gate 或升格為 interactive。
- Agent interactive lease 到期改由背景 tick 主動收回並保留 tombstone；flash 認線排除 released、recovering、drain 中與前景忙碌的 session；human console 單行超過 16385 bytes 或被拒時記錄 WARNING。
- UART bridge 停止改為兩段式 drain：先凍結 TX 與取消等待，確認在途寫入收斂並關閉 handle 後才產生回條。
- 每次 UART 寫入都在實際送出前重新驗證命令仍有效，避免送出已取消、quiet、release、lease 換手或 human 輸入上下文失效的命令與按鍵。自動登入、密碼與 ready probe 在提交前偵測到開機 banner 或登入流程已被取代時不再送出；`session.self-test` 的 nonce probe 在 quiet 期間改回 `AUTOBOOT_QUIET`（`recommended_action: wait`），不送 UART、不等待 nonce。
- 修正 OD-15 長跑 Docker Compose 未將 `dialout` 補充群組授予主機 UID 執行者，解除 realhw `serialwrap doctor` preflight 阻擋；補裝 realhw metadata 所需的 `git`，保存隔離環境下完整 doctor JSON 與 stderr，並讓 observer 以固定期限排程取樣，避免 RPC 耗時造成長跑樣本數不足。
- 修正 Windows 原生測試的 CLI 身分量測、家目錄隔離 guard 與 POSIX-only 分類理由，並保留 L1 鎖移除的精確審核清單。
- 補齊 Stage 1B 更新交接的 P2 bridge 黑箱案例、開發 bundle 專用同步測試鉤子，以及 systemd exit-1 隔離驗證。
- `service` 收到不可 JSON 序列化的 `supervision_mode` 時，改輸出與 Python CLI 相同的 `INTERNAL_ERROR` JSON 與 stderr 鏡像。
- Windows Cargo runner 新增 `--l1-set s1b_cargo`，Stage 1B 測試會依對應的凍結集合產生 L1 判定；未指定時維持 Stage 1A 預設。
- 移除 `sw-cli` 對自身的 dev-dependency。測試鉤子改由 `cargo test --features test-fakes` 啟用，不再以自我依賴打開 `test-fakes`，依賴方向回到 arch §1.3 允許表。正式 release binary 仍不包含 `SERIALWRAP_TEST_`／`SW_TEST_UPDATE_` 字串。
- Stage 1B 凍結 selector（`cargo test --workspace` 與 Windows `--package sw-pal --package sw-cli`，皆不含 `--features`）會再編譯兩支委派整合測試。行程內 override 不進入環境變數鉤子；`SERIALWRAP_TEST_DELEGATE_GROUP` 與 `SW_TEST_UPDATE_` 仍只在 `test-fakes` 建置讀取。不恢復 `sw-cli` 自我依賴，正式 release binary 不含那些字串。
- Stage 1B A 輪若所有必要節點都是核准的 `rust_only` skip，且 JUnit 與 PID 日誌證明未啟動受測行程，改記實際 runner 環境摘要；其他設定紀錄缺失仍 fail-closed。
- Stage 1B A/B 執行器的 Python 輪 skip 登記改為只核准 `rust_only` 節點，同一登記案例內的共用節點須實際執行並比較，不再因案例登記而整案拋出錯誤、導致 `summary.json` 未產生。`mkbundle.sh` 拒絕覆寫非空且非 mkbundle 建立的輸出目錄（exit 2，不寫入），mkbundle 建立的目錄則整個重建，避免 sentinel 與 dev 佈局混用、bundle 定位取到過期的頂層 manifest。

  遷移（一次性，擁有者確認後執行）：舊版留在 `target/mkbundle-dev` 的無標記目錄會使 `mkbundle.sh --dev --out target/mkbundle-dev` 以 exit 2 拒絕，指令為 `rm -rf target/mkbundle-dev`。`ab_blackbox.sh` 已改為 `target/ab-runs/<run>/bundle`，不再讀取該目錄；pytest 的 `SW_BUNDLE` 預設改為 `target/mkbundle-pytest`（`test_fresh_install_blackbox` 與 `test_cli_mgmt_doctor`），該目錄不存在時測試明確失敗並提示建置指令，不再以舊目錄的過期 bundle 驗證。
- 依 COM20:COM21 原生實測（`g3-peer-gate/261023-061416-2646662`），win-serial 端點於 com0com 且 `peer_gate=True` 時宣告 `peer_state`，對外 `status.peer_open` 為 DSR 即時取樣；非 gate 情境仍為 null。`transport_disconnect` 恆為 False（com0com 無不需管理員權限的斷線原語）。測試名稱同步改為描述新行為（`test_win_serial_status_reports_peer_open_when_peer_gated`、`test_peer_gate_declares_peer_state_capability`），並移除「函式名稱保留以維持 nodeid」之註解（該名稱未見於任何凍結鎖或腳本）。工具 tree 已變動，核准狀態見 `CHG-S1X-BRIDGE-PEER-STATE-01`（未核准）。
- 修正 holder 探測以 readlink 字串或 `st_rdev` 判定同一 tty 的缺陷：docker 容器內獨立 devpts instance 的 `/dev/pts/N` 與主機同名、同 rdev（136:N），卻非同一裝置，會被誤判為外部 holder（可能使 host namespace 的 daemon attach 回 `DeviceHeldError`，並使黑箱 helper 的等待條件假成功）。

  現行判準（`sw_core/serial_port.py::same_tty`）：`(st_dev, st_ino)` 相同即同一 tty；另允許非 devpts 字元裝置的 mknod 別名（兩邊皆 char device、rdev 相等且非零、major 不在 136–143），以涵蓋容器以 mknod 掛入同一實體 UART 節點之情境。devpts 不以 rdev 判定。`probe_holders`、`UARTBridge._enumerate_all_held_paths`（先以字串篩除非 `/dev/` link，降低 reaper 的 syscall 成本）與 `detect_multi_open` 皆採此判準；Rust `crates/sw-pal/src/proc_scan/posix.rs::serialwrapd_tty_holders` 同步。目標 tty 無法取得身分時回 `unknown`，不再當成確認無 holder。保留既有 permission／unknown 狀態語意。

  黑箱測試 helper `_external_holders` 同步採同一判準。既有以假路徑佈置 holder 的單元測試改用實體檔案，rdev-only 正面案例改為非 devpts mknod 別名正面，並新增 inode 別名與跨 devpts 負面案例。新增 `tests/test_holder_probe_namespace.py` 作為 namespace 碰撞回歸（#155：pytest 可覆蓋，免 `regression/` case）。行為變更之擁有者登記見 `CHG-S1X-HOLDER-IDENTITY-01`（OPEN）。
- Stage 1 黑箱 rig 改於獨立 user+pid+mount namespace 內執行：自啟 daemon 並呼叫 CLI `daemon start／restart`、`setup` 更新／回滾、`supervision-mode set` 的 Linux rig（`pidns_rig` 標記）整段重跑於隔離 namespace，不再因宿主生產 `serialwrapd` 或容器 daemon 被多開偵測而誤判 `DEVICE_STILL_HELD`／`DAEMON_ALREADY_RUNNING_ELSEWHERE`；namespace 不可用時 skip 並註明能力原因，不以 PASS 放行。`ab_blackbox.sh` 整段（A 輪、釋放確認、B 輪）於同一 namespace 內執行，並以 preflight（exit 4）判定非隔離或可見外部 `serialwrapd` 的 run 為 INVALID，身分檔記錄 `pid_namespace_isolated`（僅記錄，守門為 preflight）。`ab_blackbox.sh` 改於 `target/ab-runs/<run>/bundle` 建置本次 bundle，不再共用 `target/mkbundle-dev`。

  修正 Rust CLI 回滾時 `start_and_verify` 誤啟動新版 daemon 的缺陷：POSIX 穩定入口為 symlink，PAL `is_file` 不跟隨連結，改以 `CLI 自身 bundle` 的 launcher 啟動，回滾必回報 `pairing_mismatch`；改為跟隨連結判定穩定入口。交接停止（`daemon.handoff_stop`）成功後，Python daemon 的 RPC 監聽保留有界排空窗口（`HANDOFF_DRAIN_GRACE_S`，3 秒）再停止，窗口內有副作用請求取得 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`，而非 socket 錯誤（此為 SC-11 字面「回覆後立即停止」的偏離，待擁有者或規格協調確認）。交接停止成功後的 `handoff_stop` 記入 setup／supervision 回報的 `ran`（與 `daemon stop` 對稱）。黑箱 chunk 延遲改以實際送出的 chunk 指令前綴觸發；busy push 測試以 ACK-gated 傳輸（`ack_mode=auto`）、16 KiB 檔案、20 秒首個 ACK 延遲，並以 `--chunk-timeout`（`BUSY_CHUNK_TIMEOUT_S`＝60 秒）避免 CLI 在 ACK 前逾時送出清理指令而破壞零 TX 斷言；背景執行緒例外於 `join()` 重新拋出；supervision 切換測試等待 holder 行程以 `serialwrapd` 身分出現於 `/proc`，消除競態。
- 修正 Stage 1 出口 gate 與 P4 完成度檢查的 junit 計數：pytest 9 的 subtests 會計入 testsuite 的 `tests` 屬性，但不產生對應的 testcase 元素，致使 `s1_pytest_full` 的 junit 與 l1-results 列數永遠不符（5024 對 4932，差值 92）而判為證據不足。`scripts/rust/stage1a_gate.py`（黑箱分支與 `_junit_totals`）與 `scripts/rust/check_p4_cases.py`（`_junit_counts`）改以 testcase 元素逐一計數，計數實作依路徑載入 P2 `tests/acceptance/check_evidence_index.py::_junit_counts`，不另寫一份；P2 模組缺席或載入失敗時 fail-closed（閘門記入 errors 而整體 FAIL，P4 檢查列為 BLOCKED-PREREQ）。l1_judge 的逐項嚴格相等比對不變，pytest JUnit 含失敗或錯誤的整份失敗判定照舊；真正不一致（testcase 數與報告不等）仍判為證據不足。

  補充（S1X 出口收尾）：`register_p4_evidence.py` 之 junit 計數改委派 `check_p4_cases`（再委派 P2 `check_evidence_index._junit_counts`），登記與檢查路徑採同一 testcase 層計數。
- Windows L1.5 exit driver 依案例 YAML 驗證重複執行次數、run index 與證據路徑，接受完整合法重複報告並繼續拒絕缺次、重複索引及未授權失敗。
- 依計畫 05 L3733 與附錄 B-17 修正 Stage 1 P4 完成度檢查對 `p1-rst-daemon` 的擁有者分類。該案例的 windows-task 面證據須以擁有者 throwaway Windows 工作實跑（`spike2 -Phase restart`）產生，該實跑不得於 73 h 長跑進行中執行，且 stage1 driver 不執行 judge_spike2；先前 `check_p4_cases.py --check` 僅對 L2／L3／manual 且標 `evidence_status: BLOCKED-OWNER` 的案例歸類擁有者缺口，`p1-rst-daemon`（L1.5、無該標記）因而只能判為 FAIL，exit 2 不可達。現改為：`JUDGE_SPIKE2_CASES` 之案例證據檔缺席時判為 BLOCKED-OWNER（exit 2，不得 PASS）；證據檔存在時照一般規則判定，FAIL 紀錄與無效紀錄（含 INSUFFICIENT_EVIDENCE）仍為 FAIL。`JUDGE_SPIKE2_CASES` 改為 `check_p4_cases.py` 的單一共用常數，`register_p4_evidence.py` 直接引用，兩支腳本的 owner 判定不再分歧。本次未修改 `p4_case_map.yaml`、`p4_required.lock.json`、案例 layer 或 evidence_status，亦不屬 lock amendment。
- 修正 Stage 1 P4 證據管線與 Windows 黑箱腳本的缺陷（G4-p4-evidence-pipeline-and-windows-bundle-pointer），各項皆先以紅燈測試界定再修正：

  - (a) `win-blackbox.ps1` 的 `bin\current-release.json` 之 `dir` 回復為 plan 04 Task 17 的 `versions\<V>`（相對安裝根）。`realhw/deployed_content.install_dir` 的 Windows 分支與 `register_p4_evidence` 的 manifest 候選改以 `cli.parent.parent` 解析，反斜線先正規化；絕對路徑與 POSIX 行為不變。Rust launcher 對反斜線 `dir` 的拒絕規則與 plan 04 Task 17 的衝突另列 OPEN（PENDING-OD-S1X-WIN-DIR-SEP），未在本次裁定。
  - (b) `pidns_rig` 子行程寫入的 pid 為 namespace 內部編號。`bb_daemon` 的 pidlog 列附 `pid_ns`，`bb_l15` 另記錄外層 unshare 的 host pid。`ab_blackbox_1b._release_check` 對不同 namespace 的列改以「host 上無行程屬於該 namespace」判定，仍有成員即 unreaped 並列出其 host pid；與 host 同 namespace 的列照原邏輯檢查，嚴格度不降低。**`bb_daemon.py` 與 `bb_l15.py` 為黑箱 oracle 基礎設施，待非實作者審查。**
  - (c) `register_p4_evidence` 的 cargo 執行檔改由凍結 `s1b_cargo` selector 的 argv 執行 `cargo test --no-run --message-format=json`（Windows 加 `--target x86_64-pc-windows-gnu`）取得，依 package＋target 唯一對應；對應不到或不唯一仍 FAIL。取代先前以 glob 搜尋 `target/debug/deps` 的做法。
  - (d)(e) 平台適用性共用規則（`check_p4_cases.platform_applicability`，`register`、`check`、`ab_blackbox_1b` 皆採用）：cargo 節點以是否在該平台 `s1b_cargo` 凍結鎖中判定；contract_blackbox pytest 節點以該平台 `s1b_20groups` 凍結 selector 的 markexpr 收集結果判定。不適用的（案例, 平台, 節點）組合不產生紀錄、不在該平台執行，並逐列列於 `summary.json` 與標準輸出的 `not_applicable`。兩平台皆不適用者一律 FAIL。**此規則縮小了必要證據的分母，須擁有者核准：見 CHG-S1X-P4-APPL-01（OPEN，共 14 組）。** Windows 巢狀 ab-suite 因此不再執行 `test_daemon_restart_ab`（依賴 /proc 與 sh，不在 Windows selector 中）。
  - (d') Linux A/B 選取回復 HEAD 之負向標記過濾，`test_daemon_restart_ab` 仍在 Linux A/B 內（A/B 覆蓋不縮減，CHG-S1X-P4-LINUX-AB-01）。
  - (f) `win-blackbox.ps1` 於 staging 刪除前把 `l1\windows\blackbox\ab-suite\` 複製回 OutDir；Windows dev bundle 匯出到 `<OutDir>\bundle\`（`bin\serialwrap.exe`、`bin\current-release.json`、`versions\<V>\release-manifest.json`，以及 manifest 的 `cli.path` 所指的 `versions\<V>\bin\serialwrap.exe`）。匯出失敗只記錄警告、不覆蓋原錯誤，且 scratch 一定刪除（巢狀 `finally`）；逐檔檢查來源存在，缺者略過並警告。`register_p4_evidence` 的 Rust CLI 以 `<OutDir>\bundle\bin\serialwrap.exe` 登錄時，Windows 穩定入口是 manifest 所指檔案的 byte 複本，故新增 `_windows_stable_entry_of`：僅限入口位於 `bin\`、同目錄指標解析到該 manifest 版本目錄，且內容仍須通過既有 sha256 比對；位於他處的同 byte 複本仍拒絕。

  未修改 `p4_case_map.yaml`、`p4_required.lock.json` 與 `l1_required.lock.json`；未刪除案例、未放寬斷言或 judge 的判定規則。(d)(e) 之分母縮減與 `_windows_stable_entry_of` 的路徑放寬，皆待擁有者核准（見 CHG-S1X-P4-APPL-01 與 addenda）。

  未完成項（OPEN，不得記為 PASS）：`test_daemon_restart_ab` 不在任一平台的凍結 selector 內，sc20-daemon-restart 兩平台皆 FAIL（PENDING-OD-S1X-P4-SC20）；bc05-three-topologies 在 Windows 無任何適用節點（PENDING-OD-S1X-P4-BC05-WIN）。兩者使 `check_p4_cases.py --check` 與 `test_p4_required_cases_have_passing_evidence` 恆為 FAIL，此為事先存在之缺口（sc20 於 HEAD 即無 Linux 紀錄來源），須由擁有者或計畫層級裁決。原生 Windows 黑箱實跑（2026-10-22）：巢狀 ab-suite 因 Windows selector 僅剩 rust-only 的 `test_autostart_l15_tx_once`，A 輪無受測行程而未留下 PID 日誌，B 輪未啟動；Windows COM 自測 ST-04 為 FAIL（bridge 工具，非本組）。`ab_blackbox_1b._bundle_identity` 於 Windows staging（無 `.git`）執行 `git rev-parse HEAD` 未捕捉 `CalledProcessError`，屬潛在缺陷，本次未修。
- `process_ctl` 測試中以重新執行自身方式驗證 SIGCHLD 忽略（ECHILD）的 helper 改以 `output()` 擷取子行程 stdout／stderr，避免子 libtest 的 `running 1 test`、`.` 與 `test result:` 列繼承到父行程，與並行的 `test … ok` 結果列交錯，造成 L1 結果列解析缺漏；子行程失敗時輸出放入斷言訊息，斷言條件與 helper 行為不變。

  同類寫法 `crates/sw-pal/tests/exec_delegate.rs` 的 `exec_or_wait_propagates_exit_code_and_env` 改以 `output()` 擷取子行程輸出（原 `status()` 繼承 stdout），斷言條件不變；`windows_process_ctl_preserves_unsigned_exit_code`（cfg(windows)）之 spawn 路徑仍繼承 stdio，屬 production 行為，未在本次修改。
- 修正前置空行後 readline 折行命令回顯未剝除的問題。只接受命令原文加上符合間距的折行標記，保留其他 stdout。
- 修正 Stage 1B sentinel 扁平 bundle（`scripts/rust/mkbundle.sh --dev --delegate-sentinel`）的 `SHA256SUMS` 缺列問題。先前組裝腳本刻意排除根目錄的組裝期所有權標記 `.mkbundle-owner`，但 Rust 驗證器（`verify_bundle`）要求 bundle 內除 `release-manifest.json` 與 `SHA256SUMS` 外每個檔案都有 checksum，因此 `serialwrap setup --on-demand` 回 `CHECKSUM_VERIFY_FAILED`（`sha256:.mkbundle-owner missing`）。現改為將 `.mkbundle-owner` 納入 `SHA256SUMS`，僅 `release-manifest.json` 與 `SHA256SUMS` 本身豁免；驗證器未放寬。所有權拒絕覆寫（`refuse_foreign_out`）保留不變。本條取代 `rust-s1x-sentinel-flat-bundle.md` 中「`SHA256SUMS` 排除 `.mkbundle-owner`」的敘述。

  測試端：`tests/test_rust_mkbundle.py` 新增 `test_sha256sums_lists_every_file_dev`、`test_sha256sums_lists_every_file_sentinel`（除 `release-manifest.json` 與 `SHA256SUMS` 外每個檔案都列於 `SHA256SUMS` 且數值相符）與 `test_setup_on_demand_from_sentinel_bundle_isolated_home`（隔離 HOME 執行 `setup --on-demand` 須 rc=0）。最後一項於非特權 pid namespace 內執行，與 `pidns_rig` 同一隔離手段，避免宿主上無 `--socket` 的生產 daemon 進入 setup 的 safety check。
- Spike 1 控制線隔離量測修正（S0-SPIKE1-REMAINING）：量測前改為把所有已開埠埠位的 RTS／DTR 全部拉低。pyserial 開埠預設 RTS／DTR 為 True，原先只拉低 COM20／COM22，未拉低的 COM23 會把 COM22 的 CTS／DSR／CD 釘在 true，使隔離比較無法偵測 console 端的洩漏。隔離判定改為基線與各 DUT 階段，兩個 console 埠的輸入線皆須全低，不再以「與基線相同」比對。新增 B→A（COM23→COM22）反向正向對照，確認兩個 console 埠的輸入線皆可觀測，缺此方向時不得判為隔離通過。報告新增 `outputs_cleared_before_baseline` 欄位與 `console_pair_positive_control` 的 `reverse_rts_only`／`reverse_dtr_only`。

  證據登錄：Windows 原生重跑（DUT-side COM20:COM21、Console-side COM22:COM23）三項皆 PASS 後，將報告存入 `evidence/stage0/owner/p2-t18/spike1-remaining.json` 並登錄 `S0-SPIKE1-REMAINING` 為 present（sha256 已填）。證據索引相關測試的暫存根目錄改為一併複製既有 present 項目的 artifact，只調整夾具，不改動任何斷言。
- Spike 3 將 go/ack 綁定至實際 lease acquisition 與期限，拒絕過期 acknowledgement；producer 與 Stage 1A gate 同時檢查主要 WAL workload 和 oplock 變體的 append 延遲樣本，依 OD-22 驗證 p99 不高於 100 ms、最大值不高於 600 ms。
- Spike 3 現在記錄主案例 lease／刺激時點，以及 oplock 變體的實際 rename request、完成時間、append 樣本時間窗、RH break 通知與 close-handle 隱含 ACK。Stage 1A gate 以同一 lease request id 關聯這些事件，允許 ACK 後完成輪替，但要求 request 位於有效 lease、完成時間有界且與 append 樣本一致；主案例與 oplock append 樣本各自依 OD-22 檢查 p99 ≤100 ms、最大值 ≤600 ms。缺證據仍為 INSUFFICIENT_EVIDENCE，超限仍為 FAIL。
- Windows Rust 黑箱 workflow 建立來源 HEAD 的 Git bundle 並傳給 `win-blackbox.ps1`，讓 staging 測試可驗證提交歷史；A/B comparator 明確驗證兩側 config digest 各自有效，但不把不同的 runner／夾具設定誤當成不相容。新增回歸固定這兩項行為。
- 隔離 systemd L1.5 測試容器只增加 `SYS_PTRACE` capability，使非 dumpable 行程的 fd holder 可讀；掃描仍對未讀取成功的 PID fail-closed，並在證據中保留 PID 與讀取錯誤細節。
- 修正 Stage 1B A/B 執行器在全 `rust_only` 選取下的 A 輪釋放檢查（S1X 出口 G2，PENDING-OD-S1X-WIN-AB-A-ROUND）。Windows 平台的 `ab_blackbox_1b` 只選到 `test_autostart_l15_tx_once`（rust_only），A 輪（Python 基線）於 conftest 即以 `rust-only contract` skip、不啟動受測行程，因此不會留下 PID 日誌；原本的 release 判定要求必有 PID 日誌，判為 `ok:false`，B 輪未啟動，`test_cli_mgmt_ab_suite_execution`、`s1a_blackbox.windows`、`s1b_20groups.windows` 與 `sc18-windows-ondemand-autostart` 因此 FAIL。Linux 的 `_release_check` 在同一條件下有相同問題。

  依據 plan 05 Task 16（`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` 第 3523 行）：「`rust_only` 案例只跑 B 並驗絕對契約」。新增共用前置判斷 `_a_round_release`（Linux 與 Windows 共用），僅當下列條件全部成立時，release 記為 `ok:true`、`a_round_spawned:false`、`reason` 為「全部選取節點為 rust_only，A 輪僅收集並 skip，未啟動受測行程」，並照常啟動 B 輪：

  - 每個選取節點皆為 `_approved_python_skips` 核准的 `rust_only` 節點。
  - A 輪 exit 0，JUnit 可解析，且每個選取節點恰一列、結果為 `rust-only contract` 的 skip（passed、xfail 或其他理由的 skip 不符合）。
  - `a.pids` 不存在或無任何非空白列。

  任一條件不成立（含選取中有共用節點、有 PID 列）一律沿用既有 `_release_check`，缺 PID 日誌仍判 FAIL（fail-closed）。

  未放寬任何 judge 或斷言：`test_cli_mgmt_ab_suite_execution` 的斷言、`ab_compare` 判定規則、`_junit_required` 的 skip 核准規則與凍結 lock（`p4_required.lock.json`、`p4_case_map.yaml`）皆未改動；`_release_check` 本身的判定也未改，僅在上述前置條件成立時不呼叫它。B 輪仍須通過 Windows COM pair 與 selftest。`_junit_matches` 只抽出既有的節點比對邏輯，行為不變。

  測試以 failing-first 方式加入 `tests/contract_blackbox/test_cli_mgmt_ab_suite.py`：全 rust_only 且 A 輪 skip 乾淨者放行並啟動 B 輪；混入共用節點者仍 `ok:false`；全 rust_only 但 A 輪有 passed、未核准 skip 或 exit 非 0 者仍 `ok:false`；PID 日誌有列者沿用既有檢查。

  **待非實作者審查**：本變更對應 `docs/rust-refactor/owner-decisions-addenda.md` 中 PENDING-OD-S1X-WIN-AB-A-ROUND 的選項 (b)（放寬 release 判定），依該登記須經擁有者或非實作者核准。在核准前不得記為 PASS；該項狀態更新由 G5 負責，本 fragment 不改變其 OPEN 狀態。

  **核准範圍（2026-10-23）**：Reviewer A（Sonnet max，非實作者）判定 G2 實作 sound；`_a_round_spawned_nothing` 之前置條件在真實凍結資料上 Windows 僅選到 `test_autostart_l15_tx_once`（核准 rust_only）、Linux 選取 13 節點且含共用節點故不觸發。正式核准仍待辦，PENDING-OD-S1X-WIN-AB-A-ROUND 維持 OPEN。
- Windows 黑箱 runner 的 bridge 自測改以本機 scratch 作為 `--out`（與 stdout／stderr），不再直接寫入 OutDir（`\\wsl.localhost` 網路路徑）。ST-04 會對 `--out` 持續寫入 50 MB stderr 證據，網路路徑上曾出現 `dut.stderr.log` 截斷（48431104 bytes）與 `HARNESS_EVIDENCE_WRITE_FAILED`（bridge rc=3），連鎖使 `windows-com` 判 FAIL、B 輪缺 COM pair、`test_autostart_l15_tx_once` SKIP。行程結束後整份回寫 `OutDir\windows-com\bridge-selftest`（`selftest-report.json` 與 `work/`），並以 SHA256 逐檔比對；回寫不一致只會使證據不足，不會放行。資格判定（`is_qualified`、`verdict`、`tree_sha256`）維持原樣，`windows-com.json` 新增 `selftest_paths` 記錄本機與 OutDir 兩個路徑。新增 `tests/test_rust_win_scripts.py` 靜態斷言，鎖定 `--out` 不在 OutputRoot 或網路路徑之下、回寫步驟存在且位於行程結束之後。

  補充（S1X 出口收尾）：selftest 回寫最多 3 次有界重試；stdout／stderr 兩份 log 併入 SHA256 逐檔比對，不一致即證據不足，不放行。
- 修正 Windows COM 橋接自測與黑箱腳本的 Stage 1 出口缺陷：`WinSerialEndpoint` 原以單一狀態鎖包住阻塞的讀取與寫入，寫入阻塞（對端不讀、緩衝區滿）時會連帶阻住讀取端，bridge 無法排空接收端，即 ST-03(a) 所描述的雙向死鎖。現改為狀態鎖只保護 handle／generation／timeout 設定，讀與寫各以獨立串行鎖進行、彼此並行。ST-03(a) 的 Windows 分支回到計畫定義的「一次寫 2 MiB 且不讀，之後再讀」，寫入與讀取皆有界期限（逾時即 FAIL，不無限等待，不回傳部分資料當成功）；先前的並行 helper 已移除，改以端點層級測試（`tools/uart_stdio_bridge/tests/test_selftest_win_round_trip.py`）驗證。Windows 實機結果於 Stage 1 證據中另行記錄，未通過即 FAIL，不得以測試改寫放行。`win-blackbox.ps1` 快取 Process Handle，ExitCode 為 null 一律判定失敗；B 輪 junit 與 `SW_RESOURCE_REPORT` 改寫入 staging `l1\windows\blackbox\` 後再複製回 OutDir。bridge 工具樹的 `APPROVED_REVISION.json` 已還原為 HEAD（先前由實作者自行 re-pin 的內容不得保留）；新工具樹須由非實作者獨立審查後才可 pin。
- 修正 Windows 委派 Job 的終止傳遞：`exec_or_wait` 的 Job 不再設定 `SILENT_BREAKAWAY_OK`，委派父行程被終止時，一般方式建立的孫行程不再靜默脫離 Job 而殘留；正常結束時改於關閉 Job 前解除 `KILL_ON_JOB_CLOSE`，維持 `daemon start` 類背景行程存活。
- 修正 Stage 1 出口 Windows L1.5 producer（`win-func-test.sh`，COM20:COM21）首次實跑未產生 `func-test-report.json` 的六項缺陷（S1X G3）：

  - runner 崩潰：`daemon_harness.stop()` 的臨時目錄回收遇 WinError 32 時中止整次執行。改為 daemon 行程以 Popen 確認結束、案例自建的 WAL 讀取 handle 持有者於回收前等待結束、臨時目錄回收有界重試；回收仍失敗者只寫入該案 `harness_diagnostics`，不改變案例判定。daemon 未確認結束或關閉失敗者判為 INSUFFICIENT_EVIDENCE、不得 `gate_eligible`，原已 FAIL 者保留判定。runner 於每案結束後寫出 `func-test-report.json`（原子取代），單一案例的未預期例外記為該案 INSUFFICIENT_EVIDENCE 並繼續其餘案例。
  - DUT 開機 banner 遺失（HARNESS_STREAM_MISMATCH）：未修正（BLOCKED，見下）。根因已實測確認：pyserial 開埠會清除該埠尚未讀取的位元組（COM20:COM21 實測），bridge 於 daemon 開啟 COM20 前輸出的 banner 因此遺失。harness 端無法僅以延後 banner 解決：延後 banner 會使 DUT 的 prompt（取自 boot_banner 末行）消失，probe 無法取得 prompt。需 bridge win-serial 端點提供寫入閘門（對應 posix peer_gate），屬 tools/uart_stdio_bridge 變更，本組未修改。（Supersede，2026-10-23：本條之「未修正（BLOCKED）」已由 `rust-s1x-win-serial-peer-gate.md` 所述之寫入閘門取代並完成原生實測；PENDING-OD-S1X-WIN-BANNER 仍 OPEN，待 Windows L1.5 重跑證實。）
  - 編碼：wrapper 不再全域強制 `PYTHONUTF8=1`（改以 `PYTHONIOENCODING=utf-8` 僅作用於 runner 自身的 stdio），bridge 的 stdout 明確以 UTF-8（`errors=replace`）解碼，使 bridge 子行程呼叫 git／powershell 時依系統 locale 解碼。
  - 基本案例：`win-func-test.sh` 於同一 staging 另跑一次基本案例（不帶 `--scenarios`），輸出 `basic/func-test-report.json`；驅動以第二個 `--l15-report windows=` 交給 gate（gate 5c-iv：cases.lock 宣告的 windows 平台都須有列）。`cases.lock.json` 的 platforms 未變動。
  - 測試資源標記：`tests/test_rust_win_func_test.py` 的 13 個項目逐一補上資源 marker（純字串讀取為 `memory_only`，執行 bash 子行程為 `process_rpc`），斷言未改。
  - 驅動 `stage1_exit_driver.sh`：`RUN_ID`、`WIN_L15_WRAPPER`、`SPIKE3_WRAPPER` 不再列於 `local`，環境覆寫恢復生效。

  補充（報告完整性，S1X 出口收尾）：`func-test/runner.py` 之 `func-test-report.json` 加入 `complete` 旗標；每案中途寫出為 `false`，僅全部案例結束後的最後一次寫出為 `true`。`stage1a_gate.py` 拒收缺少或非 `true` 的 l15-report（整體 FAIL）。
- Windows serial endpoint 改用高解析度 `perf_counter` 計時；讀寫於鎖等待後重算剩餘期限。bridge core 以 50 ms 呼叫 `read()`；當端點期限較短時，endpoint 只取 driver buffer 現有 bytes，沒有可讀資料時每 10 ms 輪詢一次 `in_waiting`，不進入固定 50 ms 的 pySerial 阻塞讀取。寫入 timeout 依 1 ms driver 精度向下量化，降低亞毫秒抖動引起的 setter churn；setter 耗時後若已設定 timeout 超出呼叫剩餘預算，端點不送出資料。DSR 在 reconfigure setter 前與 write 前都重新取樣；保留 busy-reader、鎖等待期限與未送出資料回歸案例。

  期限邊界以可控時鐘驗證，包括 read/write 鎖等待、setter 完全或部分耗盡期限、零期限空讀取與 DSR 在鎖等待期間關閉。Bridge core 的 Windows serial 測試替身補上 `in_waiting`，與短期限讀取使用的 pySerial 介面一致。
- 修正 `uart_stdio_bridge` Windows win-serial 端點缺少寫入閘門的問題（S1X，PENDING-OD-S1X-WIN-BANNER）：

  - 端點以 COM-B 的 DSR 取樣對端（COM-A）是否開啟；`peer_gate=True` 時，對端未開啟或仍在 settle 窗口內不寫入（寫入串流長度為 0），以跨過 pySerial `open()` 的 `PurgeComm`。settle 窗口 0.2 s 為未經量測的選值，待原生實測校準。
  - `peer_gate` 僅對 `virtual_com0com` 啟用（CLI 預設啟用，`--no-peer-gate` 可關閉）；`physical_usb_uart` 建構時拒絕啟用，因其 DSR 映射未實測。
  - 寫入鎖等待期間 peer 可能關閉；端點在持有寫入串行鎖、呼叫 pySerial 前重新取樣 DSR，若已關閉則持續等待至呼叫期限並回 0，不送出 bytes。
  - 需要 reconfigure `write_timeout` 時，取得寫入與讀取鎖後、呼叫 setter 前再取樣 DSR；等待期間 peer 關閉時不改寫 COM timeout。
  - `peer_open()` 不等待 endpoint I/O lock，避免 peer-state 查詢被關閉中的寫入卡住。
  - `peer_gate=False`（selftest 既有規格與 CLI 的 `--no-peer-gate`）時 `peer_open()` 回 None、不取樣，維持既有行為；若回 False 會使對端關閉時寫入逾時並 fail-stop（HARNESS_WRITE_UNCERTAIN）。
  - 對外 `status.peer_open` 僅於端點宣告 `peer_state` 能力時揭露；win-serial 的 virtual com0com 且 `peer_gate=True` 時提供 DSR 狀態，其餘模式仍回 null。
  - `assert_peer_closed` 會依執行端點能力判定；Windows com0com 啟用 `peer_gate` 時可執行，未啟用或使用實體 UART 時仍須回報 `cap_missing:peer_state`。
  - `capabilities()` 僅在 virtual com0com 且 peer gate 啟用時宣告 `peer_state`；`transport_disconnect` 仍未宣告，避免把 peer close 誤當成傳輸斷線。
  - 原生實測（COM20:COM21）：peer 開啟後 DSR 轉高並寫出 banner；peer 關閉後 1 s 內 `peer_open` 轉 False，關閉期間寫入回 0 且不到達 peer（`b-log/rust-refactor/g3-peer-gate/261023-061416-2646662`）。本 fragment 不代表 Windows L1.5 案例已重跑；PENDING-OD-S1X-WIN-BANNER 維持 OPEN。
  - 本變更改動 `tools/uart_stdio_bridge/src`，`APPROVED_REVISION.json` 的核准 tree 失效，須經非實作者審查並重新核准後，Windows 與 Linux 的 L1.5 結果才可視為有效證據。
- Windows Stage 1 黑箱 runner 改由 WSL 端輸出 exact HEAD 的 Git bundle，再複製至本機 staging 載入歷史；避免 Windows Git 無法沿 UNC 路徑解析 WSL worktree 的 `.git` common-dir。
- Stage 1 出口收尾第 2 輪：黑箱 A/B 腳本可於腳本模式執行、A 輪不再要求 release-manifest、必要清單改為正規化形式；P4 證據登記拒絕髒樹；daemon EIO 回 LOGIN 語意與 POSIX `current-release.json` 路徑修正；L15 延遲回覆情境補 READY 等待，runner 對未解析樣板 fail-closed；Windows COM 閘門改讀真機 `sequence.json` 佈局。未放寬任何斷言，凍結鎖未變動。

  Stage 1 出口收尾修正輪：黑箱 L1 報告 deployed_content 於 B 輪恆為 null 之根因修正（contract_blackbox 共用已載入 plugin；ab_blackbox B 輪明定 SERIALWRAP_DAEMON_EXE；Windows current-release 指向改為相對 bin）；Windows 黑箱 staging 補 owner_gated 並加入間接 import 覆蓋測試；doctor A/B 比對前於隔離 namespace 輪詢殘留 daemon；bc05 三拓樸改 A/B 雙輪（test oracle 變更，待非實作者審查）。凍結鎖未變動，未放寬任何斷言。
- 修正 bash readline 折行標記緊貼命令原有空白時，命令回顯未能剝除而混入 stdout 的問題；補上前置空行與多次折行回歸案例。
- 修正 update／rollback／`setup` 中斷復原的交接停止等待：daemon 回 `daemon.handoff_stop` 後先保留 3 秒排空窗口才關閉監聽，CLI 卻自 grant 起只等 10 秒，排空窗口吃掉 SC-12「ping 不通且行程消失，上限 10 秒」的時間，已凍結、正在有序關機（bridge drain、thread join、WAL fsync）的 daemon 稍慢結束時，on-demand 模式即回 `TIMEOUT`（`reason:"handoff_timeout"`）並保留 journal。仍回 ping 時維持 grant 起 10 秒上限；只在 ping 於連線階段以 `ConnectionRefused`／`NotFound` 失敗且舊行程仍在時，改自監聽關閉起算 10 秒；ping 恢復時清除此計時。recv 逾時等仍連得上的失敗維持 grant 起 10 秒上限。
- 修正 fake ProcScan 對重拉 daemon 回報的 socket：Unix RPC URL 轉回原始 `--socket` 路徑，TCP endpoint 維持原值。這讓 Windows 原生 legacy handoff 測試與真實行程掃描的欄位語意一致。
- 修正 Linux release bundle smoke 的版本檢查：只接受 Python 基線的純版本行或附 12 位小寫十六進位程式碼指紋的整行，避免正式 Rust bundle 因合法指紋被拒絕；錯版、格式錯誤與多餘輸出仍會失敗。
- 修正 Stage 1 出口 driver 對 `RC_SUMMARY` 的 fail-closed 判定。`l15_exit_driver.py` 未捕捉例外或摘要不是合法 JSON、缺少非負整數 `failed_steps` 時一律失敗，避免空字串被 `[[ -eq 0 ]]` 誤判通過。註解改為第二層判定：唯一核准的 `win_l15_func_test 1` 可不擋出口。
- Docker 升級驗證的 PyYAML wheelhouse 明確鎖定容器的 CPython 3.12／cp312／manylinux x86_64，映像與容器固定 linux/amd64，避免主機 Python 版本影響離線安裝。正式 bundle 與升級驗證入口拒絕未提交的來源變更，並在結束時再次核對；升級報告新增 driver 版本與容器內實際載入的兩支 driver 雜湊，區分 bundle 來源與驗證工具來源。開發 bundle 與 dry-run 保留開發用途，判定及必要檢查集合不變。
- `contracts/cli_schema.json` 補記 e8aa5aa6 更新 `--expect-reboot` 說明時漏掉的 `x-catalog-revision`（其後 a494670c、ac9e0e0c 已各為自己的修改 +1，現行 9→10）。
- 不完整的十六進位跳脫（`\x`、`\u`、`\U`）錯誤訊息改為帶上已讀到的跳脫原文與位置，與 CPython 3.12.3 `re.error` 逐字一致（例如 `\x4` 為 `incomplete escape \x4 at position 0`）。
- 委派測試新增子行程在 `exec_or_wait` 前停住的負向自測，與 Windows Ctrl 事件測試共用 ready 等待判準，避免將子行程存活誤認為孫行程已就緒。
- 錯誤碼抽取測試改寫入 Cargo 指定的輸出目錄，允許原始碼／測試資料樹唯讀，並補上兩種目錄覆寫的回歸。
- 修正 OD-75 出口判定：保留 nodeid 的參數、類別與路徑，依 L1 案例引用的全部 A/B 證據套用登記；已登記失敗不再污染其他通過的 JUnit 列，仍對帳其餘必要節點與 suite 失敗數。producer 退出碼只在自己的完整原始報告全部失敗均已獲准時不擋，並保存 BB-proc 與 Stage 1B 原始報告。`--rejudge` 同時重判 gate 與 producer，輸出分類明細，預設以新的出口結果決定退出碼；另提供 `--assert-same-judgment` 比對舊判定。原始 FAIL 與 OD-30～OD-32 行為維持不變。
- `setup` 在重啟 daemon 後輪詢程式碼指紋時，每一輪都重新解析本機 endpoint，與 Python `_resolve_local_endpoint_info` 對齊。reconcile 把 `config.yaml` 的 socket 寫回 canonical 之後，不再沿用 reconcile 前的舊 endpoint。明示的 `--endpoint` 或 `--socket` 仍優先於設定檔。
- 正式 Rust CLI 僅在 `test-fakes` 建置中讀取 `SERIALWRAP_TEST_DELEGATE_GROUP`。
  release profile 不含 `SERIALWRAP_TEST_` 或 `SW_TEST_UPDATE_*` 測試鉤子；
  dev bundle 與啟用 `test-fakes` 的 Cargo 測試仍可使用委派覆寫。
- Windows 黑箱 B 輪改以 `test-fakes` 建置的 `serialwrap.exe` 執行（比照 Linux dev bundle 才讀得到 `SERIALWRAP_TEST_DELEGATE_GROUP`）。標準 release 路徑在其後以不含該 feature 的建置覆寫，且不得含 `SERIALWRAP_TEST_` 或 `SW_TEST_UPDATE_`。Windows `help_root_col80` golden 依 CPython 3.12.3 實測輸出對齊 Python 基線的 `--version` 說明。
- Rust CLI 解析 systemd user unit 時，環境變數 `HOME` 優先於 passwd 家目錄，路徑固定為 `HOME/.config/systemd/user/serialwrap.service`，不改走 `XDG_CONFIG_HOME`。一般 config 與 state 仍依既有 XDG 與 `SERIALWRAP_*` 覆寫。Rust 黑箱 session 結束時比對真實 user unit 的存在性、mtime 與內容雜湊，與 session 開始不同即失敗，且不改寫該檔。
- 修正監管模式切換沿用 `HOME` 優先的家目錄解析，避免把 systemd user unit 寫到 passwd 家目錄。
- Rust CLI 的 systemd user unit 寫入、更新快照、失敗還原與 hold 計畫共用 `HOME/.config/systemd/user` 路徑，避免誤用 XDG 或 passwd 家目錄；一般 config／state 維持既有覆寫優先序。
- 負路徑測試的 passwd 家目錄改放測試暫存目錄，並拒絕指向行程真實家目錄，避免回歸時寫入真實 systemd user unit。
- 修正 Windows 原生 Cargo 測試的 passwd 對照夾具位置：由 runner 提供本 worktree 的隔離暫存根，保留真實家目錄防線，讓 HOME 優先序、hold 與 unit 快照還原回歸能進入原有行為斷言。
- 統一 systemd-user unit、更新 hold drop-in 與中斷恢復計畫的 HOME/.config 根目錄；XDG_CONFIG_HOME 指向其他目錄時，恢復仍可移除本次寫入的 hold 並保留其他檔案。
- 修正 `mode=interactive` 命令（含 human line console 輸入 `vim`／`top`／`less` 等被判為 interactive 的行）永遠送不到 UART 的回歸：interactive 分支的 TX permit 改用與 lease owner 一致的 `interactive:<lease_id>`，恢復 0.3.2 的 `ok:true` 並寫出命令；送出被拒或拋例外時會關閉剛開的 lease，後續命令不再被遺留 lease 卡成 `SESSION_INTERACTIVE_BUSY`（S1X-a-1）。
- 修正 attach 進行中（probe／login 期間，無 drain、無 release 請求、非 RELEASED）再呼叫 `device attach` 時誤回 `SESSION_RECOVERING` 的回歸：恢復 0.3.2 的 `{"ok":true,"session":{...,"state":"ATTACHING"}}`；`SESSION_RECOVERING` 只保留給 drain／release 進行中（S1X-a-3）。
- 修正 `file.push`／`file.pull` 傳輸中目標重開機（boot quiet）或 bridge drain 時 `TxIncompleteError` 穿越 RPC 邊界、回 `EXCEPTION` 的回歸：quiet 拒絕改回 `AUTOBOOT_QUIET`＋`retry_after_s`（SC-03），其餘 TX 拒絕回傳輸自身的 `TRANSFER_TIMEOUT`（0.3.2 結束碼），push 附 `chunks_sent`／`chunks_total`；被拒後 best-effort 清半行與遠端暫存檔，再被拒即停，不送 bytes 進 autoboot 倒數（S1X-a-4）。
- 修正共用主機上另有他人（或本人以 systemd-user、不帶 `--socket`）執行的 `serialwrapd` 時，`serialwrap setup` 模式切換與 `serialwrap daemon restart` 在舊 pid 未知時 10 秒後回 `TIMEOUT` 的回歸：舊 daemon 存活掃描改為略過 uid 不同的行程，同 uid 但不帶 `--socket` 的 daemon 以預設 endpoint 比對，讀取失敗只在同 uid 行程上視為掃描不完整（S1X-a-5）。
- 修正一般使用者執行的 daemon 每次開埠都因讀不到他 uid 行程的 `/proc/<pid>/fd` 而寫 WARNING、`daemon status`／`doctor` 的 `foreign_holders` 全面變成 `{dev: {"device":…,"holder_unknown":true}}` 的回歸：掃描不完整只因他 uid 行程不可讀時記為 `partial_other_uid`、不升為 holder_unknown，`foreign_holders` 回到 0.3.2 的 `{dev: pid}` 形狀；同 uid 行程不可讀仍為 holder_unknown（BC-04）。實機 case `f8-tty-holders-reported` 與 `p0-multiopen` 改為同時接受 dict entry，不再拋 TypeError 或假 FAIL（S1X-a-6）。
- 修正 drain 化後 `device release`／`session clear` 收尾欄位與 0.3.2 不同的回歸：release 完成時更新 `detached_at` 並重置 `reprobe_attempts`／`reprobe_exhausted`；clear 在裝置在場轉 ATTACHING 時把 `last_error` 清為 `null`，不再於 attach 完成前在 `session list` 看到 `"CLEARED"`（S1X-a-7）。
- `scripts/rust/ab_blackbox.sh` 在 A/B 比較器回非 0 但已寫出報告時，仍輸出報告路徑並照傳 exit code，exit driver 不再因取不到路徑使 gate 誤判「缺少 ab-report」；比較器未產生報告時不印路徑。
- Stage 1 A/B 黑箱身分的 `config_digest`（G4-ab1b-config-digest）改為反映各夾具實際建立的設定內容：`isolated_env` 登記的隔離根於每個案例 call 階段結束時，以 `tests/contract_blackbox/config_digest.py` 記錄 YAML（config.yaml、profiles）內容雜湊與正規化後的 env 摘要（隔離根路徑為 `<ROOT>`，認證類鍵只記存在與否），逐案例、逐夾具保存於 `SW_AB_CONFIGLOG` 紀錄，A／B 兩輪身分各自引用彙整後的摘要。`scripts/rust/ab_blackbox_1b.py` 不再以鎖檔與 applicability 檔的雜湊充當 `config_digest`，該雜湊改存獨立欄位 `lock_applicability_sha256`；設定紀錄缺失或為空不以例外中止，而記入 summary 的 `config_errors` 並判 fail。`scripts/rust/ab_blackbox.sh`（1A）同步改用設定紀錄，原以 bundle 目錄雜湊充當的 `config_digest` 改存獨立欄位 `bundle_tree_sha256`。比較器（`ab_compare.py`）的正規化與比較規則不變。
- 修正 Rust CLI 的 `pyre` 字元類別解析：範圍 `a-b` 改在讀到當下即檢查（對照 CPython `sre_parse`），`bad character range` 的報錯優先序先於其後的 `unterminated character set` 與類別內跳脫錯誤；`-` 後直接結束仍回 unterminated。跳脫 token 的範圍訊息與位置亦對齊 CPython（例如 `[\x7a-a]` 回 `bad character range \x-a at position 3`）。`profile test` 的 `checks.<regex>.error` 因此與 Python 3.12.3 逐字一致（S1X-b-1）。`pyre.tsv` golden 新增 14 筆語料（add-only）。
- 修正 Rust CLI `event add --file`：規則 JSON 含孤立 surrogate 跳脫（如 `"\udc80"`）時，原本依 RPC-H-03 改寫成 U+FFFD 後照送並寫入 daemon（exit 0）；現比照 Python 在連線成功後、送出前以 `INTERNAL_ERROR`（context=`event.rule_set`，訊息 `'utf-8' codec can't encode character '\udcXX' in position N: surrogates not allowed`，連續者為區間）exit 1 結束，daemon 收不到請求行（S1X-b-2）。回應端的 RPC-H-03（孤立 surrogate 轉 U+FFFD）不變；`sw_proto::pyjson` 新增 `loads_keep_lone_surrogates` 供請求端檔案內容使用。
- 修正 Rust CLI `event add --file`：規則 JSON 以重複鍵覆寫掉含孤立 surrogate 的值（如 `{"lone":"\ud800","lone":"ok"}`，或被覆寫的巢狀物件）時，原本仍以 `INTERNAL_ERROR` 拒送；現比照 Python `json.load`（後值覆寫前值）只依最終物件判定，最終值不含孤立 surrogate 即正常送出（S1-2a65438-1）。最終值仍含孤立 surrogate 時的拒送行為不變。
- 修正 Rust CLI 的 `pyre`：樣式以單獨的 `\` 結尾時，比照 CPython `sre_parse` tokenizer 的預讀語意，取走倒數第二個 token 的當下即報 `bad escape (end of pattern)`，優先於該 token 之後才判定的錯誤（如 `[z-a\`、`[\d-z\` 的 bad character range、`[z\A\` 的 bad escape、`a\8\` 的 invalid group reference、`(?P<\` 的 unterminated name、`a{2,1}\` 的 min repeat）；原本通過的 `(?x)a#c\`（註解內讀到結尾）現亦正確報錯。在此之前已判定的錯誤（如 `++\` 的 nothing to repeat）與偶數個反斜線結尾者不變。`profile test` 的 `checks.<regex>.error` 因此與 Python 3.12.3 逐字一致（S1-2a65438-2、S1X-b-X1）。`pyre.tsv` golden 新增 25 筆語料（add-only）。
- 修正 Rust CLI `event add --file`：規則 JSON 同時含合法鍵「NUL＋U+FD800」（如 `"\u0000\udbb6\udc00"`）與孤立 surrogate 鍵（如 `"\ud800"`）時，兩者原本被當成同一鍵合併，本應拒送的請求被改寫後送出；現比照 Python `json.load` 保留兩個鍵，於送出前以 `INTERNAL_ERROR` 拒送（S1-f2a23b1-1）。拒送時的錯誤位置也不再把合法的 NUL＋U+FD800 或 `\\u0000` 文字誤認為 surrogate，與 Python 相同。只含合法鍵時照常送出。
- 修正 `process_ctl` 測試 `spawn_daemon_detached_new_session` 讀 `/proc/<pid>/environ` 的時序競態：原本 spawn 回傳後立即讀取，若子行程尚未完成 exec，會讀到 fork 自測試行程的父環境（缺 `P4_DETACHED_ENV=supplied`、帶 `HOME=`）或尚未發布的空 env 區段。改為先等 `/proc/<pid>/exe` 指向目標執行檔、再等 environ 非空（3 秒有界期限，逾時以 exe／environ 長度／stat 診斷判失敗）後才讀；`P4_DETACHED_ENV=supplied` 與不含 `HOME=` 兩項斷言不變。`spawn_daemon` 正式行為未改動。
- 收斂 Stage 1 出口 gate 的 cargo 證據綁定與 junit 計數（G2-cargo-evidence-binding-junit-suite-counts），各項皆先以會失敗的測試界定再修正：

  - (1) cargo 執行檔綁定（sol 58e539d#5、3f05797#1）。`cargo_l1_results.py` 於建置後、任何測試執行前記錄每個測試執行檔的 path 與 sha256，執行結束後重新核對（改寫或移除即 `finished: false`），並在報告頂層寫入 `provenance`（`cargo-l1-provenance.v1`）：受測 git HEAD 與工作樹乾淨狀態（開始與結束各一次）、`rustc -vV` 逐字輸出與摘要、RUSTFLAGS／CARGO_ENCODED_RUSTFLAGS 的設定狀態與 sha256（旗標文字不落盤），以及每個執行檔所含的 nodeid。Cargo 固定在收集器所在 repo 執行，並拒絕指向其他 workspace 的 `--manifest-path`，避免把外部產物配上本 repo 的 provenance。`register_p4_evidence.py` 移除登錄時的 `cargo test --no-run` 重建與以 `--list` 推定身分的路徑，改為只核對報告內的紀錄：缺 provenance、HEAD 或工作樹狀態不符、缺 rustc 紀錄、執行檔未唯一對應、缺 sha256，或磁碟上檔案的 sha256 與報告不符（改寫或重建後未重跑）一律 FAIL 並要求重跑。Windows 仍由 `win-test.sh` 呼叫同一收集器，`win-stage-lib.sh` 未修改。
  - (2) junit suite 宣告值（sol 11b1fd4）。P2 `tests/acceptance/check_evidence_index.py::_junit_counts` 同時讀取 testsuite 宣告的 failures／errors；宣告值大於 testcase 層計數即拋出 ValueError（fail-closed）。`check_p4_cases.check_l1` 將其判為 INVALID；pytest 集合的 junit 缺席或無法解析改判 INSUFFICIENT（原先會略過交叉核對）。`register_p4_evidence.py` 的 junit 列表與計數同樣 fail-closed。只拒收「宣告大於實際」方向，宣告值小於 testcase 層者不在此拒收（testcase 層失敗仍由 l1_judge 判 FAIL）。
  - (3) 測試可攜性（sol 3f05797#3）。`tests/test_register_p4_evidence.py` 不再使用 shebang 假 cargo、假 win-runner 與文字 `.exe` 替身（登錄器已不執行任何測試執行檔）；每個測試改用自備的乾淨 git repo 作為受測 repo，使登錄結果不受開發中工作樹的變更影響。
  - (4) manifest 指標邊界（58e539d#4 殘留）。`current-release.json` 的 `dir` 含 `..`、絕對路徑、空段、`.` 或磁碟代號即拒收，與 Rust launcher 的 `current_release_manifest_path` 同一規則；原「絕對路徑照用」的測試案例已依此改為拒收。

  未修改凍結鎖（`l1_required.lock.json`、`p4_required.lock.json`）、selector、l1_judge 判定規則與 `win-stage-lib.sh`；未刪除或弱化既有斷言。plan 04／05 的對應文字由 G10 處理，addenda 列由 G8 處理，本次只在 commit 與本段說明。既有未含 provenance 的 cargo L1 報告將被拒收，須以新收集器重跑。
- Windows `.py` daemon launcher 缺 manifest runtime 時，`daemon start` 訊息改為指出 launcher 與缺少的 runtime 路徑（仍為 `DAEMON_BINARY_NOT_FOUND`、fail-closed）。
- 修正 docker systemd 實跑 harness 的 holder 掃描假釋放缺陷（sol 300f383，Medium）：`scripts/docker/run_systemd_container.py` 的 `_PROC_FD_SCAN` 原本只要 fd 目錄可列舉就把行程計為 `readable`，之後 `readlink()` 的 `PermissionError` 全被吞掉。兩輪掃描都無法解析某個現存 fd 時，該 PID 仍被視為已覆蓋，`_holder_pids()` 可能回傳空集合，使交接測試誤認裝置已釋放。

  現行判準（fail-closed）：掃描 `scan()` 的輸出新增 `unresolved` 清單。fd 目錄列舉失敗（非 `FileNotFoundError`）、或有 fd 的 `readlink()` 失敗（非 `FileNotFoundError`）且未找到目標者，計為 `unresolved` 而非 `readable`。fd 或行程於讀取期間消失（`FileNotFoundError`）與無 fd 目錄者可忽略，不計為未解析。`_holder_pids()` 合併 root 與 daemon 使用者兩輪掃描：行程只要任一身分能完整解析其 fd 表即視為已覆蓋；兩個身分皆無法解析、行程仍存在、且屬於權限所及 uid（0／1000）時拋出 `RuntimeError`（證據不足，不回傳空集合）。判定範圍以 root 掃描時已存在的行程為準，兩輪之間新出現者（例如 `runuser` 父行程）不計入，避免誤報；其他 uid 行程仍不計入判定不足。殘留風險與原註記相同。

  測試：新增 `tests/test_docker_holder_scan.py`（`posix_only` 與 `memory_only`，行程內以暫存假 `/proc` 樹執行與 harness 相同的掃描原始碼，注入 `PermissionError`／`FileNotFoundError`）。涵蓋：可列舉但 readlink 拒絕的行程不得為 readable、同行程另一 fd 命中仍判為 holder、消失的 fd 與行程可忽略、兩身分皆無法解析須 fail-closed、單一身分可解析時以其結果為準、消失的行程不 fail-closed。既有 `tests/test_docker_l15.py::test_holder_scan_fail_closes_only_on_reachable_uids` 未修改、仍通過。本變更不改動任何 L1 凍結鎖或已凍結的測試節點。
- docker smoke 的 selftest 診斷遇到頂層非物件的報告 JSON 時改列「頂層非物件」，不再於 assert 訊息求值時拋 `AttributeError` 吞掉原失敗訊息。
- `test_docker_smoke_two_container` 失敗時把 bridge selftest 報告的 verdict 與非 PASS 檢查附進斷言訊息（POSIX 短 basetemp 於 pytest 結束時刪除，失敗當次證據原本無法事後查閱）；判定不變。
- Stage 1A gate 依 OD-30 (2) 把 Windows `l15-transport-disconnect` 因 com0com 無斷線能力（`HARNESS_CAPABILITY_UNAVAILABLE`）未執行的列判為 BLOCKED-OWNER（不計 PASS），Linux 列仍須 PASS，其他失敗照判 FAIL；延後只適用登記的 verdict 與完整符合的訊息型態，列帶 harness 關閉失敗診斷時不延後。
- Stage 1A gate 依 OD-31 把 Windows `ev-01-spawn-handler` 因 Python 基線 shell handler 在 Windows 不觸發（等待 `ev-fired` 逾時）的列、依 OD-32 把 Windows `sm-04-device-unplug` 的 `cap_missing:by_id_link` SKIP 列判為 BLOCKED-OWNER（不計 PASS）；Linux 列仍須 PASS，其他失敗型態照判 FAIL。
- Stage 1A gate 的 `s1_pytest_full` 逐列對帳保留參數化 id，`[linux]`／`[windows]` 各自恰一次 passed 才放行，不再合併成同 key 而恆判證據不足，也不再以同函式另一參數補位；key 與 pytest JUnit classname 同形並保留完整目錄路徑，不同目錄的同名模組不會合併。
- ### Fixed

  - 修正 Python 基線交接凍結（SC-11 `daemon.handoff_stop`）的 fail-closed 缺口，違反 OD-16 窗口零 TX：TX fence 於期限內未收斂時，`freeze_tx` 回傳未收斂結果，交接回 `SESSION_BUSY`＋`busy`（不再回 `ok:true`），並撤銷本次新建立的 admission 與背景凍結（不凍結、不停止）；probe 於鎖外執行期間完成交接時，結果不得把 session 推進至 READY 或清除 `last_error`；reconcile tick 的 lease 到期、重探排程、console 回收與 lease 自癒區段於鎖內重新檢查凍結；attach 因凍結早退時還原預先標記的 `ATTACHING`。新增互斥交錯測試 `tests/test_handoff_freeze_races.py` 與 `tests/test_stage0_handoff_stop.py::test_handoff_refused_when_background_write_holds_tx_fence`。
- 新增 R-21 整檔豁免防線 `tests/test_policy_r21_allow_guard.py`（逐字複製的雲端審查原文只容許第 3 行一處 structural 命中、不得有 credential／marker）；OD-23／OD-24／能力閘防線與此防線共 12 筆以 add-only 登記進 `s1_pytest_full.linux` 凍結集合。
- OD-24：`scripts/rust/cargo_l1_results.py --list-only` 輸出改為 `{"collected":[...],"ignored":[...]}`，`tests/acceptance/regen_lock.py --l1-add-only` 新增時排除 ignored nodeid（鎖中既有 ignored 列不視為移除，只能逐名 `--allow-removal`）；新增防線要求所有 cargo 凍結集合不得含 `#[ignore]` 列，並自 `s1b_cargo` 逐名移除 5 筆 `#[ignore]` 列（CI `--allow-removal` 與 pinned 清單同步列名）。同次真實收集另登記 `start_windows_python_launcher_without_runtime_does_not_fall_through`（linux、windows 各 1 筆）。
- OD-25：`scripts/rust/win-startup-100.ps1` 改為只做隔離環境——100 次 CLI 執行的子行程 TEMP／TMP／LOCALAPPDATA 指向本次專用的空隔離目錄，隔離目錄內任何新增（不限副檔名）即 FAIL；exe 目錄新增目錄／exe／dll 仍 FAIL；真實 `%LOCALAPPDATA%`／`%TEMP%` 的新增只記於 `ambient_changes`，不再因其他桌面程式的目錄誤報。
- OD-29 (4) `cap_interactive_desktop__exec_or_wait_ctrl_c`：子行程重入補 `--ignored`（首跑空跑根因）、子行程先寫 ready 標記才送事件、委派前重新啟用 Ctrl+C 使 `CTRL_C_EVENT` 可送達新 process group，並由父／子寫診斷檔；期望值不變。runbook 更新新 exe 與重跑指令；新增 OD-28 登出觸發 runbook。
- OD-29：`cap_supervisor_runtime__systemd_hold_blocks_restart_on_failure` 的清理一併移除 release 後留下的空 `<unit>.service.d` 目錄，確保本 host 實跑後 `~/.config/systemd/user` 不留暫時 unit 痕跡；新增擁有者 runbook `docs/rust-refactor/owner-runbook-od29.md`（`cap_interactive_desktop__exec_or_wait_ctrl_c` 需互動 console）。
- 所屬 template 已改名或刪除的 RELEASED 紀錄不再永久阻擋裝置（S1X-a-8，擁有者裁決 OD-63）：載入時實體化為 RELEASED 佔位 session（passthrough、`command_capable:false`），by_id 維持阻擋並跨重啟保留；`device attach COMx` 時佔位移除自身並解除 by_id，已取得的 UART 交 template 偵測在同一 COM 建立正式 session（不以佔位 profile attach、不改綁其他 DETACHED 空槽）。不新增 RPC／CLI 表面；DF-04 說明同步。
- `check_no_onefile_extract.py` 新增 `--private-tmp`（Linux）：於 user+mount namespace 內以 bind 進來的全新空目錄作為獨占 /tmp 執行，其中任何建立或殘留都判 FAIL（含寫死 /tmp 的解壓器），不再因共用 /tmp 的他人事件恆判證據不足；無法隔離時判 INSUFFICIENT。直譯器、本工具、受測 CLI（含 /tmp 下的 symlink）或工作目錄位於 /tmp 下時一律以用法錯誤 exit 2 拒絕。Stage 1 exit driver 改用此模式，並核對報告新鮮度與 `verdict PASS`＋`tmp_isolation`。
- 依非實作者 oracle 審查（`docs/rust-refactor/reviews/s1x-oracle-review-1.md`）後續：Spike 3 主／oplock 分開記錄案例的替身改由第一次 rename 請求觸發 break（修正高負載下的 INSUFFICIENT 誤判）；SC-18 被拒請求的零 TX 檢查前確保距被拒回傳已過 wire 落盤等待上限並 settle，並新增「被拒請求延遲落盤＋最後一次送出遺失」必 FAIL 的回歸；L1 新增 `autostart_resend_carries_original_command_params`，斷言 SC-18 重送的 `command.submit` params 與原請求相同且請求數為 1。
- 修正 Stage 1 擁有者機台登記掃描（`scripts/rust/owner_gated.py`，sol ccf29ce）的靜默消失缺口：owner-gated 節點若同時列於 `collected` 與 `deselected`、且 `results` 無對應列，舊版 `scan_cargo_report` 仍視其為「存在」，`owner_gated_skipped` 與 `absent_expected` 皆不列，最小報告可使 `l1_judge` 判 PASS，`stage1a_gate.py` 與 `check_p4_cases.py --check` 因而不列 BLOCKED-OWNER 亦不列缺失。現改為：`present` 不再計入 deselected 且無結果者；deselected 且無結果的擁有者節點一律列入 `owner_gated_skipped`（BLOCKED-OWNER 待補），登記列另列入 `absent_expected`（收集失效，INSUFFICIENT）。`scan_cargo_report` 新增回傳鍵 `owner_gated_deselected`。本次未修改 `l1_judge.py`、`stage1a_gate.py`、`check_p4_cases.py`、凍結鎖與 selector；新增 `tests/test_rust_owner_gated.py` 覆蓋 scan 層與兩支閘門的子行程情境。
- 修正 interactive 命令改以 `interactive:<lease_id>` permit 送出後，取消若落在送出前檢查與實際提交之間仍會寫出命令的競態：permit 帶著原命令 `cmd_id` 的取消身分（`TxPermit.cancel_op_id`），由 TX fence 內的提交點同時檢查 `cmd_id` 與 lease op 的撤銷狀態，取消後零 TX 並回 `FLUSHED_BY_RECOVERY`、關閉本命令開的 lease（sol S1-6485e90-1）。
- R-21 整檔豁免防線在無 policy engine（CI）時改以釘選的 engine baseline markers 解析 marker 集合（與 `resolve_markers` 同式，集合為空即 FAIL），修正 CI 上 marker 維度空轉；新增無 engine 反證測試並登記進 `s1_pytest_full.linux`。
- 撤回 `68d0941` 以 `--l1-add-only` 夾帶回 `s1b_cargo` 的 10 筆已由 OD-16／OD-17 核准移除的凍結列（cap_* 擁有者機台列、`windows_switch_running_exe_rename`、`real_mkbundle_dev_output_verifies`），經 `regen_lock.py --allow-removal` 逐名撤回，修復 `test_l1_lock_frozen_set_excludes_capability_gated`。
- `session bind` 不再繞過 OD-63 以佔位 profile 實體化 RELEASED 佔位 session（sol 增量審查 S1-9990b18-1）：對佔位 bind 比照 `session attach` 的 RELEASED 早退，回 `ok:true`、`released:true`、`recommended_action:"device_attach"`，不解除 by_id 阻擋、不寫 binding override、不取消在途 attach、不開埠；`_attach_by_id` 另加防線，佔位 session 一律不建 bridge（attach worker 也不對佔位重試）。佔位仍只能經 `device attach` 解除後交 template 偵測。
- `device attach` 對 RELEASED 佔位 session 開埠遇一般 OSError（如 dialout 權限不足的 `PermissionError`、`EIO`）時不再讓裝置成為孤兒（sol 增量審查 S1-9990b18-2，OD-63）：比照成功分支交接，佔位移除自身並保留原 COM，交 dynamic 偵測重新開埠；仍開不了埠時依現行 dynamic 開埠失敗語意（有 passthrough fallback template 即在原 COM 留下 `DETACHED`／`ATTACH_FAILED:<例外名>` 列），修好後可再 `device attach`，不必拔插或重啟 daemon。回應仍為 `ok:true`＋`DETACHED` session，不新增 RPC／CLI 表面。
- SC-18 原生案例的首次 `cmd submit` 接受原驗收形狀（直接成功）或零 TX 的 `SESSION_NOT_READY` 拒絕，其他錯誤一律 FAIL；passthrough READY 等待上限改為 120 秒。
- SC-18 原生案例只比對自身開始後的 DUT TX（wire 為整個 B 輪共用），並要求該段恰為 daemon 實際送出的 `echo X\n` 一行；修正從未在原生跑到的 `echo X\r` 錯誤期望。
- SC-18 原生案例在成功送出後有界等待 bridge 證據 writer 落盤再比對 TX，並把被拒請求的零 TX 檢查移到 READY 等待之後。
- Stage 1A gate 撤除未經 OD-22 授權的「oplock rotation 完成 ≤ 600 ms」FAIL 條件；OD-22 上限只作用於 append 延遲樣本，rotation 仍須在某次受上限涵蓋的 append 內完成。
- Windows Rust CLI 以 on-demand／`daemon start` 啟動背景 daemon 時，不再讓 daemon 繼承 CLI 的 stdout／stderr pipe；擷取 CLI 輸出的呼叫端（agent、`subprocess.run`）在 CLI 結束後即 EOF，不再等到 daemon 結束（SC-18 autostart 在原生 Windows 卡死 3600 秒的根因）。
- Windows 黑箱 dev bundle 的內嵌 3.12.3 runtime 依 PyYAML 與 pyserial 實際匯入位置（含 user site）補入 `._pth` 並驗證兩者可匯入；修正 B 輪 on-demand daemon 開 COM20 時 `ATTACH_FAILED:OSError`（SC-18）。
- Windows Rust CLI 在 manifest 宣告 `.py` daemon launcher 但缺 manifest runtime 時改為 `DAEMON_BINARY_NOT_FOUND` fail-closed，不再落到 CLI 同層 `serialwrapd.exe` 而繞過版本配對。
- Windows bridge selftest 資格檢查輸出 raw verdict 與報告／目前／核准 tree 診斷，`selftest-result.json` 的 `reason` 區分 raw 失敗與核准 tree 不符；判定不變。
- `file push`／`file pull` 傳輸中因 drain、bridge 世代過期或取消中止且已寫出 ≥1 byte 而回 `COMMAND_ABORTED_IN_FLIGHT` 時，比照一般命令（BC-03）寫 `command_aborted_in_flight` WAL META（編排者裁決 S1X-a 第 2 輪 SPEC-GAP 1：欄位與時機相同）：`source=system`、`cmd_id` 為該次傳輸的 cmd_id 前綴 `ft-<8hex>`（各子命令 TX 紀錄為 `ft-<8hex>-<子命令>`）、`confirmed_tx_bytes` 為整段傳輸累計、`tx_outcome` 依同一規則判定，於映射當下（回應送出與 foreground 操作釋放之前）寫入；零寫出回 `FLUSHED_BY_RECOVERY` 時照舊不寫。一般命令與 file.* 共用同一個 META 寫入 helper。
- 收緊 Stage 1 驗收與 0.4.0 升級驗證：SC-18 以 bridge barrier／checkpoint 核對完整 TX wire 前綴；出口 driver 只豁免 gate 已判定的 OD-30～OD-32 Windows `BLOCKED-OWNER` 列；正式候選 bundle 隔離 Python runtime、自帶 PyYAML 且不編入測試故障注入 feature。升級容器不再安裝系統 PyYAML 或共享 site-packages，避免掩蓋一般 pipx 使用者缺少執行依賴。
- Stage 1 出口 G9b：補上 POSIX 回滾入口 symlink 的啟動回歸測試（sol 72a887e，Low）。新增 `crates/sw-cli/tests/rollback_entry_symlink.rs`：穩定入口 `bin/serialwrapd` 為指向回滾目標版本 launcher 的 symlink 時，on-demand 回滾的 `spawn_daemon` 必須以該入口啟動，測試直接斷言 spawn 的 argv[0]、入口解析結果與入口位元組，不再只依賴版本回報替身。spawn 路徑以原生 `Path` 比較，保留入口身分斷言並接受 Windows 的 `/`、`\` 等價分隔符表示。以暫時把入口判定還原為 `is_file()` 語意驗證測試會失敗（spawn 改用 CLI bundle launcher），驗證後已恢復。本次未變更產品碼 `crates/sw-cli/src/rollback.rs`。
- 修正 func-test runner 的未解析樣板檢查（`TestRunner._require_resolved`）把合法的 shell 參數展開誤判為樣板的缺陷：原本以 `\{([a-zA-Z0-9_.]+)\}` 掃描展開後的字串，`${HOME}` 這類 DUT 端 shell 形式會被判成未解析樣板並拋出 StepError，例如 `cmd submit --cmd "echo ${HOME}"` 在送出前即被拒絕。

  現行判準（`func-test/runner.py::_UNRESOLVED_TEMPLATE_RE`）：裸 `{var}`／`{var.field}`（前面不得緊接 `$`）與誤加 `$` 前綴的點號形式 `${var.field}` 仍視為未解析樣板並 fail-closed；`${NAME}`（不含點號）與 `$NAME` 為字面字串，原樣送出。`${var.field}` 含點號、不是合法 shell 識別字，因此仍予拒絕，避免 `${after_cmd.cmd_id}` 這類樣板因誤加 `$` 而漏檢。

  已知殘留（未於本次處理）：(1) 不含點號的 `${NAME}` 若實為未就緒的 harness 樣板，無法與 DUT 端 shell 參數從字串區分，會被視為字面字串放行；(2) `_expand` 仍會把 `${NAME}` 中的 `{NAME}` 替換為同名 save_as 變數的值，與 save_as 名稱同名的 shell 變數會被展開錯誤，此行為未變更。

  測試：`tests/test_functest_harness.py` 新增兩組案例，分別驗證 shell 形式（`${HOME}`、`$HOME`、`${A:-b}`）通過檢查，以及未解析樣板（含 `{after_cmd.cmd_id}`、`${after_cmd.cmd_id}`）仍拋出 StepError 且錯誤訊息不列出合法的 shell 形式。
- 穩定 Stage 1A Spike 3 的 Windows helper 證據：ready 記錄以同目錄暫存檔原子發布，consumer 對未完成記錄重試；四種主案例都先確認 helper 已持有現行 WAL 再注入操作。helper 啟動失敗仍輸出符合 schema 的 INSUFFICIENT_EVIDENCE，保留可診斷證據。
- 修正 Spike 3 在 Windows 使用粗解析度 `monotonic_ns()` 量測 append 的問題，改用 `perf_counter_ns()` 並將實際時鐘與解析度寫入證據；helper 到期釋放改以相同 QPC 截止值判定。oplock ACK 記錄 CloseHandle 呼叫區間，gate 對齊 helper 原始 break 記錄及延遲樣本。
- Spike 3「主／oplock 分開記錄」案例的替身改以虛擬 `perf_counter_ns` 推進 lease、break 與 rename 等待，判定不再依賴真實排程；失敗訊息另列 `lease_acquired_ns` 與 append 延遲樣本。
- Stage 1A gate 保留 Cargo 的實際 `FAIL`，並補上 provenance 缺漏與格式錯誤的回歸案例。
- 修正 Stage 1 exit closure 第二輪審查缺陷：handoff holder observer 在隔離 PID namespace 逐次掃描候選行程；bundle evidence reader 與 Rust launcher 一致拒絕不安全指標；Cargo provenance 綁定 repo toolchain 與乾淨 HEAD；Spike 3 以 no-delete share 作主注入並另記 oplock 變體。修正深層 checkout 下 handoff 測試的 AF_UNIX 暫存路徑，並同步 SC-11 的 TX fence timeout 契約與 P8 實作計畫。
- Stage 1 exit closure 第四輪補強 Spike 3 lease 握手與 oplock workload 證據、holder observer 的 zombie leader 判定及短 socket temp root，並保留 Cargo provenance 拒絕理由。
- 修正 Spike 3 unlink 案的 helper 啟動競態：ready 改為首次目錄掃描後回報已持有檔案，unlink 前先確認 helper 實際持有現行 WAL；避免在注入尚未生效時誤判干擾未發生。
- 修正 Stage 1B A/B harness 的 B 輪 daemon 路徑，改由 release manifest 解析版本安裝樹內 launcher；Windows COM autostart 案例也等待 passthrough session READY 後才送出唯一命令。
- systemd autostart hold 驗證失敗的撤銷路徑（`rollback_hold`）比照 release，best-effort 移除已空的 `<unit>.service.d`（user：`remove_dir`；system：`sudo rmdir --ignore-fail-on-non-empty`）；release 的 rmdir 失敗不影響結果改以腳本化非零 rc 的測試固定。
- systemd hold release 移除 drop-in 後，若 `<unit>.service.d` 已空則 best-effort 一併移除（systemd-user 經 `FsOps::remove_dir`；systemd-system 以 `sudo rmdir --ignore-fail-on-non-empty`），目錄內另有他人 drop-in 時保留；`FsOps` 新增 `remove_dir`（空目錄 `rmdir` 語意，預設回 `Unsupported`）。
- 修正 Windows COM 黑箱 runner：raw selftest 通過時執行 COM 與 B 輪診斷案例，並為隔離的 autostart 測試傳入 staging bundle manifest；核准 revision qualification 仍控制最終 PASS。
- `scripts/rust/win-blackbox.ps1`：`Stop-WindowsComBridge` 拋例外時（主流程 catch 與 finally 兜底）也先回寫本機 bridge 證據再刪 scratch，回寫錯誤附到 `bridge_stop`；B 輪結果另記回寫後的 `bridge_wire_output`，避免只留下指向已刪 scratch 的路徑。判定不變（例外路徑仍為 FAIL）。
- `win-blackbox.ps1` 的 COM 序列 bridge 改把 `--evidence-dir` 與 stdout／stderr 寫在 Windows 本機 scratch，停止後（啟動失敗時亦然）整份回寫 `OutputRoot\windows-com\bridge` 並逐檔比對 SHA256；回寫不完整時 bridge stop 降為 EVIDENCE-INSUFFICIENT。避免 `\\wsl.localhost`（9P）上 `os.fsync` 失敗（CRT 一律回 EBADF）使 bridge 未建立 `control.json` 即結束。
- Windows COM 寫入在 setter 或重複 DSR 取樣耗時時，會將實際量到的成本預留於呼叫期限內，再以最後一次 peer 樣本和剩餘期限決定是否寫入。新增延遲 DSR 路徑回歸，確認穩定 peer 可在期限內完成寫入，且不會設定超過剩餘預算的 pySerial timeout。
- 修正 Windows COM peer gate 在最後寫入檢查沿用重連前 settle 起點的競態。DSR low→high 後重新起算 200 ms settle；新回歸確認過渡期間不會把 banner 寫入 COM。
- 修正 sol ca0106f（Medium）：win-serial 端點的期限設定原本在取得讀寫串行鎖之前完成，並行呼叫會互相覆寫 pySerial 的 `timeout`／`write_timeout`，可能使第一筆寫入提早逾時並觸發 `HARNESS_WRITE_UNCERTAIN`；且 setter 會在另一方向 I/O 進行中呼叫，pySerial 會重設整個 COM handle。修正後寫入期限設定於雙向靜止（寫入鎖與讀取鎖皆取得）時執行，鎖序固定為寫入 → 讀取，且期限設定與本次寫入共用寫入鎖。讀取期限固定為開啟時的 50 ms；`read()` 以短讀取輪詢至呼叫期限，不再等待寫入鎖來改設讀取期限，使阻塞寫入期間讀取仍能排空資料。已補交錯測試（以 fake serial 記錄實際期限、資料排空與 setter 違規），既有 com0com 與 peer gate 行為不變。工具 tree 已變動，核准狀態見 `PENDING-OD-S1X-BRIDGE-REVISION` 之 G9 段落（未核准；`APPROVED_REVISION.json` 本輪未變更）。
- Windows `exec_or_wait` 以原始標準 handle 建立行程時，與背景 daemon spawn 暫時取消繼承旗標的區段串行；並新增呼叫端以單一 pipe 合併 stdout／stderr 時 `daemon start` 須立即 EOF 的原生案例。

  此鎖修正沒有決定性回歸測試：合併 pipe 案例只覆蓋 `spawn_daemon` 的去重路徑，拿掉 `exec_or_wait_opts` 的鎖仍會通過（`exec_or_wait_opts` 成功時以 `process::exit` 結束行程，無法在測試內重現並行視窗）。
- `win-com-txrx-bridge` 情境改依規格送 `echo W_1`～`echo W_50`，斷言每個 payload 在 wire 依序恰一次、50 個命令逐一 done（burst 送出後以第二個 `repeat` 逐一檢查），並新增 console bytes 原樣出現的檢查；func-test runner 的 `repeat` 提供 `{repeat.index}`，cli 的 `save_as` 可含參照逐次另存，並支援一層巢狀參照 `{w_{repeat.index}.cmd_id}`。舊情境以相同 payload 送 50 次卻預期 1 次，正確行為也會 FAIL。
- 修正 Windows Rust 黑箱 bundle 將 daemon launcher 留在 staging 樹外的問題，並讓 SC-18 原生案例明確綁定 COM0 至核准的 COM20 DUT。
- 修正 Windows Rust CLI 啟動 manifest 指定的 Python daemon shim 時，改以同一 manifest 的 Python runtime 執行 `.py` launcher。
- POSIX backend 的 `serialwrap setup --on-demand` 在同模式刷新時保留
  `config.yaml` 既有有效 TCP `socket_path`，包含 loopback SSH tunnel；
  不因 endpoint 暫時無法連線而改寫為本機 Unix socket。

  - 保存規則限定目前與目標都是 on-demand、未顯式指定 endpoint 的情境；
    systemd、模式轉換與 win backend 維持既有契約。
  - `./install.sh --on-demand` 可轉交模式旗標；未帶旗標的 auto 安裝在有
    systemd 時會轉換模式，不能視為本次已修復的遠端客戶端流程。
  - 顯式 `--socket`／`--endpoint` 仍供探測使用，setup 寫回 canonical
    socket，並非持久化旗標值。詳見 `docs/setup-tcp-endpoint.md`。
  - 原 PR 的四個新增案例改為隔離測試矩陣，補重跑冪等、一般 RPC 路由、
    斷線保存及各排除邊界；不使用真實 TCP endpoint 或系統控制指令。
  - policy engine 與 PR／release reusable workflow 同步釘選正式 v1.0.18；
    manifest 明確啟用 `branch_source.allowed_prefixes: [feature, fix]`，
    由中央 R-12 原生驗證既有分支契約，不注入自動豁免 label。
  - Policy Check 同時響應 PR 內文更新，R-11 以最新驗收勾選與 event context 重驗；
    不以舊事件快照重跑或豁免宣告檢查。
- 修正 `--source` 打錯或冒用時默默改變恢復行為的問題。外部命令入口的 human／agent 權限依 RPC 原始輸入判定：bare `human` 與 `human:`（沒名字，不分大小寫）正規化為 `human:cli`，依 OD-74 享有 human gate 與逾時升格；明寫 `human:<x>`（包括 `human:cli`）仍依 DF-02 比照 agent，不能由 `source_class` 或正規化結果授權。外部傳 `system:recover` 可能觸發 force clear + reattach，因此來源入口（`command.submit`／`file.push`／`file.pull`／`device.release`／`session.interactive_open` 的 `owner`，CLI 與 RPC 兩端）拒絕 `system`／`system:*` 並在入列與送 UART 前回 `SOURCE_RESERVED`。成功正規化的回應帶 `source` 與 `source_normalized_from`；`command.submit` 回應與 `command.get` 記錄另帶僅供顯示的 `source_class`。內部路徑（human console 行、`recover_session`、self-test、login probe）不經外部驗證。skill、README 與 spec 同步說明 raw-source 權限差異。
- Smart4 空行 prompt 辨識使用獨立且有界的一秒回應窗，不再被小於 reader 排程週期的 RX idle 門檻縮短；已解鎖 shell 不會因過短等待誤送 unlock。PTY fixture 保留短 idle 門檻，提供完整解鎖／nonce 的總時間預算。
- Smart4 shell prompt 後立即接非同步 Wi-Fi log 時，保留 hostname 行首與 `#` 邊界而不要求行尾，避免已完成命令誤判 timeout／partial 並注入 recovery 按鍵。出貨模板 PTY 測試涵蓋 nonce 與 marker 命令；實機驗證由 TestPilot ST-06 案例執行。
- 修正 #237 折行回顯剝除的誤判：舊判定把命令與輸出都去掉所有空白後比對，echo 關閉時若輸出剛好等於命令去掉空白（例如 `cat file` 印出 `catfile`），整行有效 stdout 會被當成回顯刪掉，不需要發生折行就會觸發。現在只接受「命令原文加上終端折行插入的字元」：命令本身的每個字元（含空白）都必須依序出現，只有換行（BusyBox ash）或多出的一個空白（bash readline）可以跳過，插入點彼此至少相隔 20 個字元，且回顯須結束在行尾。既有 BusyBox／readline 折行與帶 prompt 前綴的案例行為不變。
- 命令回顯剝除支援同一行中多次 readline 自動折行，並只在第一個換行前驗證及移除回顯標記，避免命令內容殘留在有效標準輸出中。
- 修正長命令回顯被終端折行拆成多行時殘留在 line mode `stdout` 的問題，同時保留命令中的有效空白。只有原始串流帶 BusyBox ash 的 `\r\r\n` 折行標記時才跨行接合；bash readline 的單行 `" \r"` 插入空白則由呼叫端只在辨識到該控制字元後處理。一般 stdout（例如 `cat file` 輸出 `catfile`，或普通換行輸出 `echo x `／`y`）不會因去除空白或跨行拼接而被誤刪。新增兩種反例回歸，既有 BusyBox／readline 折行測試仍通過。

### Security
- POSIX 開啟 UART 時取得非阻塞 flock 並設定 TIOCEXCL；開啟衝突會拒絕收送並回報可識別的 holder，掃描不完整時在日誌與 daemon status.foreign_holders 標示 holder_unknown。Windows 存取拒絕映射為 DEVICE_STILL_HELD。device.attach 改為同步取得獨占後才進入 ATTACHING，`--force` 不會略過實際開檔與鎖後 holder 檢查；release、rebind 與 hot-unplug 會等在途 PROBE 安全收斂後才交還 handle。動態 session 的 RELEASED 狀態跨重啟保留，裝置離線時不會降為 DETACHED。
- 修正 event 規則 ID 路徑穿越、`contains` flags 驗證與規則載入容錯；client 非 JSON 回應及壞 YAML 改以穩定錯誤回覆；修正 setup state 遷移目標、WSL sudo 閘門、實際命令記錄、minicom broker 路徑與 Windows skill link 複製退路。

## [0.3.2] - 2026-09-21

### Added
- 新增全域 `--bench <code>` 參數：以 `connect` 記住的 bench 代號解析 endpoint，讓後續命令免帶 `--endpoint`。
  
  - 端點解析優先序為 `--endpoint` > `--socket` > `--bench` > config fallback；`--bench` 指向尚未 connect 的代號時回明確錯誤，不靜默 fallback 到本機 daemon。
  - 同步修補 `daemon start`／`daemon stop`（含 systemd 模式 route）與 `remote` 路徑對 bench endpoint 的定址與錯誤處理。
  - README 內嵌的 `serialwrap --help` 區塊同步更新（R-16）。
- 新增 bench 佈署腳本（provider-specific，刻意放 `tools/` 不進 `sw_core`，維持 #185 provider-neutral）：
  
  - `tools/bench-issue.sh <code>`：管理端一鍵發代號——`tunnel create`／`route dns`／產 cloudflared config／打包 `handoff/<code>/` 憑證包／寫入 `benches.yaml`。domain 由 `--domain` 或 `BENCH_ISSUE_DOMAIN`／`SERIALWRAP_BENCH_DOMAIN` 提供，未給即非零退出。
  - `tools/bench-enroll.sh --bundle <dir>`：bench 端一鍵入列——安裝 cloudflared（帶 `NEEDRESTART_SUSPEND=1`）→ 佈署 `/etc/cloudflared/` → 以 `--config` 做 service install → 加 sshd 金鑰限定 drop-in 並 `reload`（不 restart）→ 追加 controller 公鑰 → 跑 CP-1/CP-2 檢查點。任一步失敗即非零退出，且不改動既有 serialwrap 設定。
  - 兩支皆支援 `--help` 與 dry-run，shellcheck 乾淨；`tests/test_bench_tools_cli.py` 以 pytest 包裝覆蓋參數解析與檢查點分支，不需實機。
- 新增 `serialwrap benches`：列出 `benches.yaml` 的各 bench 代號、`connect` 記住的 endpoint 與 tunnel alive 狀態，讓操作者不必自己去翻 `benches.state.json`。
  
  - `bench_registry` 新增 `load_configured_benches()`，供 CLI 直接取全部代號（`resolve()` 改為呼叫它，行為不變）。
  - endpoint 記憶改為嚴格驗證：非字串／空值／非 `tcp://`／非 loopback host 一律視為記憶損壞並回明確錯誤，不再靜默當成「未 connect」。
  - alive 判定改為比對記住的 endpoint 實際 port，而非 `benches.yaml` 宣告的 `local_port`。
  - `connect --code --close` 同步改用記住的 port 拆隧道；tunnel 仍存在時不清除 endpoint 記憶。
  - README 內嵌的 `serialwrap --help` 區塊同步更新（R-16）。
- 新增 `serialwrap connect <code>` 代號連線子命令：從 `benches.yaml` 解析代號，展開成等價的
  `remote -L` 參數並重用既有 spawn 路徑建立隧道，成功後把 loopback endpoint 記入
  `benches.state.json`；`--close` 拆除該代號的隧道並清除 endpoint 記憶。
  
  - 未知代號、參數錯誤一律走既有 `TunnelError` → CLI JSON `{ok:false,error_code}` 邊界，例外不穿越 CLI。
  - 建立失敗時回滾已開的隧道；拆除前驗證隧道歸屬，避免誤關他人或孤兒隧道。
  - endpoint state 更新序列化，避免併發寫入互相覆蓋。
- 新增 provider-neutral 的 bench registry 載入層（`sw_core/bench_registry.py`），讓
  `benches.yaml` 可定義 `target`、`remote_socket`、`local_port`、`ssh_opts`、
  `autossh`，並強制 schema 驗證：拒絕 provider 專屬欄位、要求 `user@host`、
  保留 `ssh_opts` 原樣與 `SERIALWRAP_BENCHES_FILE` 覆寫行為。
- `sw_core/bench_registry.py` 新增 `to_remote_argv(entry)`：把 `BenchEntry` 展開成等價 `serialwrap remote -L` 的 argv（`--remote-socket`、逐項 `--ssh-opt`、`--autossh`、`user@host:port`），供 `connect` 重用既有 spawn 路徑而不重造隧道建構邏輯。

### Changed
- CLI 新增最小控制平面 RPC trace：`-v`/`-vv` 與 `SERIALWRAP_LOG_LEVEL` 可在 stderr 輸出單行白名單 JSON，記錄實際 RPC method、endpoint 來源、遮罩後 endpoint 識別、elapsed、error_code、retry 次數與最後一次主請求 attempt 的 errno。
  
  - 新增專用 `serialwrap.cli_trace` logger，`propagate=False` 且 handler 冪等，不污染 root / `serialwrap` logger；預設 `WARNING`，因此既有 stdout/stderr 契約維持不變。
  - `_run_rpc()`、event 路徑、`daemon stop` 與 `daemon start` 的**前置 health probe／就緒等待** CLI RPC 呼叫統一走同一個 trace wrapper；`event add` 會正確記錄真正送出的 `event.rule_set` method。
  - `-v` / `--verbose` 為公開 help 旗標，README `serialwrap-help` marker 同步更新，不再以隱藏旗標繞過 R-16。
  - trace wrapper 不再因 `trace_sink` 相關 `TypeError` 退回第二次 `rpc_call()`；mutating RPC 維持無額外重送契約。
  - Unix socket path 一律 SHA-256；TCP 僅對 loopback literal 保留 `host:port`。trace 不輸出 params、command、secret、raw endpoint path 或完整 response。
  - `sw_core.client.rpc_call()` 新增內部 metadata sink：`elapsed_ms` 含既有 retry/backoff/TIMEOUT enrich，`errno` 僅取最後一次**主請求** attempt 的 `OSError.errno`，因此 retry 後 success 與 TIMEOUT enrich 失敗都不會留下錯誤 errno。
  
  **regression-case 評估**：新增 `tests/test_cli_diagnostics.py`，搭配既有 CLI / endpoint / timeout / event / Windows seam 測試即可完整覆蓋；此變更只碰 client/CLI transport seam，不依賴真板、真 daemon 或外部工具搶 tty，故不需新增 `regression/` 真機 case。
- 釐清兩條對外契約的邊界，皆為純文件變更、不動實作。
  
  - **`file push` 的檔案大小與通道界線（#188）**：`SKILL.md` 的「避免 base64 inline」原本只寫
    「改用 `serialwrap file push`」，照這條規則走 agent 會把整包 firmware image 也交給
    `file push`。實測一顆 81 MB image 走 SCP 經 LAN 數秒完成；同一顆走 UART 115200
    （原始上限約 11 KB/s，echo-ACK 節流後更低）即使通道穩定也需數小時。現在明訂
    `file push` 適用**數十 KB 內的小檔**（設定檔／腳本／探針），DUT 有 SSH／TFTP／HTTP
    通道時大檔一律走 SCP／TFTP、serialwrap 只負責控制面，`file push` 用於大檔僅能是
    「確認無網路通道後」的明確 fallback。`README.md`（中英雙語）與 `SKILL.md` 的範例
    一併從 `./firmware.bin` 改為 `./probe.sh`——原範例本身正是要避免的反面示範。
  - **`serialwrap remote` 的責任邊界與拓樸模型（#185）**：明訂 `serialwrap remote`
    **不實作 NAT traversal**，只在一個本來就可達的 SSH target 上管理 forward lifecycle；
    可達性由外部 reachability provider 提供（LAN／Cloudflare Zero Trust／Tailscale／
    WireGuard／ZeroTier／VPN／jump host），新增責任分層圖。拓樸重新分級：overlay 已提供
    互相可達性時，`UART host -R→ agent` 為 **preferred**（即使兩端皆在 NAT 後也不需要
    relay 或成對的 `-L`）；public relay 的 `-R/-L` 鏈降為兩端完全互不可達時的
    **fallback**，不再是雙 NAT 的唯一描述。明文記錄刻意**不提供** `--cloudflare`／
    `--tailscale` 之類 provider-specific 旗標、不引入 provider SDK 或執行期依賴——
    provider 細節屬於 `ssh_config` alias／`ProxyJump`／`--ssh-opt`。既有 loopback bind
    不變量、`--remote-socket` 硬化與單租戶 relay 要求在所有拓樸下維持不變。
  
  同時涵蓋 #185。`code_paths`（`**/*.py`／`**/*.sh`／`scripts/**`）未變動，本 fragment
  為自願記錄以利 release 收斂。
- 補上 `serialwrap remote` 的 **agent-pull** 形狀：測試覆蓋、文件範例，以及兩處會誤導的敘述。
  不改 `sw_core/` 任何實作——現行程式碼本來就支援，缺的是驗證與文件。
  
  - **agent-pull 形狀**（`serialwrap remote -L --remote-socket <UART host 的 socket> user@dut:7777`）：
    agent 主動 ssh 進 UART host、把對方的 `serialwrapd.sock` 拉回自己的 loopback，
    **UART host 端不需跑 `-R`、不需任何 relay**。這是「開發機要連一台在 NAT 後的 bench」
    最自然的形狀，搭配把 SSH 架在 overlay 上的 provider 即得到兩端皆只有出站連線的路徑
    （遠端桌面工具用的模型），且不必自己營運 relay。
    `sw_core/remote_tunnel.py` 早已支援——`_direction_args()` 的 connect 分支對「對端是誰」
    無任何假設，全檔亦無強制 `-L` 必須配 `-R` 的檢核——但三個既有 docker 拓樸
    （`direct`／`nat_host`／`dual_nat`）**全是 UART host 用 `-R` 推出去**，方向相反的
    agent-pull 從未被測過，README／SKILL 也沒有任何範例。
  - **新增第 4 個 docker 拓樸 `agent_pull`**（`tools/docker/remote_tunnel_test.sh`）：
    方向反轉，sshd 改跑在 uart 容器、agent 容器主動連入。斷言粒度比照 `topology_direct()`
    ——①預設不啟用 ②session list＋cmd submit/status ③daemon pid 全程不變
    ④close all 後乾淨復歸 ⑤loopback 不變量＋獨立 attacker 容器連不到。
    `realhw/cases/remote.py` 註冊 `rm-topo-agent-pull`（`remote` tier ×7 → ×8）。
    - 兩個新 helper：`uart_sshd_up()` 強制以 `-u root` 在 uart 容器起 sshd
      （`start_uart` 用 `docker run -u tester`，`docker exec` 不帶 `-u` 會繼承該非 root
      使用者、讀不到 0600 的 host key 而 `no hostkeys available -- exiting`；既有
      `sshd_up()` 只用在 `start_plain`/`start_role` 建的 root 容器上，未踩過此坑）；
      `uart_socket_path()` 讀 uart 容器的 `sw-uart.env` 推算 `RUN_DIR/serialwrapd.sock`
      （`DaemonHarness` 用 tempdir，路徑每次啟動都不同，不能寫死）。
  - **`BatchMode=yes` 為強制且不可覆寫**（README 中英雙語＋SKILL.md）：readiness／安全預設
    `-o` 置於使用者 `--ssh-opt` 之前，OpenSSH 取同鍵第一個值，故蓋不掉（刻意如此）。
    後果是不接受任何互動式認證；provider 憑證會過期時（如 Cloudflare Access token），
    過期會讓 ssh 直接失敗且無有用診斷，無人值守需用 service token 或事先 refresh。
    此限制先前完全沒有記載。
  - **修正 `--remote-socket` 的配對敘述**：原文「`-R`／`-L` 兩端須成對指定同一路徑」的脈絡
    是共享 relay 的硬化情境，但照字面讀會讓人以為 agent-pull 不合法。改為明確界定該配對
    要求只適用 relay 類形狀。
  - 順帶修掉 `role_exec` 一段「connect 端（-L，僅拓樸 3 用）」的過期註解（拓樸 4 也用它）。
  
  **regression-case 評估**：本變更的驗證面**就是** `regression/`／realhw 這一側——新增的
  `rm-topo-agent-pull` 即為回歸 case，涵蓋 pytest 無法覆蓋的部分（真 sshd、真 ssh forward、
  真 unix socket 轉發、跨容器隔離）。不需另加 pytest：`sw_core/` 零改動，無新的 in-process
  行為可測；既有 `tests/test_remote_tunnel.py`／`test_remote_cli.py` 已覆蓋 argv 組裝與 CLI 解析。
- README（中英雙語）與 `SKILL.md` 新增「以 Cloudflare 當 reachability provider」的可操作實例。純文件變更，`sw_core/` 零改動。
  
  - **情境**：agent 跑在本機、bench 在遠端，兩端皆在 NAT 後；本機不裝 overlay client、不對 bench 建立系統級 ssh 關係——只有 `serialwrap remote` 派生的那條 ssh 過去；不自養 relay VPS。這是 #193 拓樸二（agent-pull）的實例化：Cloudflare Tunnel 讓 bench 的 sshd 以 hostname 可達，bench 只跑 `cloudflared`、**不跑 `serialwrap remote`**；本機的 `cloudflared` 只是 serialwrap 那條 ssh 的 per-connection `ProxyCommand` helper，透過 `--ssh-opt` 傳入而非寫進 `~/.ssh/config`。
  - **兩條路線**：Quick Tunnel（無帳號無網域，hostname 每次重啟換，`cloudflared tunnel --url ssh://localhost:22` 一行）與 Named Tunnel 不開 Access（帳號裡有網域，hostname 固定，`tunnel login` → `create` → `route dns` → `config.yml`）。本機指令兩條路線相同。附對照表、bench 一次性前提（sshd key-only／pubkey／`dialout`）、以及「先用純 ssh 驗 ProxyCommand」的里程碑。
  - **刻意不寫 Access**：它是把服務發佈給組織的企業形狀工具，對「我連我自己的 bench」是錯的量級；其瀏覽器登入 token 會過期、撞上 serialwrap 強制的 `BatchMode=yes` 會靜默失敗。只註明日後要加需用 service token。
  - 附自養 relay（拓樸三）與 tmate 的一句話對照，避免讀者再繞一次：前者功能等價、差在誰養會合點；後者同為「別人養 relay＋token 會合」但只橋 terminal 不傳 socket。
  - 寫進文件的 `cloudflared` 旗標（`tunnel --url`／`access ssh --hostname`／`--service-token-id`／`--service-token-secret`／`tunnel route dns [TUNNEL] [HOSTNAME]`／`service install`）已對 cloudflared 2026.8.3 的 `--help` 逐一核對；`ssh://` ingress scheme 以離線 `tunnel ingress validate` 驗證為 OK。未實際開 tunnel（會把本機 sshd 曝到公網），Cloudflare 端到端待帳號加上網域後由使用者實跑。
- README（中英雙語）與 `SKILL.md` 的 Cloudflare 路線 B（Named Tunnel）補上 2026-09-16 真機實跑發現的兩個坑，並註明已驗證版本。純文件變更，`sw_core/` 程式碼零改動。
  
  - **`tunnel login` 要選 zone 再按 Authorize**：只登入 Cloudflare 帳號不會產生 `~/.cloudflared/cert.pem`，cloudflared 會一直印 `Waiting for login...`。
  - **`sudo cloudflared service install` 找不到設定**：sudo 之下 `~` 是 `/root`，直接執行報 `Cannot determine default configuration path`。步驟改為把 `config.yml` 與該 tunnel 的 `<UUID>.json` 搬到 `/etc/cloudflared/`、改寫 `credentials-file` 路徑，再以 `--config` 明確指定安裝，並以 journal 的 `Registered tunnel connection` 當檢查點。
  - 註記路線 B 已以 cloudflared 2026.9.1 於 WSL2 Ubuntu 24.04 bench 端到端實跑（純 ssh 往返約 3 秒、`cmd submit` 取得真實 UART 輸出），對應 #197 Phase 2／Phase 3 的 Named Tunnel 部分；Quick Tunnel（Phase 1）與重開機測試仍待做，故本 PR 不關閉 #197。
- README（中英雙語）新增「新 bench 佈署檢查清單」：把原本分散在 Cloudflare 實例、bench 代號、發代號與入列三節的資訊，收斂成一份可逐項打勾的操作清單——每網域一次的帳號前提、新 bench 在跑腳本前要自備的條件（含 socket 路徑確認與監管模式選擇）、三條佈署指令、controller 端驗收步驟，以及「已跑 Named Tunnel 的 bench 不可裸跑 `cloudflared tunnel --url`」的陷阱。
- 回填 Cloudflare 路線 A（Quick Tunnel）的真機實跑結果與一個新坑：bench 若已跑 Named Tunnel，裸的 `cloudflared tunnel --url ssh://localhost:22` 會沿用 `/etc/cloudflared/config.yml` 的 `tunnel:`／`credentials-file:`，起出來的是**該 Named Tunnel 的第二個 connector** 而非 Quick Tunnel——仍會印出 trycloudflare hostname，但該 hostname 一律回 `websocket: bad handshake`，且 production hostname 會被 edge 分流到這個多出來的 replica。必須以 `--config <空檔>` 隔離，並檢查啟動的 `Settings: map[...]` 不含 cred-file。同步標註路線 A 已驗證版本與延遲數據（README 中英兩段＋SKILL.md）。
- 架構事實層（#200）：
  - 新增 serialwrap 來源固定的原生 Archify 流程架構 HTML 與 facts／JSON，區分命令呼叫鏈、Session、lease、背景結果及實體證據。
  - 補齊中英 README 入口、文件負控制與只讀 Architecture HTML CI；不改 runtime、不部署 github.io，清除中斷留下的一次性傳輸檔。
  - 交付驗收包含來源雜湊核對、原生 HTML 逐位元組重建及 CI 中實際 file:// 瀏覽器測試；驗證紀錄與最終 CI 狀態附於 PR，並與使用者流程驗收、實機 E2E 分開記錄。
- 文件對齊 bench 代號層：README 中英雙語新增「bench 代號」與「發代號與入列」兩節（`connect`／`benches`／`--bench` 用法、`benches.yaml` 五個必填欄位 schema 與 provider-neutral 約束、endpoint 記憶與解析優先序、`tools/` 兩支佈署腳本的職責與檢查點）；`docs/serialwrap-spec.md` §11.1 補 `--bench` 與 `remote`／`connect`／`benches`；`sw_core/assets/skill/SKILL.md` 補精簡版供 agent 取用。
- 同步 hamanpaul project policy 1.0.15 → 1.0.17：`.project-policy.yml`、`Policy Check` workflow **與** `Publish Release` workflow（`uses:` 與 `policy_engine_ref` 雙重釘選至 `9e7fabbf0b5eea9ad933fa6798764b723934a0b7`）、canonical `CLAUDE.md`（symlink `AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md` 自動跟隨，含內文兩處內嵌 pinned SHA 安裝指令引用）全數同步至 v1.0.17。1.0.16／1.0.17 對下游 repo 未新增或變更任何規則，僅上游引擎自身的 distribution identity、runtime bundle 與 release workflow 修正，故本次為純版本同步；其中 1.0.16 引入的引擎版本 gate（執行中引擎版本與 repo 宣告的 `policy_version` 不符即 fail-loud）是本次同步的實益。

### Fixed
- 檔案傳輸新增 base64／OpenSSL 雙向工具 fallback 與可選的 `max_console_line_chars` 單行預算；
  以實際成功 sentinel 驗證 probe、chunk、checksum 與搬移，避免命令回顯或 prompt 假成功。
  profile 不足以容納固定命令時會在 TX 前回報 `CONSOLE_LINE_LIMIT_TOO_SMALL`。
  preflight 的驗證 payload 受實際檔案長度封頂；pull 的 encoder 失效、partial failure
  marker、GNU escaped `md5sum` 與 YAML target budget 物化均有回歸防線。
- `session pin` 對既有 session 恢復生效，`others-template` fallback 不再是沒有出口的單向門。
  
  - **根因**：#95 建立的四層優先序（pin > sticky > detect > fallback）只實作在
    `_attach_by_id_dynamic`——也就是「該裝置**從未**建過 session」的路徑。既有 session 走
    `_attach_by_id`，它以 `device_by_id` 找到 session 後就原封不動沿用 `session.profile`；
    而 `clear_session` 只 detach bridge、**不刪 session 物件**，於是 re-attach 又回到
    `_attach_by_id`。結果是 `session pin` 被接受、被寫進 `state.json`（key 正確），然後
    在 attach 時被完全忽略——連 `session clear` 強制重新 attach 也一樣。pin 的說明是
    「最高優先，繞過偵測」，是使用者唯一被告知的逃生口，而它是壞的；配上 fallback 的空
    `ready_probe` 讓 `command_capable` 永久 false，掉進 `others-template` 後**沒有任何 CLI
    途徑**能救回，只能改設定檔再重啟 daemon（#182 描述的代價）。
  - **修法**：新增 `_reresolve_profile_on_reattach()`，在 `_attach_by_id` 開 bridge **之前**
    重新解析既有 session 的 profile：
    - **pin**：命中且與現行 profile 不同即改用該 template，`profile_source` 標 `pin`。pin
      存在即為最高優先，命中與否都不再往下偵測（維持「繞過偵測」契約，不開 PROBE bridge）。
    - **fallback 視為暫時分類**：`profile_source == "fallback"` 是**未經量測**的結果
      （attach 當下板子還在噴 boot log、`last_probe_at` 為 `null` 就會掉進來），因此下一次
      attach 以獨立 PROBE bridge 再偵測一次（與 `_attach_by_id_dynamic` 對稱）。
      `yaml-target` / `sticky` / `detected` 是宣告過或量測過的，一律不重解析。
    - 偵測會送 `\r`，故 boot quiet window（#130 U-Boot autoboot 保護）內跳過；session 已有
      bridge 時也跳過（避免再開 PROBE bridge 造成 two-reader）。
  - **`_rematerialize_profile_locked()`**：原地 mutate 既有 session 物件而非重建——
    `retained_consoles`（保留給 human minicom 的 PTY）、boot quiet 狀態等執行期欄位都掛在
    該物件上，重建會把正掛著的 human console 一併丟掉。保留 `com` / `act_no` /
    `device_by_id`；alias 只在仍是自動產生的 `<profile_name>+<act_no>` 形式時跟著改名，
    使用者自訂過的一律保留。`session_id` 依新 profile 名重算（`others-template:COM2` →
    `prpl-template:COM2`），並同步搬移 `_sessions`（以 comprehension 重建以保留插入順序，
    #186 的 tiebreak 看得到它）／`_binding_overrides`／`_reprobe_probe_locks`／
    `_loaded_released`／alias registry。新 session_id 已被別的 session 佔用時不動作，
    避免把兩個 session 併成一個。
  - **`self-test` 給得出可執行的出口**：`profile_source == "fallback"` 的 PASSTHROUGH 分類
    改為把最近的 RX 餵給所有非 passthrough template 的 `prompt_regex`（`_suggest_profile_from_rx`，
    與 `serialwrap profile test` 同源的判斷），命中則回 `recommended_action: "pin_profile"`
    ＋ `suggested_profile` ＋可直接照抄的兩行 hint，而不是只回 `console_attach`。
    `others-template` 這類 passthrough template 的 `prompt_regex` 是 `.*`、恆真，一律排除在
    建議之外。顯式宣告的 passthrough（`yaml-target`，如 `uboot-template` target）不勸退。
  - **`PROFILE_NOT_COMMAND_CAPABLE` 的 hint** 改為寫出可用的動詞（`session self-test` →
    `session pin` → `session clear`），並明講 `command_capable` 純粹由 `ready_probe` 決定、
    **與 session state 或 console 是否被佔用無關**——#181 的排查一開始正是被「COM2 有
    console 佔用所以不能下命令」帶偏（實測 COM1 同樣掛著 console 卻能正常 `cmd submit`）。
  - **文件**：`README.md`（中英雙語）的 profile 解析優先序補上「每次 attach 都重新套用」與
    fallback 的暫時性；`session pin / unpin` 段落原本寫「對已存在的 session 下次 daemon
    重啟才生效」已過期，改為 `pin` + `session clear` 即可。`SKILL.md` 補 fallback 出口 SOP
    與 `command_capable` 的判準澄清。
  
  **regression-case 評估**：新增 `tests/test_profile_reresolve_on_attach.py`（22 個 pytest，
  修復前全數可重現地 FAIL）——涵蓋 pin 對既有 fallback session 生效、session_id 重算與
  COM／act_no／alias 保留、pin 命中不開 PROBE bridge、跨重啟、`yaml-target` 不受影響、
  未知 profile no-op、session_id 佔用時不合併、fallback 再偵測、偵測仍失敗時維持原狀、
  boot quiet 內不偵測、`detected` 不重偵測、已有 bridge 時不偵測、`_attach_by_id` 確實接上
  再解析，以及 `self-test` 的建議與 hint 文字。全部為 in-process session-state 與純函式
  邏輯，unit/mock 已完整覆蓋，**不需**新增 `regression/`（TestPilot 實機）case——本修復
  不依賴真板 boot 時序、USB 列舉順序或外部工具搶 tty。
- explicit `targets` binding 的 `device_by_id` 現在對其他 target 排他：restart 後不再受
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
- WAL 檔案不存在時不再靜默回 `ok:true` ＋ 空陣列；daemon 會自癒重建被刪掉的 WAL 目錄。
  
  - **事故**：daemon 同一 PID 連續運行六天，`log tail-raw` / `log tail-text` /
    `wal export` 全部回 `ok:true` ＋ 空陣列 ＋ rc=0，而 `current_seq` 已累加到
    1,261,000——WAL 目錄被外部工具 rmtree 掉了（testpilot 的 `clean_wal()`，已於
    hamanpaul/testpilot-core#36 追蹤），服務對此毫無所覺、也不告訴任何人。該 bench
    六天的 console 紀錄因此無法回溯，兩輪事故取證落空。`doctor` 的 `wal_dir` 檢查
    當時回的是 ok:true——它只比對 shell/daemon 的 WAL_DIR 是否一致並印出路徑，
    **從不檢查該路徑是否存在**。
  - **機制修正**：issue 原文推測是「daemon 對著已 unlink 的 fd 續寫」。實際不是——
    `append()` 每筆都重新 `open(path, "a")`，被刪掉的是**整個目錄**，於是每次 append
    都以 `FileNotFoundError` 失敗、被既有的 `except OSError` best-effort 分支吞掉
    （#79 STA-1 的「稽核寫入失敗不得讓 RX thread 崩潰」），而 `_seq` 照常累加。
    告警確實有發，但走 `logging` 而沒有任何 log 落地（正是 #171 的論點）。
  - **自癒**：`append()` 撞到 `OSError` 時先 `os.makedirs(wal_dir, exist_ok=True)`
    並重試一次；成功即續寫並告警（記 `recreated_count`），重試仍失敗才維持既有的
    best-effort 標 loss 行為（記 `write_failures` / `last_write_error`）。因為每筆
    append 都重開檔，目錄被刪之後**下一筆寫入**就會偵測到並修好，不需要另開週期性
    自檢執行緒。
  - **讀取路徑誠實化**：`WalWriter.health()` 攤開 `wal_dir` / `wal_path` /
    `wal_dir_exists` / `wal_file_exists` / `wal_dir_writable` / `current_seq` /
    `write_failures` / `last_write_error` / `recreated_count` / `healthy`。
    `log.tail_raw` / `log.tail_text` / `wal.range`（＝`serialwrap wal export`）在
    「`current_seq > 0` 但現行檔不存在」時回 `ok:false` ＋
    `error_code: "WAL_MISSING"` ＋ 實際 `wal_path` ＋ 可行動 hint。
    **`current_seq == 0` 不誤報**——全新 daemon 尚無 UART 流量時檔案本來就還沒建立。
    成功路徑也一律帶 `wal_path` / `wal_file_exists`，讓呼叫端分辨「查得到但沒有符合
    的紀錄」與「稽核檔案不見了」。
  - **輪替可分辨**：`wal.range` 另回 `available_from_seq`（現行檔最小 seq）與
    `rotated_out`（請求區間是否落在已輪替掉的範圍），使「這個區間本來就沒有紀錄」與
    「曾經存在但已被 rotate 掉」不再都只是空陣列。
  - **`doctor` 新增 `wal_writable` 檢查**（消費 `health.status` 新增的 `wal` 欄位）：
    實際驗證目錄存在且可寫，**非 advisory**——稽核紀錄整個消失必須拉低 doctor 整體
    ok。既有 `wal_dir`（shell/daemon 一致性）維持 advisory WARN，兩者各司其職。
    `tests/test_doctor.py` 的兩份 pinned 清單同步更新。
  
  **契約變更**：WAL 檔案缺失時三條讀取路徑由 `ok:true` 改為 `ok:false` ＋
  `WAL_MISSING`。這是本 issue 的核心訴求（先前的靜默成功正是讓事故延續六天的原因）。
  只讀 `records` / `lines` 的呼叫端行為不變（仍是空的）；檢查 `ok` 的呼叫端會開始看到
  失敗——那正是期望的行為。
  
  **regression-case 評估**：新增 `tests/test_wal_missing_detection.py`（19 個 pytest，
  修復前 18 個可重現地 FAIL）——涵蓋 health 欄位、rmtree 後自癒重建並告警、重建後可再
  讀、重建失敗時標 loss 且不崩、`available_from_seq`、三條讀取路徑回 `WAL_MISSING`、
  全新 daemon 不誤報、成功路徑帶 `wal_path`/`wal_file_exists`、`rotated_out` 判定、
  `health.status` 暴露 wal 健康，以及 doctor `wal_writable` 的四種情形與非 advisory
  性質。全部可用 tmpdir ＋ `shutil.rmtree` 精確模擬，**不需**新增 `regression/`
  （TestPilot 實機）case——本修復不依賴真板時序或外部工具。
- Session ownership 補強（#198）：
  - 補強同一 session 的 foreground/file transfer admission 與 operation
    epoch cleanup；競爭 writer 會以 `SESSION_BUSY` 拒絕且不產生 UART TX，舊
    bridge callback 不會清除新 epoch 的 busy 狀態。
  - 操作期間的新 POSIX／TCP human console 延後取得 raw ownership；raw write 在
    實際寫入前重驗 admission，已接收輸入進 bounded deferred buffer。收尾在
    manager lock 外回放，保留 agent interactive 接管與 FLASHING 優先丟棄政策。
- #198 補充 review fix：未知 `SERIALWRAP_LOG_LEVEL` 安靜回退 `WARNING`，數字值固定最多
  4300 位（不計合法負號），超限不受 Python 整數字串轉換限制設定影響；`setup` 的既有
  `health.ping`／`mcu.status` RPC 接入 CLI trace 且保留原 probe 順序、timeout、best-effort
  與 `FLASHING_BUSY` 行為；F7 對明確 `CHECKSUM_MISMATCH` 回報
  `FAIL/test/binary_roundtrip_mismatch`，不再被工具探測結果降為環境 `SKIP`。
  `rpc_call()` 的 trace sink 例外也採 best-effort，不覆蓋既有 RPC 結果或重試／enrich 行為。
- 修復 `SessionManager._probe_external_holder` 在非 POSIX 或 Windows 環境下因 `os.stat` 結果缺乏 `st_rdev` 屬性而拋出 `AttributeError` 的問題。
  
  - **根因**：`_probe_external_holder` 直接存取 `os.stat(real_path).st_rdev` 與 `os.stat(fd_path).st_rdev`，原僅以 `try ... except OSError` 包裹。在非 POSIX / Windows 等缺少 `st_rdev` 屬性的環境下，直接存取會引發未捕獲的 `AttributeError`，中斷 holder 探測與狀態判斷。
  - **修法**：兩處皆改以 `getattr(..., "st_rdev", 0)` 防禦性取得。若值為 `0`，依契約不視為裝置同一性證據（由既有 `if not matched and target_rdev:` 排除），避免跨裝置誤配；同 path 字串比對及正常 POSIX char-device 裝置比對維持原有邏輯。
  - **測試與邊界**：新增 `tests/test_holder_probe_portability.py`，驗證 endpoint 缺 `st_rdev`、fd 缺 `st_rdev`、正常 POSIX `st_rdev` 匹配、不同 `st_rdev` 不誤判、同 path 匹配、`rdev=0` 不誤判、無 `/proc` 安全回傳空結果等情境。
  - **regression-case 評估**：本變更屬 in-process 平台相容屬性存取，pytest 邊界測試已完整覆蓋；真實 Windows 序列埠與外部 flasher 斷線為既有實機回歸項目，本輪不假稱實機通過，無需新增 `regression/` 案例。
- Windows（nt）上 `wal reset` 與 WAL 大小輪替不再對目錄做 POSIX fsync：`os.open(<dir>)` 在 Windows
  會拋 Permission denied，導致 `wal.reset` RPC 回 EXCEPTION（檔案已輪替但 seq 未歸零）、
  每次輪替誤記「WAL 輪替失敗」警告。兩處抽成 `_fsync_dir()`，nt 跳過（比照 session_manager
  #84 PORT-4 守衛），POSIX 行為不變。

## [0.3.1] - 2026-08-11

### Fixed
- CLI 失敗時 stderr 不再丟棄具體錯誤原因（#172）。
  
  - `_run_rpc`：原本 `resp.get("error_code") or resp.get("message")` 的 `or` 短路，讓有
    `error_code` 時 `message` 永遠不會出現在 stderr——對 `SOCKET_ERROR` 來說，`message`
    正是 `str(OSError)`（errno 與連線細節），三種截然不同的根因（socket 路徑算錯／daemon
    已死／權限不符）被壓成同一個字串，只讀 stderr 的呼叫端（如 testpilot）事後無法定案。
    現在 stderr 一行同時輸出 `error_code`、`message`（有值才追加）與 `hint`（有值才追加），
    形狀為 `serialwrap: {method} failed: {code}[: {message}][（hint: ...)]`；
    `serialwrap: {method} failed: {code}` 這段前綴逐字保留，既有以 substring 比對
    `failed: SOCKET_ERROR` 之類的下游不受影響。
  - 新增 `_mirror_err()` helper，補齊先前只 `_print()` JSON 到 stdout、exit 非零時 stderr
    卻全空的多個出口：`daemon start` 的 `--endpoint` 拒收 / `--endpoint`,`--socket` 不一致 /
    systemd 監管模式重導 `service start`（含 `NEEDS_SUDO`）/ Windows 找不到 daemon binary /
    env file 載入失敗 / daemon 提前結束 / daemon 逾時未就緒；`daemon stop` 重導
    `service stop` 與 on-demand `daemon.stop` 失敗；`event` 未知子命令與各 `event.*` RPC
    失敗；`remote`（native Windows 不支援 / tunnel 例外 / 未預期例外）；`setup` 的
    `FLASHING_BUSY` 兩處；`service` 子命令失敗；未知頂層子命令的 `INVALID_ARGS`。stdout
    的 JSON 契約與 `ok:True` 路徑完全不動。
  - 對應實地事故：systemd-system 模式未帶 `--with-sudo` 執行 `daemon start`
    時，呼叫端原本只拿到 `serialwrap: daemon start failed: ` 這種冒號後空白的字面，
    現在會看到 `NEEDS_SUDO` 與代跑提示（hint）。
- 修正 daemon endpoint 無法被非 wrapper client 發現的問題（#173）：POSIX 上
  `serialwrapd` 過去只有 Windows 後端才會把實際 bind 的 socket 寫回
  `config.yaml`（理由是「POSIX on-demand 模式下 CLI 一定算得出同一個
  `SOCKET_PATH` 預設值」），但部署 wrapper 若以 `SERIALWRAP_STATE_DIR` 把 socket
  搬離 XDG 預設路徑，任何不經過該 wrapper 的 client（例如其他工具內嵌的
  venv、CI runner）就永遠連不到健康運作中的 daemon，只會拿到
  `SOCKET_ERROR`，且沒有任何診斷指出真正原因；更危險的是，這類「探測失敗」
  的 client 若接著自己觸發 on-demand `daemon start`，會在 XDG 預設路徑另外
  spawn 出第二個 daemon，兩個 daemon 同時開同一批裝置（two-reader）。
  
  三處修法：
  
  1. **daemon.py**：拿掉 `_write_config_endpoint()` 呼叫外層「僅 Windows」的
     gate，改為 server 啟動成功後全平台無條件寫入 `config.yaml` 的
     `socket_path`（`_write_config_endpoint` 本身不變，仍是 best-effort、失敗
     只記 stderr、不影響 daemon 運行）。讓「daemon 實際綁在哪」有一個與呼叫端
     環境變數無關的單一事實來源。
  2. **doctor**：新增 `endpoint_reachable` 檢查（非 advisory，會拉低整體
     `ok`）。以與 CLI 相同的解析 seam 得出本 client 會連上的 endpoint 與其來源
     （`config.yaml` 或平台預設），並掃 `/proc` 找出實際執行中的 `serialwrapd`
     行程與其 `--socket` 引數：沒有任何 daemon 行程時視為 on-demand 模式正常
     （advisory ok）；有行程但本 client 解析到的 endpoint 連不上時明確判定
     `not ok`，`detail` 同時列出本 client 解析到的路徑（含來源）與實際執行中
     daemon 綁定的路徑，方便一分鐘內定位落差，而不是像本次事故那樣耗掉一整個
     下午。
  3. **`serialwrap daemon start`（on-demand spawn 防線）**：spawn 新 daemon
     前先掃 `/proc`，若已有 `serialwrapd` 行程且其 `--socket` 與本次 spawn
     目標不同，預設拒絕（`error_code=DAEMON_ALREADY_RUNNING_ELSEWHERE`，訊息
     帶出兩個路徑與修復提示），避免同一批裝置被兩個 daemon 同時開啟；新增
     `--force-spawn` 供已確認情境下跳過此防線。既有「同一個 socket」的冪等
     探測路徑不受影響。
  
  `sw_core/multi_open.py` 的 `detect_multi_open()` 同步擴充：每個偵測到的
  daemon 項目現在附帶從 cmdline 擷取到的 `socket`（供上述 doctor 檢查與 spawn
  防線共用；擷取不到時為 `None`，呼叫端須視為「無法判定、保守處理」）。
  
  README.md 與 `docs/serialwrap-spec.md` 補充「自訂 wrapper 若以
  `SERIALWRAP_STATE_DIR` 搬移 socket，必須讓 config.yaml 同步（本修正後
  daemon 啟動即自動寫入，不需再手動處理）」的說明。
- login FSM 硬化：`POST_LOGIN_CMD_TIMEOUT` 混淆三種原因且 `last_error_detail` 恆為 `null`、`brcm-template` 的 `prompt_regex` 未錨定行首在洪流／banner 板上可誤配登入成功。
  
  - **送 `post_login_cmd` 前的 login guard**：`login_fsm._finalize_ready()` 送出前先查 rx tail 是否命中 `login_regex`／`password_regex`（`login_fsm.matches_login_or_password()`）——命中就代表 `prompt_regex` 誤配（如 BDK login banner 的 `#####` 裝飾線／CEVENT 洪流被誤配成 prompt）、`_maybe_login` 整段被跳過，此時**絕不**把 `post_login_cmd` 當帳密送進 login prompt，直接回可行動的 `LOGIN_REQUIRED`。
  - **失敗分流**：`POST_LOGIN_CMD_TIMEOUT`／`READY_NONCE_TIMEOUT` 逾時後，若 rx tail 已回到 login/password prompt（憑證錯導致板子重印 `login:`，或 guard 仍漏接的邊界情況），改分流為 `LOGIN_REQUIRED`，不再與「登入成功但指令無回應」擠同一個 timeout 碼。`LOGIN_REQUIRED` 依既有 RX_FLOOD 反分類原則永不被遮蔽。
  - **`last_error_detail` 帶 rx tail**：`session_manager._refine_probe_failure()`（attach/recover/reprobe/自動登入五處呼叫共用的單一 choke point）在 login FSM 失敗碼（`login_fsm.LOGIN_FSM_DETAIL_ERRORS`）未被翻轉為 `TRANSPORT_STALL` 時，附上失敗當下的 rx tail（`clean_text()` 去控制碼／ANSI 後截尾 300 字元），取代舊行為恆為 `null`。
  - **出貨 `brcm-template` 的 `prompt_regex` 錨定行首**：`(?m)[>#]\s*$` → `(?m)^(?:.*[^>#\s])?[>#][ \t]*$`，並排除連續 `#`/`>` 裝飾線；`login_regex` 維持不錨定行首（getty 是 `<hostname> login: `，錨定會打壞這個最常見格式）。
  - **login recovery lease**：`session_manager.interactive_open(..., allow_attached=True)` 的 `ATTACHED` 分支，bootloader prompt／boot banner 皆未命中時再檢查 rx tail 是否命中 `login_regex`／`password_regex`——命中則同樣授予 recovery lease，回應標 `login_required: true`（比照既有 `boot_interrupt` 欄位模式），讓 agent／人可用 `interactive-send` 打帳密把 session 救回 `READY`，不必整個 `device release`。三者皆未命中才維持既有 `NOT_BOOTLOADER`。
  - **`serialwrap profile test --profile <name> --sample <file> [--profile-dir DIR]`**：離線診斷子命令，不連 daemon、不碰任何 UART，對樣本文字跑 prompt/login/password/bootloader regex，stdout 印 JSON 回報命中結果，供 operator 收緊 regex 前先驗證，不必上板試錯。
  
  canonical 規格見 `openspec/specs/session-interactive/spec.md`（interactive_open 的 login recovery lease 分支）；`README.md` / `docs/serialwrap-spec.md` 同步。
  
  **regression-case 評估**：mock/unit 已覆蓋 guard／分流／last_error_detail／regex 四類樣本／login recovery lease／`profile test` CLI，共 5 個新測試檔＋既有 login FSM／RX_FLOOD／bootloader-recovery 回歸線全綠。「真板 login banner 誤配」（S2'／S4）屬只有實機才驗得到的行為，建議新增 F10（登入帳密 family）realhw case 作 follow-up，不在本 PR 實作。

## [0.3.0] - 2026-08-01

### Added
- 新增 daemon／CLI 版本可診斷性（#154）：daemon 端 `health.status`（`daemon status`）過去完全不帶版本欄位，只有 CLI 自帶的 `_resolve_version()`（#131）可用，daemon 端無等價解析器可用（架構慣例：daemon 不依賴 cli.py 這層 client），逼得 preflight 得繞 `/proc cmdline + importlib.metadata` 才問得到 daemon 版本。修法：(1) 新模組 `sw_core/version.py`，把版本解析邏輯搬出 `cli.py`（改為重新匯出、既有呼叫點不動），供 daemon／CLI 共用；(2) `SerialwrapService.__init__` 常駐 `self._version`，`health()` 顯式帶 `version` 欄位；`rpc()` 機械改名為 `_dispatch()`，新增薄 `rpc()` wrapper 對所有 method 回應 `setdefault("version", ...)`，不需逐一 handler 加；(3) CLI 新增 `_warn_version_mismatch()`，`_run_rpc()`／`_dispatch_event()` 兩條路徑收到回應後即比對 client／daemon 版本，不一致時印一行 stderr 警告（純副作用、不擋、舊版 daemon 無此欄位時靜默略過）；(4) `sw_core/sysenv.py` 新增 `Effects.which_all()`（掃 PATH 全部相符可執行檔，`shutil.which()` 只回第一個）與 `run(..., timeout_s=...)`（doctor 對 PATH 上未知來源二進位跑 `--version` 的「永不卡住」防線）；(5) doctor 新增 `other_serialwrap_installs` 檢查（advisory），偵測同機 PATH 上多份 serialwrap 安裝版本是否一致。已知限制：doctor 檢查只看得見呼叫行程 PATH 上的安裝，絕對路徑呼叫（如 TestPilot venv）看不到，該類漂移的即時防線是 (3)。回歸 plugin：preflight 的 `daemon_version_probe()` 改為優先讀 `status.get("version")`（欄位缺席才落回原 `/proc` fallback，向下相容），新開 family **F12**（診斷保真）與 case `f12-version-reported-consistent`（`cases/f12_diagnostics.py`），驗證版本欄位悄悄消失不被 preflight 的 fallback 靜默掩蓋。
- 新增 `serialwrap_regression` TestPilot 回歸 plugin（#155）：把已 CLOSED 且有實際修正、只有實機才驗得到的 bug 固化成 10 個 Scenario Family（F1 命令契約／F2 背壓／F3 失敗可觀測性／F4 狀態語義／F5 console 共存／F6 RPC 不凍結／F7 檔案傳輸／F8 daemon 單一性／F9 開機-U-Boot／F10 登入帳密）約 30 個實機回歸 case。含 #154 防線（testbed pin `serialwrap_exe`＋preflight client↔daemon 版本 gate）、`allow_destructive` gate、U-Boot 唯讀護欄（`UBootConsole` 白名單）、`ThrowawayDaemon` 隔離；realhw `SwCli` 支援注入執行檔路徑（預設行為不變）。文件：README「TestPilot 回歸測試」雙語章節、`docs/regression-plugin.md`（family↔issue 對照＋新增 case SOP）、CLAUDE.md 條件式回歸 case 政策。

### Changed
- 同步 hamanpaul project policy 1.0.14 → 1.0.15：`.project-policy.yml`、`Policy Check` workflow **與** `Publish Release` workflow（`uses:` 與 `policy_engine_ref` 雙重釘選至 `a764806046c410eb4f254ac0b6a8aec8b7559dab`）、canonical `CLAUDE.md`（symlink `AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md` 自動跟隨，含內文兩處內嵌 pinned SHA 安裝指令引用）全數同步至 v1.0.15。

### Fixed
- 自發重開機過渡態的 agent 顯式命令 gate（#139，收斂 #130 Finding 4 已知架構限制）：DUT 自發重開機（或過早 READY 競速）時 session 名義上仍停 `READY`，agent 顯式命令過去被放行、bytes 落入 U-Boot autoboot 倒數窗打斷開機、10s 後以 `PROMPT_TIMEOUT` 吞掉。修正：(1) 新常數 `ERROR_AUTOBOOT_QUIET`（字串沿用 self-test classification `AUTOBOOT_QUIET`，統一單一常數）；(2) **submit-time gate（第一層）**——`service._resolve_session_id` 增 `source` 參數，非 human 來源且 `boot_quiet_remaining_s` 非 null 時即時拒絕（附 `retry_after_s`＋session dict、不產生 cmd_id），`command.submit`／`file.push`／`file.pull` 三呼叫端傳入實際 source；(3) **execute-time gate（第二層）**——`execute_command`／`file_push`／`file_pull` locked 開頭補同語意 gate，堵「submit 通過後 banner 才到、命令已在 arbiter queue」的競態（arbiter 把 error_code 寫回、`cmd status` 可觀測、零 UART 副作用、可重送）；(4) **READY ⇒ 清除 gate（相容性關鍵）**——四個 READY 轉移點（`_probe_existing_bridge`／`_attach_by_id` 兩路徑／`_spawn_reboot_recovery`）同 locked 區塊 `clear_boot_quiet()`，另 `_handle_reboot_command`「板其實沒重開」提前返回分支補清（belt-and-suspenders），保證「READY 後立即 submit 必成功」恆真、gate 生效窗口精確等於過渡態；human console／interactive lease 循 #130 慣例永不 gate（#114 刻意進 bootloader 相容）。README（en/zh）、`docs/serialwrap-spec.md` §9.3、SKILL.md 同步改寫。回歸 plugin：F9 新增 `f9-spontaneous-reboot-agent-gated`（destructive，human console reboot 模擬自發重開機、驗雙層 gate 與 READY 後重送），`f9-quiet-window-agent-passthrough` 增 `AUTOBOOT_QUIET` 有界重試（stale-READY 窗訊號），oracle 本體「（真）READY 後命令必須成功」不變。
- 新增 `serialwrap doctor` 的 `wal_dir` 診斷項並修正誤導性文件（#148）：systemd 託管的
  daemon 不會繼承 shell 匯出的 `SERIALWRAP_WAL_DIR`（`render_system_unit()` 產生的 unit
  本就不含對應 `Environment=` 那一行，`.bashrc` 的 export 對它無效），daemon 仍照自己
  解析出的預設路徑（XDG state home）持續寫入；使用者卻常誤把 `SERIALWRAP_WAL_DIR` 指到
  `~/b-log`（那其實是 §10.4 agent on-demand capture 目錄，語意完全不同），造成「我以為
  WAL 在 A，daemon 實際寫在 B」的落差，且舊版 `doctor` 完全沒有任何一項能揭露 daemon
  實際生效的 WAL 路徑，只能用猜的。
  
  修法：`sw_core/doctor_cmd.py` 新增 `_check_wal_dir()`——經既有 `health.status` RPC
  （0.5s 短逾時、`rpc_call` 永不拋例外）讀 daemon 實際回報的 `wal_path`，與 shell 端
  `SERIALWRAP_WAL_DIR`（若有顯式覆寫）比對：daemon 未在跑／連不上時降級為 informational
  （`ok=True`，doctor 常在啟動 daemon 前執行，「連不到」本身不是這項要抓的錯誤）；一致
  或無 shell 覆寫時 `ok=True`；不一致時 `ok=False` 並在 `fix` 附 systemd `Environment=`
  與 on-demand 重啟兩種修復指引。掛入 Linux（`single_daemon` 之後、`devices` 之前）與
  Windows（`daemon_endpoint` 之後、`devices` 之前）兩份檢查清單；`cli.py` 的
  `_DOCTOR_ADVISORY_CHECKS`／`_DOCTOR_ADVISORY_CHECKS_WIN` 同步加入 `wal_dir`，避免
  shell/daemon 不一致把整體 `doctor` 的 `ok` 拉成 `false`（WARN 語意，不擋）。
  
  同時修正 `README.md`（英/繁中 Logs and Evidence／日誌與輸出章節＋既有誤導範例）與
  `docs/serialwrap-spec.md` §10.2（過期的 `/tmp/serialwrap/wal/` 預設路徑敘述＋主動建議
  把 `SERIALWRAP_WAL_DIR` 指到 `~/b-log` 的誤導範例），明確區分「WAL（權威）」與
  「`~/b-log`（agent on-demand capture）」兩種用途，並說明 systemd daemon 不繼承 shell env
  的行為。
  
  回歸 plugin：`f12-wal-path-live`（`cases/f12_diagnostics.py`，F12 診斷保真 family，
  非破壞性）驗證 `daemon status` 的 `wal_path` 真實存在、送一筆命令後 mtime 前進（非死
  路徑），以及 `doctor` 報告含 `wal_dir` 檢查項；`docs/regression-plugin.md` 的 F12 對照
  列同步補上 #148。
- 補齊 `serialwrap doctor` 的 human console 就緒檢查組，並修正 README 自相矛盾的
  `minicom` 範例（#149）：`doctor` 過去只驗 `serialwrap`／`serialwrapd` 是否在 PATH，
  完全沒驗證 `serialwrap-minicom`（human console broker wrapper 本體）、`jq`、`minicom`
  （`sw_core/assets/tools/minicom_router.sh` 的執行期依賴）是否就緒——doctor 全綠不代表
  human console 真能動。同時 `README.md` 繁中「多 minicom 使用」段落自 2026-06-22 改名
  commit 起就帶著 stale code block（`minicom` / `minicom -D /dev/ttyUSB0` 等未改名的
  裸指令），與同段落下方「不要直接打 `minicom`」的警語自相矛盾，讀者照抄該 code block
  正好重現本 issue 的錯誤重現步驟。
  
  修法：(1) `sw_core/doctor_cmd.py` 的 `_check_on_path()` 新增 keyword-only
  `check_name` 參數（既有呼叫端 100% 相容），供 `serialwrap-minicom` 產生底線命名
  `serialwrap_minicom_on_path`；Linux 檢查清單於 `dialout` 之後、`systemd` 之前插入
  `serialwrap_minicom_on_path`／`jq_on_path`／`minicom_on_path` 三項（純 `fx.which()`
  查表、無 I/O 副作用）。(2) `sw_core/cli.py` 的 `_DOCTOR_ADVISORY_CHECKS` 納入新三項
  （與 `devices`／`systemd` 同哲學：headless agent-only 部署不需要 human console，缺席
  不拉低整體 `ok`）；`_run_setup()` payload 新增 `console_hint` 主動提示
  `serialwrap-minicom COMx`；`doctor` 子命令 help／description 同步補一句（R-16）。
  (3) `README.md`：英文 Prerequisites／Quick Start 補走 wrapper 的提示與範例；繁中
  `## 依賴`／`## 快速開始`（含結尾同名的 `## 安裝` 附錄區塊）把 `serialwrap-minicom`
  提示提升為第 2 條並加粗；`## 多 minicom 使用` 段落的 stale code block 全面改用
  `serialwrap-minicom`，並在最後一行加註解澄清「僅示意 wrapper 內部 fallback 語意」，
  消除與下方警語的自相矛盾；`serialwrap --help` 的 `cli-help` marker 區塊（R-16 pinned）
  同步以實際指令輸出重新產生。
  
  真機驗證：本變更 100% 由 pytest 覆蓋（3 個新 check 為純 `fx.which()` 查表，
  `FakeEffects` 可完全模擬每種 PATH 組合；不涉及 daemon/PTY/UART，不符合
  `docs/regression-plugin.md` 的 realhw case 定位），故未新增 TestPilot
  `serialwrap_regression` case；`docs/func-test/realhw-stability-checklist.md` 的
  `p0-doctor` row 追加 `serialwrap-minicom --help` 手動等效命令與新三項 check 的驗收
  判定，讓既有 bench smoke 近似「免費」獲得覆蓋。
  
  已知風險（見 PR 描述）：`realhw/preflight.py::_doctor_ok()` 與
  `regression/serialwrap_regression/preflight.py` 皆用 `all()`（不分 advisory）判定
  suite-refuse，新 `jq_on_path` 一旦落地會讓「bench 是否裝了 jq」首次成為真機測試套件
  的 preflight 硬門檻——本批次僅以 pytest 驗證（任務範圍禁止對 live daemon 做
  attach/console 等寫操作），**合併前仍建議在真機 bench 手動跑一次 `serialwrap doctor`
  確認新三項皆 `ok:true`**，避免 realhw／regression suite 意外全面拒跑。
- Transport stall 分類與觀測面（#150）：WSL2＋usbip 偶發 USB read-endpoint stall（TX 通、RX 全凍，dmesg `urb stopped: -32`）過去被折疊進 `PROMPT_UNAVAILABLE`／`LOGIN_PROMPT_TIMEOUT`，誤導 operator 去 power-cycle DUT 或反覆 recover（皆無效）。新增：(1) 純偵測模組 `sw_core/transport_stall.py`——probe 全程零 raw RX（連 echo 都無，`UARTBridge.rx_total_bytes()` 新計數器取差）＋該 session 曾有 RX＋`last_rx_age_s >= 30s` 才翻轉為新錯誤碼 **`TRANSPORT_STALL`**（與 #153 RX_FLOOD 高 RX 特徵天然互斥）；(2) 三個 probe 落地點（`_probe_existing_bridge`／`_attach_by_id`／`_attach_by_id_dynamic`）經單一 choke point `_refine_probe_failure` 精煉，boot-quiet 合成錯不精煉；(3) `last_error_detail` 新欄位附可複製的 host 層復原指令（USB busid authorized 0→1 toggle／usbipd detach+attach），`last_error` 改 property、值變更自動清 detail；一次性 log＋WAL META（`transport_stall_suspected`，RX 恢復重臂）；(4) session 公開 dict 新增 `last_rx_age_s`／`last_tx_age_s`（RX 單邊凍結一眼可判，`idle_for_ms` 看不出）；(5) `TRANSPORT_STALL` 保留自動重探資格（RX 恢復即自癒回原分類或 READY）；`minicom_router.sh`／SKILL.md／README（含 WSL2+usbip 復原 SOP）同步。回歸 plugin F11 新增 `f11-last-rx-age-sane` 觀測面 case（真 usbip stall 依 #155 裁定排除自動化、heuristic 由 pytest 覆蓋）。
- RX 洪水分類（#153）：console 被大量輸出灌爆時，probe 失敗過去被折疊進 `PROMPT_UNAVAILABLE`／`TARGET_UNRESPONSIVE`（「灌爆」與「死了」擠同一碼），上層被誤導去重建 session 而非等排空。新增：(1) `UARTBridge` RX 速率統計視窗（`rx_stats()`，最近 10s raw bytes，含 ANSI）；(2) `login_fsm` 兩個公開出口（`probe_ready`／`ensure_ready`）統一反分類——probe 失敗且 `rx_bytes_last_10s >= 20000`（`RX_FLOOD_BYTES_PER_10S`）時回新錯誤碼 **`RX_FLOOD`**（`LOGIN_REQUIRED`／`CREDENTIALS_*` 永不被遮蔽）；(3) self-test 於洪水中回 `classification=RX_FLOOD`＋`recommended_action=wait`（ATTACHED 與 READY nonce probe 兩分支，BOOTLOADER 優先序不變）；(4) `RX_FLOOD` 保留自動重探資格——洪水中 RX-idle 閘天然退避、排空後 3s 內自動接手升 `READY`（自癒）；(5) session 公開 dict 新增 `rx_bytes_last_10s`／`rx_rate_bps` 欄位；`minicom_router.sh`／SKILL.md／README 契約同步。回歸 plugin 新增 F11 family 與 `f11-flood-probe-classified`（destructive）實機 case。
- 修復 `session recover`／命令逾時內部觸發 recovery 時，CTRL_C/CTRL_D 攔截成功路徑不 flush 舊佇列的缺口（#156）：`#128` 建立的 flush 機制唯一掛點是「detach」事件（`_on_detached` → `CommandArbiter.unregister_session`），但 `SessionManager._recover_after_failure()` 的 CTRL_C/CTRL_D 攔截成功分支語意是「session 沒有真的離線」，全程停留 `READY`、不觸發 detach，因而天生跳過既有 flush 路徑——該 session 尚未啟動的排隊命令永久卡在 `accepted`，佔用 `CMD_PENDING_MAX` 額度，之後每次 submit 連鎖 `SESSION_QUEUE_FULL`。`session.recover` RPC 在 `READY` 狀態下沿用同一段程式碼，受相同缺口影響。
  
  修法：`SessionManager` 新增第 4 個建構子 callback `on_command_flush: Callable[[str, str], None] | None`（預設 `None`，向下相容）；CTRL_C/CTRL_D 攔截成功分支於鎖外顯式呼叫 `self._on_command_flush(session.session_id, "FLUSHED_BY_RECOVERY")`，補上與 detach 路徑相同的終態語意。`SerialwrapService` 新增 `_on_command_flush()` 方法，直接委派 `CommandArbiter.flush_session()`（既有公開方法，`arbiter.py` 本身無需改動）。刻意不動「human interactive promotion」分支（`source.startswith("human:")`）：那是暫時把 raw 控制權交給人類，不是宣告佇列作廢，其餘排隊命令在人類結束 interactive lease 後仍是合法待執行工作。
  
  回歸 plugin：`f2-recovery-flushes-queue`（`issues` 疊加 `#156`）的排空 oracle 由首輪實測時因本缺口被迫放寬的 30s 收緊為 10s 嚴格斷言；相關歷史註解（`f2_queue_full_backpressure` 收尾、module docstring）同步更新為「已修復」。
- 修復 `file.push`／`file.pull` 每段等待逾時寫死常數、與 profile 完全脫鉤（#157）：`SessionManager.file_push()` 原固定 `timeout_s=10.0`（`file_pull()` 固定 30.0），即使 bcm 類慢板已把 `profile.timeout_s` 調大也從未生效。改為未顯式指定時沿用 `profile.timeout_s`（push 夾 5.0s、pull 夾 30.0s 地板），並於 CLI（`--chunk-timeout`）／RPC（`chunk_timeout_s`）開放顯式覆寫。同時將 `DEFAULT_CHUNK_SIZE` 由 2048 降為 512（單行 base64 命令 ~2789 字元在真機 UART console 實證會被截斷——issue 附 WAL 證據；512B → 單行 ~741 字元，~3.8x 安全餘裕；此值為依截斷證據反推的保守估計、非真機量測值），四處各自硬寫的 2048 收斂為 `sw_core/file_transfer.py` 單一常數。`file.push` RPC 對 `chunk_size<=0` 夾 `max(1, ...)` 防呆，避免 `ValueError` 穿越 RPC 邊界。已知殘留缺口（#157 範圍外）：`pull_file` 不分段一次讀全部，受 RX 視窗 128KiB 上限（`sw_core/uart_io.py`）限制，base64 輸出超過上限的大檔 pull 仍會 `PULL_PARSE_FAILED`，`f7-larger-file-not-truncated` 因此仍預期 SKIP，待 follow-up。
- 修復 RX 視窗有界修剪破壞 offset 語意（#158）：`UARTBridge` 的 `_rx_text` 觸頂（131072 字元）修剪前端但不記帳，使 `rx_snapshot_len()` 飽和後恆等於視窗上限、`wait_for_regex_from()`／`rx_text_from()` 以舊 offset 切片永遠空字串——prompt 永不匹配 → 快速命令迴圈於百次級必中 PROMPT_TIMEOUT（stdout 空），且 recovery 同因匹配不到 CTRL_C 拿回的 prompt 而續送 CTRL_D 誤登出 console。改為**絕對串流偏移**記帳：新增 `_rx_trimmed`（修剪／clear 累計丟棄量，單調不減），`rx_snapshot_len()` 回傳絕對偏移、切片 API 每次持鎖重算相對偏移，頭段已修剪時降級回傳現存全窗；`clear_rx_buffer()` 亦推進基準保持單調（同時修復飽和下 RX 靜默偵測恆真的隱性誤判）。`bridge.snapshot()` 新增 `rx_dropped_chars` 欄位供鑑識。回歸 plugin：`f2-history-bounded-rss` 收緊為零容忍（移除 ≤3 次 PROMPT_TIMEOUT 容忍，恢復即紅），新增決定性重演 case `f2-rx-window-crossing-prompt`（awk 大輸出推飽和 RX 視窗後連發短命令，驗證 prompt 跨界不失效）。
- 修復 background 模式快速完成命令整段輸出遺失且回 `lost: False` 假保證（#159）：複合根因——`BackgroundCapture` 在 prompt 比對成功「之後」才回溯建立（快速完成命令的全部輸出都落在建立之前，`chunks` 恆空）、`_on_bridge_rx` 用 `foreground_busy` gate 連坐擋掉 background capture 的 RX 累積（bg 命令自己的等待視窗內 `foreground_busy=True`）、`maybe_finalize()` 的 quiet-window 活動時鐘建立在從未被真實餵過資料的空殼物件上（首次輪詢即誤判 `done`）。修法三段：capture 於命令送出「之前」掛好（`from_seq` 語意由「TX 寫入 WAL 後」略前移為「TX WAL 記錄本身」，metadata 精度差異）；`_on_bridge_rx` 的 background 累積迴圈移到 `foreground_busy` gate 之前（agent-log capture 維持原 gate 行為）；`_set_terminal_capture_locked` 僅「先前未掛載」（line 模式逾時）分支回填 chunks，避免 CTRL_C 復原路徑對已即時累積的 capture 全量重讀重複疊加。修後 `last_activity_mono` 被真實 RX 正確推進、quiet-window 判定誠實，`dropped_chunks`／`lost` 如實反映唯一剩餘遺失來源（環形上限主動丟棄）。回歸 plugin：`f4-background-result-tail-consistent` oracle 不變，hints 改註「#159 已修，本 case 為其常駐回歸防線」。
- 修復 `file.push` 在無流控（`flow_control: none`）真機 console 上被節流靜默掉字（#161，#157 真機驗收揭露的更深根因——512B chunk 的 echo 於 ~73% 處斷掉）：新增 `UARTBridge.send_command_echo_paced()` 原語，把 chunk 命令行拆成 64 字元短 slice 逐段送出，每段等板端 echo 回讀確認（`_await_echo_progress()` 以 strip-ANSI＋去 CR/LF 正規化、移動起點 find 逐 slice 比對，吸收 slice 間 printk 噪音）才續送——echo 即天然的應用層流控；**全行確認後才送換行**，故 echo 停滯時命令必未執行，以 `cancel_input_line()`（Ctrl-U＋換行）清半行復原後回新錯誤碼 `TRANSFER_ECHO_STALL`（含 `chunks_sent`／`acked_chars` 診斷欄），可安全重試。`push_file()` 以 `ack_mode` 選路：`auto`（預設，bridge 支援即節流、否則 legacy 不破第三方 fake bridge）／`echo`（強制，缺原語回 `ECHO_ACK_UNSUPPORTED`）／`none`（legacy 整行送出，急件換吞吐）；CLI（`--ack-mode`）／RPC（`ack_mode`，白名單驗證）打通，#157 的 `chunk_size`／`chunk_timeout_s` 參數面與優先序完整保留。WAL 維持一命令一筆 TX。F7 回歸案：`transfer_echo_stall` 獨立 reason_code（不併一般逾時）、1MB 案 timeout 調升 1500s（echo-paced 約 10–17 分）。已知限制（範圍外、待另開 issue）：pull 側受 RX 視窗 128KiB 上限，1MB pull 仍 `PULL_PARSE_FAILED`。
  
  **review 收斂＋實機調校**：(1) `send_command_echo_paced` 的 `cmd.rstrip("\n")` 改 `rstrip("\r\n")`（Copilot review）——CRLF 結尾的命令會把 `\r` 留在 body 尾端當命令本文送出，板端多半直接當換行執行命令（早於本原語自己送的 `\n`），破壞「全段確認才送換行＝停滯時命令未執行＝可安全重試」的核心不變量；且 `_await_echo_progress` 的比對已把 RX 的 CR/LF 正規化掉，該字元永遠 ack 不到、必然假性 stall。(2) `push_file()` 模組入口補 `ack_mode` 白名單（Copilot review），非法值回 `INVALID_ARGS`（形狀與 RPC 層一致、另附 `ack_mode`）——`push_file` 亦被 `SessionManager.file_push` 與非 RPC 呼叫端直接呼叫，只在 RPC 層驗證會讓 `ack_mode="ehco"` 這類手誤從那些路徑漏進來、**靜默降級**成 legacy 整行送出而失去無流控保護。(3) echo 逾時調校：`DEFAULT_ECHO_TIMEOUT_S` 2.0 → **5.0**，並由 `SessionManager.file_push` 依 profile `timeout_s` 推導（`max(profile.timeout_s, _MIN_FILE_ECHO_TIMEOUT_S)`，比照 #157 `chunk_timeout_s`；新增可選參數 `echo_timeout_s=None`）——實機兩案都在**第 8 個 slice（448/512 字元）確定性卡住**，固定的失敗位置說明不是隨機掉字，而是板端累積輸入到某長度後 echo 延遲超過 2s，被誤判成停滯；bcm 類已調大 `timeout_s` 的慢板因此自動放寬。調高只影響失敗路徑的等待上限，成功路徑吞吐不變。
- quiet 清空改綁 READY 再確認（#162，f9-spontaneous 紅燈哨兵根修）：連續重開機後，agent 第一個命令的 stdout 吃到 prpl/OpenWrt askconsole「Please press Enter to activate this console」啟用 banner、命令文字被 askfirst 吞掉（status=done 假成功、marker 遺失）。設計實證推翻 issue 原假說——污染不是 RX buffer 殘留，而是 askconsole 提示既不匹配 `login_regex` 也不匹配 `prompt_regex` → quiet window 只能靠 180s 過期清空，期間 session 名義 `READY` 且 reprobe 跳過 READY、無人重新確認，過期即開 gate。修正：(1) `SessionRuntime` 新增 transient `ready_reconfirm_pending`（`arm_boot_quiet()` 置 True；不進 `_save_state`），`to_public_dict()` 新增同名欄位（additive）；(2) **agent gate 改判統一入口 `agent_gate_active()`**（`boot_quiet_active() or ready_reconfirm_pending`）——execute-time 三處（`execute_command`／`file_push`／`file_pull`）與 submit-time（`service._resolve_session_id`，pending-only 時 `retry_after_s` 固定 `READY_RECONFIRM_RETRY_S=5.0`、hint 改「等 daemon 重新確認 READY 後重送」，回應形狀不變）；(3) **解除唯一路徑＝READY 確認點呼叫新 `confirm_ready()`**（`clear_boot_quiet()`＋清 pending）——`_probe_existing_bridge` ok／`_attach_by_id` 兩路徑 READY 落定／`_spawn_reboot_recovery` ok／`_handle_reboot_command` 2s prompt-return／`_self_test_impl` READY nonce 成功共六點；quiet 的 RX prompt 解除與過期維持只清 TX 靜默維度；(4) **reprobe 引擎新增 READY 再確認分支**——`_prepare_reprobe_locked`／`_reprobe_target_still_valid_locked` 放行「READY＋pending 且 quiet 已結束」的確認 probe（沿用既有 RX-idle／backoff／human-active／single-flight 守衛），probe 的 `\n`＋nonce 順帶消耗 askconsole 啟用 banner，成功即 `confirm_ready()`、失敗誠實降 `ATTACHED`；`_probe_existing_bridge` quiet 早退分支對 `state==READY` 不再覆寫 `last_error=PROMPT_UNAVAILABLE`。回歸 plugin：`f09` 的 `_wait_quiet_cleared` 加等 `ready_reconfirm_pending` 歸 False（舊 daemon 無欄位→falsy 向下相容），`f9-spontaneous-reboot-agent-gated` 由紅燈哨兵改記常駐防線。README（en/zh）、`docs/serialwrap-spec.md` §9.3、`docs/regression-plugin.md`、SKILL.md 同步。
  
  **回歸修（實機事故 2026-07-31，f9-quiet-window-agent-passthrough 9 分鐘零收斂）**：初版 #162 引入三個缺陷，本次一併修正並補回歸測試。(A) **pending 有界化＋明確失敗終態**——`ready_reconfirm_pending` 原本只有一個 clearer（`confirm_ready()`，且要求對 UART probe 成功），沒有 deadline／attempt 上限／失敗終態；只要確認 probe 的任一守衛長期不成立（`reprobe_exhausted` latch 只在 READY **轉移**點解開、而「已經是 READY」的 session 不會再轉移／RX 洪水下 `_rx_idle_enough` 永不成立／human console 持續活躍／RX 週期性出現字面 `U-Boot` 重臂 quiet），pending 就永久 True，`AUTOBOOT_QUIET`（可重試）遂變成**永遠不可能成功的「可重試」錯誤**、把 agent driver 的重試迴圈拖進無界迴圈。新增 `ready_reconfirm_deadline`／`ready_reconfirm_attempts`／`ready_reconfirm_failed` 三個 transient 欄位、常數 `READY_RECONFIRM_MAX_S=300`（＝180s quiet＋120s，**重臂不延長 deadline**）／`READY_RECONFIRM_MAX_ATTEMPTS=5`／`ERROR_READY_UNCONFIRMED`，並新增 pending 的第二個也是最後一個 clearer`expire_ready_reconfirm()`；逾時／逾次後四個 gate（`execute_command`／`file_push`／`file_pull`／`service._resolve_session_id`）改回**不可重試**的 `READY_UNCONFIRMED`（不帶 `retry_after_s`、帶 `recommended_action="self_test"`），`to_public_dict()` additive 新增 `ready_reconfirm_failed`／`ready_reconfirm_remaining_s`。(B) **autoboot 保護涵蓋 agent-reboot 路徑**——(B1) `_recover_after_failure` 的 CTRL_C/CTRL_D gate 由「迴圈外評估一次」改為**逐 byte 重驗**：兩次寫入間隔最長 2s、恰好落在 U-Boot autoboot 3s 倒數窗內，WAL 物證顯示 CTRL-D 在 quiet armed 後 1.48s 才寫出、直接把板子留在 `=> `；(B2) banner 偵測比對窗改為「舊 tail 尾段＋**整個** chunk」（只有保存才截 256 字元），舊碼 `(tail+chunk)[-256:]` 會截掉大 chunk 的**開頭**，實機兩個含 `U-Boot` 的 banner chunk（345/354 字元）都因此漏判；(B3) `_handle_reboot_command` 的 2s prompt-return **不再**是 READY 確認點（改回只 `clear_boot_quiet()`）——prpl/OpenWrt 的 `reboot` 非同步，shell 11ms 內就重印 prompt 但板子確實在重開，舊碼的 `confirm_ready()` 是假確認、正是本次事故的入口。(C) **bootloader 卡死可診斷**——新增 `ERROR_BOOTLOADER_STUCK` 與 `UBOOT_FALLBACK_PROMPTS`；readiness probe 失敗且 RX tail 尾行命中 bootloader prompt 時落**有理由的**終態（`last_error=BOOTLOADER_STUCK`＋`reprobe_exhausted`＋pending 立即終結），取代原本「第 10 次靜默 exhausted、state/last_error 不變、不發事件」的無資訊放棄；`self_test`／`recover` 據此回`classification="BOOTLOADER"`＋`recommended_action="recover_interactive"`；profile 缺 `bootloader_prompts` 時退回 fallback 樣式（線上 prpl-template 實測就漏配），並新增 `serialwrap doctor` 的 `profile_bootloader_prompts` advisory 檢查指出配置漂移。(D) READY 再確認 probe 在寫 bytes 前先驗 bootloader 證據，命中即不送、直接落終態。

## [0.2.4] - 2026-07-20

### Added
- `interactive-open --allow-attached` 的 recovery lease 授予條件擴充至 **autoboot 倒數窗**：原本僅在 session `ATTACHED` 且 RX tail 末行匹配 `bootloader_prompts`（如 `=> `）時才授予 lease；現在若未匹配 prompt、但 RX tail 命中 boot banner（`Hit any key to stop autoboot` 倒數行／`U-Boot` 版本行，複用 #130 `detect_boot_banner` 與 `BOOT_BANNER_PATTERNS` 單一事實來源），亦授予相同的 recovery lease（`recovery_mode=True`），並在回應多帶 `boot_interrupt: true`（bootloader prompt 命中則省略此欄位，additive、向後相容）。用途：agent 燒壞 fw 後，若板子會 autoboot 載入壞 image，可在倒數窗搶開 lease → 以 `interactive-send` 連打按鍵中斷 autoboot 停在 `=> ` → 再逐字驅動 U-Boot 救 fw，不必等它先停在 bootloader prompt。與 #130 boot quiet window 相容——lease TX 永不受 quiet window gate，故倒數窗內連打按鍵有效。純 POSIX/共用邏輯，Windows daemon 走同路徑同步受益。README（中英）／`docs/serialwrap-spec.md`／`SKILL.md` 同步。
- 新增實機穩定性測試套件 `python3 -m realhw`（#122）：P0 煙霧×8＋P1 核心穩定性×20（console 對抗／重啟恢復／裝置交接／usbipd 插拔／命令執行／WAL）全自動、長跑（`lr-mixed`，預設 32h）無人看護＋事後分析報告（`~/b-log/realhw-reports/`，含 `snapshots.ndjson`／`events.ndjson`／`longrun-analysis.md`）；preflight 守門（部署新鮮度／doctor／兩板 READY／工具可用／環境乾淨／破壞性預告，任一不過整場拒跑）；continue-on-failure 執行引擎＋逐 case evidence 與診斷提示。`docs/func-test/realhw-stability-checklist.md` 人可讀清單含 P0/P1 逐 case 手動等效命令與 P2 手動程序（MCU flash `/dev/ttyMCU`、U-Boot recovery lease、self-test 全譜、監管模式轉換、Windows loopback）。**測部署後系統**（用已安裝 `serialwrap` CLI 操作 live daemon 與真板，不 import `sw_core`、不進 CI、不入 wheel、pytest `tests/` 不收集）。
- `serialwrap remote`：按需開關 SSH 反向隧道（`-R` expose 預設／`-L` connect），讓遠端 agent 取得本機 serialwrap 操作；background 常駐、flock registry、readiness 確認、`--remote-socket` 硬化、fail-closed 遠端 bind 驗證。daemon 零改動、不做預設、僅 POSIX（native Windows 回 `REMOTE_NOT_SUPPORTED`）。
- 擴充 `serialwrap_reliability.core` 的 case dict 映射能力：新增 longrun checkpoint steps 合成、`realhw` case → plugin case dict 轉換、依 registry 順序建構 case list，以及執行前預設排除 destructive／顯式點名才納入的選擇期過濾；同步加入 `tests/test_reliability_core.py` 覆蓋 schema 形狀、longrun steps 與 destructive filtering。
- 新增 `serialwrap_reliability.core` 最小 bootstrap：以 `REPO_ROOT` 定位 repo root、冪等注入 repo root 使 `realhw` 可 import，並提供 `load_registry()` 載入 realhw registry；同步加入 `tests/test_reliability_core.py` 驗證 repo-root、bootstrap 冪等、registry 唯一且含 `p0-doctor`，以及 `__init__.py` / `core.py` 不得 import `testpilot`。
- 新增 `serialwrap_reliability` 的 testpilot plugin 薄殼與執行契約檔：補上 `plugin.py`、`agent-config.yaml`、`testbed.yaml.example`，並以 `tests/test_reliability_pluginfiles.py` 釘住 entry point、testpilot import 邊界、remediation/retry 鎖死組態，以及 testbed example 與 `realhw/config.json` 的 bench 事實等價性。
- 新增 `serialwrap_reliability.reporter`，以不 import `testpilot` 的 duck-typed reporter 重用 `realhw.harness.write_reports()` 產生 `report.md`／`report.json`，將報告複製到 testpilot `artifact_dir`，並在 meta/payload 烙上 deployed 版本、`fw_ver`、`run_id` 與 retry `diagnostic_status` 統計；同步加入 `tests/test_reliability_reporter.py` 覆蓋一般路徑與 `run_meta`／`artifact_dir` fallback。
- 新增 `serialwrap_reliability.testbed_loader`，讓 `testbed.yaml` 與 `realhw/config.json` 都透過 `realhw.load_cfg` 單一正規化路徑產生等價 cfg；同步加入 `tests/test_reliability_testbed.py` 驗證雙來源等價、selector 排序、`longrun.duration` → `duration_s`、`win_serialwrap_exe` 直帶，以及 config.json 會剝除 `_` 開頭註解鍵。
- realhw 實機穩定性套件 Phase 1 擴充：`CaseResult` 增 `category`/`reason_code` 分類欄並擴到報表與既有 29 case；preflight 新增兩級判決（suite-refuse 的 benchlock / Windows daemon 診斷，以及 family-gate 的 remote/版本/docker 能力缺項執行期 SKIP）；`p1-hp-cycle` 接上 Windows 端自動救援鏈；新增 `remote` tier 7 case（rm-topo×4、rm-live×3）；`realhw.load_cfg()` 與 `win_serialwrap_exe` 組態欄位就緒，供後續 reliability plugin 消費。
- `reliability/`：serialwrap-reliability testpilot plugin 殼（dev-only editable dist，永不 release）——entry point 註冊、PluginBase 生命週期映射（prepare_run＝realhw preflight gate、execute_step＝black-box `case.run(ctx)`、evaluate＝分類抄寫 `_last_failure`）、testbed.yaml 與 config.json 雙來源等價 loader、md/json reporter 重用 realhw 報告與 deployed 版本烙印；release wheel 零改動。
- `SKILL.md` 的 Remote Support 章節補上 `--remote-socket` 硬化模式下的 agent 端連線格式（`unix:///path/to.sock`）、`remote_hint` 欄位在該模式下不會更新為 unix endpoint 的已知落差，以及給「隧道對端」agent 的操作提醒（`--source` 標記、共用硬體先 `session self-test`、隧道非常駐服務需回報而非自行繞過）。

### Fixed
- 帳密宣告但解析為空時不再靜默送空帳密狂 probe，改回明確 `CREDENTIALS_UNRESOLVED` 終態：`resolve_session_auth()` 除 `SessionAuth` 外新增回傳 `AuthResolution`（`reason` 為 `ok` / `env_file_missing` / `env_file_unreadable` / `key_absent` / `not_configured`，並帶實際解析到的 env_file 絕對路徑；只要帳密實際解析成功即為 `ok`，即使 env_file 缺失、只要 `os.environ` 補齊亦不誤擋）。當 profile **宣告了帳密來源**（`user_env`／`pass_env`／`env_file` 任一）但解析為空時，login 流程 **不對** `Login:`／`Password:` 送空字串、不進無限 `Login incorrect` 迴圈，session 標 `last_error=CREDENTIALS_UNRESOLVED`（與「板子尚未到 login prompt」的 `LOGIN_REQUIRED` 明確區分）並停止自動 reprobe（明確終態）；四條 attach/recover 路徑一致套用。進入此態時 daemon 輸出一次性（去重）log + WAL 警告，含 env_file 實際解析絕對路徑與原因，**絕不含帳密值**。手動 `session attach`／`recover` 視為重新介入、重置去重旗標並重解析（補齊帳密即恢復；daemon 重啟亦重讀 env_file）。profile **未宣告**帳密來源（`not_configured`）者行為完全不變（passwordless/auto-login 不受影響）。新增 error_code 為 additive，不改既有欄位；純 POSIX/共用邏輯，Windows daemon 走同路徑同步受益。
  
  **排查重點（易踩）**：profile YAML 內的**相對** `env_file` 是**相對 daemon 的 profile-dir** 解析（systemd-system＝`/etc/serialwrap/profiles/`、pipx/XDG＝`~/.config/serialwrap/profiles/`），**不是** shell CWD 或 XDG config——帳密檔要放在 daemon profile-dir，警告訊息印出的絕對路徑即 daemon 實際查找位置。README（中英）／`docs/serialwrap-spec.md`／`SKILL.md` 同步。
- docker 三拓樸驗收 harness（`tools/docker/remote_tunnel_test.sh`）補 review 發現的攻擊者隔離覆蓋缺口：`topology_nat_host`（net_a）與 `topology_dual_nat`（net_a／net_b 雙 NAT）先前僅以 `ss -ltn` 檢查 relay 綁 loopback，未實際驗證「同網段的獨立攻擊者容器」真的連不到——現在兩個拓樸都在 tcp 模式 `-R` 隧道存活期間，另起一個獨立 `attacker` 容器（與 relay 同網段），以 relay 的**非 loopback 容器位址**（`tcp://<relay-container>:7777`，而非合法路徑用的 relay 自身 `127.0.0.1`）嘗試 `daemon status`，斷言真連線失敗（`ok:false` + `SOCKET_ERROR`）；`topology_dual_nat` 額外驗證 net_b 上的 agent 若繞過自身 `-L` 轉發直連 relay 容器位址同樣失敗，證明合法路徑必須經過 relay 自身 loopback。另外 `topology_gatewayports_failclosed` 的 `--remote-socket` 成功案例先前只有 tcp fail-closed 分支有 teardown 收尾複查，現補上同樣的 `assert_default_off`／pid 不變複查，避免該路徑的 teardown 迴歸漏測。攻擊者容器已納入既有 `teardown_now` 回收流程，不留殘留資源。
- `serialwrap remote`：收斂 Copilot PR review 三項發現。① `--local` 新增範圍／角色驗證——搭 `-R`／預設（expose）帶入視為誤用，早擋 `INVALID_ARGS`；搭 `-L` 但超出 `1..65535` 範圍同樣早擋，不再交給 ssh 晚爆成非 `INVALID_ARGS` 錯誤。② `Registry` 新增 `log_path()` 單一事實來源，`remove()`（`status` prune／`close` 皆走此路徑）與 `open_tunnel` 的 spawn log 皆改用此方法，`<port>.log` 隨 state／control socket 同生命週期清除，不再於重試／失敗後於 `<run_dir>/remote/` 累積孤兒 log 檔。③ docker 測試映像（`Dockerfile`）的 host-key-skip（`StrictHostKeyChecking no`／`UserKnownHostsFile /dev/null`）改由全域 `/etc/ssh/ssh_config` 收斂至僅 `tester`（唯一會執行 `serialwrap remote`／spawn ssh 的帳號）的 `~/.ssh/config`，不再弱化映像內其他使用者（`otheruser`／`root`）的 host-key 驗證基準；已以獨立 throwaway 容器驗證 `tester` 免 host-key 提示連線成功，`root`／`otheruser` 仍正常觸發 host-key 驗證失敗。
- `serialwrap remote`：修全分支複查發現的 merge-blocker——native Windows 下 `_run_remote` 在 `guard_platform()` 之前就先 `from . import remote_tunnel`（該模組頂層 `import fcntl`，POSIX-only），導致 `ModuleNotFoundError` 穿越 CLI 邊界變成原始 traceback（而非 `REMOTE_NOT_SUPPORTED`）；且該 import 發生於 `try:` 外，`status`／`close` 兩條路徑更是完全沒呼叫 `guard_platform()`。改為在 `_run_remote` 最頂端、`import remote_tunnel` 之前先攔截 `os.name == "nt"`，一律回 JSON `REMOTE_NOT_SUPPORTED`，涵蓋 `status`／`close`／open 三條路徑。另補一個 catch-all `except Exception`，讓非 `TunnelError` 的非預期例外（如 `SERIALWRAP_RUN_DIR` 父路徑非目錄觸發的 `NotADirectoryError`）同樣轉為 JSON `INTERNAL_ERROR`，不穿越 CLI 邊界。

## [0.2.3] - 2026-07-17

### Added
- 暴露可查詢的命令長度上限，讓 client 執行期查詢而非硬編碼：`health.status`（CLI `serialwrap daemon status`）回應新增 `limits` 欄位——`max_submit_cmd_bytes`（16384 硬上限，超過拒 `CMD_TOO_LONG`）、`warn_submit_cmd_bytes`（4096 軟上限，超過附 `CMD_LENGTH_WARNING`）、`reject_error_code`、`newline_error_code`、`warning_code`、`newline_forbidden`——上限值與錯誤碼／警告碼字串皆直接引用 `sw_core/arbiter.py` 常數（單一事實來源，含 `ERROR_CMD_TOO_LONG` 等字串常數）。`serialwrap cmd submit --help` 補上限說明：`--cmd` 以 UTF-8 位元組計、broker 不截斷、含 `\n` 直接拒（`CMD_CONTAINS_NEWLINE`），並區分 broker 參數上限與 target 端 tty line buffer（常見 4096）的物理單行限制；README／spec／SKILL.md 同步。
- Windows human console listener 直接講 **Telnet**（`sw_core/telnet_console.py`）：accept 即主動協商（IAC WILL ECHO／WILL SGA／DO SGA／WILL BINARY），入向 IAC 狀態機過濾（吞協商/子協商、IAC IAC 還原、NVT CR NUL／CR LF 摺疊為單一 CR，狀態跨 recv 邊界存活）、出向 0xFF 逸出——Tera Term／PuTTY 以 **Telnet** 服務連入即得 ssh 般逐字元互動與遠端回顯；POSIX PTY 路徑逐位元組不變。`serialwrap doctor` 平台感知：Windows 檢查 pyserial／PATH／daemon endpoint／SERIALCOMM COM 列舉（dialout 等 Linux 檢查不再誤殺整體 ok），Linux 檢查清單與輸出逐字不變。新增 `serialwrap skill --platform {auto,linux,windows}` 子命令，輸出內嵌操作指南原文（新資產 `sw_core/assets/skill/SKILL_WINDOWS.md`：安裝→daemon start→用戶端→**profile 設定（路徑／三區段結構／Windows targets 綁定範例／no_profiles_loaded 語意）**→console_endpoint→Tera Term Telnet 設定→Windows/Linux 差異對照→MCU 燒錄→疑難排解）。新增 `serialwrap --version`（repo VERSION → pip/pipx metadata → PyInstaller 內嵌 assets/VERSION 三段 fallback，release exe 也能報版本）；`daemon start --profile-dir` 補 help 字串（顯示預設路徑）。

### Changed
- **README：英文 operator reference + runtime path 修正**：新增英文 README 段；flash endpoint 與 daemon socket 範例路徑由舊 `/tmp/serialwrap/…` 改為 `<run-dir>/…`（RUN_DIR 預設 `$XDG_RUNTIME_DIR/serialwrap`，可 `SERIALWRAP_TTYMCU_PATH` / `SERIALWRAP_SOCKET` 覆寫），中英範例與 endpoint 對照表對齊。新增 `brag-output/composition-brief.md`（發表影片 composition brief），舊 `brag-plan.md` 改為指向 brief 的 tombstone。

### Fixed
- 修復測試污染 live state.json 的兩個向量：`SessionManager`/`SerialwrapService` 注入 `state_path`（in-process 測試不再寫 live）；CLI `--socket` 改 None sentinel（有傳即明確，杜絕等值誤判把測試 RPC 路由到 live daemon）。新增 `tests/conftest.py` 三層防線（強制 env 隔離／autouse STATE_PATH patch／live guard gate：state/WAL/config/daemon 四維快照，`SERIALWRAP_LIVE_GATE=warn` 逃生閥）、8 檔 per-file 隔離（unittest runner 防線）、coexist/e2e 隔離 config/events 維度（e2e 另補 WAL）＋`addCleanup` 根絕 daemon 洩漏。
- 修復 host 過載／長操作下 CLI 假性 TIMEOUT（daemon 端 recover/attach/self-test/console-attach 為同步長操作，執行時間結構性超過 CLI 舊預設 5s socket timeout；實測 recover CLI 5.08s 報 TIMEOUT、daemon 其實健康且 ~7-8s 後成功），並收斂 review 意見。修法：(1) 全域 `--timeout` 預設改 None，未顯式指定時長操作（session.recover/attach/self_test/console_attach，對照 daemon 端 BLOCKING_RPC_METHODS——console_attach 的 recover 升級分支可同步跑數十秒，先前遺漏，review MAJOR-1 補上）一律採固定 45s 的 timeout floor，一般方法維持 5s、顯式指定一律照用；floor 為誠實推導的寬鬆常數（CTRL_C 2s + CTRL_D 2s + force 硬輪詢 10s + bcm 類慢板多階段 login/ready probe 裕度），不再依 CLI 端 `recover_timeout_s`／`probe_timeout_s` 做「+25s／+15s」縮放——daemon 端對這兩個參數皆有 `min(x, 2.0)` cap，超過 2.0 對執行時間無作用，該推導前提是錯的（review MINOR-2，四處文件與測試同步改寫為誠實版）；(2) TIMEOUT 錯誤 enrich——逾時後以新連線補 1s `health.ping` 輕量探測，錯誤 JSON 附 `daemon_reachable`（bool），可達時再附 `daemon_busy`（`health.status` 的 in-flight commands/sessions 計數），供呼叫端分辨「daemon 死亡/斷線」與「daemon 忙碌、操作仍在跑」（探測總預算 ≤2s，另加 2.5s wall-clock deadline 兜底避免探測本身拖時間，review MINOR-3）；(3) 全域 `--retries N`（預設 0、行為不變），僅對冪等唯讀方法白名單（session list、health.*、device list 等查詢類；console_list 額外 prune 已死 console，該副作用冪等、retry 安全，review MINOR-4）在 TIMEOUT／連線失敗／`EMPTY_RESPONSE`（review NIT-6，與 SOCKET_ERROR 同等可重試）時做指數退避重試（0.5s 起 ×2、單次上限 5s，review NIT-7），寫入類絕不自動重送；file.push/file.pull 暫不納入 floor——誠實列為已知缺口、defer 至 follow-up（review MINOR-5）；event.\* 子命令改用實際送出的 RPC method 名查詢 floor／retry（review NIT-8）。
- `log tail-raw` / `log tail-text` 預設改為 **latest 模式**：省略 `--from-seq` 時回傳符合條件的**最新 N 筆**（seq 升冪），修正原本預設 `from-seq=0` 從最舊 seq 起算、`--limit 10` 在 WAL 100 筆時回 seq 1-10 的反直覺行為。回應新增五個 metadata 欄位：`from_seq`（實際使用值，latest 模式為 `null`）、`last_seq`（回傳紀錄最大 seq，無紀錄為 `null`，可作下次增量起點）、`current_seq`（WAL 目前 seq 計數）、`returned`（回傳筆數：`tail-raw` 計 WAL records、`tail-text` 計文字行數）、`truncated`（是否還有符合但被 limit 截掉的紀錄；latest 模式指視窗前還有更舊紀錄、range 模式指視窗後還有更新紀錄；**判定範圍僅現行 WAL 檔**——輪替歸檔 `raw.wal.ndjson.<ts>` 不列入，rotation 剛發生時 latest 模式可能回不足 limit 筆且 `truncated=false`，需要歸檔紀錄請直接讀取歸檔檔）。RPC 邊界：`from_seq` 顯式 JSON `null` 視同未帶 key（走 latest）；非法值（如空字串、非數字）回 `{"ok": false, "error_code": "INVALID_ARGS"}`。相容性：顯式帶 `--from-seq N`（含 0）仍走舊的 range 增量語意（自 `seq > N` 起最舊 N 筆），老 client 不受影響；legacy `result.tail`（無 cmd_id 的 WAL fallback）與 `wal export` 行為完全不變。
- **recovery/re-attach 時 flush session 命令佇列，修掉 stale accepted 永久佔 pending 額度的洩漏（#128）**：session 因 PROMPT_TIMEOUT 等 recovery 路徑離開 `READY` 時（`_on_detached` → `arbiter.unregister_session`），原本只丟棄 PriorityQueue，佇列中尚未啟動（`status=accepted`、`done_at=None`）的命令記錄永無終結——stale accepted 永久計入 `_count_pending_locked`、佔用 `CMD_PENDING_MAX` 額度，數次 recovery episode 後該 session 直到 daemon 重啟前一律 `SESSION_QUEUE_FULL`，且 client 對被丟棄命令 `command.get` 永遠看到 `accepted`、無從得知已丟失。新增 `CommandArbiter.flush_session()`：unregister 時把尚未啟動的佇列命令以 `status=error`、`error_code=FLUSHED_BY_RECOVERY`、`done_at` 終結（**行為變更**：client 收到此終端態即代表命令未執行、應於 session 回 `READY` 後重送），記錄轉為可淘汰並即刻釋放 pending 額度；in-flight（running/interactive）命令不重複標記，由 worker 以真實結果終結；worker 取件處補「已終結（`done_at` 非 None）即跳過」防 flush/consume race。flush 於 `unregister_session` 首段鎖內與 pop queue 原子完成（防舊 worker 卡 send_cb 超過 join timeout 時，遲到 flush 誤殺 re-register 後新 submit 的 epoch race）；所有 detach 類路徑（clear/release/rebind/熱拔/re-attach）皆用 `FLUSHED_BY_RECOVERY`，daemon shutdown 用 `FLUSHED_BY_SHUTDOWN`（語意相同＝未執行、可重送）；flush 不主動 evict，避免超量時剛 flush 的記錄被當場淘汰成 `CMD_NOT_FOUND`。
- 新增 U-Boot autoboot 保護（boot quiet window）：DUT reboot 時 daemon 的自動 probe（reboot recovery／readiness reprobe 送的 `\n`）必然落入 U-Boot autoboot 倒數窗，打斷開機、把板子永卡 bootloader prompt（`=> `），session 從此回不到 `READY`。現在 agent 送出 reboot 類指令當下、或 RX 見到 boot banner（`U-Boot`／`Hit any key to stop autoboot`，涵蓋 DUT 自行重開）即進入 quiet window（預設 180s）：視窗內 gate 所有 `source=system` 的自動 probe TX（reboot recovery 迴圈、reprobe 的 prepare 與寫入前最終驗證、attach probe），純被動等 RX；RX 匹配該 session 的 `login_regex`／`prompt_regex`（開機完成訊號）即刻解除並自動回 `READY`。human console bytes、interactive lease TX 與 agent 顯式命令一律不受 gate（與 #114 刻意進 bootloader 相容）。recovery deadline 延伸涵蓋視窗結束後至少一輪 probe，並設上限防 boot-loop 板無限延命。session 公開欄位新增 `boot_quiet_remaining_s` 供觀測。另 prpl-template 資產補上 `bootloader_prompts`（`^=> $`、`^U-Boot> $`），卡 bootloader 時 `interactive-open --allow-attached` 的正規 recovery lease 可用。
  
  **對抗審查收斂（同 PR 追加）**：初版把 gate 撒在 reboot recovery／reprobe／attach 三處呼叫端，但漏了另外三條一樣會送 `source=system*` bytes 的路徑，逐一補上：(1) `_recover_after_failure` 的 CTRL_C/CTRL_D 強制按鍵迴圈——agent 命令逾時、target 恰好自發重開時最容易踩到，quiet window 內改為跳過強制按鍵直接走 timeout 收尾；(2) `session self-test` 的 READY 分支 nonce probe——`session.state` 未被 boot quiet 降級（見下方已知限制），會出現「state=READY 但已在 quiet window」的過渡態，改回報 `classification: "AUTOBOOT_QUIET"` 不送 probe；(3) `attach_session`（ATTACHED 無 human lease 分支）與 `recover_session`（ATTACHED 分支重探）共用的 `_probe_existing_bridge`——兩條呼叫路徑於此單點一起補 gate，回 `PROMPT_UNAVAILABLE`、不改動 `session.state`。另修正 quiet window 解除條件的誤觸：寬鬆撰寫的 `prompt_regex`（如 brcm-template 風格 `[>#]\s*$`）會誤配 U-Boot 自己的 `=> ` prompt，在板子仍卡 bootloader 的最危險時刻解除視窗；`_update_boot_quiet_locked` 現在解除前先用既有 `_matches_any_bootloader_prompt` 排除該 session 的 `bootloader_prompts`，命中即視為仍在 bootloader、本輪不解除。`_spawn_reboot_recovery` 順手改呼叫 `boot_quiet_active()` 統一入口，不再直接比較 `boot_quiet_until` 屬性。四項均補上 `tests/test_boot_quiet.py` 回歸測試（含「拿掉 gate 會變紅」的反證，確認測試非 vacuous）。
  
  **已知限制（未修，待開 follow-up issue）**：`boot_quiet_until` 只影響上述自動 probe/按鍵 gate，不會降級 `session.state`；自發重開機與進行中的 agent 顯式命令競速、session 仍（名義上）停在 `READY` 時，該顯式命令仍可能在 autoboot 倒數期間打到 UART——需要新增 state 或 arbiter 層 gate 才能堵住，本次收斂範圍不含此項。
- 原生 Windows CLI 三修（POSIX 行為逐位元組不變）：(1) CLI endpoint 解析平台感知——win backend 預設改用 `DEFAULT_ENDPOINT`（tcp loopback）；`_endpoint_alive`／`rpc_call` 於無 `AF_UNIX` 平台對 unix endpoint 回結構化錯誤／視為不可連，不再 `AttributeError` 崩潰；config.yaml 殘留 WSL unix `socket_path` 時比照 #108 語意自動改連 tcp canonical 並印 stderr 提示。(2) `daemon start` 於 Windows 可用：spawn 預設 `--socket` 改 tcp、支援 PyInstaller 凍結模式（同層 serialwrapd.exe → PATH → `-m sw_core.daemon` fallback，全落空回 `DAEMON_BINARY_NOT_FOUND`）、以 `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP` 脫離父 console、readiness 等待窗放寬至 10s、env 檔於 Windows 改用內建最小解析（不經 bash——Git Bash 的 `env -0` 會把整個環境 MSYS 化，spawn 出的 daemon 拿到不可用 PATH）；`--endpoint` 於 Windows 開放 loopback tcp:// 作為本機 bind 位址（非 loopback 照舊 `REMOTE_NOT_SUPPORTED`；與 `--socket` 同給且不一致回 `INVALID_ARGS`）。另：win backend 下 config 殘留死 tcp port 時 `_endpoint_alive` 改實測（lock_win 0.2s probe）使 #108 fallback 生效；`SERIALWRAP_RPC_BACKEND` 為未知值時 CLI 退回實際平台判斷不 traceback。(3) session payload（`session list`／`attach`／`self-test`）新增 `console_endpoint`（僅非 None 時輸出，POSIX 輸出不變），直接可見 COM ↔ human console TCP port 對應。
- 修復 Windows 上 `_save_state` 並發 `os.replace` 撞 `WinError 5`（存取被拒）導致 attach 執行緒靜默死亡、`state.json` 更新遺失：I/O 段以 instance 級 `_state_io_lock` 序列化（消除行程內並發 replace；仍為 last-writer-wins），並新增 `_replace_state_file()` 於 Windows 對 `PermissionError` 短退避重試（外部讀者／防毒瞬時 handle），重試耗盡才上拋。POSIX `rename()` 語意不受影響（不重試、行為不變）。原 flaky 的 `test_session_bind::test_multi_device_auto_bind_order`（clean main 2/4 重現）修復後 6/6 通過。
- 修復 Windows TCP console 於 agent 命令執行期間（suspend）連入時被誤授 raw interactive ownership 的問題（自 #84 PORT-2 起存在）：`suspend_interactive()` 會把 `_interactive_owner` 暫存為 None，新連入的 console 因此被誤判為「首個 console」直接取得 raw ownership——鍵擊繞過 deferred buffer 直寫 UART、汙染 agent 命令輸出，且 `resume_interactive()` 以暫存值蓋回後該 console 永久卡在 line-buffer 模式。修正授予條件為 suspend-aware：suspend 中且原無 owner 時改記到 `_suspended_owner`，期間輸入走既有 deferred 分支累積、resume 時無縫接手 raw ownership 並 flush；suspend 前已有 owner 者維持第二 console 的 line-buffer 行為。POSIX（PTY／lease 路徑）不經此程式碼、行為不變。
- `serialwrap setup` 物化新版 `~/.agents/skills/serialwrap` skill 後，會清理舊版 `~/.agents/skills/serialwrap-mcp` legacy symlink，避免 agent 同時載入新舊 skill 並誤走已退役 MCP 流程；清理僅針對已知 legacy symlink，不刪除同名真實目錄或一般檔。

## [0.2.2] - 2026-07-01

### Added

- **release flow 自動產出 Windows exe（#84 PORT-4 follow-up）**：`.github/workflows/release.yml` 新增 `publish-windows-exe` job（`windows-latest`，`needs: publish`）——`pip install .`（帶入 pyyaml/pyserial 供打包）+ `pyinstaller serialwrap.spec`，smoke 測試 `serialwrap.exe`/`serialwrapd.exe --help`，再 `gh release upload` 把兩個 exe 附到同一個 tag release。tag `v*` push 時，release 將同時含 wheel 與 Windows one-file exe（先前僅手動 `scripts/build_windows.ps1`）。
- **Windows daemon 完整實作（#84 PORT-4 整合）**：Windows daemon 走 TCP loopback RPC（預設 `tcp://127.0.0.1:48700`，可 `--socket` 參數或 `SERIALWRAP_ENDPOINT` / `SERIALWRAP_TCP_PORT` 環境變數覆寫）＋ msvcrt singleton lock（`WindowsSingletonLock`）；SERIALCOMM registry 列舉可用 COM，雙重藍牙排除（BTHENUM PortName 掃描 + `bthmodem` device path 啟發式兜底），`config.yaml::windows.exclude_coms` 可再手動排除；閒置非藍牙 COM 自動以 `passthrough` profile 接管 session（agent 可觀察 UART，需下命令請先 pin profile 並 attach）；PyInstaller one-file 打包為 `serialwrapd.exe`/`serialwrap.exe`（`serialwrap.spec`，建置腳本 `scripts/build_windows.ps1 -Clean`）；三個平台 seam（RPC/lock/device）分檔於 `rpc_posix.py`↔`rpc_win.py`、`lock_posix.py`↔`lock_win.py`、`device_source.py`，由 `platform_backends.py` 的 `select_*_backend()` 依 `os.name` 自動選擇（`SERIALWRAP_{RPC,LOCK,DEVICE}_BACKEND` env 可覆寫）；POSIX 路徑全程 byte-identical（shim 維持相容）。
  - **已知限制（follow-up）**：持續被外部程序佔用的 COM 不會每輪自動輪詢重試（與 POSIX dynamic-session 同語意；需拔插或手動 `session bind`/`session clear` 觸發）。— #84 PORT-4 已知限制，後續處理。
  - **後續工作（follow-up）**：Windows TCP RPC loopback 目前無 token 驗證，本機任意行程可連並下任何 RPC 指令（不同於 POSIX AF_UNIX 的檔案權限保護）；RPC token 驗證（Windows TCP loopback 存取控制）列為後續工作項目。

- **daemon/service 接線 selector，Windows 走 TCP/msvcrt/SERIALCOMM（#84 PORT-4）**：`daemon.py` 的 `--socket` 預設改為 `DEFAULT_ENDPOINT`（POSIX 等同 `SOCKET_PATH`，Windows 為 `tcp://127.0.0.1:48700`）；`_build_lock()` / `_build_server()` 依 `select_lock_backend()` / `select_rpc_backend()` 以延遲 import 選 `WindowsSingletonLock` / `TcpRpcServer`（Windows）或 `SingletonLock` / `JsonRpcUnixServer`（POSIX）；`server.start()` 後呼叫 `_write_config_endpoint()` 將有效 endpoint 寫入 `config.yaml::socket_path`，供 CLI `_resolve_endpoint` 連得上。`service.py` 的 `DeviceWatcher` 建構依 `select_device_backend()` 注入 `WindowsDeviceSource(exclude_coms=_load_exclude_coms())`（Windows）或預設 `PosixDeviceSource`（POSIX）；`_load_exclude_coms()` 從 `config.yaml::windows.exclude_coms` 讀取排除清單。`RuntimeConfig` 新增 `set_socket(endpoint)` 方法，只更新 `socket_path` 不改動 `supervision_mode`。新增 `tests/test_daemon_service_selector.py`（含 parser default、_build_lock/server win/posix、_write_config_endpoint、set_socket、_load_exclude_coms、DeviceSource 注入等共 13 個測試項）。

- **Windows 相容性：flash_endpoint、session_manager dir-fsync（#84 PORT-4 衍生）**：`flash_endpoint.py` 將 `import pty` / `import tty` 移入 `FlashEndpoint.start()` 延遲 import，使模組在 Windows 可正常 import；`start()` 在無 PTY 平台（`ImportError`）為 no-op。`session_manager.py` 的 `_save_state()` 對目錄 `os.open`+`fsync`（POSIX 磁碟 entry 持久化）加 `sys.platform != "win32"` 守衛，消除 Windows `PermissionError`。兩項修正使 `import sw_core.daemon` / `import sw_core.service` 在 Windows 不報錯，並同時解除多支 service 相關測試在 Windows 的 collection error（`test_command_guard`、`test_flash_guard`、`test_flash_probe`、`test_mcu_cli_rpc`、`test_service_human_console` 等）。`test_flash_endpoint.py` 加 `pytestmark skipif(win32)`（PTY 功能 Windows no-op，跳過對應測試）；`test_rpc_blocking_offload.py` 的 Unix-socket 並發測試加 `skipif(win32)`（AF_UNIX 不適用）。

- **Windows singleton lock（#84 PORT-4）**：新增 `sw_core/lock_win.py` 的 `WindowsSingletonLock`，以 `msvcrt.locking`（`LK_NBLCK`）對 lock 檔取獨佔鎖，並以 TCP connect 探測 endpoint 存活。語意對齊 POSIX `SingletonLock`（flock + Unix socket probe）：endpoint 可連 → `DAEMON_ALREADY_RUNNING`；refused/timeout（stale）→ 繼續取 msvcrt 檔鎖；檔鎖已被持有 → `DAEMON_ALREADY_RUNNING`。`_endpoint_alive` 抽成 module-level 函式可 monkeypatch，測試跨平台；`msvcrt` 延遲 import 使模組在非 Windows 亦可 import。

- **COM 編號確定性綁定 by-id（#100）**：`serialwrapd` restart 後 COM 名與實體板的對應會被重新指派（對調）——根因是 COM 號**不是**在裝置列舉時決定（`device_watcher._scan` 已 `sorted`），而是在**每個裝置各自的並發 attach thread 內**取「目前最低空 COM 號」（`_next_dynamic_com`），startup 多裝置並發時誰先搶到 `self._lock` 誰拿 COM0 → 分配順序＝thread 完成順序（受 probe/IO timing 影響）→ restart 後對調。修法：(1) 新增 module-level `device_sort_key(by_id, by_path)` 排序鍵（以 by-id 為主）；(2) `SessionManager.prepare_dynamic_rank(by_ids)` 在 `self._lock` 內、**spawn attach threads 之前**對整批在線 dynamic 裝置依排序鍵一次配好 COM rank（存入 `_pending_com`），`_session_from_template` 建立 dynamic session 時優先消費預配號（`_pending_com.pop`）、無則 fallback `_next_dynamic_com`；(3) `SerialwrapService.start()` 於 `update_devices`/`bootstrap_attach`（兩條會觸發並發 attach 的入口）之前呼叫 `prepare_dynamic_rank`，兩入口收斂走預配。rank 作用域僅 dynamic 自動偵測 session——explicit YAML `targets`、`session.bind`/`_binding_overrides`、RELEASED 的 by-id 一律排除在 pool 外，COM 由其權威來源決定。`_pending_com` lifecycle：裝置離線時 `update_devices` 會 prune 其 pending（避免已離線 by-id 長期 reserve COM 號），`_next_dynamic_com` 改為純掃描最低空號（移除原 `max_sessions` cap 全滿時可能回已用號的 fallback bug）。runtime hotplug 維持現有行為：不同 by-id 的板繼承空出的 DETACHED 槽，同 by-id 重接拿回原槽。**by-path tiebreak**：`device_sort_key` 已接受 by-path 參數做同款晶片（如 CH340）by-id 衝突的次序 tiebreak（排序單元已備），但 end-to-end 完整支援為 **TODO**——需 `DeviceInfo.by_path` 欄位接上資料來源，在此之前 rank 僅依 by-id。**on-demand `session renumber`（RPC/CLI）已 defer 至 follow-up（#103）**：強制重編 active session 牽動 attach 時以值捕捉 `session_id` 的 bridge callback、flash state、lease reverse-link，須改以「拆 bridge → 改號 → 重 attach」另案重做。新增 `tests/test_com_rank.py`。
- **同機多開（two-reader）被動偵測，暴露到 doctor / daemon status（#101）**：同機可同時跑多個 `serialwrapd`（不同 socket / 監管模式，如 systemd-user 與 system 並存）造成 two-reader 靜默掉字，`SingletonLock`（per-`(lock_path, socket_path)` flock）擋不到不同 socket 的第二個 daemon。新增 module-level `sw_core/multi_open.py` 的 `detect_multi_open(proc_root, tty_paths)`：掃 `/proc/*/cmdline` 找其他 `serialwrapd`、best-effort 讀 `/proc/<pid>/fd` 找 tty 持有者，回 `{multi_open, daemons, holders, holders_status}`；跨 uid 讀不到 fd symlink 時**明確降級**為 `permission`/`unknown`（此降級資訊本身即為輸出契約）。**純被動、on-demand、不終止任何 daemon、不退讓、無背景週期掃描**。兩個 surface：`serialwrap doctor` 新增 daemon-less 的 `_check_single_daemon`（回 `{check, ok, detail, fix}`，多開時 `ok=False` 並給停掉多餘 daemon 的 fix）；`serialwrap daemon status`（`health()`）回應加 `multi_open` / `foreign_holders` / `multi_open_detail`（`daemons` + `holders_status`）三欄，tty 持有者比對各 session 的 `attached_real_path`，`/proc` 掃描留在 service lock 之外。新增 `tests/test_multi_open_detect.py`。
- **PyInstaller one-file 打包（#84 PORT-4，Task 12）**：新增 `serialwrap.spec`（PyInstaller 兩 EXE 單 spec，內嵌 `sw_core/assets`，hiddenimports 含 `winreg`/`msvcrt`/`serial`/`yaml`）與 `scripts/build_windows.ps1`（`-Clean` 旗標、自動安裝 PyInstaller、煙霧測試 `--help`）。`pyproject.toml` 的 `[project.optional-dependencies]` 新增 `dev = ["pyinstaller>=6"]`。`sw_core/daemon.py` 與 `sw_core/cli.py` 補 `if __name__ == "__main__": sys.exit(main())` 入口，使 `python -m PyInstaller` 可直接以模組路徑作為 entry。`dist/` 與 `build/` 已在 `.gitignore` 涵蓋，不入版控；實機建置驗收於 Task 14 進行。

### Changed

- **採用 policy conventions v1.0.5 → v1.0.10（pin 重釘到 `ee87a6d5…`）**：`.paul-project.yml`、四份 agent 檔（`managed-by@v1.0.10` marker / `policy_version` / body 內兩處 install·pinned SHA）、`.github/workflows/policy-check.yml`（`uses:` / `policy_engine_ref` / `policy_version` 三處）一併同步至 engine commit `ee87a6d5ed91209d944934a2559f4f2622fd1ac2`（v1.0.10 tag 指向的 release commit；RELEASES.md 表記的 merge SHA `c13a68b0` 為 pre-bump、自報 1.0.9，故不採）。跨版取得 R-23（engine pin attestation，本 repo 未設 `conventions_engine.repo` → NA）／R-24（moc-alignment，未宣告 `moc` → NA）／R-25 `doc_coverage`／R-26 `generated_facts`（皆 opt-in、未宣告 → NA），對現有 policy_check 結果零破壞。並跨越 v1.0.9 的 CHANGELOG per-PR fragment 硬切換——日後 code PR 改在 `changelog.d/<issue>-<slug>.md` 記錄變更，release 時以 `python3 -m policy_check.changelog collate --version X.Y.Z --date YYYY-MM-DD` 收斂為 dated 段；`CLAUDE.md` 變更紀錄政策同步改寫（本次 release 屬過渡版，仍以既有 `[Unreleased]` 手動收斂）。
- **說明文件對齊現行設計（除 spec）**：`CLAUDE.md` 補上 `sw_core/serial_port.py`（#84 PORT-1 SerialPort 抽象）/`sw_core/multi_open.py`（#101）到架構模組清單、human console 的 Windows TCP socket 路徑（#84 PORT-2）、COM 編號 by-id 確定性 rank（#100）與 daemon 多開被動偵測（#101）兩條慣例。`README.md` 修正過時敘述：WAL/state 預設路徑改 XDG（`~/.local/state/serialwrap/`，舊 `/tmp/serialwrap/` 標為舊版）、human attach 命令 `~/.paul_tools/minicom COMx`→`serialwrap-minicom COMx`、OPI 範例 profile 目錄改 `~/.config/serialwrap/profiles` 並改用 `serialwrap service restart`。`docs/design-file-transfer.md`（含 #59 已退役的 MCP 段）與 `docs/design-heartbeat-keepalive.md` 補「歷史快照」banner 指向現行來源。serialwrap **skill**（`sw_core/assets/skill/SKILL.md`）修正 socat 範例 socket 路徑（`/tmp/serialwrap/serialwrapd.sock`→`/run/serialwrap/serialwrapd.sock` + 監管模式註記）並補 #101 多開（two-reader）診斷 FAQ；CLI `daemon status` **help** 字串補 `multi_open` 偵測。spec（`docs/serialwrap-spec.md`、`openspec/specs/*`）與歷史快照／release notes 不在本次範圍。
- **policy R-16 cli-help 覆蓋擴大**：`.paul-project.yml` 的 `cli` 段新增 `daemon`／`session`／`device` 三個群組 help 宣告，並於 `README.md` 補對應 `cli-help` marker 區塊（pin `serialwrap <group> --help` 輸出）。新增子命令但未同步 README 群組 help 時 R-16 會 FAIL，補掉「子命令層漏記」的盲區（policy 引擎側的範圍/語意擴充另記於 paulsha-conventions#26）。

### Fixed

- **`session attach` 未達 READY 時誠實回報錯誤（#94 ask#1）**：對已 `ATTACHED` 且 command-capable 的 session 做同步 readiness probe，probe 失敗時 `attach_session`（`sw_core/session_manager.py`）原本一律回 `ok:True`、把真錯（如 `PROMPT_UNAVAILABLE`）只埋進巢狀 `session.last_error`、頂層無 `error_code` → 上層（如 wifi_llapi driver）拿到空 error（`serialwrap command failed: session attach ...:` 冒號後空白）。修正（方案 C）：attach 進入點在 command-capable session 未達 READY 時改回 `{ok:False, error_code:<last_error or NOT_READY>, session:...}`（**同步 probe post-process、human interactive lease 早退、bridge-present 其他態（`FLASHING`→`FLASHING_BUSY`／`RECOVERING`→`SESSION_RECOVERING`）三條路徑皆涵蓋**——後兩者為 codex 對抗審查兩輪補洞；`READY`／`ATTACHING`／passthrough 維持 `ok:true`），CLI exit code 隨之為 2；共用 helper `_probe_existing_bridge` 不動（保留 reprobe/recover 的 stay-ATTACHED、非硬錯語意），passthrough（非 command-capable）停在 ATTACHED 仍視為成功。另 `_run_rpc`（`sw_core/cli.py`）失敗時除 stdout 的機器可解析 JSON 外，加印一行具體 error 到 stderr，讓依 Unix 慣例讀 stderr 解釋非零 exit 的 consumer 不再拿到空字串。實機重現＋修正後複驗（throwaway daemon + ttyUSB1 強制 probe 失敗）。新增 `tests/test_attach_error_surface.py`（handler `ok:False`+頂層 `error_code`、passthrough gate 守護、CLI stderr 共 3 測試，TDD RED→GREEN）。
- **測試臨時目錄洩漏修正（test hygiene）**：數個測試以 `tempfile.mkdtemp()`（不會自動清）建臨時目錄卻無對應清理，每跑一次就在 `/tmp` 留下殘骸（`sw-wal-*` 等 prefix 與 generic `/tmp/tmpXXXX`）；累積會混淆實機除錯（誤把測試殘留當 production WAL 洩漏）。修正：`tests/test_event_engine.py`（`sw-wal-` wal_dir）、`tests/test_file_transfer.py`（2 處 `outdir`）、`tests/test_session_capture.py`（8 處 setUp `self._tmpdir` + 4 處 local `tmpdir`）各以 `self.addCleanup(shutil.rmtree, <dir>, ignore_errors=True)` 補清（失敗亦清、不依賴 tearDown）。production `sw_core/` 不受影響（WalWriter 寫 XDG state dir，從不建 `/tmp/sw-wal-*`）。
- **daemon start/connect 韌性強化（#108 #1/#2 hardening）**：#108 頭號根因（POSIX daemon 啟動改寫 `config.yaml`）已由 #84 PORT-4 POSIX guard（PR #109）修掉；本變更補其殘留兩項：
  - **#1：`serialwrap daemon start` 納入監管模式 gate（`sw_core/cli.py` `_run_daemon_start`）**——systemd 模式下重導到 `service start`（回應含 `_routed_to:"service start"`，對稱於既有 `daemon stop`→`service stop`），不再顯式 spawn 非託管 daemon；故 `minicom_router` 的 `AUTO_START_DAEMON=1` 在 systemd 模式不再生 coexist daemon。on-demand 模式 spawn 前以 `health.ping` 冪等探測（`_probe_healthy_daemon`），已有健康 daemon 時回 `{ok:true, already_running:true}` no-op。`daemon start` subparser 補 `--with-sudo`；`should_auto_spawn()` 由 dead code 接回使用。
  - **#2：`_resolve_endpoint` 對 dangling socket 依 `supervision_mode` fallback**——當 `config.yaml` 的 `socket_path` 為不可連的 unix socket 時，依 `supervision_mode` 推 canonical endpoint（`systemd-system`→`SYSTEM_SOCKET`；其餘→`SOCKET_PATH`）並改連之（stderr 提示），canonical 不可連或同值則回原值讓既有錯誤照常浮現；新增 `_endpoint_alive()` helper（unix connect probe，非 unix endpoint 跳過）。CLI 對 `config.yaml` 維持唯讀、不 self-heal 改寫。明確 `--endpoint`/`--socket` 不受 fallback 影響。
  - **不實作（結案說明）**：`/tmp/sw-coexist-*` GC 與 dead-pid lock GC——前者為測試（`tests/test_human_agent_coexist.py`）tempdir、非 production 產生；後者已被 `SingletonLock`（flock 持有者死亡由 kernel 自動釋放 + stale socket `unlink` 回收）容忍。
  - 新增 `tests/test_cli_daemon_start.py::TestDaemonStartSupervision`、`tests/test_resolve_endpoint_fallback.py`（TDD RED→GREEN）；實機驗證（systemd-system，COM0/COM1）：`daemon start` 無 `--with-sudo` 回 `NEEDS_SUDO`+`_routed_to` 不 spawn、`--with-sudo` 走 `service start` idempotent、config.yaml 不被改寫、doctor 單一 daemon；config socket 改死路徑後 `session list` 仍依 fallback 連上系統 daemon（COM0/COM1 READY）並印提示。
- **PyInstaller exe CLI entry 修正（#84 PORT-4 follow-up）**：`serialwrap.exe` 實機建置後 `--help` 因 `sw_core/cli.py` 為 package-relative import、被 PyInstaller 當 `__main__` 直接執行而 `ImportError: attempted relative import with no known parent package`。新增 root `serialwrap.py` 薄 shim（絕對 import `from sw_core.cli import main`，鏡像既有 `serialwrapd.py`），`serialwrap.spec` 兩個 Analysis entry 改指向 root shim（`serialwrapd.py` / `serialwrap.py`）。實機建置驗證：`serialwrap.exe` / `serialwrapd.exe` `--help` 均 exit 0。
- **Windows exe `--help` UTF-8 修正（#118）**：PyInstaller exe 在 Windows console 預設 `cp1252` 印含繁中的 argparse `--help` 時 `UnicodeEncodeError`（`release.yml::publish-windows-exe` 的 smoke test 於首次 tag release 浮現；manual `scripts/build_windows.ps1` 因終端為 UTF-8 未捕捉此坑）。新增 `sw_core/sysenv.py::force_utf8_stdio()`，於 `cli.py` / `daemon.py` 的 `main()` 在 `parse_args()` 之前對 `win32` 把 `sys.stdout` / `sys.stderr` `reconfigure(encoding="utf-8")`（非 Windows no-op；stream 無 `reconfigure` 或 reconfigure 失敗皆安全略過，不讓修正本身成為新失敗點）。新增 `tests/test_sysenv.py` 4 測試（TDD RED→GREEN）。使 v0.2.2 成為完整 win+linux release。
- **Copilot PR #109 review 四項修正（#84 PORT-4 follow-up）**：
  - `platform_backends._select()`：未知後端值（非 auto/posix 別名/win 別名）改 raise `ValueError`，不再靜默退化為 auto。
  - `rpc_win._parse_tcp()`：新增 loopback 驗證，非 loopback host（如 `0.0.0.0`/LAN IP）raise `ValueError` 並附說明，避免 RPC server 對外暴露（RPC 無認證）。
  - `openspec/specs/windows-device-claim/spec.md` Scenario「被佔用埠跳過後重試」：文案對齊實際行為——持續被佔用的埠不會每輪自動輪詢重試；需裝置重新出現（拔插）或手動 `attach`/`clear`。
  - `device_source.WindowsDeviceSource.scan()`：對 `_read_bt_ports()`（BTHENUM registry 遞迴走訪）加 TTL 快取（預設 5 秒，`time.monotonic()`），避免每輪 1s 都重掃。
- **#84 PORT-4 根因修正：Windows daemon 無 profile 時不接管 COM（Task 14b）**：`SessionManager._attach_by_id` 守衛 `if session is None and self._templates:` 在空 templates 時跳過動態接管，導致 daemon 偵測到 COM8（`devices:1`）但 `session list` 為空（`sessions:0`）。修法：`daemon.py` 新增 `_make_windows_passthrough_templates()` 函式，在 `select_device_backend()=="win"` 時若 templates 無任何 passthrough template，自動注入 `ProfileTemplate(profile_name="windows-default-passthrough", platform="passthrough", ready_probe="")` 後再傳入 `SerialwrapService`；POSIX 路徑（`!="win"`）維持「無 profiles → 不自動接管」行為，零 regression。實機驗證（COM8 loopback）：`daemon status` 顯示 `sessions:1`，`session list` 顯示 `platform:"passthrough"`、`state:"ATTACHED"`、`device_by_id:"COM8"`、`profile:"windows-default-passthrough"`。新增自動化測試（不需真硬體）：`test_daemon_injects_windows_passthrough_template_when_no_profiles`（單元，驗證注入邏輯三個分支：空注入/已有不重複/有其他追加）、`test_no_profiles_windows_passthrough_claim`（機制端對端，monkeypatch bridge.start + polling inflight，驗證動態接管 ATTACHED）——兩者均以 TDD RED→GREEN 流程開發。

## [0.2.1] - 2026-06-29

### Fixed

- **`reconcile_readiness` 週期回收孤兒 console（#76）**：`DeviceWatcher` tick 的 `reconcile_readiness` 原只重探非 READY session，從不主動清理 console client，導致死掉的 minicom process 的孤兒 `ConsoleClient` 在 steady state 長期積存（observed：COM0/COM1 死掉的 `primary` console 不消失）。新增 `_HUMAN_PEER_GRACE_S`（3.0s）節流常數、`_last_console_reap_at` 節流時間戳；`reconcile_readiness` 在 jobs loop 之後、節流通過時做一次**共享 /proc 掃描**（`_enumerate_all_held_paths`，鎖外執行）並對所有 bridge 呼叫 `reap_stale_consoles`，單一 bridge 例外不中斷 tick；不具備該方法的測試假物件（`held=None`）各自 fallback 保守掃描。新增 `test_reconcile_reaps_orphan_console` 回歸（RED：孤兒存活；GREEN：孤兒被回收）。底層回收原語為 `UARTBridge.reap_stale_consoles`（lock-split：鎖內快照候選 → 鎖外掃 /proc → 回鎖 pop → 鎖外 close fd，沿用 pop-in-lock/close-out-of-lock 防 #83 use-after-close；硬性跳過當前 `_interactive_owner`/`_suspended_owner` 與 `internal=True` 哨兵 primary，防 #78 suspend 簿記損壞），新增 `ConsoleClient.internal` 旗標標記 `start()` 建立的哨兵 primary（永不回收），死掉的非-internal primary 則可被回收並重指。
- **minicom 無法 Tab 補完、方向鍵變 `[A` 亂碼（症狀1）**：human console 掉回 line-buffer ＝連入 client 未持有 `_interactive_owner == human:<id>`（活機坐實：COM0/COM1 `interactive_session_id:null`、`interactive_owner:false`）。**非 #84 造成**（#84 對 POSIX raw 路徑逐位元組未改、`test_interactive_raw` 全綠）。三條同源觸發分別修復：
  - **觸發A（孤兒讓新 minicom 被跳過授予）**：由上述孤兒週期回收消除。
  - **觸發B（瞬時 peer flap 誤拆 human lease）**：`_refresh_interactive_locked` 對 human lease 的被動拆除加 **peer-loss grace**——`InteractiveLease.peer_lost_at` 記首次 peer 消失時間，僅持續超過 `_HUMAN_PEER_GRACE_S`（3.0s）才 detach+close；peer 在 grace 窗內回復則清除。
  - **觸發C（owner 因故掉失後不自癒）**：`reconcile_readiness` tick 新增 **lease-backed 週期自癒**——bridge idle-無-owner（由原子鎖內 `snapshot()` 判定）且有活 primary console 時，以 `bridge.try_grant_interactive_if_idle()`（單 `_state_lock` check-and-set，消除 snapshot→grant 的 TOCTOU；Codex 對抗審查 finding-2）重授並同時建 session-layer lease（ownership 永遠 lease-backed，不在 bridge 層裸設 owner，防 agent 命令漏 suspend 致 #78/#81 deferred-input 污染）。`snapshot()` 擴充 `agent_active`/`suspended_owner`/`flash_mode`/`primary_client_id` 供決策。新增 grace/自癒/TOCTOU/agent-active-不奪權 回歸測試；#78 suspend/resume 全綠。
- **症狀1/#76 調查附記：SIGHUP trap 非必要**：原以為「關終端 SIGHUP 跳過 console-detach」為孤兒來源之一，經真機驗證（真 minicom + 真 tmux pane kill ＝ process-group SIGHUP）**推翻**——前景 minicom 被 HUP 殺死後 wrapper 仍跑到顯式 cleanup→detach、不留孤兒；故**未改** `minicom_router.sh` trap。真正孤兒來源為 SIGKILL/crash，由上述 broker 週期回收涵蓋。

- **ReDoS `_ambiguity_score` 補 GROUPREF_EXISTS 條件遞迴，杜絕條件分支藏匿指數歧義（#91 follow-up，Codex final3 [high]）**：`_ambiguity_score` 原未處理 `(?(1)yes|no)` 條件 opcode，致 `^(a)?(?(1)(a+)+b|c)$` 把指數 `(a+)+` 藏進條件 yes-分支而漏算（score=1 → 無 re2 時被接受、可凍 matcher）。改為對未明列的容器型 opcode 一律遞迴其子 SubPattern（取 max；單路徑只走一支），涵蓋 GROUPREF_EXISTS 及任何未來容器節點——避免「漏處理某 opcode → 子樹歧義被丟棄」整類盲點。條件 opcode re2 不支援 → 落標準 `re` → 兩引擎皆拒；安全條件（分支非 catastrophic）不誤擋。新增條件藏匿指數 upsert 拒絕回歸。
- **flash 偵測 probe 期間取得 bridge 寫入仲裁，且命中線 gate 持有到 enter_flashing 接管，消除雙寫入者競態（#83 RACE-2）**：`_on_flash_open` 先 `detect_mcu_line` probe 候選、命中後才 `enter_flashing`；probe 期間 `flash_mode` 仍為 False，候選 bridge 上若有 human raw console 正鍵入，其 `send_bytes` 會與 probe 的 sync bytes 在同一 UART 交錯（兩個邏輯寫入者 → 污染 target 輸入行／干擾 ACK 判讀；calibrated medium）。`_BridgeProbe.probe` 在 `flash_tx`+ACK-wait 視窗開 `flash_mode` gate 取得該 bridge 寫入仲裁：console→device 寫入被 drop，而 probe 的 `flash_tx`（帶 `_allow_during_flash`）與 device→buffer 的 ACK 擷取（`_handle_serial_rx` 不看 flash_mode）皆不受影響。**Codex 對抗式審查 [high] 補強**：`detect_mcu_line` 會掃完所有候選做 ambiguity scan，故命中候選若於其 probe 結束即解 gate，會在後續候選 probe 期間（含較晚候選 timeout 拖慢）重新可被 human/console 寫入而污染 bootloader 串流。改為**命中（收到 ACK）即「持有」gate（`transport.held`）不解**，直到偵測完成由 `_on_flash_open` 將命中線交接 `enter_flashing`（`release_held(adopt=com)`，gate 維持）、其餘 held 與早退／例外路徑於外層 `finally` 統一釋放——命中線自 probe 命中到 `enter_flashing` 全程連續 gated、無窗口。**Codex final [high] 補強**：`flash_mode` gate 只擋 console→device **寫入**，但 probe 窗口（`enter_flashing` 之前、`session.state` 仍 READY/ATTACHED）內 destructive **lifecycle op**（`clear_session`/`release_device`/`bind_session`）只守 `state == "FLASHING"` → 仍可 detach/rebind bridge 中斷 probe。新增 per-session `flash_critical` 標記：**四個** destructive guard（`clear_session`/`release_device`/`bind_session`/**`recover_session`** 含 force）改以 `_flash_busy_locked`（`FLASHING` 狀態 **或** probe critical）回 `FLASHING_BUSY`；命中線於 `enter_flashing` 後即由 FLASHING state 接管、立即解標其餘候選（避免長時 pump 誤擋無關 COM）。候選 snapshot 與標記改由 `collect_flash_candidates_and_mark` **在同一把鎖內原子完成**，杜絕「收集候選→釋鎖→標記」之間並發 bind/release 的 TOCTOU（標記後 bind 被擋，probe 期間 device_by_id 不變）。**Codex final2 [high] 補強**：`flash_critical` 不僅 RPC 路徑，**device_watcher 與 bridge-down 回呼**亦須 flash-aware——`_detach_by_id`（device hotplug：MCU reset/BSL 期間 by_id 常短暫消失）與 `_handle_bridge_down` 改為 `_flash_busy_locked` 時略過 detach/reattach，不切斷燒錄/偵測 transport，待 flash 結束後下一個 watcher tick 再 reconcile。**至此 #83 RACE-1/STA-4/RACE-2 全數收斂，issue 結案**。新增 held-on-match、release_held(adopt)、「先命中後較晚候選 timeout」、probe 窗口 clear/release/bind/**recover(force)** 一律 FLASHING_BUSY（含不誤擋他 COM）、snapshot+mark 原子性等回歸。
- **事件規則 regex ReDoS 防護：有 re2＝線性免疫、無 re2＝結構可疑 fail-closed 拒絕（#91，9 輪 Codex 對抗式審查後定案）**：標準 `re` 於 catastrophic backtracking 期間獨佔 GIL 且不可中斷，使用者可控規則 regex 可凍結單執行緒 matcher。`PatternMatcher`：可匯入 `re2`（可選依賴 `pip install 'serialwrap[redos]'` / `google-re2`）時以其線性引擎（Thompson NFA、無回溯）求值，**徹底免疫所有 catastrophic backtracking**（flags 以 inline `(?ism)` 帶入、群組語意與 `re` 一致）；否則退回標準 `re`。**關鍵結論**：對「無 re2 的標準 `re` 路徑」，任何**靜態 AST 啟發式**或**經驗式子行程探測**都被對抗式審查**逐輪繞過**（指數→多項式→寬原子 `[\s\S]*`→大量有界 `a{0,4096}`→backref `(a*)\1`→多字元單元 `(?:ab\|abab)*`→負類排除→inline flags `(?i:…)`→失敗尾→path-context prefix，共 9 輪），且 timing-based 探測本質 flaky——這是無界軍備競賽。故最終定案 **fail-closed**：`validate_rule_dict` 於 upsert，當 pattern 將落到標準 `re` 路徑（`_re2_compile` 回 None＝re2 不可用或 pattern 用 re2 不支援的構造 backref/lookaround）且**結構上可疑**時一律拒絕。可疑判準＝沿 concatenation 路徑「回溯歧義來源數」`_ambiguity_score >= 2` **或** backreference。歧義來源＝可變量詞（`?`/`*`/`+`/`{m,n}` with `mn != mx`）與 alternation（BRANCH）；固定 `{N>=2}` 重複一個有歧義 body 則以 `2*body_score` 反映（把歧義序列化）。理論：單一來源至多 O(n) 回溯（線性）、沿同一路徑 **≥2 來源即可 compound 成超線性**——此統一度量**取代逐一列舉各 catastrophic 類**，一併涵蓋多項式（`a.*a.*`）、指數（`(a+)+`、`(a|aa)+`）、optional-chain（`a?a?…aaa`）、固定重複歧義（`(a?){30}`）與 **unrolled 重疊 alternation 序列**（`(a|aa)(a|aa)…b`，無量詞，Codex final2 [high]）。經 13 輪對抗式審查全部攻擊 + 經典安全 pattern 驗證為 catastrophic 的**完整結構超集**（單一非歧義量詞、單 alternation、固定非模糊 body 如 MAC `(?:[0-9A-F]{2}:){5}…` 皆為線性、安全），故無漏放、確定性、無 flaky，且**不誤擋** `contains` 與單量詞 regex（`Kernel panic`、`temp=(\d+)C`、`root@.*# `、`dhd_dpc.*firmware_trap`；專案內現有事件規則零受影響）。保守代價：少數實際安全的多量詞 regex（`\d+\.\d+`、`\w+\s+\w+`、`tx=\d+ rx=\d+`）在無 re2 時被拒——裝 `serialwrap[redos]` 即解禁。matcher 另以 `REGEX_MATCH_INPUT_MAX=4096` 封頂 re 路徑輸入長度作 runtime 兜底。CI 於同一 job **先無 re2 跑一輪**（驗 fail-closed）**再裝 google-re2 跑一輪**（驗 re2 免疫）。回歸涵蓋 9 輪全部 catastrophic（指數/多項式/寬窄原子/大量有界/backref/多字元單元/負類/inline-flags）無 re2 拒、re2 接受；re2-不可編譯者（backref/超大 repetition）兩引擎皆拒；單量詞安全 pattern 兩引擎皆不誤擋。`_re2_compile` 為 schema 與 matcher 共用的單一引擎判定。**#91 結案**。

- **`_load_state` 讀取失敗改 fail-closed，杜絕以空狀態 clobber RELEASED 交接（#82 Codex 必修）**：原 `_load_state` 以 `except Exception` 將 `PermissionError`／暫時性 I/O 等讀取失敗一律當「損毀」備份至 `.corrupt` 後回傳空狀態，`__init__` 尾段 `_save_state()` 隨即以空的記憶體狀態覆寫磁碟檔 → 遺失 RELEASED（裝置交接）→ 重啟後 daemon 重新 attach 已交給 flasher/人類的 tty 形成 two-reader。改為**嚴格區分**：僅「確認的 JSON 格式損毀」（`json.JSONDecodeError`/`UnicodeDecodeError`）才備份重建；讀取失敗（`OSError`/`PermissionError`）改拋 `StateLoadError`，由 daemon 啟動層攔截、釋放 lock、以非零碼拒絕啟動（原檔絕不動），fail closed 杜絕帶病啟動 clobber 交接。新增 fail-closed 與 `__init__` 不 clobber RELEASED 兩項回歸測試。
- **`_save_state` 改原子寫入，崩潰/磁碟滿不再損毀 state.json 致 RELEASED 交接遺失（#82）**：原 `_save_state` 以 `open(STATE_PATH, "w")` 直接覆寫（先 truncate 再寫、無 fsync），崩潰/斷電/ENOSPC 中途失敗會留下截斷檔，`_load_state` 解析失敗即靜默全棄 → alias/binding/**released（裝置交接）**歸零，重啟後 daemon 重新 attach 已交給 flasher/人類的 tty 形成 two-reader。改為 `tempfile.mkstemp`（唯一名，並發 `_save_state` 安全）+ `fsync` + `os.replace` + 目錄 `fsync`（比照 `wal.py` 慣例）；`_load_state` 遇損毀檔改備份至 `state.json.corrupt` + 告警，而非靜默全棄。新增 `tests/test_state_persistence_atomic.py`。
- **無界 in-memory 結構加上限/淘汰，防長壽 daemon OOM（#81）**：(1) `BackgroundCapture.chunks` 對狂吐 target 無上限 → 改環形上限 `BG_CAPTURE_MAX_BYTES`（增量維護 `total_bytes`、超量丟最舊、`dropped_chunks` 累計使 `get_background_result` 絕對 cursor 仍對齊，未丟棄時行為不變並回報 `dropped_chunks`/`lost`）；(2) `_background` dict 永不淘汰 → 加 `BG_CAPTURE_MAX_COUNT`，超量淘汰最舊「已終結」capture（進行中永不淘汰）；(3) arbiter `_commands` 永不淘汰 → 加 `CMD_HISTORY_MAX`，淘汰最舊「已完成（有 `done_at`）」命令（進行中保留）；(4) agent 命令期間 human deferred 輸入緩衝無上限 → 加 `DEFERRED_INPUT_MAX_BYTES`，超量丟最舊位元組。新增 `tests/test_bounded_memory.py`。
- **補齊 #81 兩處 OOM 漏洞（Codex 對抗式審查必修）+ Copilot perf**：(A) **[high] arbiter command queue admission control**——原 `submit()` 無 per-session pending 上限，`_evict_commands_locked()` 只淘汰已完成（有 `done_at`）命令；client 比 UART worker 快時 `_commands` 與 `PriorityQueue` 會持續累積 accepted/running records 而永不被 cap 淘汰，OOM 修復在 backlog 場景仍失效。新增 `CMD_PENDING_MAX`（每 session 進行中命令硬上限），`submit()` 於同一把鎖內原子完成 admission check + 入列，超限以 `SESSION_QUEUE_FULL`（含 `pending`/`limit`/backpressure hint）拒絕而非排隊——eviction 無法回收尚未執行者，admission control 不可省。(B) **[medium] 淘汰 background capture 同步清 `background_cmd_ids`**——`_evict_background_locked()` 原只從 `_background` 移除，未從擁有者 `SessionRuntime.background_cmd_ids` 移除對應 id；該 per-session list 僅在 detach 時 clear，會隨長壽 daemon 無界成長且 `_on_bridge_rx` 每筆 RX 都逐一掃描死 id（記憶體 + per-RX CPU）。改為淘汰時一併 `owner.background_cmd_ids.remove(cid)`。(C) **Copilot 審查 perf**：`BackgroundCapture.chunks` 環形淘汰由 `list.pop(0)`（O(n) 位移、狂吐時成 CPU 熱點）改 `collections.deque.popleft()`（O(1)，`get_background_result` 切片改 `itertools.islice`）；arbiter `_commands` 的 `CMD_HISTORY_MAX` 淘汰除 `submit()` 外，命令轉終結（done/error/canceled 設 `done_at`）時也觸發，確保尖峰後收斂回上限。新增 admission control（含 drain 後可再 submit）、淘汰同步清 list、deque 環形、終結後收斂的回歸測試。
- **補齊阻塞型 RPC handler 的 executor offload，避免單一慢 RPC 凍結全 daemon（#80，延伸 #52）**：#52/PR#73 僅 offload `file.push`/`file.pull`，但 `session.recover`（force 路徑硬 sleep 10s + probe 數十秒）、`session.attach`（reprobe 完整 ensure_ready/probe）、`session.self_test`（同步等 UART 2×timeout）、`session.console_attach`（可觸發 recover）、`result.tail`/`log.tail_raw`/`log.tail_text`/`wal.range`（整檔讀取）仍在單執行緒 asyncio loop 上同步執行，期間**所有** RPC（含 `health.ping`）凍結。將以上方法併入 `BLOCKING_RPC_METHODS` 一併 offload。安全性：這些 handler 都經 `service.rpc` → `SessionManager`/`WalWriter` 鎖保護方法、本已被多執行緒並發呼叫，於 executor 跑亦執行緒安全；RPC 為每連線請求/回應模型，不影響回應對應。新增 `tests/test_rpc_blocking_offload.py`。（REACT-4：reader thread `_write_all` 於 serial EAGAIN 自旋，屬 reader thread 而非 RPC loop，列為後續。）
- **補漏 `command.result_tail` 的 offload（#80 Codex 必修）**：上一條只加了 legacy `result.tail`，但 CLI `serialwrap cmd result-tail` 實際送的是 `command.result_tail`（`service.rpc` 內與 `result.tail` 同樣 `get_background_result` 讀取/切片大 capture 並序列化）。漏了它，**最常用**的 background 結果查詢路徑仍跑在 asyncio loop 上，大 capture 慢查詢照樣凍結其他 RPC（含 `health.ping`）。將 `command.result_tail` 併入 `BLOCKING_RPC_METHODS`；新增以「實際 CLI 方法名 + 真實 BLOCKING set」驅動的並發回歸（大 `command.result_tail` 進行中 `health.ping` < 1s 仍即時回應）。
- **RX fan-out use-after-close 競態 + 規則 regex ReDoS 防護（#83）**：(RACE-1) `_handle_serial_rx` 原在 `_state_lock` 外才對快照到的 `console.master_fd` 做 `os.write`，與另一執行緒「pop 後 `os.close(fd)`」形成 use-after-close／寫到被重用 fd（資料誤送）窗口 → 改在持 `_state_lock` 下逐一 best-effort 寫入（所有關閉路徑都先在鎖內移出 `_clients` 再於鎖外 close，故持鎖迭代永不含已移除者）。(STA-4) 使用者可控規則 regex 由單執行緒 matcher 無 timeout 逐行求值，catastrophic backtracking 可凍結 event engine → `validate_rule_dict` 對 regex 加長度上限 + 巢狀量詞（`(a+)+` 類）靜態拒絕。新增 `tests/test_concurrency_races.py`。（RACE-2 flash 偵測 probe 與進行中命令的雙寫入者競態，需 flash 流程寫入仲裁，列為後續，#83 保留追蹤。）
- **強化 ReDoS 防護，堵住可繞過的 catastrophic regex（#83 STA-4 Codex 必修）**：原靜態防護只是「長度上限 + regex-on-regex 巢狀量詞 heuristic（`(...[+*]...)[+*]`）」，可被 `(a|aa)+$`、`(a?)+$`、`(a{1,3})+$` 等同樣指數回溯的模式繞過（matcher 仍單執行緒 `re.search` 無 timeout）。CPython `re` 於 catastrophic backtracking 期間獨佔 GIL、不檢查 signal，thread/signal per-line timeout 無法中斷，故改採兩層：(1) upsert 時以 **stdlib 解析器走訪 AST** 結構性拒絕「重複>1 次且重複單元含巢狀量詞或 alternation（模糊重複）」的整類指數 ReDoS（取代脆弱 heuristic，完整涵蓋上述繞過例，且不誤拒 `(abc)+`／頂層 alternation／單一 bounded repeat 等安全 pattern）；(2) matcher 對 regex 求值的輸入長度封頂 `REGEX_MATCH_INPUT_MAX=4096`（contains 字面量不截斷），使殘餘多項式回溯成本有界、即便靜態分析漏放亦不凍結 matcher。新增 bypass pattern 拒絕、安全 pattern 不誤拒、輸入封頂三組回歸。（多項式 ReDoS 殘餘移交 #91 追蹤；完全消除需 re2 類線性引擎。）
- **長壽 worker thread 加例外防護，單一 I/O/handler 例外不再靜默殺死整條 thread（#79）**：(STA-1) UART reader `_loop` 的 RX handler 派發包 try/except——原本 `_handle_serial_rx` 首步 `wal.append` 在 ENOSPC/EROFS/fd 耗盡時拋 OSError 會殺死 reader（RX 永久停擺、session 卻仍顯示 READY）；另 `WalWriter.append` 寫入改 best-effort（失敗標 `loss_flag`+告警、不拋，讓 console fan-out 續行）。(STA-2) `DeviceWatcher._loop` 的 `poll_once`/`on_tick` 包 try/except，避免一次例外讓 hotplug 與週期 reconcile 永久停擺。(STA-3) event matcher `_run` 的 `_evaluate` 包 try/except，counter store I/O 失敗只丟該 item、不殺 matcher。(STA-6) reboot-recovery worker 的 `ensure_ready`/login 包 try/except，例外時續迴圈待逾時收尾、不讓 session 永卡 RECOVERING。新增 `tests/test_thread_survival.py`。（STA-7 SingletonLock liveness 邊界為 low，列為後續。）
- **WAL 輪替例外納入 append 失敗含住，rotation 失敗不再逃出殺 RX reader（#79 Codex 必修）**：上一條只把 `append` 的「開檔/寫入」包進 best-effort，但 `_rotate_if_needed()` 在 try 之外先執行；rotation path 的 `getsize`/`replace`/`open(dir)`/`fsync(dir)` 因 EIO/權限/fd 耗盡拋 OSError 時，例外仍會逃出 `append` → 上拋 `_handle_serial_rx` 殺死長壽 RX reader（含 console fan-out 一併中止）。改為 `_rotate_if_needed()` 自身 best-effort 含住 OSError：失敗則告警、續用既有（可能超大的）檔案、回傳 `rotation_failed` 旗標，`append` 將其標於 record（conditional key，常態 record 向後相容）且資料仍正常寫入。新增「rotation 失敗不殺 reader、仍落盤、標 rotation_failed」回歸。
- **`suspend_interactive`/`resume_interactive` 改為可重入，修正並發 agent 操作使 human 永久失去 raw ownership（#78）**：原本以單一 `_suspended_owner` 保存 owner，兩條 agent 路徑（`execute_command`／`file_push`/`pull`／`self_test`／interactive lease soft-preempt）重疊呼叫時，第二次 suspend 會把已保存的 `human:X` 覆寫成 `None`，resume 後 human 永久掉到 line-buffer/deferred（打字卡/掉字）。改以巢狀深度計數（`_suspend_depth`）：只有最外層 suspend 保存 owner、最外層 resume 才還原並 flush deferred；不平衡 resume 為 no-op；被 suspend 的 human console 斷線則重置深度。新增 `tests/test_suspend_resume_reentrant.py`。
- **systemd-system 模式可在 pipx 使用者安裝下實際啟動（#76）**：原 system unit 寫死 `User=serialwrap` dedicated account 且 `ExecStart=/usr/local/bin/serialwrapd`，但 pipx 只把 serialwrapd 裝到安裝者家目錄 venv（`~/.local/bin/serialwrapd`）、且 install 從未建立 `serialwrap` 帳號 → `setup --system` 起不來。改為 **run-as-user**：`render_system_unit(exec_start, run_user=...)` 參數化 `User=`，`setup_cmd` 以 `_resolve_run_user()`（`SUDO_USER`→`getpass.getuser()`）帶安裝者本人帳號、`_resolve_serialwrapd_path()` 解析其 pipx serialwrapd 絕對路徑組 ExecStart。保留「非 root + dialout」安全邊界，socket 仍於 `/run/serialwrap`（不受 WSLg `/run/user` 遮蔽、不依賴脆弱的 `user@<uid>`）。

### Added

- **跨平台序列埠抽象 `SerialPort` + Windows 原生序列埠與 TCP human console 支援（#84 PORT-1/PORT-2）**：核心 broker 邏輯（仲裁/狀態機/WAL）本就 OS 無關，但序列埠 I/O 原先在 `sw_core/uart_io.py` 直接綁 `termios` + `os.open` + `fcntl`（`_BAUD_MAP` 用 `termios.Bxxxx` 常數於 import 期求值），使該模組在 Windows（無 `termios`/`fcntl`）**連 import 都 `ModuleNotFoundError`**，整個 daemon 載入即失敗；且 repo 文件聲稱依賴 `pyserial` 但實際完全沒 import（pyproject 僅 `PyYAML`）。新增 `sw_core/serial_port.py` 把序列埠 I/O 收斂成單一 `SerialPort` 介面（`open`/`configure`/`read`/`write`/`close`/`set_baud`/`is_alive`/`fileno`）與兩個後端：`_PosixSerialPort`（維持現有 termios 行為，**Linux/WSL 生產路徑逐位元組等價**，`fileno()` 暴露可被 `select()` 多工的整數 fd）、`_PySerialPort`（pyserial，Windows；序列埠 handle 無法被 `select()` 故 `fileno()` 回 `None`，改走「阻塞讀取＋read timeout」路徑）；後端由 `open_serial_port` 依平台選擇，並可用 `SERIALWRAP_SERIAL_BACKEND`（`auto`/`posix`/`pyserial`）覆寫以利測試。`uart_io.py` 移除 top-level `import termios/fcntl` 與 `_BAUD_MAP`（PTY console 相關 termios/fcntl 改 lazy import），`_loop`/`send_bytes`/`snapshot`/`mirror_termios_from` 依 `_serial_fd`（POSIX 為真實 fd、Windows 為 None）分派：**POSIX 熱路徑（select/`os.read`/`self._write_all`）逐字不變**，Windows 走 pyserial 分支。（唯一刻意的 POSIX 行為微調：`mirror_termios_from` 鏡射失敗時的 fallback baud 由原本 raw 寫 `c_ispeed/c_ospeed`（不設 `CBAUD`）改為 `port.set_baud()` 走 `cfsetispeed/cfsetospeed`——後者同時設 `CBAUD`、在現代 glibc 上才能讓標準速率真正生效，屬修正而非回歸，僅影響 #55 flash 接手且鏡射拋例外的罕見分支。）**PORT-2（Windows human console）**：無 PTY 平台 `start()` 改開 `127.0.0.1` TCP listener ＋ console accept/pump thread，**TeraTerm（TCP/IP, Other）/PuTTY（Raw）**連入即一個 socket-backed `ConsoleClient`，沿用既有 console 狀態機（raw ownership、suspend/resume coexistence、RX fan-out），agent 下命令期間連線不中斷；`ConsoleClient` 增 `sock` 欄位、`_console_send` 依 sock/PTY 分派、斷線由 `recv b""` 偵測 drop，`console_endpoint()`/`attach_console()` 回傳端點。Linux PTY 路徑邏輯逐字不變（新行為一律 gate 在 `sock is not None`/`not _pty_available()`）。`#69` flash gate 原子性（`_write_lock ⊃ _state_lock`、授權綁內部能力而非可控 `source`）完全保留。`pyproject.toml` 新增 `pyserial>=3; sys_platform=='win32'` 依賴（修正「文件聲稱依賴卻未使用」）。新增 `tests/test_serial_port.py`（factory 後端選擇、device 名稱正規化、importability、POSIX PTY 往返、env-gated 真機 loopback 與 UARTBridge 端到端；並含 **`_serial`/`_serial_fd` 成對設定/清除不變式**回歸與 **Windows 雙執行緒（serial reader ↔ console thread ↔ flash gate）並發無死鎖**壓力回歸——後者守護 `_write_lock ⊃ _state_lock` 單向鎖序）。`UARTBridge` 補上鎖序/reader-thread 模型/`_serial` 配對不變式的 class docstring。**真機 gate 已通過**：Windows 對 CH340（COM8，TxRx 短接 loopback）實測 `import sw_core.uart_io` 成功、`UARTBridge` start/RX/TX/WAL/clean-stop 全過、`test_uart_flash_bridge.py` 既有契約測試（直接戳 `_serial_fd`/monkeypatch `_write_all`）於 Windows pytest 全過。**Copilot PR review 補強**：factory 對非 POSIX 強選 posix 後端回明確 `OSError`（不再回傳會在 `open()/configure()` 才 `NameError` 的物件）；`snapshot` 的 `vtty_alive` 在 TCP console 改以 listener 是否監聽判定（不再因 `host:port` 非檔案而恆 False、誤觸 `VTTY_STALE`）；`_console_send` 修 non-blocking socket partial-send 截斷（迴圈送完）；`SessionManager.attach_console` 對無 PTY（`client_id is None`）只回端點、不開 `human:None` lease 覆寫 bridge accept 時授予的 raw ownership。本變更涵蓋 **PORT-1（序列埠）與 PORT-2（Windows TCP human console）**；AF_UNIX 控制通道（PORT-4，含 `device release` 編排與 **Windows MCU flash 之 detach-COM 流程**，見 README）、`/proc`（PORT-5）、`/dev` 列舉（PORT-6）、WAL 目錄 fsync（PORT-7）、systemd/dialout（PORT-8）仍為 Linux-only，列為後續。
- 動態裝置 profile 持久化（#95）：`session pin`/`unpin` 手動釘選 + 偵測達 READY 後自動 sticky，根治 daemon 重啟後 profile 漂移；`session list` 新增 `profile_source` 欄位（pin/sticky/detected/fallback/yaml-target）。
- **`serialwrap setup` 在 WSL 上主動啟用 systemd（#76）**：新增 `ensure_wsl_systemd()`——偵測到 WSL 但 systemd 尚未啟用時，合併寫入 `/etc/wsl.conf` `[boot] systemd=true`（保留既有段落／鍵，經 staging + `sudo install`，不破壞性丟棄無法解析內容），並早退提示使用者於 Windows 端 `wsl --shutdown` 重啟後再跑 `setup --system`。非 WSL 或 systemd 已啟用 → no-op。新增 `tests/test_wsl_systemd.py` 與 system unit run-as-user 測試。


### Added

- **`serialwrap setup` / `doctor` 子命令接線（`feature/install-flow-systemd-pipx`，T12）**：新增 `sw_core/doctor_cmd.py`（`run_doctor` 對 Python 版本／PyYAML／`serialwrap`+`serialwrapd` 是否在 PATH／`dialout` 群組／systemd／監管模式／by-id 裝置／WSL systemd 做唯讀診斷，每項永不拋例外並附繁中修復提示；systemd／wsl_systemd／devices 為 advisory 不拉低整體 ok）；`sw_core/setup_cmd.py` 新增 `detect_legacy_install`（偵測舊版 `~/.paul_tools/serialwrap` 安裝並回報 minicom 符號連結／`/tmp/serialwrap/state.json` 殘留與退役指引，只指引不刪除）；`sw_core/cli.py` 註冊 `setup`（`--user`/`--system`/`--on-demand` 互斥目標模式、`--force`、`--with-sudo`）與 `doctor` 子命令並接上 `materialize_assets`＋`reconcile`（daemon/flash 偵測為 best-effort try/except 預設 False，不阻擋 setup；`FlashingBusy` → `FLASHING_BUSY` rc 2），同步重生 `README.md` `serialwrap-help` marker（R-16）。
- **修整 `reconcile` 對真實 `SystemEffects` 的相容性（T12 整合時發現）**：`reconcile` 回傳的 `ran` 欄位原直接讀 `fx.calls`（僅 `FakeEffects` 測試替身有此屬性），接上真實 `SystemEffects` 時會 `AttributeError`；改以 `getattr(fx, "calls", [])`，真實路徑回空 `ran`、`FakeEffects` 行為不變。
- **安裝流程重構為 pipx + systemd（進行中，`feature/install-flow-systemd-pipx`）**：新增 `pyproject.toml`（setuptools，console_scripts `serialwrap`/`serialwrapd`、宣告 `PyYAML`、`requires-python>=3.10`、資產 relocate 進 `sw_core/assets/` 並以 `importlib.resources` 取用）；新增 `serialwrap setup`/`doctor`/`service` 子命令；daemon 生命週期改以 systemd 服務為主（`systemd-user` 預設、`--system` 選項），無 systemd 平台退回現有 on-demand spawn 降級備援，並以 `config.yaml` 的 `supervision_mode` 為單一事實來源 gate 掉 auto-spawn 競態；路徑改 XDG（脫離 `/tmp`）並保留所有 `SERIALWRAP_*` 覆寫；minicom wrapper 改以 `command -v minicom` 解析；向後相容遷移既有 `~/.paul_tools` 安裝。設計見 `docs/superpowers/specs/2026-06-22-install-flow-systemd-pipx-design.md`，OpenSpec 變更 `openspec/changes/install-flow-systemd-pipx/`。
- 四份 agent 檔（`CLAUDE.md`/`AGENTS.md`/`GEMINI.md`/`.github/copilot-instructions.md`）新增「MCU flash 真機驗證手法（#55 `/dev/ttyMCU`）」段：throwaway daemon + FTDI-only sandbox `SERIALWRAP_BY_ID_DIR` 隔離跑法、DUT console GPIO BSL-invoke（長指令需逐行）、以及三個只有實機才現形的坑（idle 汙染／double-sync 吃 ACK／PTY 無 EOF 卡 FLASHING）。
- **MCU 韌體升級 flash 端點 `/dev/ttyMCU`（#55）**：daemon 持續 maintain tty（維持 real device 唯一 reader、無 two-reader race）下提供 byte-transparent flash 端點，外部 flasher（如 `ocp-mcu-upgrade -d /dev/ttyMCU`）原生走通燒錄。以非破壞性 **sync-probe** 自動認 MCU 線（偵測階段排除 `command_capable` console，避免燒到 DUT；多候選 ACK → 不自動挑，狀態經 `mcu status` 的 `last_detect` 呈現），不依賴會漂移的 `/dev/ttyUSBx`/by-id；FLASHING 期間封鎖 console / interactive 注入（`FLASHING_BUSY`），確保 SBL binary 不被汙染；可擴充 **pattern registry**（`sw_core/mcu_patterns.py`，預設 TI CC2674/CC2652 `55 55`→`00 cc`，非破壞不變式守於 `__post_init__`）；新增 `FlashEndpoint`（`sw_core/flash_endpoint.py`，常駐 PTY slave 設 raw 確保 byte-transparency、未 bridge 時一律沉默不主動寫入、偵測 client 寫入才進 flash、雙向 pump；清單查詢走 `mcu patterns`/`mcu status` 不經此 PTY——真機實證 idle 寫入會汙染 flasher sync）；`UARTBridge.flash_tx`（原樣 TX 跳過行處理）+ baud termios 鏡射；session `FLASHING` 狀態（不 detach，`cmd submit` 回 `FLASHING_BUSY`，結束自動恢復 console）；`mcu patterns` / `mcu status` RPC+CLI 與 MCP tool。全程 RAW WAL 留證。設計／計畫見 `docs/superpowers/specs/2026-06-17-mcu-fw-upgrade-flash-broker-design.md`、`docs/superpowers/plans/2026-06-17-mcu-fw-upgrade-flash-broker.md`。**真機 gate 已通過**：OCTOPUS/CC2674（FTDI as COM0）經 `/dev/ttyMCU` 完整實燒 `Return error code : 0x0`（APP+CCFG erase/write/CRC）、燒後 session 自動恢復 `ATTACHED`。真機過程另修三個只有實機現形的問題：idle 清單寫入汙染 flasher（改為端點一律沉默）、獨立 probe 吃掉 MCU sync ACK 的 double-sync（改用 flasher sync 並回放 ACK）、flasher 斷線因 PTY 無 EOF 卡 FLASHING（改用 holder-probe 偵測斷線收尾）。
- `device release` / `device attach`（RPC `device.release` / `device.attach`）：把單一 session 綁定的 UART 乾淨交給外部 flasher 並可手動收回（#54）；新增 `RELEASED` 狀態、`_spawn_attach` released guard、跨 daemon 重啟持久化、`self_test` 的 `external_holder`/`reclaimable` 標註與 `device attach` 安全 guard（`DEVICE_STILL_HELD`，`--force` 可略過）
- 新增 device release / handoff 設計文件 `docs/superpowers/specs/2026-06-15-device-release-handoff-design.md`、OpenSpec change（已封存於 `openspec/changes/archive/2026-06-16-device-release-handoff/`，含 proposal/design/specs/tasks）與實作計畫 `docs/superpowers/plans/2026-06-15-device-release-handoff.md`（#54：daemon 持續運作下把單一 device 交給外部 flasher、燒完手動收回的 surgical release 機制）
- 新增 `.github/workflows/tests.yml`：在 PR 上以 `python3 -m pytest -q tests/` 執行測試套件（修補 R-19：repo 有 `tests/` 但無 CI 執行測試）。
- **command_capable 判準與 `PROFILE_NOT_COMMAND_CAPABLE` 錯誤碼（#51）**：以 `command_capable = bool(profile.ready_probe.strip())` 判定 session 可否下命令，取代以 `platform == "passthrough"` 寫死；非 command-capable（無 ready_probe，含 passthrough / others-template 等僅 console 的 profile）在 `ATTACHED` 下 `cmd submit` 改回語意明確的 `PROFILE_NOT_COMMAND_CAPABLE`（附 hint），不再回易誤解的 `SESSION_NOT_READY`；`to_public_dict()` 新增 `command_capable` 欄位。
- **`self_test` 在最外層 result dict 暴露 `command_capable`（#51）**：所有分類分支（含 `SESSION_NOT_FOUND` 早退、`RELEASED`、`ATTACHED`/`READY`/`OK` 等）的最外層 result 都帶 `command_capable`，呼叫端不必鑽進巢狀 `"session"` dict；查無 session 時為 `False`。
- **新增 `uboot-template` profile（#51）**：`profiles/default.yaml` 增加 bootloader 導向的 command-capable profile（`platform: passthrough`、`prompt_regex` 匹配 `=>` / `u-boot>` / `CFE>`、`ready_probe: echo __READY__${nonce}`），讓停在 U-Boot prompt 的 session 能進 `READY` 並接受 line command；`ProfileTemplate` 比照 `SessionProfile` 新增 `command_capable` property。
- **追蹤 human 真實鍵入並新增 `human_active` 時間窗語意（#53）**：`UARTBridge` 新增 `last_human_input_at`（僅在 human-OWNER 的直接 raw 送出分支更新，deferred buffer／serial RX loop／agent 注入皆不更新）並由 `snapshot()` 暴露；`sw_core/constants.py` 新增 `HUMAN_ACTIVE_WINDOW_S = 60.0` 時間窗常數；`self_test` 結果在最外層新增 `human_active`（僅在 `human_attached` 且最後鍵入仍在時間窗內為 `True`），讓「人類已 attach 但長時間 idle」的 lease 不再被當成正在使用。`human_attached` 語意維持不變（#53 issue groundwork）。
- **閒置 human lease 可被 agent soft preempt（#53）**：`interactive_open` 在 READY 路徑遇到既有 human lease 但 `human_active=False` 時，改以 suspend + stash 將 human **降級**（console 不中斷、其鍵入進 deferred buffer，agent 關閉 lease 後還原並回放）取得控制權，回傳 `soft_preempted`；human 仍 active 或既有為 agent lease 維持 `SESSION_INTERACTIVE_BUSY`。死孤兒（console peer 已關）沿用既有 liveness 在 `self_test` 時 detach；活著但 idle 的 console 只降級、不自動 detach（清理交由 agent 主動 `recover`/`console-detach`）。解決孤兒 minicom 假性佔用 console 導致 co-work 卡住的問題。

### Changed

- **agent 檔改為單一事實來源 + symlink**：`AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md` 改為指向 `CLAUDE.md` 的 symlink，往後只維護 `CLAUDE.md`；原 `.github/copilot-instructions.md` 專屬的「實際命令／高層架構／關鍵慣例／測試與除錯重點」併入 `CLAUDE.md`（操作事實對齊現行 pipx + systemd + XDG 流程）。policy_check R-13（`is_file()`）/ R-14（`read_text()` 找 `policy_version`）跟隨 symlink 解析至 `CLAUDE.md`，仍合規 v1.0.5。
- agent skill 整併為 repo 內唯一權威來源 `skills/serialwrap/SKILL.md`（CLI-first，改名 `serialwrap-mcp` → `serialwrap`），`install.sh` symlink 到 `~/.agents/skills/`；移除 root `skills.md`。(#59)
- 移除 `Dockerfile` 對已退役 `serialwrap-mcp` 的 `chmod +x`（該檔已隨 MCP 退役刪除，否則 `docker build` 及 `tools/docker/remote_smoke.sh` remote smoke 會因檔案不存在而失敗）。(#59)
- 文件與現行架構對齊：刪除 `sills.md` 轉址 stub；`docs/serialwrap-spec.md` 降級為概覽並指向 `openspec/specs/*`；`docs/plan.md`/`docs/todos.md` 標為歷史快照；`README.md` 狀態機補 `RELEASED`(#54)/`FLASHING`(#55)；`skills.md` 加 #59 cross-ref 與過時標記。
- 升級 policy conventions v1.0.4 → v1.0.5（pin 重釘到 `484f963a…`）：`.paul-project.yml`、四份 agent 檔（marker/policy_version/install·pinned SHA）、`.github/workflows/policy-check.yml`（uses/policy_engine_ref/policy_version）一併同步。新增 R-22 doc_reference 於本地與 CI 生效。
- **command_capable 改以 ready_probe 為準（#51）**：`_attach_by_id` / `_attach_by_id_dynamic` / `_probe_existing_bridge` 不再以 `platform == "passthrough"` 寫死 `ok=False`，改以 `profile.command_capable` 判定——有設 `ready_probe` 的 target（含 passthrough）能走正常 probe 進 `READY`，無 `ready_probe` 者維持 `ATTACHED`。

- **同步 policy 1.0.1**：`policy_version` 1.0.0 → 1.0.1（`.paul-project.yml` + 四份 agent 檔 + `managed-by@v1.0.1` + agent 檔內 engine pinned SHA），caller `policy-check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@4ff59b6c35a46a87af3c3e641975743ee8fa0858`（含 R-17 / R-18）；agent 檔追加 R-17 / R-18 與語言規範說明
- `tools/minicom_router.sh` 的 broker minicom 自動 transcript **預設維持 minicom 內建 `-C`**（乾淨序列 log）；新增 `MINICOM_CAPTURE_MODE=script|minicom|off` 控制模式，`script` 為顯式 opt-in 的完整終端 transcript。（修正先前一度把預設改為 `script` 導致 `mini_*.log` 夾帶 minicom UI ANSI 的回歸；查證「`-C` native crash」無實證。）
- **採用 policy 1.0.4**：`policy_version` 1.0.1 → 1.0.4（`.paul-project.yml` + 四份 agent 檔 + workflow `uses:`/`policy_engine_ref` 重釘至 `hamanpaul/paulsha-conventions@v1.0.4`，SHA `77a3e83`）；`.paul-project.yml` 宣告 `tier: shareable`。
- **強化 CLI help（`sw_core/cli.py`）**：為全部命令群組（`daemon`/`device`/`session`/`alias`/`cmd`/`stream`/`log`/`file`/`wal`/`event`）補上 `help=` 摘要與 `description=`，並為每個子命令（含先前看不到的 `recover`/`self-test`/`release`/`attach` 等）補繁中 `help=`；子命令選單 `metavar` 由冗長的 `{...}` 改為 `<group>`／`<command>`，`event` 既有英文 help 一併改為繁中；同步重生 `README.md` `## Usage` 的 `serialwrap-help` marker 區段（R-16）。純說明文字調整，不影響任何指令行為與參數。

### Removed

- 退役 vestigial MCP 層：刪除 `sw_mcp/`（含 server.py）與 `serialwrap-mcp` shim，並移除/改寫 4 個 MCP-coupled 測試（event/remote/bootloader 改走 CLI/RPC 路徑覆蓋）。(#59)

### Fixed

- **bulk 傳檔不再凍結共用 daemon 的所有 RPC（#52）**：`file.push`/`file.pull` 過去在單執行緒 asyncio event loop 內**同步**執行整段 UART base64 分段傳輸（含阻塞式 `wait_for_regex_from` 輪詢），傳輸期間 `JsonRpcUnixServer` 無法服務任何其他連線，導致所有 COM 的 `cmd submit`／`session *`／`file *`／RPC-mediated interactive console 全部凍結（真機實測：一次 `health.ping` 被卡 **19.8s**＝整段傳輸時間）；reporter 當初量到「控制面 0.045s」是閒置時量的。修法（B-lite）：`JsonRpcUnixServer` 新增 `blocking_methods` 參數，`serialwrapd` 以 `BLOCKING_RPC_METHODS = {"file.push", "file.pull"}` 把這兩個長阻塞 handler 丟到 executor 執行（`loop.run_in_executor`），event loop 於傳輸期間維持可服務其他 RPC；CLI 介面與同步語意完全不變。**真機驗證**：傳輸進行中 40 次 `health.ping` 全部 **0–1ms**（修前 19.8s）；資料面 raw console（走 `_loop` 執行緒、繞過 RPC）本就不受影響（傳輸期間最差 gap 86ms）。已知限制：同一 session 並發兩個傳輸在罕見情況可能各自回 `CHECKSUM_MISMATCH`（已偵測、非靜默損壞），列為後續強化。
- **#69 開機窗自動重探**：daemon 針對 `ATTACHED/PROMPT_UNAVAILABLE` 與 `DETACHED/*_PROMPT_TIMEOUT` 等可復原非 READY session，於 RX 閒置後以有界 backoff 自動重跑既有 readiness probe；公開 `reprobe_attempts` / `next_reprobe_at` / `reprobe_exhausted`，並讓 `minicom_router.sh` 在 DUT 開機窗提示自動重探與手動 `session recover` 路徑。
- **#69 對抗式審查（codex adversarial-review）修正**：(1) `UARTBridge.send_bytes` 於 `_flash_mode` 期間丟棄非 `flash` 來源寫入，堵住 reconcile/probe 在 FLASHING 競態下寫 bytes 汙染 SBL 串流的真正 choke point；auto worker 寫 probe 前在 lock 內再驗證狀態（Finding 1）。(2) 新增 per-session probe 互斥鎖，auto 自動重探與 manual `session attach`/`session recover` 不再並發 probe 同一 bridge（Finding 2）。(3) ATTACHED readiness probe 改背景 worker 執行（`reconcile_readiness` 不再同步阻塞 DeviceWatcher tick／延誤裝置偵測；新增 `join_reprobe_workers()`，Finding 3）。(4) 手動 `session recover` 視為顯式介入，先重置 reprobe 上限/進度（re-arm），避免 exhausted 的 ATTACHED session 在手動 recover 仍失敗後永遠被 reconcile 跳過（Finding 4）。(5) 失敗重探的 backoff 改以 probe「完成時間」計算 `next_reprobe_at`（而非開始時間），避免慢速/靜默開機下 probe 阻塞超過 backoff 間隔時 `next_reprobe_at` 落在過去、下一 tick 立刻重探、過早 `reprobe_exhausted`（Finding round6）。
- **FLASHING 狀態完整性硬化（#69 對抗式審查 r2–r5，含既有 gap）**：(a) `send_bytes` flash gate 與 `set_flash_mode` 共用 `_write_lock`（鎖序 `_write_lock ⊃ _state_lock`），使「檢查 flash_mode → 寫入」原子化，杜絕 flash 開啟插進空隙；flash 寫入授權改用內部能力 `_allow_during_flash`（僅 `flash_tx`），不再綁可偽造的 `source` 字串。(b) `exit_flashing` 不論 state 一律 `set_flash_mode(False)`（防禦縱深），並為 `_probe_existing_bridge`/`_transition_to_attached`/reconcile reprobe except／reboot-recovery 迴圈加 FLASHING/RELEASED guard，杜絕競態路徑把 FLASHING 打回 ATTACHED 導致 bridge 永久卡 flash 模式。(c) `clear_session` / `release_device` / `bind_session` / `recover --force` 在 FLASHING 期間一律回 `FLASHING_BUSY`、不 detach bridge，避免並發管理操作切斷進行中的 MCU 燒錄 transport（既有 gap，比照 cmd submit 既有 FLASHING_BUSY 守法）。
- 修正 broker minicom 正常或非 0 結束時可能因 shell 被 `exec` 取代而跳過 `session console-detach` 的 lifecycle cleanup 風險。
- **device handoff（#54）C1**：修正飛行中的 attach 在 `bridge.start()`+probe 窗口期間遇到 `release_device` 時，會在最終 commit 把 `RELEASED` 覆寫成 `READY`/`ATTACHED` 並重開燒錄中裝置 raw FD 的 race；`_attach_by_id` 與 `_attach_by_id_dynamic` 兩段防護：設 `ATTACHING` 前 RELEASED 早退（常見情形不開 FD），最終 commit 前加 RELEASED backstop（關掉剛開的 FD、保留 `RELEASED` 不打回 `DETACHED`）。
- **device handoff（#54）C2**：`attach_session` / `recover_session` 對 RELEASED session 改為比照 `clear_session` 早退（回 `released=True` + `recommended_action: device_attach`），不再把 session 打成 wedged-`ATTACHING`；同時避免下一次 `_save_state` 因 `state != "RELEASED"` 把 released map 寫空，確保重啟後 RELEASED 保護不失、bootstrap 不搶回裝置。
- **device handoff（#54）I1**：`bind_session` 對 RELEASED session 重綁新 by_id 時，先把舊 by_id 移出 `_released_by_ids` 並清 provenance（`released_by`/`released_at`/`released_reason`），避免舊 by_id 永久殘留集合。
- **device handoff（#54）I2**：`self_test` 的 RELEASED 早退分支改為在 `self._lock` 內擷取 `real_path`/provenance/`to_public_dict()`，出 lock 後才呼叫 `_probe_external_holder`（掃整個 /proc），避免持 lock 期間阻塞所有 RPC。
- **device handoff（#54）PR review 修正**：`device.attach` 對「已 attached 且非 RELEASED」的 session 改為冪等回覆 `already_attached`，不再誤設 `ATTACHING` 導致 `_attach_by_id` 早退而卡死；`_probe_external_holder` 加上 device number（`st_rdev`）比對，外部即使以 `/dev/serial/by-id/...` 等 symlink 開啟裝置也能偵測到，避免 `device.attach` 誤判可收回而重回 two-reader race；`to_public_dict` 補 `released_reason`。

### Security

- 配合 `tier: shareable` 的 R-21（已對公開廠商/OS 名減敏），清除 tracked 內容中的個人家目錄絕對路徑（docs/skills 改 `~/`、func-test 改 `/tmp/` 以維持可攜）與雇主裝置型號（改用公開的 `Broadcom CFE 平台` 通稱）；停止追蹤 ctags 索引檔（含絕對路徑）並加入 `.gitignore`。
- 將 `profiles/brcm.env`（含 `BRCM_USER` / `BRCM_PASS`）改為 `profiles/brcm.env.example` 範本並停止追蹤；`.gitignore` 新增 `profiles/*.env`，避免本機憑證 profile 被提交。實際憑證由使用者複製範本後在本機填入（`profiles/default.yaml` 的 brcm-template 仍以 `env_file: brcm.env` 載入）。

## [0.1.0] - 2026-05-15

### Added

- 導入 [paulsha-conventions](https://github.com/hamanpaul/paulsha-conventions) v1.0.0 治理基線（`.paul-project.yml`、`policy_version: 1.0.0`）
- 新增 `VERSION` 檔案並將正式 release 版本更新為 `0.1.0`
- 新增 `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`（AI agent policy checklist）
- 新增 `.github/pull_request_template.md`（含 R-11 policy checklist）
- 新增 `.github/workflows/policy-check.yml`（PR 自動 policy 驗證）
- README.md 補充 `## Install`、`## Usage`、`## Version` 段落與 CLI help marker
- `sw_core/config.py`：`ProfileTemplate` 與 `SessionProfile` 均新增 `bootloader_prompts: tuple[str, ...]`（預設 `()`，immutable）；loader 從 YAML list 解析時只保留 `str` 元素，並傳播至 session profile
- `sw_core/constants.py`：新增 `MAX_RECOVERY_LEASE_S = 120.0` 與 `BOOTLOADER_RX_TAIL_BYTES = 512`（為 Issue #44 bootloader recovery 所需）
- `profiles/default.yaml`：`brcm-template` 加入 `bootloader_prompts`（CFE、U-Boot、BCM 系列 prompt pattern）
- `sw_core/session_manager.py`：新增模組層級 helper `_matches_any_bootloader_prompt`，以 RX tail 最後一個非空行比對 profile bootloader_prompts regex list（Issue #44 Phase B）
- `sw_core/session_manager.py`：`session.self_test` ATTACHED 路徑在 passthrough / LOGIN_REQUIRED / REBOOTING 之後、ATTACHED_NOT_READY 之前，新增 BOOTLOADER classification；命中時回傳 `matched_prompt`、`rx_tail`、`recommended_action: recover_interactive`
- `sw_core/session_manager.py`：`InteractiveLease` 新增 `recovery_mode: bool = False` 與 `suspended_human: bool = False` schema 欄位（Phase B 基礎，後續 interactive_open allow_attached/stash 使用）
- `sw_core/session_manager.py`：`SessionRuntime` 新增 `_stashed_human_lease: InteractiveLease | None = None`（Phase B 基礎，不透出 RPC）
- `sw_core/session_manager.py`：`_lease_context()` 新增 `recovery_mode` 欄位（所有 self_test 分類結果均含此欄位）
- `sw_core/session_manager.py`：`interactive_open(allow_attached=True)` 支援 ATTACHED 狀態下通過 bootloader prompt 比對後開啟 recovery lease；human lease 自動暫停（stash），close 時恢復
- `sw_core/session_manager.py`：recovery lease timeout 受 `MAX_RECOVERY_LEASE_S`（120s）clamp
- `sw_core/session_manager.py`：`interactive_open` / `interactive_status` 回傳 `recovery_mode` 欄位
- `sw_core/session_manager.py`：`_PostCloseAction` 機制保證 `bridge.resume_interactive()` 在 `_lock` 外執行
- `sw_core/session_manager.py`：`_detach_session_locked` 清除 `_stashed_human_lease`
- `sw_core/session_manager.py`：`_refresh_interactive_locked` 自動清除 expired 非 human lease，並透過 lock 外 post-close action 恢復 human deferred input
- `sw_core/service.py`：RPC `session.interactive_open` 透傳 `allow_attached` 參數
- `sw_core/cli.py`：`session interactive-open` 新增 `--allow-attached` 選項
- `sw_mcp/server.py`：`serialwrap_open_interactive` 工具 schema 新增 `allow_attached: boolean`
- `tests/test_bootloader_recovery.py`：新增 56 個 TDD 測試，涵蓋 recovery lease 完整生命週期（開啟、stash/restore、逾時 clamp、send、status recovery_mode、expired 清理、BUSY early-return post-close、detach 清除 stash、RPC/CLI/MCP 透傳）
- 新增 `docs/superpowers/specs/2026-05-15-release-repo-hygiene-design.md` 與本實作計畫，記錄 repo hygiene release 流程。

### Changed

- `.github/copilot-instructions.md` 前置 paulsha-conventions marker 與 policy_version
- `.gitignore` 新增 `test/reports/` 與常見本機測試、報告、log、coverage 產物規則。
- `.github/pull_request_template.md` 與 `docs/releases/v0.1.0.md` 補充 release PR 階段 label-aware policy check 與 post-tag plain policy check 的驗證時機。

### Removed

- 從目前 tracked tree 移除 `test/reports/` 測試報告；不重寫既有 git history。

### Fixed

- `sw_core/session_manager.py`：動態 auto-detect session 現在會從 `ProfileTemplate` 傳播 `bootloader_prompts`，避免未宣告 targets 的 template session 無法進入 BOOTLOADER recovery

### Security

- 掃描 tracked paths 與 tracked content，確認未發現需移除的非例外機敏資料；`profiles/brcm.env` 屬 console login profile 例外。

### Notes

- Phase A 為治理/文件/CI scaffolding，不含 Issue #44 recovery 功能
- Release PR 必須標記 `release:0.1.0`，讓 R-07 在 tag `v0.1.0` 建立前跳過
- policy_check engine pinned to `ff1a031172ec24fc155699f9f3ce5bdea24d9e24`
