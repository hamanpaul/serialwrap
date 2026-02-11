# minicom-router-ready-select 測試報告

- 時間(UTC): 2026-02-11 09:20:04
- 版本: v1.1.1-serialwrapd-test
- 範圍:
  - router 自動啟 daemon 後等待 socket ready（避免啟動競態）
  - broker 無 READY session 時輸出 session state/last_error

## 變更
- 檔案: `/home/paul_chen/arc_prj/ser-dep/tools/minicom_router.sh`
  - `daemon start` 後改為最多 3 秒輪詢 `session list` 成功
  - 失敗訊息新增 sessions 摘要（`state` + `last_error`）

## 驗證
### 1) 語法
```bash
/usr/bin/bash -n /home/paul_chen/arc_prj/ser-dep/tools/minicom_router.sh
```
結果: `OK`

### 2) 失敗路徑可診斷
```bash
/usr/bin/timeout 3 /home/paul_chen/arc_prj/ser-dep/tools/minicom_router.sh
```
結果:
- 顯示 `sessions:`
- 顯示 `COM0 default+1 state=DETACHED last_error=ATTACH_FAILED:PermissionError`
- 顯示下一步 hint 指令
