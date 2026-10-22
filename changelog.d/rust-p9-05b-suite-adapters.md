---
type: feat
scope: stage3
---
新增 L1.5、L2 與 L3 suite adapter，固定以單次 func-test invocation 執行每側的 case／scenario，核對實際 CLI、daemon 與部署內容身分。L2 保留呼叫者 HOME 與共用 bench lock，並驗證 plugin 回報的破壞性授權；L3 長跑預先驗證時長、baseline gate 與 observer header，另保存 observer、指定 WAL 檔與 release gate 證據。補強 Windows COM 對應、Docker digest pinning、gate rc/verdict 一致性及不足證據的 fail-closed 判定。
