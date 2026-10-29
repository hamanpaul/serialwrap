---
type: fix
scope: release
---

Docker 升級驗證（`scripts/docker/run-upgrade.sh`）自建的 0.4.0 bundle 改比照 release workflow 的 `publish-linux-bundle`：先確認主機 `/usr/bin/python3.12` 可執行、實體位於 `/usr/bin` 且版本符合 `contracts/cli_help/_meta.json`，以它建立 build venv 後執行 `mkbundle.sh --release-bundle`；自建或 `--bundle` 提供的 bundle 皆須 `py/pyvenv.cfg` 為 `home = /usr/bin` 且 `include-system-site-packages = false`。任一不符以 rc=3 拒絕，不回退其他 python（release 前置第 2 輪 OWNER-BATCH #2）。合入 OD-48 後新出現的 `--scenario install-sh --mode systemd-system` 組合改為 rc=2 明確拒絕。
