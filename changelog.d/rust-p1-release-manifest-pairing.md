---
type: feat
scope: release
---

新增 Python 側 release manifest 讀取與部署程式碼 `tree_sha256`（`sw_core/release_manifest.py`，契約見 `contracts/release_manifest.schema.json`）：位於發佈組合（bundle）內時，`doctor` 新增非 advisory 的 `version_pairing` 檢查，比對 CLI／daemon 版本、launcher／runtime／套件樹與 SKILL 資產雜湊；純 Python 安裝的 `doctor` 輸出不變。`daemon start` 改依 manifest launcher、同目錄 `serialwrapd`、checkout 腳本、`python -m sw_core.daemon` 順序定位 daemon（DF-05），修正 pipx 安裝下 spawn 不存在的 `serialwrapd.py`（CLI-7.4）。同機多開偵測（`doctor` 的 `single_daemon`、`daemon start` 防線）同步計入 `python -m sw_core.daemon` 形式的 daemon。
