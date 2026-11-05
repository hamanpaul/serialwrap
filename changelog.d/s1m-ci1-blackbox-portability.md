---
type: fix
scope: ci
---
Rust Linux 黑箱測試補上獨立 conftest 的 Windows 原生平台防線，並以唯讀根目錄、獨立 PID 與 runtime 的 bubblewrap 沙箱執行巢狀 A/B。CI 使用與容器相同的系統 Python base 建 bundle，並保留 A/B 與 systemd 診斷，L1.5 報告失敗時輸出完整原因；INSUFFICIENT_EVIDENCE 仍判失敗，Docker 案例根因待實跑確認。
