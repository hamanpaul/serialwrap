---
type: feat
scope: doctor
---

Rust CLI 的 `serialwrap doctor` 新增 Rust-only advisory 檢查 `profile_template_fields`：比較出貨資產與線上單一 `profiles/default.yaml` 中共同 template 的頂層欄位存在性，不比較值與巢狀欄位。check.extra 提供 `comparison_status`、`missing_fields`、`compared_templates`、`reason`；未物化、不可讀、不可解析、結構錯誤或沒有共同 template 時明確標示不可比較且 check `ok=false`，但不改整體 doctor 結果或 exit code。OD-33 只豁免一般 `devices` advisory；新 check 依執行前選定的受版控情境政策嚴格核對唯一性、可比較狀態、payload 與完整缺漏集合。p290 政策識別 `p290-template-migration-v1` 僅對管理目錄中缺欄的 `platform: prpl` template 補 `timeout_recovery_keys: [CTRL_C]`，其他 profile 保存由 p290 負責。本項保留 Python 19 項基線，僅增加 Rust 行為與 Rust-only 黑箱證據。

OD-33 升級 driver 依執行模式選擇 `migrated` 或 `materialized` 政策，並在升級完成時分別核對 p290 候選資產與實際管理目錄的 `prpl-template.timeout_recovery_keys`。

issue 289 升級 gate 以結構狀態辨識 block mapping key，忽略 plain scalar 內的 flow 字元並解碼 quoted key；安裝完成後比對實際 CLI 與 bundle 候選的 SHA-256，再對安裝副本執行內嵌資產檢查。路徑、摘要或欄位無法確認時維持 FAIL。
