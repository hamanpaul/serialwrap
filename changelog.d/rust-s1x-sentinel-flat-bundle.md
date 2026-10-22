---
type: change
scope: rust-cli
---

Stage 1B 20groups 委派 sentinel bundle 改為扁平佈局（PENDING-OD-S1X-20GROUPS-LAYOUT 選項 (i)）：`scripts/rust/mkbundle.sh --delegate-sentinel` 的組成與 dev bundle 相同（`bin/serialwrap`、`bin/serialwrapd`、`py/sw_core`、`py/serialwrapd.py`、`SHA256SUMS`、`release-manifest.json`），僅將 `python_cli_delegate.entry` 改為 P4 sentinel（非委派守門：任何漏網委派都寫入 `DELEGATED` marker 並 exit 97）；stage 仍為 `rust-1b-dev`。組裝程序抽為 `assemble_bundle` 由 dev 與 sentinel 共用。`SHA256SUMS` 排除組裝期所有權標記 `.mkbundle-owner`。

測試端：`tests/contract_blackbox/test_cli_mgmt_doctor.py::test_doctor_ab_version_pairing_in_bundle` 標 `pidns_rig`，host 上其他 serialwrapd 造成的 pid 計數不穩由隔離 namespace 排除（測試斷言不變）。plan 05 Task 16 Step 4 的 20groups 命令補 `SERIALWRAP_DAEMON_EXE` 並於執行前移走既有 ab-suite 輸出。整段重跑證據尚未取得（見 `owner-decisions-addenda.md` CHG-S1X-OD16-OPEN-01 (e)）。
