# uart-ownership-evidence Specification

## Purpose
以決定性證據約束同 session 操作仲裁、interactive／human console 與 recovery epoch 邊界。

## Requirements
### Requirement: logical 操作不得交錯
系統 SHALL 序列化或明確拒絕同 session 的競爭 file.push/pull/foreground command；正常 epoch 的 command.submit 仍允許多 client accepted。

#### Scenario: 傳輸阻塞時競爭
- **WHEN** 第一個 push 或 pull 停在 fake bridge barrier，另一傳輸或 command 到達
- **THEN** 競爭者不得在第一個操作完成前寫入該 bridge，忙碌拒絕有明確錯誤碼且不 TX。

### Requirement: 失效 lease 與 console 契約
系統 SHALL 拒絕 unknown/closed/expired/replaced interactive ID 的寫入，並保持 secondary console 的 line-buffer broker 行為。

#### Scenario: 舊 handle 發送
- **WHEN** lease 已 close、expiry 或 replacement 後使用舊 ID
- **THEN** 返回對應錯誤且 fake UART send 次數為零。

#### Scenario: 次要 console
- **WHEN** raw owner 存在，secondary console 輸入 bytes 後換行
- **THEN** 原始 bytes 不直接 TX，完整行只經 broker 提交。

#### Scenario: 操作期間新 console
- **WHEN** foreground command、file push 或 file pull 已取得 operation admission，新的 POSIX 或 TCP human console 加入
- **THEN** console 可觀察輸出並沿 line-buffer broker 提交，但不能在操作期間取得 raw TX；操作成功或例外結束後依既有 lease、suspend 與 FLASHING 條件恢復 admission。原有 agent interactive owner 的授予語意不因新增 human gate 而改變。

### Requirement: recovery epoch 隔離
系統 SHALL 以決定性測試驗證舊操作不寫新 bridge、也不清除新 epoch 的 busy 狀態；未被證實的其他生命週期界線明列而非宣稱全稱安全。

#### Scenario: 舊 callback 跨越重建
- **WHEN** 舊 callback 被阻塞，session 換 bridge 或重新註冊後再釋放
- **THEN** 新 bridge 無舊操作寫入，新操作持有的 busy 狀態不被舊 finally 清掉。
