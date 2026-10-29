---
type: change
scope: install
---

`install.sh` 改為安裝 Rust bundle（OD-50）：預設從 GitHub release 下載 `serialwrap-<版本>-linux-x86_64.tar.gz` 與 `SHA256SUMS` 並驗證（未給 `--version` 取最新 release；雜湊不符或缺列即失敗、不執行 setup），或以 `--bundle <目錄或 .tar.gz>` 離線安裝，解到暫存目錄後執行 `<bundle>/bin/serialwrap setup` 並回傳其 rc。安裝前檢查 x86_64、glibc ≥ 2.39 與 `/usr/bin/python3.12`，不符時 rc=2 且不改動任何東西；偵測到舊 pipx 安裝時提示可 `pipx uninstall serialwrap`（不自動移除）。pipx 安裝路徑退役，README 安裝章節改以 `install.sh`／bundle 為主要方式。
