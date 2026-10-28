---
type: feat
scope: version
---

看得出 daemon 跑的是哪份程式碼，且 `serialwrap setup` 升級後會讓 systemd 服務的 daemon 換新碼。版本字串只來自 `VERSION`，release、分支 build、main 全印 `0.3.2`；`pipx install --force` 加 `serialwrap setup --user` 之後，已在跑的 systemd 服務不會被重啟（同模式刷新只重寫 unit，轉換路徑的 `systemctl start` 對 active 服務是 no-op），daemon 靜默沿用舊碼，agent 只能靠檔案時間猜，曾因此判斷錯誤。

- 程式碼指紋：`sw_core/**/*.py` 的 sha256 前 12 碼（不含 assets、`__pycache__`；每個行程只算一次）。`serialwrap --version` 印 `0.3.2 (code 1a2b3c4d5e6f)`（僅被問時才算）；daemon 啟動時算一次，`daemon status` 回 `code_fingerprint` 與 `code_path`，每個 RPC 回應也帶 `code_fingerprint`。
- client↔daemon 比對：版本相同但指紋不同時，CLI 在 stderr 印一行並建議 `serialwrap service restart`；exit code 與 stdout JSON 不變。舊 build 的 daemon 沒有此欄位時靜默。
- `serialwrap setup`：同模式的 systemd 服務 active 且 daemon 指紋與剛安裝的不同（或 daemon 完全不回報指紋）時，刷新 unit 後自動 restart 並核對重啟後的指紋，結果放在輸出的 `daemon_code`；指紋相同、daemon 連不上、服務非 active、flash 進行中、on-demand 一律不動。system scope 未帶 `--with-sudo` 只把 `sudo systemctl restart serialwrap` 列入 `pending_sudo`。新增 `--no-restart` 略過重啟。
- `serialwrap doctor` 新增 advisory 檢查 `daemon_code_current`。
- 相容性：`serialwrap --version` 輸出由 `serialwrap 0.3.2` 變成 `serialwrap 0.3.2 (code …)`，以 regex 取 `\d+\.\d+\.\d+` 的既有消費者（realhw、regression preflight）不受影響。實測 CLI 每次呼叫約多 1.6 ms（約 59 ms 的 2.7%）。
- 規格見 `docs/serialwrap-spec.md` §8.7。
