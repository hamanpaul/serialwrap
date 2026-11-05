---
type: feat
scope: rust-stage1
---

Stage 1 出口 gate 讀取 Python daemon 基線缺陷登記（OD-75）。符合平台、狀態與失敗訊息特徵的案例維持證據 FAIL，另列「基線已知缺陷（不擋）」；未登記、只有 B 輪失敗、特徵不符，以及 A/B 兩輪都失敗尚未分類者仍擋出口。`l15_exit_driver.py --rejudge` 可用既有證據重判，不重跑測試。OD-30～OD-32 的擁有者延後列行為不變。初始登記為空。
