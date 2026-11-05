---
type: fix
scope: ci
---
Rust Linux 黑箱測試補上獨立 conftest 的 Windows 原生平台防線，並以唯讀根目錄、獨立 PID 與 runtime 的 bubblewrap 沙箱執行巢狀 A/B。CI 使用與容器相同的系統 Python base 建 bundle，並保留 A/B 與 systemd 診斷，L1.5 報告失敗時輸出完整原因；INSUFFICIENT_EVIDENCE 仍判失敗，Docker 案例根因待實跑確認。

沙箱自測在尚無 `target/` 的乾淨 checkout 也會建立成果目錄；明確要求 Docker 但缺少 Unix socket 時，回傳證據不足 exit 3 與診斷，不執行後續命令。

CI 命令順序自測同步要求沙箱入口與 resource／JUnit 報告，並以負向測試拒絕改回直接執行黑箱的工作流程。
