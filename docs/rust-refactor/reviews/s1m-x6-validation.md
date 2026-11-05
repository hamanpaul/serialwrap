# S1M-x6d：接線與整體驗收

分支 `feature/s1m-x6`。本輪起點 `d929cac4`。產品修正已在前幾輪落地，本輪只補黑箱路徑斷言、凍結鎖 add-only 與本紀錄。未改 Python daemon、契約或 golden。

## 根因

`42b3bc13` 的 `run_supervision_switch` 以 passwd 家目錄（`current_user_home()`）優先於環境變數 `HOME`，systemd user unit 寫到真實家目錄。同一時期 unit 路徑在 `XDG_CONFIG_HOME` 有值時走 XDG，缺省才退回 passwd。Python 基線的 user unit 固定為 `HOME/.config/systemd/user/serialwrap.service`，一般 config／state 仍走 XDG 與 `SERIALWRAP_*` 覆寫。

修正提交：`068c8e54`（監管切換採用 `HOME`）、`6f67a0cb`（寫入、快照、還原與 hold 固定 `HOME/.config`）、`e56c5475`（真實 SystemdCtl hold 根目錄）。黑箱 session 防線在 `78cbc7a3`。

## 入口與命令

沙箱為 `target/s1mx6d-sbox`：`bwrap --ro-bind / /` 只綁定本 worktree 可寫，不把 `~/.config` 蓋成 tmpfs，因此真實 unit 可讀、寫入會得到 `Read-only file system`。PATH 排除 pipx，並設定 `CARGO_NET_OFFLINE`。未呼叫 production daemon，未開真實序列埠。

修前紅燈沿用同一 nodeid 的既有紀錄（`~/b-log/rust-refactor/exec/s1m/x5/round-2.log` 第 45234–45312 行，副本 `target/s1mx6d-red-reused.log`）。失敗在 `assert result.returncode == 0`，早於本輪新增的路徑斷言：

`寫入 user unit 失敗：… /home/paul_chen/.config/systemd/user/serialwrap.service … Read-only file system`

候選 bundle 由目前產品碼重新建置，未沿用舊執行檔：

```bash
bash target/s1mx6d-sbox scripts/rust/mkbundle.sh --dev --out "$PWD/target/s1m-x6-bundle"
```

CLI sha256 `1aaa9697a9469ba623bef9a35ce8d22f160a52fb8a4cc8c7bcdd8038d6d0befc`，manifest `git_revision` 為 `d929cac4`。

綠燈：

```bash
bash target/s1mx6d-sbox env \
  SERIALWRAP_AB_LABEL=rust-cli-python-daemon \
  SERIALWRAP_CLI_EXE="$PWD/target/s1m-x6-bundle/bin/serialwrap" \
  SW_BUNDLE="$PWD/target/s1m-x6-bundle" \
  SW_RESOURCE_REPORT="$PWD/target/s1m-x6-blackbox.json" \
  python3 -m pytest -q -rs --confcutdir=tests/contract_blackbox \
  tests/contract_blackbox/test_cli_mgmt_supervision_switch.py::test_supervision_set_rust_only_switch
```

## 驗收結果

| 檢查 | 結果 |
|---|---|
| 指定 Rust 黑箱 | `1 passed`、0 skipped；L1 `exit_code=0`、`finished=true` |
| `cargo test --workspace` | 1034 passed、0 failed、7 ignored；exit 0 |
| `cargo test --workspace --features test-fakes` | 1034 passed、0 failed、7 ignored；exit 0 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0 |
| cfg 邊界、依賴方向、`tools.contracts check` | exit 0；契約 `ok: true`、`findings: []` |
| `tests/test_resource_markers.py` | 9 passed |
| `regen_lock.py --check --base 42b3bc13` | exit 0 |

真實 user unit 在沙箱測試前後的存在性、mtime_ns、mode 與 sha256 相同（`350f9a251dd94b3092c1af287fad3d68ba351bf06b2f4919dee1419584517b97`）。本輪未跑全套 pytest、Docker 或 Stage 出口 driver。

凍結鎖只經 `--l1-add-only`，linux、selector 不變、移除 0。新增均為 `s1mx6` 回歸：`s1a_cargo` 與 `s1b_cargo` 各 5 筆；`s1a_blackbox` 與 `s1b_20groups` 各 7 筆 harness 防線（後者的既有 selector 會收集到這些案例）。selector 還收集到其他子項尚未鎖定的 nodeid，本輪未登記。
