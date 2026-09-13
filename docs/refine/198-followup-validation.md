# #198 後續安排與有界成效驗證

日期：2026-09-13。這份紀錄區分本輪重播、待實作功能與待外部環境驗證；不把執行測試的時間當成人工節省時間。

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
