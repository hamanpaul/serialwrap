# Windows 開發執行環境

Windows 開發版 CLI 不會因為 `%LOCALAPPDATA%\Programs\serialwrap` 存在 `current-release.json`，就自動載入該安裝的 release manifest。Rust CLI 只有在目前執行檔是安裝根 `bin\serialwrap.exe` 或 `bin\serialwrapd.exe` 時，才會透過該索引找到版本目錄；開發版執行檔仍可讀取自己 bundle 根目錄內的 manifest，或使用明確設定的 `SERIALWRAP_RELEASE_MANIFEST`。

Windows 測試與黑箱 harness 必須把 `SERIALWRAP_INSTALL_ROOT` 指向該案例的暫存安裝根，避免繼承使用者安裝路徑。需要驗證特定 staged manifest 時，另外明確設定 `SERIALWRAP_RELEASE_MANIFEST` 指向測試 fixture。

Stage 2 Windows bundle 的 CLI delegate 仍使用內附 Python runtime 與 PyYAML，因此 `doctor` 保留 `python`、`pyyaml` 檢查。當 manifest 同時宣告 `stage: rust-2` 與 `components.daemon.implementation: rust` 時，daemon 不使用 Python 序列埠後端，`doctor` 不要求 `pyserial`；有效 manifest 的 `version_pairing` 仍照常檢查。
