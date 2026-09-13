## Why

#198 已校正共享 UART 的契約與投入範圍，仍需以實作及可重現測試處置檔案傳輸、ownership 邊界與診斷證據缺口。這輪沿既有 broker 改善，不建立新的硬體平台。

## What Changes

- #166 增加可驗證的 Base64 工具偵測／OpenSSL fallback，以及 per-profile 單行預算推導；保留 checksum 與 echo stall 安全網。
- 補上 ownership 正反例；重現的 logical writer 競態必須修復，無法由本輪證據支持的界線明文列管。
- #171 首批僅加入可選 CLI RPC trace，保持既有輸出及 RPC／UART 副作用。
- #199 holder 探測容忍 stat 缺少 st_rdev，但不把缺資料視為 Windows 排他證據。
- 記錄 #182／#197 的獨立後續安排及有界 replay 成效，不宣稱已部署、真板通過或量化長期收益。

## Capabilities

### New Capabilities

- `uart-transfer-portability`: 傳輸工具偵測、行長預算及失敗訊號。
- `uart-ownership-evidence`: logical writer 互斥、stale lease 與 recovery epoch 的測試契約。
- `cli-rpc-trace`: 白名單、可選且不改操作語意的控制平面診斷。
- `holder-probe-portability`: 缺少 POSIX metadata 時的保守探測行為。

### Modified Capabilities

無既有 requirement 取代；新增契約補充現有 session-interactive 與 MCU flash 的行為。

## Impact

涉及 sw_core 的 file_transfer/config/session_manager、cli/client 及相關測試、文件與 regression 案例。無新依賴、無預設 runtime 變更、無公開 ACL；不重做 #172、#181、#189，不實作 #182 reload 或 #197 provider SDK。
