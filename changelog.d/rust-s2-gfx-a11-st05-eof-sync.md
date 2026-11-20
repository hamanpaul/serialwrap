---
type: fix
scope: rust-s2-gfx
---

Bridge ST-05 改以有界等待 `dut_stdout_eof` META 作為同步點，再驗證後續 TX；逾時仍判 FAIL，未調整 ST-12 或資格判準。
