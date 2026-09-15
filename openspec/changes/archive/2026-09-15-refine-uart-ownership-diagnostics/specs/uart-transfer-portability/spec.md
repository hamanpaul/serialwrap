## ADDED Requirements

### Requirement: 可驗證的雙向傳輸工具 fallback
系統 SHALL 在 push/pull 資料傳輸前驗證 base64 工具；不可用時驗證 OpenSSL 替代，全部不可用時明確回報 TARGET_DECODER_MISSING（push）或對應 encoder 錯誤（pull），不得誤用 echo 為成功證據。

#### Scenario: 板端僅有 OpenSSL
- **WHEN** base64 不可用而 OpenSSL 編解碼可用
- **THEN** binary 與空檔的 push/pull 使用 fallback 且 checksum 正確。

#### Scenario: 所有候選不可用
- **WHEN** 只有命令回顯、沒有成功 sentinel
- **THEN** 早期拒絕資料傳輸並回報工具缺失，不搬移損毀檔。

### Requirement: profile 單行預算
系統 SHALL 支援可選 max_console_line_chars，依實際 shell 命令的 UTF-8 byte 長度（不含換行）限制分段。不得把 505 硬編碼成所有 prpl 的行長。

#### Scenario: 505-byte 預算
- **WHEN** 設定 505 且使用者指定過大 chunk
- **THEN** 每一送出命令皆在預算內，資料正確或在 TX 前明確拒絕無法容納的固定命令；保留 echo stall 清半行及 checksum 防線。

#### Scenario: 無效或太小預算
- **WHEN** profile 值無效，或合法值小到容不下控制命令
- **THEN** 解析拒絕無效設定或傳輸在 TX 前回明確錯誤，不送截斷命令。
