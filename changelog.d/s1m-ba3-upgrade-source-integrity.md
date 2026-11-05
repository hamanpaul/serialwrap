---
type: fix
scope: release
---

Docker 升級驗證的 PyYAML wheelhouse 明確鎖定容器的 CPython 3.12／cp312／manylinux x86_64，映像與容器固定 linux/amd64，避免主機 Python 版本影響離線安裝。正式 bundle 與升級驗證入口拒絕未提交的來源變更，並在結束時再次核對；升級報告新增 driver 版本與容器內實際載入的兩支 driver 雜湊，區分 bundle 來源與驗證工具來源。開發 bundle 與 dry-run 保留開發用途，判定及必要檢查集合不變。
