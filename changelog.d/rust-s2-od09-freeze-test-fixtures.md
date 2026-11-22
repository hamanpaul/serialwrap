---
type: fix
scope: realhw
---
OD-09 相關測試不再假設 `realhw/acceptance/longrun_acceptance.yaml` 的延後項仍是 `deferred`，門檻檔凍結前後都通過：`od09_env` 與 `LongrunFixture` 改由真門檻檔衍生尚未凍結的內容（`tests/fakes_od09.py` 的 `deferred_acceptance`，衍生結果以 schema 驗證）；`test_acceptance_file_schema` 與 `test_stage0_index_od09_item_ids` 改為斷言凍結前後兩種合法狀態各自的不變量。
