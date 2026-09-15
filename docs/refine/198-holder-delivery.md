# #199 缺 st_rdev 的 holder 探測修復交付文件

## 1. 概述與問題背景

- **Issue**: [#199](https://github.com/hamanpaul/serialwrap/issues/199)
- **Task**: Task 4（#199 缺 `st_rdev` 的 holder 探測修復）
- **規範依據**: `openspec/specs/holder-probe-portability/spec.md`

在 `sw_core/session_manager.py` 的 `_probe_external_holder` 函式中，程式碼原先直接存取 `os.stat(real_path).st_rdev` 與 `os.stat(fd_path).st_rdev`。在非 POSIX（如 Windows）環境下，`os.stat()` 所回傳的 `stat_result` 物件不具備 POSIX 意義的 `st_rdev` 屬性。當執行期存取該屬性時，會拋出未被 `try ... except OSError:` 捕獲的 `AttributeError`，導致整個 holder 探測流程崩潰中斷。

## 2. 變更範圍與最小實作

本任務嚴格遵守邊界規範，**僅修改** `sw_core/session_manager.py` 中的 `_probe_external_holder` 函式，未變更任何其他函式：

1. **Endpoint stat 防禦性存取**：
   ```python
   try:
       target_res = os.stat(real_path)
       target_rdev = getattr(target_res, "st_rdev", 0)
   except OSError:
       target_rdev = 0
   ```
2. **FD stat 防禦性存取**：
   ```python
   if not matched and target_rdev:
       try:
           fd_res = os.stat(fd_path)
           if getattr(fd_res, "st_rdev", 0) == target_rdev:
               matched = True
       except OSError:
           pass
   ```

### 契約與同一性保證
- 當 `st_rdev` 缺失或為 `0` 時，`target_rdev` 為 `0`，`0` 不視為可做裝置同一性證據。
- 既有條件 `if not matched and target_rdev:` 保證當 `target_rdev` 為 `0` 時，絕不會進入 char device major/minor 比對邏輯，避免跨裝置因 `0 == 0` 發生誤判。
- 保留 `real_path` 與 `os.readlink(fd_path)` 的路徑相等性字串比對。
- 保留正常 POSIX 環境下有效 char-device `st_rdev`（非 0）的同一性比對。

## 3. TDD 執行與驗證記錄

### Step 1 & Step 2：Behavioral RED 驗證
新增測試檔案 `tests/test_holder_probe_portability.py`，模擬 `os.stat` 缺乏 `st_rdev` 的物件邊界，在未套用 production 修復前執行：

```bash
python3 -m pytest -q tests/test_holder_probe_portability.py
```

**輸出結果（RED）**：
```
FF......                                                                 [100%]
=================================== FAILURES ===================================
______ TestHolderProbePortability.test_missing_endpoint_st_rdev_tolerated ______
sw_core/session_manager.py:1842: in _probe_external_holder
    target_rdev = os.stat(real_path).st_rdev
E   AttributeError: 'StatResultWithoutRdev' object has no attribute 'st_rdev'

_________ TestHolderProbePortability.test_missing_fd_st_rdev_tolerated _________
sw_core/session_manager.py:1872: in _probe_external_holder
    if os.stat(fd_path).st_rdev == target_rdev:
E   AttributeError: 'StatResultWithoutRdev' object has no attribute 'st_rdev'

=========================== short test summary info ============================
FAILED tests/test_holder_probe_portability.py::TestHolderProbePortability::test_missing_endpoint_st_rdev_tolerated
FAILED tests/test_holder_probe_portability.py::TestHolderProbePortability::test_missing_fd_st_rdev_tolerated
2 failed, 6 passed in 0.20s
```

精確重現未修復版本在缺少 `st_rdev` 時引發的兩處 `AttributeError`。

### Step 3 & Step 4：GREEN 與全套測試
套用最小兩處 `getattr` 修復後：

1. **定向新測試**：
   ```bash
   python3 -m pytest -q tests/test_holder_probe_portability.py
   ```
   **結果**：`8 passed in 0.11s`。

2. **定向相關 holder/flash 測試群**：
   ```bash
   python3 -m pytest -q tests/test_holder_probe_portability.py tests/test_device_handoff.py tests/test_flash_endpoint.py tests/test_flash_guard.py tests/test_flash_probe.py tests/test_flash_pump.py tests/test_flash_service_wiring.py tests/test_flashing_state.py tests/test_uart_flash_bridge.py
   ```
   **結果**：`90 passed, 6 subtests passed in 4.98s`。

3. **全套測試**：
   ```bash
   python3 -m pytest -q tests/
   ```
   **結果**：`1692 passed, 16 skipped, 44 subtests passed in 96.53s`（相較於 root baseline `1684 passed`，正好增加 8 個新通過測試，0 失敗）。

4. **Policy Check**：
   ```bash
   python3 -m policy_check --repo .
   ```
   **結果**：`24 pass, 0 fail, 2 warn`（2 個警告為既有 R-19 與 R-22，完全符合政策標準）。

## 4. Mocked Seam 與實際 Windows 平台差異

1. **Mocked Seam**：
   - 測試中透過 mock `os.stat` 回傳缺少 `st_rdev` 屬性的物件，精確重現跨平台存取 `os.stat_result` 時屬性缺席的邊界。
   - 同時以臨時目錄模擬 `/proc` 檔案結構與 symlink，驗證無 `st_rdev` 時的路徑回退機制與正常 POSIX `st_rdev` 比對。

2. **實際 Windows 差異**：
   - 在原生 Windows（`nt`）環境下，標準庫 `os.stat()` 產生的 `stat_result` 物件不保證包含 POSIX 專屬欄位（如 `st_rdev`）。
   - Windows 原生系統不具備 Linux `/proc` 目錄。當 `_proc_root` 設為預設 `/proc` 時，`os.listdir("/proc")` 會直接觸發 `FileNotFoundError`（繼承自 `OSError`），並由既有的 `try ... except OSError:` 安全回傳 `{"pids": [], "holder": None}`。
   - 注意：無 `/proc` 並非 Windows 獨佔特徵（BSD、macOS 或精簡容器亦可能無 `/proc`）。
   - 本修復消除的是屬性存取上的語法崩潰點，使程式碼在任何缺少 `st_rdev` 的環境下均具備穩健的降級能力。

3. **實機驗證聲明**：
   - 本次修復屬 in-process 的 Python 邊界與跨平台屬性存取容錯，已由 pytest 完整覆蓋。
   - 實際外部 flasher 斷線偵測為既有的硬體與 real-hw 回歸項目（#55 / PR #66），本輪純屬 mock seam 與單元驗證，**絕不假稱或冒充真實 Windows 平台或實機驗證通過**。
