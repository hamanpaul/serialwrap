---
type: fix
scope: remote
---
`serialwrap remote`：收斂 Copilot PR review 三項發現。① `--local` 新增範圍／角色驗證——搭 `-R`／預設（expose）帶入視為誤用，早擋 `INVALID_ARGS`；搭 `-L` 但超出 `1..65535` 範圍同樣早擋，不再交給 ssh 晚爆成非 `INVALID_ARGS` 錯誤。② `Registry` 新增 `log_path()` 單一事實來源，`remove()`（`status` prune／`close` 皆走此路徑）與 `open_tunnel` 的 spawn log 皆改用此方法，`<port>.log` 隨 state／control socket 同生命週期清除，不再於重試／失敗後於 `<run_dir>/remote/` 累積孤兒 log 檔。③ docker 測試映像（`Dockerfile`）的 host-key-skip（`StrictHostKeyChecking no`／`UserKnownHostsFile /dev/null`）改由全域 `/etc/ssh/ssh_config` 收斂至僅 `tester`（唯一會執行 `serialwrap remote`／spawn ssh 的帳號）的 `~/.ssh/config`，不再弱化映像內其他使用者（`otheruser`／`root`）的 host-key 驗證基準；已以獨立 throwaway 容器驗證 `tester` 免 host-key 提示連線成功，`root`／`otheruser` 仍正常觸發 host-key 驗證失敗。
