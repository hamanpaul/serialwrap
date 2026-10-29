# B-01 擁有者程序：四平台實機登入（Slice 2.3，P7 Task 19）

> 依據：`docs/rust-refactor/plans/08-slice23-login-events.md` BLOCKED-OWNER 總表 B-01、規格 §11.6。
> 對象：bcm、prpl、op3 shell、smart4R console unlock 四平台，各至少**一次登入成功**與**一次帳密錯誤**。
> 證據到位前，`scripts/rust/p7_gate_report.py` 的 B-01 一律為 BLOCKED-OWNER；證據檔須以路徑＋sha256 登入
> `results.json` 的 `owner_evidence["B-01"]`，經 `p7_gate_report.py verify` 核對後才可為 PASS。

## 0. 前置

1. 受測 artifact：與本次 gate 相同 commit 建置的 Rust bundle（`scripts/rust/mkbundle.sh`），記錄
   `git rev-parse HEAD` 與 `serialwrap`／`serialwrapd` 的 sha256（寫入 `b01-identity.txt`）。
2. 隔離：停止 production daemon（`serialwrap service stop`，或於另一組 `SERIALWRAP_STATE_DIR`／`_RUN_DIR`／
   `_WAL_DIR`／`_CONFIG_DIR` 下起 throwaway daemon，OD-11）。**不得**與人類 minicom 同時開同一 tty（two-reader）。
3. 每平台準備兩份帳密檔：正確（`<plat>.env`）與錯誤（`<plat>-bad.env`，鍵名相同、值錯誤），profile 的 `env_file`
   指向正確檔；smart4R 另需 `console_unlock_env` 對應的解鎖變數。
4. 以下以 `SW=<bundle>/bin/serialwrap` 代表 Rust CLI，`$COM` 為該平台在 `session list` 的 COM。

## 1. 每平台：登入成功

```bash
$SW daemon start
$SW session attach --selector $COM            | tee b01-<plat>-attach-ok.json
$SW session list                              | tee b01-<plat>-list-ok.json
$SW cmd submit --selector $COM --source agent:b01 --cmd 'echo B01_OK' | tee b01-<plat>-submit.json
$SW cmd status --cmd-id <上一步 cmd_id>        | tee b01-<plat>-status.json
```

判定欄位：

| 檔案 | 欄位 | 期望 |
|---|---|---|
| `b01-<plat>-attach-ok.json` | `ok` | `true` |
| `b01-<plat>-list-ok.json` | `sessions[com=$COM].state` | `READY` |
| `b01-<plat>-list-ok.json` | `sessions[com=$COM].last_error` | `null` |
| `b01-<plat>-status.json` | `command.status`、`command.stdout` | `done`、含 `B01_OK` |
| WAL `raw.wal.ndjson` | `source=system:secret` 的 TX | **不存在**（secret 不落 WAL） |

## 2. 每平台：帳密錯誤

```bash
$SW device release --selector $COM
# 將 profile 的 env_file 改指向 <plat>-bad.env（或刪除正確檔中的密碼鍵以製造 CREDENTIALS_UNRESOLVED）
$SW device attach --selector $COM
$SW session attach --selector $COM            | tee b01-<plat>-attach-bad.json
$SW session list                              | tee b01-<plat>-list-bad.json
$SW cmd submit --selector $COM --source agent:b01 --cmd 'echo NO' | tee b01-<plat>-submit-bad.json
```

判定欄位：

| 檔案 | 欄位 | 期望 |
|---|---|---|
| `b01-<plat>-attach-bad.json` | `ok`／`error_code`／`last_error` | `false`／`SESSION_NOT_READY`／帳密未解析時 `CREDENTIALS_UNRESOLVED`（SC-34：`error_code` 與 `last_error` 分離） |
| `b01-<plat>-list-bad.json` | `sessions[com=$COM].state` | `ATTACHED`（帳密未解析為終態，不自動重探） |
| `b01-<plat>-submit-bad.json` | `ok`／`error_code` | `false`／`SESSION_NOT_READY` |
| WAL | 帳密錯誤後的 `system:*` TX | 無空帳密 TX（不得對 login prompt 送空帳號或空密碼，§11.3-1） |

帳密值錯誤（非未解析）時，期望 `last_error` 為登入失敗類（依平台 FSM：`LOGIN_PROMPT_TIMEOUT`／`LOGIN_FAILED` 等），
以 Python 基線同板同檔的輸出為準；兩者不同即記為 1:1 差異回報，不得自行判 PASS。

## 3. TestPilot 回歸（同一 bundle）

`testbed.yaml` 的 `serialwrap_exe` 指向 Rust bundle 的 `serialwrap`，逐一執行 B-02／B-04 中涵蓋該平台的案例：

```bash
testpilot run serialwrap_regression --case f10-creds-fixed-then-ready
testpilot run serialwrap_regression --case f10-unresolved-creds-terminal
testpilot run serialwrap_regression --case f9-brcm-spaced-prompt-reboot-login   # bcm
```

## 4. 證據登入

1. 將上述 `b01-*` 檔、WAL 摘錄、`b01-identity.txt` 與 TestPilot 報告收進同一目錄，計算各檔 sha256。
2. 寫入 gate run 的 `results.json`：`"owner_evidence": {"B-01": [{"path": "<相對 run 目錄>", "sha256": "<64 hex>"}, …]}`。
3. 執行 `python3 scripts/rust/p7_gate_report.py build --results-dir <run> --out <md>` 與
   `verify --results-dir <run> --md <md>`；verify 須回空清單。缺任一平台的成功或錯誤紀錄 → 不得登入 B-01。
