## ADDED Requirements

### Requirement: 缺 POSIX stat 欄位保守處理
holder 探測 SHALL 容忍端點及 fd stat 缺 st_rdev，維持存在時的 POSIX char-device 比對，不把無 /proc 或缺資料的空結果解讀為 Windows exclusive 證明。

#### Scenario: 缺少 st_rdev
- **WHEN** 任一 stat 結果缺少 st_rdev
- **THEN** 不拋 AttributeError，仍能做既有 path 比對且不誤配不同裝置。

#### Scenario: 正常 POSIX 裝置
- **WHEN** path 不同但有效 char-device rdev 相等
- **THEN** 保持既有 holder 偵測結果。
