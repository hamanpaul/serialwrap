---
type: fix
scope: stage3
---

修正 T3 A/B manifest 第 2 輪凍結缺口：以 tool_revision 的原始 Git blob 綁定 repo 內凍結輸入，保留 Stage 3 原呼叫介面並讓可信 base 成為額外檢查；補強 1A／1B Python 基線元件與 Docker image 驗證；Windows L1.5 改由注入的 trusted_pair_map 綁定核准 COM pair，未提供時回報 BLOCKED-OWNER；恢復 T1 `--check` 的 schema、seed 短路及診斷順序。
