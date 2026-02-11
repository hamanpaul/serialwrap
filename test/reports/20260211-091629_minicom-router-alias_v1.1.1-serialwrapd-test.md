# minicom-router-alias 測試報告

- 時間(UTC): 2026-02-11 09:16:29
- 版本: v1.1.1-serialwrapd-test
- 範圍:
  - 驗證 `tools/minicom_router.sh` 可透過 broker PTY 下 `ls` 並取回內容
  - 更新 `~/.bashrc` 的 `minicom` alias 指向 router

## 測試方法
- 使用 fake UART target（pty）+ `serialwrapd`
- 以 `minicom -S`（透過 `minicom_router.sh`）送 `ls`
- 用 WAL 驗證：
  - `TX source=human payload=ls`
  - `RX payload` 含 `fileA  fileB` 與 `RESULT:ls:OK`

## 測試命令（沙盒外）
```bash
/usr/bin/python3 /tmp/test_minicom_router_e2e.py
```

## 結果
- 腳本回傳 `ok=true`
- `wal_ok=true`
- `saw_human_ls=true`
- `saw_result=true`

## alias 變更
- 檔案: `/home/paul_chen/.bashrc`
- 行號: `189`
- 內容:
  - `alias minicom="/home/paul_chen/arc_prj/ser-dep/tools/minicom_router.sh"`
