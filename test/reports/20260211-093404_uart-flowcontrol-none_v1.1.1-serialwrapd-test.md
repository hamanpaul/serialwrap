# uart-flowcontrol-none 測試報告

- 時間(UTC): 2026-02-11 09:34:04
- 版本: v1.1.1-serialwrapd-test
- 範圍:
  - 修正預設 UART flow control 由 `rtscts` 改為 `none`
  - 驗證 PermissionError 已排除後，session 可進 READY

## 變更
- `sw_core/config.py`
  - `UartProfile.flow_control` 預設值改為 `none`
- `profiles/default.yaml`
  - `prpl-template.uart.flow_control` 改為 `none`
- `docs/serialwrap-spec.md`
  - UART 範例同步改為 `flow_control: none`

## 驗證
### 1) 直連 UART 可讀
```bash
stty -F /dev/ttyUSB0 115200 cs8 -cstopb -parenb -crtscts -ixon -ixoff raw -echo min 0 time 5
printf "\\r\\n" > /dev/ttyUSB0
cat /dev/ttyUSB0 | head -c 400 | xxd -g 1
```
結果: 可讀到 BusyBox/OpenWrt 開機 banner。

### 2) 回歸測試
```bash
PYTHONPATH=/home/paul_chen/arc_prj/ser-dep /usr/bin/python3 -m unittest -v \
  /home/paul_chen/arc_prj/ser-dep/tests/test_config_profiles.py \
  /home/paul_chen/arc_prj/ser-dep/tests/test_multiagent_e2e.py
```
結果: `Ran 3 tests ... OK`

### 3) daemon attach 狀態
觀察: `session list` 進入 `state=READY`，`last_error=null`，有 `vtty=/dev/pts/*`。

## 結論
- 目前阻塞主因不是權限，而是錯誤流控設定；改為 `none` 後可正常 attach。
