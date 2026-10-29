# Slice 2.3（P7）測試／案例設計變更登記（CHG）

本檔登記 P7 分支的案例設計修正（rulings/p7.md 2026-10-29「P7 第 5 輪案例設計修正：編排者裁決接受」）。
裁決條件：每項逐列登記 CHG、附 Python 基線實測證據（命令與輸出）、案例須先在 Python 基線 PASS；任何一項若改變
對外欄位、錯誤碼或狀態語意，改列 OWNER-BATCH（不在本檔 APPLIED）。

- 實測環境：CPython 3.12.3、PyYAML 6.0.3、Linux（非 root），Python 基線 daemon `tests/contract_blackbox/bin/py-serialwrapd`。
- 探針：`scripts/rust/p7_chg_probe.py <子命令>`（Python 基線＋PTY FakeTarget，只讀外部 CLI／session list，輸出一行 JSON）。
- 「契約語意」判定：只改案例的前置／觀測方式、不改被斷言的欄位值、錯誤碼或狀態集合者為「否」。

| CHG | 案例（cases.yaml） | 計畫原文 | 變更 | 改變對外欄位／錯誤碼／狀態語意？ | Python 基線實測證據 | Python 基線 PASS | 狀態 |
|---|---|---|---|---|---|---|---|
| CHG-S23-ATTACHED-PATH-01 | `p7-quiet-reconfirm-attached-bootloader-probe-sets-stuck`、`p7-quiet-reconfirm-attach-during-quiet-zero-tx`（ATTACHED 子案）、`p7-selftest-recover-selftest-classifications`（bootloader／flood 子案） | 前置「ATTACHED（無 READY 歷史）」 | 前置改為 READY＋banner，quiet 期間 `session recover` 收斂 ATTACHED＋`PROMPT_UNAVAILABLE` 後再測 | 否：被斷言的 `BOOTLOADER_STUCK`／`BOOTLOADER` 分類、`reprobe_exhausted`、零 TX 不變，只改抵達 ATTACHED 的路徑 | 附錄 E1 | 附錄 P1 | APPLIED（Review-pending） |
| CHG-S23-REPROBE-ORDER-01 | `p7-quiet-reconfirm-attached-bootloader-probe-sets-stuck` | 先手動 recover 取分類、再等自動 reprobe 落 STUCK | 先等自動 reprobe 落 `BOOTLOADER_STUCK`，再手動 recover 取 `BOOTLOADER` 分類 | 否：兩個斷言值不變；只消除 quiet 過期瞬間兩個 probe 競爭（後到者覆寫 last_error）造成的時序依賴 | 附錄 E2 | 附錄 P1 | APPLIED（Review-pending） |
| CHG-S23-SC34-NONCE-01 | `sc34-attach-recover-last-error-field` | READY_NONCE_TIMEOUT 以初次 attach 的 nonce 逾時取得 | 改由 ATTACHED＋`CREDENTIALS_UNRESOLVED` 補帳密後 `session recover`（既有 bridge 再探）取得 ATTACHED＋`READY_NONCE_TIMEOUT` | 否：SC-34 被斷言的 `error_code:SESSION_NOT_READY`、`last_error`／`last_error_detail` 欄位與值不變 | 附錄 E3 | 附錄 P1 | APPLIED（Review-pending） |
| CHG-S23-CTRLC-FIELDS-01 | `p7-selftest-recover-recover-ready-ctrl-c` | 命令終態 `PROMPT_TIMEOUT_RECOVERED` | 依 Python 斷言 `status:"done"`、`error_code:null`、`partial:true`、`recovery_action:"CTRL_C"`、`stdout:"^C"` | 否（以 Python 外部可觀察值為準；計畫值不是此路徑的外部值，1:1 原則下不得以計畫值改 Rust） | 附錄 E4 | 附錄 P1 | APPLIED（Review-pending） |
| CHG-S23-STALL-OBS-01 | `p7-stall-transport-stall-flip` | recover 回應即帶 `TRANSPORT_STALL` | READY recover 先回 `PROMPT_TIMEOUT`（救援鍵），以輪詢 `session list` 觀察其後 reattach 列的 `last_error:"TRANSPORT_STALL"` 與 META 次數 | 否：`TRANSPORT_STALL`、`零 RX ` detail、META 一次性告警的斷言不變 | 附錄 E5 | 附錄 P1 | APPLIED（Review-pending） |
| CHG-S23-NOISE-LINE-01 | `p7-latency-noise-login-with-delay-and-noise` | 每 50 ms 一行 noise（未規定換行） | 雜訊行以 `\r\n` 起頭（printk 獨立成行） | 否：斷言仍為延遲＋雜訊下最終 READY；不換行雜訊在 Python 永不 READY，非可通過情境 | 附錄 E6（對照 E6b） | 附錄 P1 | APPLIED（Review-pending） |
| CHG-S23-L15-PATH-01 | l15 slice23 六案 | `tests/acceptance/l15/slice23/*.yaml` | 情境檔放 `func-test/scenarios/s23-*.yaml`（P2 runner `--scenarios` 只探索該目錄；runner 屬測試工具、改動須獨立 PR），比照 06 T21 的 `s21-*` | 否（檔案位置） | 不適用 | 附錄 P2 | APPLIED（Review-pending） |
| CHG-S23-REPLAY-BOUNDARY-01 | l15 `s23-reboot-recovery-no-replay`（本輪新建） | §11.3-3「Recovery／Reboot 不回放舊輸入」 | 判定窗口自重開機邊界（U-Boot banner 上 wire 後的 `rebooted` op）起算，而非自 human 輸入起算 | 否：不回放判準不變；Python 在 agent 命令進行中即透傳 human raw 輸入（非 deferred），屬既有外部行為 | 附錄 P2 說明 | 附錄 P2 | APPLIED（Review-pending） |

