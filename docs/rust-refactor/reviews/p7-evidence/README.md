# P7 雲端 session 執行證據（2026-10-27）

審查 de24500_fa847c7（Opus #9、grok #5）要求可追溯的 gate 證據。以下為本 session 在雲端容器（Linux、root、
CPython 3.12.3 跑 pytest；系統 `python3` 為 3.11.17）實跑的失敗清單（nodeid 排序去重）。

| 檔案 | 時點 | 筆數 | sha256 前 16 |
|---|---|---|---|
| `pytest-fail-start-de24500.txt` | 起點 de24500（`python3.12 -m pytest -q tests/` 分段前景） | 543 | 2ed0d942635a3c54 |
| `pytest-fail-merge-be89081.txt` | merge P5 T15～T19 後 | 509 | de1e51bc8ae12610 |
| `pytest-fail-92e0223.txt` | Task 6 後 | 505 | 97227e891849425d |
| `pytest-fail-d5b2dd6.txt` | 審查第 3 輪後（多出 `test_functest_bridge_smoke::test_bridge_smoke_sm04_until_op`：Python daemon `ATTACH_FAILED:DeviceHeldError`、本 commit 未改 `sw_core/`，同 sm01 環境類；`test_multiagent_e2e` 五 agent 案重跑 2/2 通過） | 508 | c2e8c596adb453e6 |
| `cargo-fail-start-de24500.txt` | 起點 `cargo test --workspace --features test-fakes --no-fail-fast` | 51 | f357f5d2317730d8 |

失敗主要成因（非本分支引入，皆於起點即存在或由 P5 merge 帶入、在 P5 分支同樣失敗）：
- `tests/contract_blackbox/test_bb_cli_help.py` 432 筆 ERROR：fixture 以系統 `python3`（3.11.17）比對 golden 方言 3.12.3，
  `AssertionError: Python 基線 3.11.17 ≠ golden 方言 3.12.3（證據不足）`——容器環境差異。
- `tests/test_slice21_calibration.py`／`test_slice21_gate_judge.py`／`test_slice21_gate_report.py`：P5-T22／T23 的
  `scripts/rust/slice21_gate_judge.py` 與 `docs/rust-refactor/slice21-calibration.md` 尚未交付（P5 範圍）。
- `tests/test_rust_mkbundle.py`、`test_rust_bench_tools.py` 等：需原生 Windows／打包工具。
- `tests/contract_blackbox/slice21/*` 11～12 筆：P5 預寫黑箱案例（P5 分支同樣失敗）；setup 偶發
  `ATTACH_FAILED:DeviceHeldError`（同範圍重跑兩次失敗集合不同）。
- cargo：起點 51 筆中 46 筆為 P5-T15～T19 骨架（`not implemented: slice21 T15`），merge 後只剩 sw-pal
  `file_eacces_matches_golden`、`unix_fixture_rows_match_python` 兩筆（root 執行使 EACCES 不成立的環境差異）。

每個 commit 群組 push 前的判定：失敗集合不得超出起點（多出者同範圍重跑兩次取證）；比對結果記於各 commit message。
