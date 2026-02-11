# minicom-router-autostart 測試報告

- 時間(UTC): 2026-02-11 09:18:47
- 版本: v1.1.1-serialwrapd-test
- 範圍:
  - `minicom_router.sh` 自動啟 daemon
  - `minicom` 無 selector 時自動選第一個 READY session
  - broker 不可用時錯誤訊息與 hint 改善

## 變更
- 檔案: `/home/paul_chen/arc_prj/ser-dep/tools/minicom_router.sh`
  - 新增 `SERIALWRAP_PROFILE_DIR`、`SERIALWRAP_AUTO_START_DAEMON`
  - socket 不可用時自動執行 `serialwrap daemon start`
  - selector 空白時，改選第一個 `state=READY` 且有 `vtty` 的 session
  - 補充錯誤 hint

## 驗證
### 1) 語法檢查
```bash
/usr/bin/bash -n /home/paul_chen/arc_prj/ser-dep/tools/minicom_router.sh
```
結果: `OK`

### 2) e2e（無 selector）
```bash
/usr/bin/python3 /tmp/test_minicom_router_e2e.py
```
結果(JSON):
- `ok=true`
- `wal_ok=true`
- `saw_human_ls=true`
- `saw_result=true`

## 結論
- `minicom` 直接執行（不帶 selector）可經 broker 接到 READY session，且可下 `ls` 並回收結果。
