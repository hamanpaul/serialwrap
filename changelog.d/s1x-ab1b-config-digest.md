---
type: fix
scope: test-infra
---

Stage 1 A/B 黑箱身分的 `config_digest`（G4-ab1b-config-digest）改為反映各夾具實際建立的設定內容：`isolated_env` 登記的隔離根於每個案例 call 階段結束時，以 `tests/contract_blackbox/config_digest.py` 記錄 YAML（config.yaml、profiles）內容雜湊與正規化後的 env 摘要（隔離根路徑為 `<ROOT>`，認證類鍵只記存在與否），逐案例、逐夾具保存於 `SW_AB_CONFIGLOG` 紀錄，A／B 兩輪身分各自引用彙整後的摘要。`scripts/rust/ab_blackbox_1b.py` 不再以鎖檔與 applicability 檔的雜湊充當 `config_digest`，該雜湊改存獨立欄位 `lock_applicability_sha256`；設定紀錄缺失或為空不以例外中止，而記入 summary 的 `config_errors` 並判 fail。`scripts/rust/ab_blackbox.sh`（1A）同步改用設定紀錄，原以 bundle 目錄雜湊充當的 `config_digest` 改存獨立欄位 `bundle_tree_sha256`。比較器（`ab_compare.py`）的正規化與比較規則不變。
