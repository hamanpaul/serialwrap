---
type: feat
scope: test-infra
---
自 `FakeTarget` 抽離 sans-IO 的 DUT 行為模型 `DutModel` 與 `ScenarioPlan`，支援開機 banner、命令回應、登入狀態機、動態提示符、串行延遲、雜訊以及故障注入。新增 `uart_stdio_bridge.dut` 模組，提供標準串流模擬器（`run_stdio`）與純二進位回顯模式（`run_echo`），並實作基於目錄檔案的控制通道（`ControlFileChannel` 與 `DutControlClient`）。改寫 `func-test/lib/fake_target.py` 內部委派至 `DutModel`，並新增安全匯出模組 `target_config.py`，保持位元組層級完全等價。
