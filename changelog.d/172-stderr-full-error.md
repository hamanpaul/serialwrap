---
type: fix
scope: cli
issue: 172
---

CLI 失敗時 stderr 不再丟棄具體錯誤原因（#172）。

- `_run_rpc`：原本 `resp.get("error_code") or resp.get("message")` 的 `or` 短路，讓有
  `error_code` 時 `message` 永遠不會出現在 stderr——對 `SOCKET_ERROR` 來說，`message`
  正是 `str(OSError)`（errno 與連線細節），三種截然不同的根因（socket 路徑算錯／daemon
  已死／權限不符）被壓成同一個字串，只讀 stderr 的呼叫端（如 testpilot）事後無法定案。
  現在 stderr 一行同時輸出 `error_code`、`message`（有值才追加）與 `hint`（有值才追加），
  形狀為 `serialwrap: {method} failed: {code}[: {message}][（hint: ...)]`；
  `serialwrap: {method} failed: {code}` 這段前綴逐字保留，既有以 substring 比對
  `failed: SOCKET_ERROR` 之類的下游不受影響。
- 新增 `_mirror_err()` helper，補齊先前只 `_print()` JSON 到 stdout、exit 非零時 stderr
  卻全空的多個出口：`daemon start` 的 `--endpoint` 拒收 / `--endpoint`,`--socket` 不一致 /
  systemd 監管模式重導 `service start`（含 `NEEDS_SUDO`）/ Windows 找不到 daemon binary /
  env file 載入失敗 / daemon 提前結束 / daemon 逾時未就緒；`daemon stop` 重導
  `service stop` 與 on-demand `daemon.stop` 失敗；`event` 未知子命令與各 `event.*` RPC
  失敗；`remote`（native Windows 不支援 / tunnel 例外 / 未預期例外）；`setup` 的
  `FLASHING_BUSY` 兩處；`service` 子命令失敗；未知頂層子命令的 `INVALID_ARGS`。stdout
  的 JSON 契約與 `ok:True` 路徑完全不動。
- 對應實地事故：systemd-system 模式未帶 `--with-sudo` 執行 `daemon start`
  時，呼叫端原本只拿到 `serialwrap: daemon start failed: ` 這種冒號後空白的字面，
  現在會看到 `NEEDS_SUDO` 與代跑提示（hint）。
