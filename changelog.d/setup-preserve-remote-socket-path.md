---
type: fix
scope: setup
---
`serialwrap setup`（含 `install.sh` 內部自動呼叫）不再無條件把 `config.yaml`
既有的遠端 `tcp://` `socket_path` 覆寫回本機預設路徑。

- **事故**：DUT daemon 跑在遠端主機（例如透過 reverse SSH tunnel 把
  daemon 的 TCP endpoint 映成本機可達位址），使用者已手動把
  `~/.config/serialwrap/config.yaml` 的 `socket_path` 設成
  `tcp://127.0.0.1:<port>`；但只要重跑 `install.sh`／`serialwrap setup`
  （常見於重裝或升級套件），`setup` 子命令會依
  `effective_socket = SYSTEM_SOCKET if target == "systemd-system" else
  SOCKET_PATH` 無條件把 `socket_path` 打回本機檔案路徑，導致後續所有
  未顯式帶 `--socket`/`--endpoint` 的呼叫（如 `wal reset`）連到一個
  根本沒有 daemon 監聽的本機 socket，回報 `SOCKET_ERROR: No such file
  or directory`。
- **修復**：`_run_setup()` 新增例外——POSIX（非 win backend）、目標模式
  非 `systemd-system`、使用者未顯式帶 `--socket`/`--endpoint` 時，若
  `config.yaml` 既有 `socket_path` 已是 `tcp://` endpoint（不論是否
  loopback——reverse SSH tunnel 的本質正是把遠端 daemon 映成本機
  loopback 位址，不能用「是否 loopback」排除這個最常見的真實案例），
  保留原值不覆寫。因為 POSIX 上 `setup` 自然算出的本機預設
  （`SOCKET_PATH`/`SYSTEM_SOCKET`）永遠是檔案路徑、絕不會是
  `tcp://`，故「既有值是 `tcp://`」本身即是使用者刻意配置遠端拓樸的
  充分訊號。
- **驗證**：新增 3 個 `tests/test_setup_cli.py` 案例（保留既有遠端
  loopback/非-loopback tcp:// 值、無既有遠端值時維持本機預設、顯式帶
  `--socket` 時不誤觸保留邏輯）；全套 `pytest -q tests/` 1844 passed
  （另 2 個既有失敗與 pyserial/minicom 環境相依，與本次改動無關，非
  新增迴歸）；已手動重現「`config.yaml` 設好遠端 endpoint 後重跑
  `serialwrap setup --on-demand`」情境，確認 `socket_path` 不再被覆寫。
  修正 policy checklist
