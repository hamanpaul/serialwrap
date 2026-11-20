---
type: fix
scope: rust-s2-gpz
---
修正七項 gate 前獨立審查 Medium：ConsoleHub 的 com0com 開埠命令綁定 close-all 世代；S2/P7 前置流程分開驗證 L1.5 Python 與 Rust daemon artifact、合併先前切片 verdict 時保留最差結果、MCU parity 缺少 observation 時 fail closed、slice24 gate prereq 失敗即記錄並終止，以及 gate build 明確使用本輪 artifact 目錄。另將 `coverage_map.yaml` 中錯誤碼映射改為實際觸發與斷言的節點，並為 `daemon_stop_during_force_recover_with_client_gone` 的假 DUT 加入可控暫停，避免過快回覆掩蓋 client disconnect 順序。