## 附錄：Python 基線實測輸出

（各探針輸出原樣；`observed_state_last_error` 為 45～60 s 內每 0.25 s 取樣的 `state/last_error` 計數。）

### E1：`attached-history`

shell profile、帳號後永不出現 Password prompt：45 s 內只見 `ATTACHING`／`DETACHED`（`SHELL_PROMPT_TIMEOUT`），從未 `ATTACHED`——capable profile 初次 attach 失敗回 DETACHED 重試，黑箱到不了「無 READY 歷史的 ATTACHED」。

```text
$ python3 scripts/rust/p7_chg_probe.py attached-history
{"observed_state_last_error": {"ATTACHING/None": 119, "DETACHED/SHELL_PROMPT_TIMEOUT": 2}, "probe": "attached-history", "profile": "shell"}
```

### E2：自動 reprobe 與手動 recover 的競爭

無獨立探針：quiet 過期瞬間 Python readiness tick 的自動 reprobe 與手動 `session recover` 共用 per-session probe lock（`_reprobe_probe_locks`，`sw_core/session_manager.py:905`：auto 為 non-blocking、manual attach／recover 為 blocking），後執行者的 `_probe_existing_bridge` 覆寫 `last_error`（`sw_core/session_manager.py` `_probe_existing_bridge`）。依新次序（先等 `BOOTLOADER_STUCK` 再手動 recover）的案例於 Python 基線 PASS 即為證據（附錄 P1 第一列）。

### E3：`sc34-initial-nonce`

登入成功但 nonce 永不回：初次 attach 的 nonce 逾時落 `DETACHED`＋`READY_NONCE_TIMEOUT`（不經 ATTACHED），之後重試；`session attach` 回 `ok:true`（重新進入 ATTACHING）——無法以初次 attach 取得 SC-34 的 ATTACHED＋`READY_NONCE_TIMEOUT` 失敗回應。

```text
$ python3 scripts/rust/p7_chg_probe.py sc34-initial-nonce
{"attach": {"last_error": null, "ok": true, "rc": 0, "session_state": "ATTACHING"}, "observed_state_last_error": {"ATTACHING/None": 56, "DETACHED/READY_NONCE_TIMEOUT": 26}, "probe": "sc34-initial-nonce"}
```

### E4：`ctrl-c`

逾時救援的命令紀錄：`status:done`、`error_code:null`、`partial:true`、`recovery_action:CTRL_C`、`stdout:^C`，session 仍 READY；無 `PROMPT_TIMEOUT_RECOVERED`。

```text
$ python3 scripts/rust/p7_chg_probe.py ctrl-c
{"command": {"error_code": null, "partial": true, "recovery_action": "CTRL_C", "status": "done", "stdout": "^C"}, "probe": "ctrl-c", "state": "READY"}
```

### E5：`stall-order`

READY 後 RX 凍結 35 s：`session recover` 先回 `PROMPT_TIMEOUT`（`partial:true`、`recovery_action:NONE`），約 10.6 s 後 reattach 列為 `DETACHED`＋`TRANSPORT_STALL`（detail 以 `零 RX ` 起頭）。

```text
$ python3 scripts/rust/p7_chg_probe.py stall-order
{"first_stall_row": {"after_s": 10.6, "detail_prefix": "零 RX ", "state": "DETACHED"}, "probe": "stall-order", "recover": {"error_code": "PROMPT_TIMEOUT", "ok": false, "partial": true, "recovery_action": "NONE"}, "recover_rc": 2, "rows_before_stall": ["ATTACHING/None", "DETACHED/TRANSPORT_STALL"]}
```

### E6：`noise-inline`

雜訊不換行、直接接在 `Login: ` 之後（500 ms 間隔、無回應延遲）：60 s 內從未 READY，只見 `ATTACHING`／`DETACHED`＋`LOGIN_PROMPT_TIMEOUT`。

