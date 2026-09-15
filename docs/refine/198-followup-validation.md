# #198 後續安排與有界成效驗證

日期：2026-09-13。這份紀錄區分本輪重播、待實作功能與待外部環境驗證；不把執行測試的時間當成人工節省時間。

## 2026-09-15 驗收階段裁決

使用者確認目前本機沒有 UART environment，實機測試先延後。Root 負責既定技術取捨及測試分層；不要求使用者逐條判斷 admission、回放、epoch cleanup 等實作是否正確。

依 [專案政策](../../CLAUDE.md) 的 pytest／回歸 case 要求與 [README](../../README.md) 的實機章節，採下列分期。
這不是免驗，也不是改寫既有 FAIL：本輪可執行的離線驗證照常要求通過。

| 階段 | 必要證據與處理 | 本輪無 UART 的影響 |
|---|---|---|
| 本地開發／程式審查 | 真正 RED→GREEN、決定性競態／例外測試、完整 pytest、policy、精確候選的審查結果；必要實機回歸 case 的歸屬評估 | 不阻擋；使用隔離 fake transport、PTY／socketpair，不連真板、不降低 assertions |
| PR／整案收尾 | 完整 review gate、OpenSpec archive、具體 PR／CI／exact-head merge 與 issue 證據，不能用本地測試代替遠端結果 | 不因缺 UART 額外擋住；但不免除程式審查或遠端流程，也不自動授權 push／merge |
| 發版前實機回歸 | 在有設備的隔離 bench，對預定發布候選執行受影響的 TestPilot regression，保存版本及逐 case 結果 | **DEFERRED — no UART environment**；保留待驗項，不標 PASS，不作為目前開發的先決條件 |
| 重大更新部署後 | 對已安裝 CLI／daemon 與真板跑實機穩定性套件，確認 runtime 與部署狀態 | 本輪未部署／未執行，不以 offline suite 代替 |

README 另外建議有設備時在改動後常跑實機回歸；不應把「發版前補驗」誤寫成只有發版前才允許測試。
本次沒有降低 release 或部署證據要求，也沒有批准建立 UART 環境、安裝候選、重啟 daemon、reset／reboot 板卡或建立 tunnel。

### 留待有環境時的最小實機清單

- #166：依板端實際工具能力及量測行長跑 binary roundtrip／checksum 與較長檔案案例；保留 OpenSSL fallback、echo stall 與 RX 視窗界線。505 是板端量測，不對所有 prpl 自動套用；1MB pull 的既有 RX 邊界未宣稱修復。
- #198 ownership：真 UART 下的 agent 命令與 human console 共存、傳輸期間輸入的延後回放、斷線／重連後恢復；機械期待沿既有契約，不新增 read-only ACL。僅在另有授權時執行會 reset／reboot 或干擾現場的案例。
- #199／Windows：實際目標平台的 holder／外部工具斷線行為；Linux mock 缺 st_rdev 的通過不是 native Windows 已驗證。
- #197：維持獨立追蹤票及下表的隔離／授權前提，不因本輪無 UART 轉成已完成的 Cloudflare 驗收。

每次實機驗證須記錄候選 SHA、已安裝 CLI／daemon 版本及一致性、平台／板卡／profile、case ID、時間、結果、報告位置及未驗原因。版本不一致、checksum FAIL 等不能以「缺環境」改成 PASS；沒有執行就是 DEFERRED。既有執行與新增 case SOP 見 [回歸插件文件](../regression-plugin.md)。

### 審查工具限制另列

原獨立 reviewer 曾被平台中止，沒有有效裁決，也沒有具體 production defect。
2026-09-15 使用者明確指定「由你審，把這個 goal 跑完」；root 已完成
[最終審查](198-root-final-review.md) 並裁決本輪範圍 PASS。
原中止紀錄不改成 Sol PASS，其他範圍的 scoped 雙審也不冒充 ownership 審查。
本次交付不再等待原失敗席次；實機 DEFERRED 與程式審查仍分開記錄。

## 後續承接

