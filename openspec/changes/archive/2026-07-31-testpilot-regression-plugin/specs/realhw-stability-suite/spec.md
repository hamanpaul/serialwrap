# realhw-stability-suite（delta）

## MODIFIED Requirements

### Requirement: 測試部署後系統
所有 case 的驅動 SHALL 經由已安裝的 `serialwrap` CLI（subprocess）操作 live daemon 與真板；CLI 執行檔 SHALL 可由呼叫端注入路徑（`SwCli` 建構子參數，預設 `"serialwrap"` 走 PATH，既有行為不變），供在 PATH 受污染環境（如綁 stale client 的 venv，#154）中 pin 定部署版執行檔。套件 MUST NOT import `sw_core`、MUST NOT 直接修改 daemon 的 state/config 檔案。破壞性 case SHALL 帶 `destructive` 標記並自行還原（release 後收回、reboot 後等 READY）；harness SHALL 於 case 之間驗證兩板 READY，不 READY 時嘗試恢復一次，仍失敗則後續依賴板卡的 case 記 SKIP。

#### Scenario: case 弄髒環境不擴散
- **WHEN** 某 destructive case 結束後板卡未回 READY
- **THEN** harness 嘗試一次恢復（如 `device attach`／等待），仍失敗則後續依賴 case 記 SKIP 而非 FAIL，報告載明起因 case

#### Scenario: 注入執行檔路徑生效
- **WHEN** 呼叫端以絕對路徑建構 `SwCli`
- **THEN** 所有 serialwrap 子命令經該執行檔執行，不經 PATH 解析；未注入時行為與既有版本相同