```text
$ python3 scripts/rust/p7_chg_probe.py noise-inline
{"interval_ms": 500, "observed_state_last_error": {"ATTACHING/None": 155, "DETACHED/LOGIN_PROMPT_TIMEOUT": 8}, "pattern": "[  12.345678] kernel: usb 1-1: noise", "probe": "noise-inline"}
```

### E6b：`noise-own-line`

對照：同條件但雜訊行以 `\r\n` 起頭 → READY。

```text
$ python3 scripts/rust/p7_chg_probe.py noise-own-line
{"interval_ms": 500, "observed_state_last_error": {"ATTACHING/None": 41, "READY/None": 120}, "pattern": "\r\n[  12.345678] kernel: usb 1-1: noise", "probe": "noise-own-line"}
```

### P1：受影響案例於 Python 基線（`git` HEAD 起點 `b93849e` 之上、無 `SERIALWRAP_DAEMON_EXE`）

```text
$ python3 -m pytest -v tests/contract_blackbox/test_p7_quiet_reconfirm.py::test_attached_bootloader_probe_sets_stuck tests/contract_blackbox/test_p7_quiet_reconfirm.py::test_attach_during_quiet_zero_tx tests/contract_blackbox/test_p7_selftest_recover.py tests/contract_blackbox/test_p7_stall.py tests/contract_blackbox/test_p7_latency_noise.py
tests/contract_blackbox/test_p7_quiet_reconfirm.py::test_attached_bootloader_probe_sets_stuck PASSED [  7%]
tests/contract_blackbox/test_p7_quiet_reconfirm.py::test_attach_during_quiet_zero_tx PASSED [ 14%]
tests/contract_blackbox/test_p7_selftest_recover.py::test_selftest_classifications[ready] PASSED [ 21%]
tests/contract_blackbox/test_p7_selftest_recover.py::test_selftest_classifications[bootloader] PASSED [ 28%]
tests/contract_blackbox/test_p7_selftest_recover.py::test_selftest_classifications[flood] PASSED [ 35%]
tests/contract_blackbox/test_p7_selftest_recover.py::test_selftest_classifications[passthrough_attached] PASSED [ 42%]
tests/contract_blackbox/test_p7_selftest_recover.py::test_selftest_classifications[passthrough_ready] PASSED [ 50%]
tests/contract_blackbox/test_p7_selftest_recover.py::test_selftest_classifications[released] PASSED [ 57%]
tests/contract_blackbox/test_p7_selftest_recover.py::test_attach_recover_last_error_field PASSED [ 64%]
tests/contract_blackbox/test_p7_selftest_recover.py::test_recover_force_timeout_attached PASSED [ 71%]
tests/contract_blackbox/test_p7_selftest_recover.py::test_recover_force_on_ready_runs_keys_first PASSED [ 78%]
tests/contract_blackbox/test_p7_selftest_recover.py::test_recover_ready_ctrl_c PASSED [ 85%]
tests/contract_blackbox/test_p7_stall.py::test_transport_stall_flip PASSED [ 92%]
tests/contract_blackbox/test_p7_latency_noise.py::test_login_with_delay_and_noise PASSED [100%]
======================== 14 passed in 687.69s (0:11:27) ========================
```

### P2：l15 slice23 六案（bridge 模式，Python 基線＋Rust debug daemon，連跑兩次）

```text
$ SERIALWRAP_GATE=1 SERIALWRAP_L15_SELFTEST_REPORT=… SERIALWRAP_L15_PY_DAEMON=tests/contract_blackbox/bin/py-serialwrapd \
  SERIALWRAP_L15_RUST_DAEMON=target/debug/serialwrapd python3 -m pytest -q tests/acceptance/l15/test_slice23_l15.py -k s23
FAILED tests/acceptance/l15/test_slice23_l15.py::test_s23_scenario[rust-SERIALWRAP_L15_RUST_DAEMON-s23-interleave-login-agent-human]
FAILED tests/acceptance/l15/test_slice23_l15.py::test_s23_scenario[rust-SERIALWRAP_L15_RUST_DAEMON-s23-reboot-recovery-no-replay]
2 failed, 11 passed, 7 deselected in 328.23s (0:05:28)
```

Python 六案兩次皆 PASS。Rust 兩案 FAIL 均為步驟 1 `attach_console` 回 `METHOD_NOT_FOUND`（`session.console_attach` 屬 P6 human console，BLOCKED-UPSTREAM P7-BU-7）。
第一次執行時 `s23-reboot-recovery-no-replay` 的 Python 列 FAIL：Python 基線在 agent `reboot` 命令進行中把 human raw 輸入**直接透傳**上 wire（seq 24，早於 U-Boot banner），並非重開機後回放；情境原本「重開機前輸入須被 deferred、整段不得出現」的前提與 Python 不符，改為「重開機邊界（`rebooted` op）之後不得再出現重開機前的輸入」（§11.3-3 原意），未放寬不回放判準。
