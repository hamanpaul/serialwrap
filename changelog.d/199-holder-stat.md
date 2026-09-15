---
type: fix
issue: 199
scope: session-manager
---
修復 `SessionManager._probe_external_holder` 在非 POSIX 或 Windows 環境下因 `os.stat` 結果缺乏 `st_rdev` 屬性而拋出 `AttributeError` 的問題。

- **根因**：`_probe_external_holder` 直接存取 `os.stat(real_path).st_rdev` 與 `os.stat(fd_path).st_rdev`，原僅以 `try ... except OSError` 包裹。在非 POSIX / Windows 等缺少 `st_rdev` 屬性的環境下，直接存取會引發未捕獲的 `AttributeError`，中斷 holder 探測與狀態判斷。
- **修法**：兩處皆改以 `getattr(..., "st_rdev", 0)` 防禦性取得。若值為 `0`，依契約不視為裝置同一性證據（由既有 `if not matched and target_rdev:` 排除），避免跨裝置誤配；同 path 字串比對及正常 POSIX char-device 裝置比對維持原有邏輯。
- **測試與邊界**：新增 `tests/test_holder_probe_portability.py`，驗證 endpoint 缺 `st_rdev`、fd 缺 `st_rdev`、正常 POSIX `st_rdev` 匹配、不同 `st_rdev` 不誤判、同 path 匹配、`rdev=0` 不誤判、無 `/proc` 安全回傳空結果等情境。
- **regression-case 評估**：本變更屬 in-process 平台相容屬性存取，pytest 邊界測試已完整覆蓋；真實 Windows 序列埠與外部 flasher 斷線為既有實機回歸項目，本輪不假稱實機通過，無需新增 `regression/` 案例。
