# Windows Remote Live 端到端驗證程序（rm-live-e2e）

本文件說明在原生 Windows 環境下，透過遠端通道（Remote Tunnel / SSH port forwarding）存取 Windows serialwrap 服務並連接實體裝置或虛擬 COM 埠的即時端到端驗證程序（對應 P2 案例 `rm-live-e2e` 之 Windows 面）。

## 前置條件

1. **網路與 SSH 通道**：
   - 遠端主機具備 SSH 服務，且客戶端已建立本機金鑰免密認證。
   - Windows 原生 OpenSSH 或相容通道代理。
   - 信任邊界遵循 OD-12：Windows RPC 與 Console 信任範圍為本機使用者及經由 SSH 認證登入之操作者。

2. **序列埠裝置環境**：
   - DUT 端連接實體 USB-UART 裝置或通過 com0com 設定之虛擬連接埠配對（如 `COM20:COM21`，OD-15）。
   - 裝置白名單已設定允許存取目標連接埠。

3. **受測組件**：
   - Windows 平台已安裝 Rust CLI（`serialwrap.exe`）與背景 daemon（`serialwrapd.exe`）。

## 步驟

1. **建立遠端通道連線**：
   使用 Rust CLI 的 remote 指令建立通往遠端主機的通道（-L forward/connect）：
   ```powershell
   serialwrap remote -L "user@remote-win-host:48700"
   ```

2. **驗證遠端端點可用性**：
   透過本機映射之端點向遠端 daemon 發送狀態探測：
   ```powershell
   serialwrap --endpoint tcp://127.0.0.1:48700 daemon status
   ```
   確認 RPC 回應包含 `ok: true` 與遠端主機資訊。

3. **端到端命令提交與 Console 連線**：
   - 透過遠端連線提交測試命令並確認 WAL 雙軌紀錄：
     ```powershell
     serialwrap --endpoint tcp://127.0.0.1:48700 cmd submit --selector COM20 --cmd "echo remote-e2e-ping" --source "agent:wine2e"
     ```
   - 透過 TCP Console 連入確認即時雙向位元組流（OD-01 規範之 TCP+Telnet 串流）。

4. **紀錄觀測數據**：
   使用證據收集腳本紀錄連線建立時間、RTT 延遲、位元組傳輸完整性與連線關閉狀態。

## 預期證據

產出之證據檔 `rm-live-e2e.json` 須包含：
- 遠端 RPC 連線成功之 ping 回應與伺服端識別資訊。
- 命令提交之完整生命週期（submitted → completed）。
- 遠端位元組傳輸無亂序或遺失（`no_forbidden_interleave`）。
- 連線關閉後行程與 SSH 通道之乾淨清理。

## 判定

判定器執行：
```bash
python3 scripts/rust/judge_spike2.py --evidence-dir <evidence_dir> --require-case rm-live-e2e
```

- **PASS**：遠端通道成功建立，RPC 與 Console 操作正常完成，位元組串流無損壞且無安全不變量違規。
- **FAIL**：連線中斷、RPC 探測超時、資料傳輸毀損或程序殘留。
- **INSUFFICIENT_EVIDENCE**：缺漏連線觀測點或遠端回應紀錄。

## 清理

測試結束後執行清理：
1. 終止遠端連線與 SSH 通道：
   ```powershell
   serialwrap remote close 48700
   ```
2. 釋放佔用之序列埠資源：
   ```powershell
   serialwrap --endpoint tcp://127.0.0.1:48700 device release --selector COM20
   ```
3. 關閉測試用之本機轉發連接埠。
