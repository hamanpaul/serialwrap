# open-pr-audit-20261122.md 節錄（PR 279 相關）

| [279](https://github.com/hamanpaul/serialwrap/pull/279) | Python TCP console／session lease、測試及 golden | 部分報告的已完成非跳過 CI 成功；無紅燈 log | **3 檔**：兩個 version golden、argparse golden | **(c) 改到 Rust／納 0.5.1；替代交付後再考慮 (d) 關舊 PR** | 高（無 Rust 交付）；競態未實跑 | 現有變更不會修到正式 0.5.x Rust daemon；Rust 目前也有「accept 授 owner 卻未建 lease」的對應路徑 |

### 3.2 #279：修正思路可沿用，不能把 Python 合併當成 Rust 交付

**已查證的實作**：[PR 279] `sw_core/uart_io.py:2725` 的 getter 依序查看 active／suspended／pending owner，且只認有 socket 的 human client；`sw_core/session_manager.py:2641` 附近採用鏡像 lease，`:2665` 的 drop 只撤 session 紀錄；`:6182` 對 bridge_owned lease 先比 owner，換人時 drop/adopt、不呼叫一般 close 清 bridge owner。這正對 A 斷線、B 在 grace 窗內重連被誤清的問題。mock 及真 localhost socket 測試在 `tests/test_tcp_console_lease.py:79`、`tests/test_serial_port.py:846`，不是 Windows COM 實跑證據。

**0.5.x 缺口（高優先）**：本 PR **沒有 Rust production code 修改**。0.5.0 main 的 `VERSION:1` 為 0.5.0；[main] `scripts/rust/mkbundle.sh:603`、`:653`～`:655` 的 Stage 2 manifest 選 Rust daemon entry。合入這些 Python 檔不會修到正式 Rust daemon。

目前 Rust 對應路徑已能具體定位：

- [main] `crates/sw-core/src/session_core/console.rs:708`／`:743`：stream accept 建 console、授 ownership，沒有同時建 `InteractiveLease`。
- [main] `crates/sw-core/src/session_core/lease_p6.rs:35` 與 `crates/sw-core/src/lease.rs:163`：human_attached／human_active 來自 lease；`:194` 無 lease 直接返回；`:261` self-heal 若已有 owner 就返回，補不上 accept 直接授出的 owner。
- [main] `crates/sw-core/src/session_core/console.rs:749` 斷線清 ownership；`crates/sw-core/src/session_core/lease_p6.rs:207`～`:231` 仍有 peer grace／owner 不符就一般 close 的規則。

這些是 **Rust 也有對應缺口的靜態證據**；不代表本輪已重現所有 Windows／重連時序。不能沿用 PR 內文「Rust console／lease 尚未實作」的舊敘述。

**具體移植與缺測**：在 actor 內將 TCP accept ownership 與 lease 記錄協調，涵蓋 active／suspended／pending；換人撤舊紀錄不能清新 owner；補 Rust self-test 人類欄位、活躍 reprobe、idle preempt／BUSY、斷線後 B 重連、agent 命令中 self-test 的受控測試。PR 的新 mock case 加真 socket case 可當情境表，不應照搬 Python 鎖結構到 actor。

**其他審查點**：[PR 279] `sw_core/session_manager.py:2144` 雖會補記 lease，`:2157` 仍比對 snapshot active owner；suspended／pending getter 能保留鏡像，並不表示每個 human_active 呼叫都會把 suspend 視為 active。移植時需按 reprobe／foreground gate 的實際語意驗收，不以 getter 單測當全路徑證據。外部 BUSY／human 附掛行為改變沒有 behavior_changes 登錄。

**衝突**：僅 `contracts/cli_help/linux/action_version.json`、`action_version_session.json`、`crates/sw-cli/tests/argparse_golden.rs`。舊 PR version golden 為 0.4.1，main 已為 0.5.0；應以目標 release 與合併後內容重產，不能選一邊字串硬解。

**舊路徑檢查**：`sw_core/session_manager.py` 與 `sw_core/uart_io.py` 從 `47097715` 到 main 內容**完全相同**，都未移除；真正改變的是正式 daemon 的實作權威移到 `crates/`。選 (c)。若要關舊 PR，應先有 Rust replacement 承接與來源連結，再由 owner 採 (d)；本輪未關閉。


### 5.2 #279 與 issue 294：相關，但不等價

**相關處**：鏡像 lease 讓 session 看得見 TCP human，因而影響 human_active、reprobe、soft preempt、bare-human BUSY 與 agent 執行期間是否 suspend。Rust [main] `crates/sw-core/src/session_core/console.rs:39` 只在已有 human lease 時 suspend，正好與 #279 的可見性缺口相關。

**不能解決的部分**：issue 294 是 human 命令產生的 prompt 可以滿足 agent 命令完成。Rust [main] `crates/sw-core/src/op_runner.rs:710`～`:715` 選 completion/profile regex 並記 RX offset，`:764` 從該 offset 等符合 regex 的 RX，並沒有以造成 prompt 的命令身分配對。human 在 agent 開始前已送出的命令，之後才回的 prompt，仍可能落入這段 RX；補 lease／suspend 只約束後續 human TX，不會自動辨認已在途的 human 命令輸出。

[收尾 main] `docs/rust-refactor/stage2-release-install.md:68`～`:70` 也明列 #294 的相同觸發條件、暫時使用唯一 completion 記號及未驗證的非行尾 prompt；與本輪機制分析相符。

**結論**：#279 是應承接的 ownership／admission 修正，可作 #294 防護的一部分，**不能宣稱它解決 #294**；#287～#290 也沒有改這個完成判定。要有受控交錯：human 先送、agent 開始、延遲 human prompt 抵達、agent 自己完成訊號稍後抵達，斷言 agent 不可提前 done。是否採 nonce／echo 配對、RX 邊界或 UART quiescence 是後續設計決策；本輪只有機制關係的讀碼推論，沒有重現硬體競態。

## 6. 需要擁有者決定的事項

1. **0.5.1 的 baseline／契約政策**：287～290 要保留 Python 舊 oracle 並登錄 Rust-only 差分，或明確核准雙邊基線改動；把適用版本、BC／DF／OD 與必要案例寫入權威檔。
2. **#279 的交付形式**：請原作者移植到 Rust，或另做 replacement PR 承接並保留來源；替代交付完成後才決定是否關閉舊 Python PR。不得把 #275／#294 一併當已解決。
3. **#289 OD-33 gate**：是否接受 profile_template_fields 在升級時非綠；若接受，核准精確預期缺欄位及診斷，不採無條件整類豁免；同步既有 release-plan。
4. **#290 預設與遷移**：核准 prpl 新 template 只送 Ctrl-C 的取捨、舊檔如何提示／手動補欄位；決定 [] 的 deferred 失效語意，以及是否要求真 ash 不登出的 L2 case。
5. **尚未定因的紅燈**：安排 #287 sticky cancel 與 #290 writer 的受限範圍 main／PR 對照證據；#288 吸收 main 修正後重跑。未授權或完成補證前，不把未知改標成 flake。

#277 的 update hold 漏判、#289 的「不可解析卻稱一致」屬具體工程修改點，不需要 owner 選擇是否維持錯誤行為；owner 要決定的是對外錯誤／略過形式與驗收契約。

## 7. 本輪查證與推論的界線

| 類型 | 本輪做了什麼／能支持什麼 |
|---|---|
| **實際查證** | 讀取部分報告與三份完整 CI log；固定 main/head/merge-base；完整 diff 與所有變動檔案審查；相關 main 原始碼／契約比較；初輪六次對 main、六次兩兩、收尾六次對新 main，共 18 次實際 merge；每次 abort／remove；重算各 head Python 指紋；逐葉比較 290 golden；L1 鎖與新增測試名稱核對；289 純函式字串輸入重現 |
| **由原始碼與歷史修正推論** | 277 漏 update hold 可導致提前重新啟動；279 缺 Rust lease 的現行症狀與 294 的 prompt 競態關係；288 此輪失敗與 main 修正的因果對應；290 [] 對 deferred 安全性的風險及 hosted writer 時序候選 |
| **仍不確定** | 287 sticky 狀態失敗究竟是產品回歸或測試同步；290 writer 的精確 failure_detail／實際停滯間隔；277 Windows 上該交錯是否已發生；279 原生 Windows／COM 真正表現；290 真 ash console 的完整不登出驗收 |
| **沒有執行／沒有取得** | 新基底測試／CI、Rust build、Windows exe 執行、daemon／UART／實機、線上 PR 狀態與評論更新、policy gate、contract gate、commit／push／PR／merge／部署 |

證據產物全部在本報告同目錄。既有 repo untracked `brag-output/share-copy.txt` 保留；沒有將它當成本任務變更。收尾快照及清理核對見 `audit-closeout.json`；status 保持不變，origin/main／origin/HEAD 的外部更新另記，不宣稱來源 refs 全程不變。

## 附錄 A：逐檔審查索引

以下逐項列出每個 PR 的完整變動檔案。行號為 head 的第一段變更位置（不是 bug 的唯一行號）；具體問題與後續修改詳見 §3。`main 既有` 表示此路徑在目前 main 仍存在，`新增` 表示該 PR 新檔；本輪沒有發現老 PR 正在修改 main 已刪掉的 production path。


### PR #277（6 檔）

| 檔案:head 行號 | main 路徑 | 審查結果 |
|---|---|---|
| `README.md:1524` | main 既有 | 中英同步描述 ENABLE／Run；有 hold 的聲明需補 update journal 範圍，見 §3.1。 |
| `changelog.d/276-windows-task-start-reenable.md:1` | 新增 | direct-child fragment 存在；記載本 PR 的功能／修正，不能代替 behavior_changes 核准。 |
| `crates/sw-cli/src/commands/service_cmd.rs:47` | main 既有 | Start 路由與 ENABLE 失敗早退合理；update hold／併發交易辨識不足，需改。 |
| `crates/sw-cli/src/rollback.rs:4302` | main 既有 | 新增 helper 只查 switch 檔；既有 update journal hold 未被涵蓋。 |
| `crates/sw-cli/tests/mgmt_daemon.rs:338` | main 既有 | 既有已鎖測試改驗先 ENABLE；缺 daemon 入口真 update hold／交錯反例。 |
| `crates/sw-cli/tests/mgmt_service.rs:89` | main 既有 | 新增 switch 檔與 ENABLE 失敗案例；尚未驗真正更新交易與新 hold 插入。 |

### PR #279（12 檔）

| 檔案:head 行號 | main 路徑 | 審查結果 |
|---|---|---|
| `README.md:860` | main 既有 | 中英同步 TCP console 人類可見性；正式 Rust daemon 尚不具此新交付。 |
| `changelog.d/275-tcp-console-lease.md:1` | 新增 | direct-child fragment 存在；記載本 PR 的功能／修正，不能代替 behavior_changes 核准。 |
| `contracts/cli_help/linux/action_version.json:4` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `contracts/cli_help/linux/action_version_session.json:5` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `contracts/daemon_logging.yaml:92` | main 既有 | 只改 source 行號，不改 level／模板；整合 session_manager 後須重對實際行號。 |
| `crates/sw-cli/tests/argparse_golden.rs:5` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `sw_core/assets/skill/SKILL_WINDOWS.md:123` | main 既有 | 新增操作語意；要綁 Rust 實際可用版本，避免先發文件宣稱已修。 |
| `sw_core/session_manager.py:373` | main 既有 | 鏡像採用／撤除與 preempt 路徑已審；只有 Python，需 actor 等價實作。 |
| `sw_core/uart_io.py:2722` | main 既有 | getter 檢查 active／suspended／pending；不會自行修改 bridge owner。 |
| `tests/test_contracts_keep_baseline.py:222` | main 既有 | 兩個寫死 logger source 行號與該 head 同步；多 PR 整合後須重對。 |
| `tests/test_serial_port.py:807` | main 既有 | 新增真 localhost socket owner／A→B 重連測試；非 Windows COM 驗收。 |
| `tests/test_tcp_console_lease.py:1` | 新增 | 8 基礎測試由 public-path 子類繼承，另 3 公開情境；19 個收集項不等於 19 個獨立場景。 |

### PR #287（19 檔）

| 檔案:head 行號 | main 路徑 | 審查結果 |
|---|---|---|
| `README.md:595` | main 既有 | 中英同步 never-RX 與 TRANSPORT_STALL 的區別。 |
| `changelog.d/never-rx-probe-detail.md:1` | 新增 | direct-child fragment 存在；記載本 PR 的功能／修正，不能代替 behavior_changes 核准。 |
| `contracts/cli_help/linux/action_version.json:4` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `contracts/cli_help/linux/action_version_session.json:5` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `contracts/daemon_logging.yaml:92` | main 既有 | 只改 source 行號，不改 level／模板；整合 session_manager 後須重對實際行號。 |
| `contracts/golden/p7/transport_stall.ndjson:28` | main 既有 | 新增 031～040 共 10 列，不改舊 STALL golden。 |
| `contracts/session_last_errors.yaml:10` | main 既有 | PROMPT_UNAVAILABLE detail／處置已更新；缺行為變更登錄。 |
| `crates/sw-cli/tests/argparse_golden.rs:5` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `crates/sw-core/src/transport_stall.rs:1` | main 既有 | 未翻轉時按 login-detail→never-RX→None 排序；None 時間與 Python ≤0 對應。 |
| `crates/sw-core/tests/login_quiet.rs:966` | main 既有 | 既有已鎖測試改驗新 detail，並保留有 RX 反例。 |
| `crates/sw-core/tests/transport_stall.rs:39` | main 既有 | 新增 golden 矩陣／整合兩項；未加 L1 必要節點。 |
| `scripts/rust/capture_p7_goldens.py:310` | main 既有 | 新 7 個分類與 3 個 hint case 對應 Rust 消費者。 |
| `sw_core/assets/skill/SKILL.md:489` | main 既有 | 操作／受管表格隨契約同步；適用版本與行為登錄需一併補正。 |
| `sw_core/assets/skill/SKILL_WINDOWS.md:338` | main 既有 | 操作／受管表格隨契約同步；適用版本與行為登錄需一併補正。 |
| `sw_core/session_manager.py:68` | main 既有 | 只改 refine 接線，保留 login detail 優先與 STALL 告警路徑。 |
| `sw_core/transport_stall.py:10` | main 既有 | 純判定及逐字 hint；Python 基線新增可觀察行為需核准。 |
| `tests/contract_blackbox/test_bb_cli_help.py:71` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `tests/test_contracts_keep_baseline.py:222` | main 既有 | 兩個寫死 logger source 行號與該 head 同步；多 PR 整合後須重對。 |
| `tests/test_transport_stall.py:135` | main 既有 | 5 個新純函式＋2 個 probe mock 整合，未提供靜默 attach 黑箱。 |

### PR #288（20 檔）

| 檔案:head 行號 | main 路徑 | 審查結果 |
|---|---|---|
| `README.md:489` | main 既有 | 中英行長限制與 UTF-8 byte 語意同步；沒有替 profile 自動設 505。 |
| `changelog.d/submit-console-line-limit.md:1` | 新增 | direct-child fragment 存在；記載本 PR 的功能／修正，不能代替 behavior_changes 核准。 |
| `contracts/cli_help/linux/action_version.json:4` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `contracts/cli_help/linux/action_version_session.json:5` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `contracts/error_codes.yaml:149` | main 既有 | 沿用 CMD_TOO_LONG 並更新意義；contract_tests 仍舊分支、behavior_change 空。 |
| `crates/sw-cli/tests/argparse_golden.rs:5` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `crates/sw-core/src/arbiter.rs:147` | main 既有 | 新 admission helper 保留原 API，hard→profile→newline 次序與 Python 一致。 |
| `crates/sw-core/src/session_core.rs:3481` | main 既有 | RPC 取當前 profile 上限；human console 保持原 admit。 |
| `crates/sw-core/tests/arbiter.rs:892` | main 既有 | 4 新案例測精確回應／boundary／mode／order；尚未加鎖。 |
| `crates/sw-core/tests/console_line_limit.rs:1` | 新增 | 4 新 session 測試含 zero TX／human 不限制；尚未加鎖。 |
| `crates/sw-proto/src/error_codes.rs:27` | main 既有 | 生成註解同步 CMD_TOO_LONG meaning，無新 enum。 |
| `docs/serialwrap-spec.md:247` | main 既有 | 限制與 on-wire newline 計法一致；需補行為登錄版本。 |
| `sw_core/arbiter.py:192` | main 既有 | UTF-8 byte 判定與 Rust 相同；Python baseline 變更需裁決。 |
| `sw_core/assets/skill/SKILL.md:269` | main 既有 | 操作／受管表格隨契約同步；適用版本與行為登錄需一併補正。 |
| `sw_core/assets/skill/SKILL_WINDOWS.md:221` | main 既有 | 操作／受管表格隨契約同步；適用版本與行為登錄需一併補正。 |
| `sw_core/service.py:1251` | main 既有 | RPC 帶 profile limit；無 session/profile 時維持 None fallback。 |
| `tests/contract_blackbox/p7_profiles/shell_line_limit.yaml:1` | 新增 | 限制 20 bytes 的專用 fixture，其餘登入設定維持既有。 |
| `tests/contract_blackbox/test_bb_cli_help.py:71` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `tests/contract_blackbox/test_bb_console_line_limit.py:1` | 新增 | 23-byte 中文拒絕／20-byte 接受／TX 核對；新檔未入 L1 鎖。 |
| `tests/test_submit_console_line_limit.py:1` | 新增 | arbiter 6＋service 3 情境，與 Rust／黑箱對應。 |

### PR #289（16 檔）

| 檔案:head 行號 | main 路徑 | 審查結果 |
|---|---|---|
| `README.md:243` | main 既有 | 中英說明頂層缺欄位與 null 例外；需配合未知／解析失敗的新公開診斷。 |
| `changelog.d/doctor-profile-template-fields.md:1` | 新增 | direct-child fragment 存在；記載本 PR 的功能／修正，不能代替 behavior_changes 核准。 |
| `contracts/cli_help/linux/action_version.json:4` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `contracts/cli_help/linux/action_version_session.json:5` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `crates/sw-cli/src/doctor_cmd.rs:48` | main 既有 | Rust advisory／排序同步；不可解析被等同一致是缺陷，見 §3.5。 |
| `crates/sw-cli/tests/argparse_golden.rs:5` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `crates/sw-cli/tests/mgmt_doctor.rs:184` | main 既有 | 清單與 3 新 scenario 同步；缺壞 YAML 的公開輸出反例。 |
| `docs/serialwrap-spec.md:693` | main 既有 | 明訂解析失敗 ok，但未區分不能比較與一致；需同步修正 detail 契約。 |
| `scripts/docker/upgrade_checks.py:87` | main 既有 | 整類放寬 doctor_green_new，與 OD-33 原 devices-only 判準不同。 |
| `sw_core/assets/skill/SKILL.md:126` | main 既有 | 操作／受管表格隨契約同步；適用版本與行為登錄需一併補正。 |
| `sw_core/doctor_cmd.py:678` | main 既有 | Python 與 Rust 同樣吞解析錯誤；純函式抽取已重現空 list。 |
| `tests/contract_blackbox/test_bb_cli_help.py:71` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `tests/contract_blackbox/test_cli_mgmt_doctor.py:180` | main 既有 | 既有已鎖 A/B 改 19→20 檢查數，非新增 nodeid。 |
| `tests/test_docker_upgrade.py:785` | main 既有 | 新增 allowlist PASS／non-advisory FAIL；缺實際預期差異集合斷言。 |
| `tests/test_doctor.py:50` | main 既有 | 多 7 scenario；malformed→[] 是目前錯誤設計的預期，應改 caller 公開診斷測試。 |
| `tests/test_stage0_release_manifest.py:44` | main 既有 | 兩平台基線 doctor 清單同步新增 check。 |

### PR #290（36 檔）

| 檔案:head 行號 | main 路徑 | 審查結果 |
|---|---|---|
| `CLAUDE.md:255` | main 既有 | 僅在 template 欄位清單加入新鍵；symlink 政策仍維持。 |
| `README.md:426` | main 既有 | 中英解釋 list、預設、逾時／READY recover；舊檔手動遷移需明確保留。 |
| `changelog.d/timeout-recovery-keys.md:1` | 新增 | direct-child fragment 存在；記載本 PR 的功能／修正，不能代替 behavior_changes 核准。 |
| `contracts/cli_help/linux/action_version.json:4` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `contracts/cli_help/linux/action_version_session.json:5` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `contracts/daemon_logging.yaml:120` | main 既有 | 只改 source 行號，不改 level／模板；整合 session_manager 後須重對實際行號。 |
| `contracts/golden/load_profiles.tsv:1` | main 既有 | 46→51；五個 fixture、新欄位；舊 missing_dir_case 移至 p051 未刪除。 |
| `contracts/golden/p7/auth_resolution.ndjson:1` | main 既有 | 31 列只多 profile 新欄位。 |
| `contracts/golden/p7/login_fsm_scenarios.ndjson:1` | main 既有 | 69 列只多 profile 新欄位。 |
| `contracts/golden/p7/quiet_rx.ndjson:1` | main 既有 | 20 列只多 profile 新欄位。 |
| `contracts/golden/p7/recover_responses.ndjson:16` | main 既有 | 019～022 實際改 TX／耗時，019 成功→PROMPT_TIMEOUT；需核准 oracle 差分。 |
| `contracts/golden/profiles_fixtures/46_timeout_recovery_keys/keys.yaml:1` | 新增 | 覆蓋預設／null／單鍵／反序／空鍵及 target 繼承、覆寫。 |
| `contracts/golden/profiles_fixtures/47_timeout_recovery_keys_str_raises/bad.yaml:1` | 新增 | 字串非 list 期待 ValueError。 |
| `contracts/golden/profiles_fixtures/48_timeout_recovery_keys_unknown_raises/bad.yaml:1` | 新增 | 大小寫錯誤的未知鍵期待 ValueError。 |
| `contracts/golden/profiles_fixtures/49_timeout_recovery_keys_duplicate_raises/bad.yaml:1` | 新增 | 重複項目期待 ValueError。 |
| `contracts/golden/profiles_fixtures/50_timeout_recovery_keys_target_raises/bad.yaml:1` | 新增 | target 非字串項目期待 ValueError。 |
| `crates/sw-cli/tests/argparse_golden.rs:5` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `crates/sw-core/src/op_runner.rs:881` | main 既有 | 依 enum list 逐鍵送 byte；保留 fence 與錯誤收尾。 |
| `crates/sw-core/src/recover.rs:23` | main 既有 | ReadyKeys 註解同步空鍵後 force 語意；無新增狀態機。 |
| `crates/sw-core/src/session_manager.rs:34` | main 既有 | minimal passthrough constructor 補預設；其他 clone 自動攜帶。 |
| `crates/sw-core/tests/common/p7.rs:181` | main 既有 | golden profile helper 支援新欄，舊 golden 欄缺仍用預設。 |
| `crates/sw-core/tests/timeout_recovery_keys.rs:1` | 新增 | 9 新案例含順序、target override、READY recover；未入 L1 鎖。 |
| `crates/sw-proto/src/config_types.rs:36` | main 既有 | enum、payload、解析、template／target 繼承同步；不改未知鍵靜默 fallback。 |
| `crates/sw-proto/tests/common/mod.rs:78` | main 既有 | golden 序列化 helper 輸出新欄位；與 Python dataclass 輸出一致。 |
| `docs/serialwrap-spec.md:321` | main 既有 | 新增配置語意與兩條恢復路徑；需同步 PROMPT_TIMEOUT catalog。 |
| `sw_core/assets/profiles/default.yaml:17` | main 既有 | prpl 新模板只送 CTRL_C；舊線上檔不會自動得到。 |
| `sw_core/assets/skill/SKILL.md:107` | main 既有 | 操作／受管表格隨契約同步；適用版本與行為登錄需一併補正。 |
| `sw_core/config.py:8` | main 既有 | 預設 tuple 與 parser 同 Rust；target null 繼承，[] 清空。 |
| `sw_core/session_manager.py:22` | main 既有 | dynamic／pin 攜新欄；[] 同時跳過 deferred 失效需補安全裁決／測試。 |
| `tests/contract_blackbox/p7_profiles/shell_ctrl_c_only.yaml:1` | 新增 | 專用 [CTRL_C] fixture。 |
| `tests/contract_blackbox/p7_profiles/shell_no_recovery_keys.yaml:1` | 新增 | 專用 [] fixture。 |
| `tests/contract_blackbox/test_bb_cli_help.py:71` | main 既有 | 單一 head 指紋重算相符；與其他 PR 整合後必須重產，不能挑一邊常數。 |
| `tests/contract_blackbox/test_bb_timeout_recovery_keys.py:1` | 新增 | 4 黑箱場景核對恢復 byte；真時間 12～15s 斷言需穩定化，非此次紅燈。 |
| `tests/test_contracts_keep_baseline.py:222` | main 既有 | 兩個寫死 logger source 行號與該 head 同步；多 PR 整合後須重對。 |
| `tests/test_issue26_interactive_guard.py:40` | main 既有 | MagicMock 明示預設 tuple，防迭代空 mock 假裝不送鍵；保留原 Ctrl-C 意圖。 |
| `tests/test_timeout_recovery_keys.py:1` | 新增 | 解析、動態／pin、預設／單鍵／空鍵測試；缺帶 deferred input 的反例。 |

## 附錄 B：離線產物與複核入口

- `audit-manifest.json`：固定 main／head／merge-base／檔案清單。
- `pr277.diff`、`pr279.diff`、`pr287.diff`～`pr290.diff`：完整差分。
- `sources/<PR>/`、`sources/main/`：本輪用到的原始碼快照；未變更的引用可用 `git show <固定SHA>:<path>` 補讀。
- `merge-results.json`、`merge-N.log`：六支對 main 的實際試合併及清理。
- `merge-pair-results.json`、`merge-pair-A-B.log`：六組兩兩試合併及清理。
- `ci-logs/*.log`：原始輸入；`*.numbered.txt`：保留行號的去時間戳版本。
- `golden-290-leaf-diff.json`：golden 解碼逐葉差分；NaN 保留原值，不列成值改變。
- `doctor-malformed-repro.json`：抽取純函式、三組字串輸入的實際結果。
- `new-tests-lock-check.json`：新增具名 cargo 測試與 L1 鎖比對。
- `file-inventory.json`：逐檔 head hunk 行號及 main 路徑存在性。
- `repo-before.txt`、`repo-after.txt`、`audit-closeout.json`：原 repo status 保留、origin/main／origin/HEAD 的外部變動及暫存 worktree 清理查核。

- `main-refresh.json`、`main-refresh.diff`、`merge-refresh-results.json`、`merge-refresh-N.log`：收尾 main 更新範圍與六次重驗。
- `sources/main-release/`：收尾版 CHANGELOG 與新增已知問題文件；不覆寫初輪比較基準。
- `overlap-files.json`：287～290 各組完整同檔交集。
