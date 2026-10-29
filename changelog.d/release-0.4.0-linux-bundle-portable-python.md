---
type: fix
scope: release
---
release workflow 的 `publish-linux-bundle` job 改為 `runs-on: ubuntu-24.04`，以系統 `/usr/bin/python3.12` 建立 bundle 內附的 Python runtime（`pyvenv.cfg` 為 `home = /usr/bin`，對齊 OD-50 的使用者主機），不再使用 `actions/setup-python` 的 hosted toolcache Python——後者建出的 runtime 依賴 `/opt/hostedtoolcache` 的 libpython 與標準函式庫，搬到使用者主機後 daemon 無法啟動（S1-3185c6d-1）。smoke 新增 `pyvenv.cfg` 可攜性檢查，並在乾淨 `ubuntu:24.04` 容器（`--network none`、只唯讀掛載解開的 bundle）內以 bundle 的 `bin/serialwrap daemon start`／`status`／`stop` 啟停 on-demand daemon，失敗即阻擋上傳。
