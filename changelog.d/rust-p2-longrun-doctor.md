---
type: fix
scope: test-infra
---
修正 OD-15 長跑 Docker Compose 未將 `dialout` 補充群組授予主機 UID 執行者，解除 realhw `serialwrap doctor` preflight 阻擋；補裝 realhw metadata 所需的 `git`，保存隔離環境下完整 doctor JSON 與 stderr，並讓 observer 以固定期限排程取樣，避免 RPC 耗時造成長跑樣本數不足。
