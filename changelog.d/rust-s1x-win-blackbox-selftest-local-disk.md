---
type: fix
scope: rust-cli
---

Windows 黑箱 runner 的 bridge 自測改以本機 scratch 作為 `--out`（與 stdout／stderr），不再直接寫入 OutDir（`\\wsl.localhost` 網路路徑）。ST-04 會對 `--out` 持續寫入 50 MB stderr 證據，網路路徑上曾出現 `dut.stderr.log` 截斷（48431104 bytes）與 `HARNESS_EVIDENCE_WRITE_FAILED`（bridge rc=3），連鎖使 `windows-com` 判 FAIL、B 輪缺 COM pair、`test_autostart_l15_tx_once` SKIP。行程結束後整份回寫 `OutDir\windows-com\bridge-selftest`（`selftest-report.json` 與 `work/`），並以 SHA256 逐檔比對；回寫不一致只會使證據不足，不會放行。資格判定（`is_qualified`、`verdict`、`tree_sha256`）維持原樣，`windows-com.json` 新增 `selftest_paths` 記錄本機與 OutDir 兩個路徑。新增 `tests/test_rust_win_scripts.py` 靜態斷言，鎖定 `--out` 不在 OutputRoot 或網路路徑之下、回寫步驟存在且位於行程結束之後。

補充（S1X 出口收尾）：selftest 回寫最多 3 次有界重試；stdout／stderr 兩份 log 併入 SHA256 逐檔比對，不一致即證據不足，不放行。
