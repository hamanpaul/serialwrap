# minicom-router-attach-install 測試報告

- 時間(UTC): 2026-02-11 09:26:16
- 版本: v1.1.1-serialwrapd-test
- 範圍:
  - `minicom_router.sh` 自動 `session attach`（非 READY session）
  - `minicom_router.sh`/`minicom-broker.sh` 支援部署路徑自動尋址
  - 新增 `install.sh`（預設 `~/.paul_tools`，可帶安裝路徑）

## 變更
- `tools/minicom_router.sh`
  - 新增 `ATTACH_WHEN_NOT_READY`、`ATTACH_WAIT_TICKS`、`PREFERRED_COM`
  - selector 存在且非 READY 時，先 `session attach --selector <selector>` 再等待 READY
  - 無 selector 時：優先第一個 READY；否則先 attach `COM0`（可用 `PREFERRED_COM` 覆蓋）
  - 支援自動解析 base 目錄（repo 與部署目錄）
- `tools/minicom-broker.sh`
  - 改為在相鄰路徑尋找 `minicom_router.sh`，不再硬編碼 repo 絕對路徑
- `install.sh`
  - 安裝核心腳本與 `sw_core/sw_mcp/profiles/tools/docs` 到目標路徑

## 測試
### 1) shell 語法檢查
```bash
/usr/bin/bash -n /home/paul_chen/arc_prj/ser-dep/tools/minicom_router.sh
/usr/bin/bash -n /home/paul_chen/arc_prj/ser-dep/tools/minicom-broker.sh
/usr/bin/bash -n /home/paul_chen/arc_prj/ser-dep/install.sh
```
結果: `OK`

### 2) 安裝流程
```bash
/home/paul_chen/arc_prj/ser-dep/install.sh /tmp/paul_tools_test_install
```
結果: 成功安裝，產生 `serialwrap/serialwrapd.py/serialwrap-mcp/minicom_router.sh/...`

### 3) minicom router e2e（含指令回收）
```bash
/usr/bin/python3 /tmp/test_minicom_router_e2e.py
```
結果(JSON):
- `ok=true`
- `wal_ok=true`
- `saw_human_ls=true`
- `saw_result=true`

## 結論
- `minicom_router.sh` 已可在 session 非 READY 時自動 attach，再連 broker PTY。
- 部署到 `~/.paul_tools` 的安裝入口已準備完成。

### 4) attach 觸發驗證（實機 session DETACHED）
命令:
```bash
MINICOM_BIN=/bin/true /usr/bin/timeout 4 /home/paul_chen/arc_prj/ser-dep/tools/minicom_router.sh COM0
```
觀察:
- 執行前 `detached_at=2026-02-11T09:25:37.936683+00:00`
- 執行後 `detached_at=2026-02-11T09:26:55.934701+00:00`
- 表示 router 已觸發 `session attach`；此環境仍因 `ATTACH_FAILED:PermissionError` 未進 READY。
