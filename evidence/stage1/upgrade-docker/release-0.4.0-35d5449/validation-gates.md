# REL-040-r2 檢查摘要

執行於 `feature/release-0.4.0`，候選 HEAD `35d5449bd4afa7cd82151c04f54b0328c15f746c`。本輪只新增 Docker 升級證據與更新 release 計畫，未修改產品碼或 Python 測試。

| Gate | 結果 |
|---|---|
| `cargo test -p sw-cli --features test-fakes` | PASS，634 passed、3 ignored、0 failed |
| `cargo test --workspace` | PASS，973 passed、7 ignored、0 failed |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | PASS，exit 0 |
| `python3 scripts/rust/check_cfg_boundary.py` | PASS，exit 0 |
| `python3 -m tools.contracts check` | PASS，`findings=[]`、`ok=true` |
| `python3 tests/acceptance/regen_lock.py --check` | PASS，鎖檔、案例快照、統計檔及 L1 必要集合皆一致 |
| `python3 -m policy_check --repo .` | FAIL：24 pass、1 fail、2 warn；R-07 版本 `0.4.0` 與最新 tag `v0.3.2` 不一致。R-19、R-22 為 WARN |

R-07 在本輪起點已存在：起點 `35d5449` 的 `VERSION` 為 `0.4.0`，當時最新 tag 為 `v0.3.2`；release PR 可使用政策允許的 `release:0.4.0` exemption。

全套 `python3 -m pytest -q tests/` 與 slice gate 依 REL-040-r2 brief 明確要求未執行；本輪沒有觸及 Python 測試檔，因此沒有受影響的 Python 測試可列。當前已執行測試沒有失敗 ID；唯一 gate FAIL 是既有 policy R-07。
