---
type: feat
scope: test-infra
---
新增 OD-15 Docker L1.5 驗證環境：兩容器假 UART PTY 拓樸、systemd PID 1 驗證、長跑與監管 runner，以及會拒收 Docker／throwaway 假冒真板證據的層級標記與索引檢查。Docker 證據保留三個 image digest 與套件樹摘要；48 h／>72 h runner 僅在 P1 與 P1-OC 門檻通過後啟動。