| 追蹤票 | 下一份可獨立驗收交付 | 前提／停止條件 | 不能宣稱的事 |
|---|---|---|---|
| [#171](https://github.com/hamanpaul/serialwrap/issues/171) | CLI trace 之後，另案做共用 RPC server trace、daemon sink／rotation，再評估 session state gate | 沿當次狀態來源，不因觀測 touch lease、自動 probe/reset 或多 TX；日誌白名單與 rotation 有測試才啟用 | 首批 CLI trace 不等於 daemon logging 完成 |
| [#182](https://github.com/hamanpaul/serialwrap/issues/182) | 以 manager 單一 ProfileCatalog、generation 與 validate-before-swap 實作 reload | 先完成既有盤點文件的變更類型表：同名 pin revision、explicit target override、新 target、owner conflict；invalid/stale candidate 全量回滾 | 現有 pin/clear 不會重讀 YAML，不能稱 reload 已有 |
| [#197](https://github.com/hamanpaul/serialwrap/issues/197) | 隔離 bench 的純 SSH → Quick Tunnel → remote health → 一條指定 UART 命令 → 單一 tunnel cleanup；Named Tunnel／重開機另階段 | operator 先指定隔離 bench／帳號／socket／key-only SSH 與公網暴露窗口；未具備不開 tunnel、不改 sshd、不建 DNS／帳戶或 service | argv／mock health 綠燈不等於 Cloudflare/NAT/重開機已驗 |

owner 為 serialwrap 維護者，實作與現場 operator 在各票派工時具名；本輪不假稱已有人承接未派出的任務。以上票保持 OPEN，原始 #32/#33 不重開。

## 本輪已執行的 bounded replay

root 在隔離 integration worktree、baseline ae1296e（production 等同 cf2aaba）執行：

```bash
python3 -m pytest -q tests/test_profile_reresolve_on_attach.py tests/test_profile_pin_sticky.py tests/test_remote_tunnel.py --durations=5
```

結果：**92 passed in 0.80s，exit 0**。測試以 fake bridge、temp registry 與 subprocess 邊界替身處理，不連真實 UART／遠端 SSH／Cloudflare；全套 conftest 保留 live guard。

可支持的具體範圍：

- #181 已載入 template 的 pin 可套用既有 fallback，保留 COM／device identity；pin-hit 不開 PROBE，explicit target 不被 pin 改寫。測試名稱含 test_pin_hit_does_not_probe、test_pin_rekeys_session_id_and_keeps_com、test_pin_does_not_touch_yaml_target_session。
- remote 的 Unix socket/loopback forward argv、SSH opts 透傳、readiness health.ping、bind 未驗證時 fail-closed、dead process registry 清理可重播。test_role_probe_connect_uses_health_ping、test_open_tunnel_fail_closed_removes_state_on_bind_unverified 等確認的是本地邏輯，不是 provider 成功。
- 自動化重播不需要現場人工協調、物理介入或重試；這是本次 fixture 的屬性，不是對真實事故的零介入保證。

## 成效記錄與量測限制

### CLI trace 候選的額外重播

root 對 CLI trace 候選 `9b7ef46` 與原 main `cf2aaba`，各做 5 次「新建暫存目錄中不存在的 Unix socket」操作；候選分別測 quiet 與 verbose，共 15 次 CLI subprocess。每次都是 `session list`，顯式指定不存在的 socket，不觸 production endpoint。5/5 quiet 的 exit/stdout/stderr 逐字等同 baseline；5/5 verbose 的 exit/stdout 不變，stderr 保留既有 error line 並只多 1 筆 trace。

trace assertion：來源 `--socket`、method `session.list`、error `SOCKET_ERROR`、errno `2/ENOENT`、retry_count `0`、endpoint 為 64 字元雜湊且無原始 path/basename。整個 CLI wall time：baseline 48.954–56.929 ms、quiet 50.679–54.548 ms、verbose 54.908–60.108 ms；trace 內部 RPC elapsed 以整數毫秒表示，本例為 0。這是單機短跑、包含 Python 啟動與排程雜訊，不據此主張 logging overhead 的穩定上限或人工定位收益。

CLI 修正兩輪並通過 Sol 聚焦重審後，root 對整合候選 `be35aeb` 與 main
`cf2aaba` 重做同等 15 次 subprocess 驗證：**PASS**。5/5 quiet 的
exit/stdout/stderr 逐字相同，5/5 verbose 維持 exit/stdout 與既有 stderr 錯誤，
並符合上述單筆 trace assertions。這輪 wall time：baseline 49.713–54.286 ms、
quiet 51.327–69.141 ms、verbose 53.135–65.333 ms；差異未排除排程雜訊。
本次為本地候選的 CLI 驗證，仍不是 installed runtime／真實 daemon／UART 證據。

| 指標 | 本輪可確認值／狀態 | 真實 pilot 必須另外記錄 |
|---|---|---|
| 故障定位時間 | 92 個離線回歸 assertion 在 0.80s 收斂；尚無受測 operator 對照定位時間 | 從首次失敗至正確分類的單調時間，不能用 CLI duration 取代 |
| 人工協調 | 此次離線重播無需向 bench 使用者協調；未測 live reload | 受影響 session／使用者數、通知與等候時間 |
| 重試 | 測試直接驗 retry/readiness/fail-closed 分支；沒有現場重試率樣本 | 使用者重送、工具自動 retry 分開計數；mutating timeout 不任意重送 |
| 恢復 | registry teardown／pin re-resolution 的結果已有 assertion；沒有真 tunnel outage 恢復時間 | 首次斷線、重新可用、daemon PID／bridge identity、UART 操作結果 |
| 物理介入 | 本次 0 次，因為未使用真板；不可外推為硬體故障不需介入 | 拔插／reset／重開機／現場人員介入次數及原因 |
| 工具維護 | 本輪不新增 provider SDK、依賴、dashboard 或第二份 state；測試範圍可由既有 pytest 執行 | 新增控制命令/probe 的代價、誤判、版本差異、維護工時 |

沒有 baseline/after 的人工對照樣本，故不宣稱收益倍數、事故率下降或「不需現場協調」。#182 的收益需實際 reload 實作後評估；#197 在已批准的隔離環境內完成每個里程碑後才回填版本與實跑差異。

## 本輪明確未做

- 未安裝候選版本、未重啟／重載 production daemon，未改 live profiles、ownership、state 或 WAL。
- 未公開 SSH、建立 Quick/Named Tunnel、操作 Cloudflare 帳戶／DNS，未重開 bench。
- 此文件不替代 ownership／傳輸／CLI trace 的候選實作測試報告，亦不代表 #198 已可結案。
