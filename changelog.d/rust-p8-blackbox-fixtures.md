---
type: change
scope: tests
---
P8（Slice 2.4）Task 0：新增上游介面核對（`scripts/rust/check_p8_prereqs.py` 的 MISSING／SKELETON／DIFFERS／PLANNED:Tn／LANDED:Tn 狀態與 `crates/sw-core/tests/p8_upstream_interfaces.rs` 編譯期核對），以及黑箱共用 fixture：`SwEnv`（`sw_env`／`throwaway_daemon`）、以真實 `/bin/sh` 扮演 DUT 的 `ShellTarget`、TI SBL 子集 `FakeMcu`、`FakeFlasher`、`ExternalHolder` 與連續取樣的 `HolderSampler`。
