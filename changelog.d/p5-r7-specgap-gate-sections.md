---
type: feat
scope: rust-s21
---

Slice 2.1 gate 新增 Python 基線 parity section（以完整 selector 的新集合 `s21_gate:py_parity_full` 實跑，舊集合保留並仍由全套 pytest 判定），以及 OD-55 四個 linux 能力集合各自的 section：gate 先探測能力（CAP_SYS_ADMIN、systemd user 執行期、原生 Windows），具備才以 `--caps` 實跑，否則記證據不足並標明所需能力與實跑方式，不得記 PASS。`test_cargo_toml_bypass` 改用明確參數 id，在 pytest 9.0.3 與 9.2 下 nodeid 相同。
