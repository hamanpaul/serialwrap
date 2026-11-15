# OD-29 (4) 擁有者 runbook：`cap_interactive_desktop__exec_or_wait_ctrl_c`

本測試需要 Windows **互動 console**（stdin 與 stdout 都必須是終端機；`win_ctrl::interactive_console_attached()`），在 WSL interop、排程工作或任何導向輸出的非互動環境下會以「證據不足，非 PASS」失敗。依 OD-29 (4)，由擁有者在 Windows Terminal 執行一次；agent 不得在非互動環境代跑。

## 首跑結果（2026-10-27，FAIL，已保存）

擁有者於 2026-10-27 21:39（Windows Terminal PowerShell 7.6.9，host `9900X`）以舊版 exe（sha256 `74f26463e220d658f6fe33a57723eb5874d6b30562176348cbf6347eaa53a95b`）執行：`exit_code=101`，**FAIL**。父行程 panic 於 `crates/sw-pal/tests/exec_delegate.rs:139`（`event=0 left: Some(0) right: Some(3221225786)`），子行程輸出為 `... ignored, cap:interactive_desktop`、`0 passed; 1 ignored`。

根因（測試程式缺陷）：子行程重入參數缺 `--ignored`，子 libtest 把 #[ignore] 測試直接略過並以 0 結束，父行程 2 s 後才對已結束的行程送事件；產品程式碼未被這支測試驗到。另查證 Win32 行為：`CREATE_NEW_PROCESS_GROUP` 會代子行程隱式呼叫 `SetConsoleCtrlHandler(NULL, TRUE)`（停用 Ctrl+C 並由子孫繼承），即使補上 `--ignored`，送往子 process group 的 `CTRL_C_EVENT` 也不會被孫行程收到。修正與 oracle 變更登記見 `owner-decisions-addenda.md` 的 `CHG-S1X-OD29-CTRLC-REENTRY-01`。首跑原始檔（`ctrl_c-result.json`、`ctrl_c-transcript.txt`、舊 exe）已保存於 `~/b-log/rust-refactor/od29/ctrl_c-owner-run1-fail/`（`SHA256SUMS` 列完整 sha256）。

## 第 2 次執行結果（2026-10-28，PASS，已實證；適用於當時的測試原始碼）

擁有者於 2026-10-28 07:12（台北；`finished_utc` `2026-10-27T23:12:01.7150514Z`，Windows Terminal PowerShell 7.6.9，host `9900X`）以下表 exe（sha256 `3607686f1390e88f15fc00c5e86ff19b85987f9e7d64c208552f2665f18efd76`，與 `d0b8ad7` 建置一致）執行：`exit_code=0`，**PASS**。

- Ctrl+C 圈：子 pid 4488 ready（`interactive=true ctrl_c_reenabled=true`）→ `GenerateConsoleCtrlEvent(0, 4488) ok=true` → `code=c000013a`。
- Ctrl+Break 圈：子 pid 39248 ready → `GenerateConsoleCtrlEvent(1, 39248) ok=true` → `code=c000013a`（依下方「判定範圍」只證明行程樹會被終止）。
- `ctrl_c-parent.txt` 最後一列 `PASS`；transcript 未收錄原生 exe 輸出（PowerShell transcript 不擷取原生程式的 console 輸出），以父／子診斷檔取證。

原始檔逐字複製於 repo `evidence/stage1/owner/od29-ctrlc/`（來源 `~/b-log/rust-refactor/evidence/od29-ctrlc-20261028T071201/`，`SHA256SUMS` 同目錄）：

| 檔案 | sha256 |
|---|---|
| `ctrl_c-result.json` | `c7905571da2b16ac4ff6cebc38bcb17d8a2d27f48bbb98d7882236467e13d061` |
| `ctrl_c-parent.txt` | `41fabe3954388efdff6aee2af88e2843dab35784821b4cf4fb2c5f39e5e9ff33` |
| `ctrl_c-child-ctrl_c.txt` | `00024be237b18ded9f1d13a54295b86eaf477a0c2b625078b8fd9fe4474cb38e` |
| `ctrl_c-child-ctrl_break.txt` | `307f3a189f124aaa7453ec4a77f0c17ccc24df1c2036b863a21632465fa16e0d` |
| `ctrl_c-transcript.txt` | `a509edfba75708ef0a9aac0a5960a42371249c67dd84e51f77d2a63fd8f11309` |

`ctrl_c-result.json` 內記錄的三個診斷檔 sha256 與上表逐一相符。

## 受測 binary（第 2 次執行用）

| 項目 | 值 |
|---|---|
| 測試原始碼 | `crates/sw-pal/tests/exec_delegate.rs`（sha256 `beefcdda877851c10d598eecaf0ef5b94c29241e3baeb2cb0fd79247efccefac`；commit 見 addenda `CHG-S1X-OD29-CTRLC-REENTRY-01`） |
| 建置 | WSL：`cargo test --no-run --target x86_64-pc-windows-gnu -p sw-pal --features test-fakes --test exec_delegate` |
| 放置路徑 | `%LOCALAPPDATA%\Temp\sw-od29\exec_delegate.exe`（舊結果檔已移到上述保存位置，目錄內只剩新 exe） |
| sha256 | `3607686f1390e88f15fc00c5e86ff19b85987f9e7d64c208552f2665f18efd76` |
| 預檢 | `--ignored --list --exact cap_interactive_desktop__exec_or_wait_ctrl_c` 列出恰好 1 個測試 |

測試只在本行程與自身重入的子行程之間送 `CTRL_C_EVENT`／`CTRL_BREAK_EVENT`（子行程以 `CREATE_NEW_PROCESS_GROUP` 建立、委派前以 `SetConsoleCtrlHandler(NULL, FALSE)` 重新啟用 Ctrl+C，事件只送該 process group），孫行程為 `ping -n 30 127.0.0.1`；不碰正式安裝、不開任何 COM。子行程進入子分支後先寫 ready 標記，父行程等到標記才送事件，標記未出現即 FAIL（防止再次空跑）。

診斷檔（環境變數 `SW_PAL_CTRLC_DIAG_DIR` 指定的目錄，未設時為 `%TEMP%`），transcript 未收錄原生 exe 輸出時仍可取證：

| 檔案 | 寫入者 | 內容 |
|---|---|---|
| `ctrl_c-parent.txt` | 父行程 | 父 pid、是否互動 console、每個事件的子 pid、ready 標記、`GenerateConsoleCtrlEvent` 回傳、子行程 exit code；全部通過時最後一列 `PASS` |
| `ctrl_c-child-ctrl_c.txt`、`ctrl_c-child-ctrl_break.txt` | 子行程 | 子 pid、是否互動 console、Ctrl+C 重新啟用結果（即 ready 標記） |

agent 診斷實驗（**非 OD-29 證據**，只用來確認觸發機制，2026-10-28）：以 `Start-Process -WindowStyle Hidden` 在本 host 開新 console 執行同一顆 exe，`exit=0`，兩圈皆 `code=c000013a`、`PASS`；反證版（子行程不重新啟用 Ctrl+C）Ctrl+C 圈 `GenerateConsoleCtrlEvent ok=true` 但 10 s 內未結束、`exit=101`。兩組診斷檔與 exe 保存於 `~/b-log/rust-refactor/od29/ctrl_c-agentdiag-261028/{pos,neg}/`。依 OD-29 (4)，正式證據仍須由擁有者在 Windows Terminal 互動執行。

## 擁有者執行的單一指令

在 **Windows Terminal 的 PowerShell 分頁**（不要從 WSL、不要導向或管線輸出）貼上並執行：

```powershell
$d="$env:LOCALAPPDATA\Temp\sw-od29"; $env:SW_PAL_CTRLC_DIAG_DIR=$d; Start-Transcript -Path "$d\ctrl_c-transcript.txt" -Force | Out-Null; & "$d\exec_delegate.exe" --ignored --exact cap_interactive_desktop__exec_or_wait_ctrl_c --nocapture; $rc=$LASTEXITCODE; Stop-Transcript | Out-Null; $h={param($f) if (Test-Path $f) {(Get-FileHash $f -Algorithm SHA256).Hash.ToLower()} else {$null}}; [ordered]@{test='cap_interactive_desktop__exec_or_wait_ctrl_c'; exit_code=$rc; exe_sha256=(& $h "$d\exec_delegate.exe"); parent_diag_sha256=(& $h "$d\ctrl_c-parent.txt"); child_ctrl_c_sha256=(& $h "$d\ctrl_c-child-ctrl_c.txt"); child_ctrl_break_sha256=(& $h "$d\ctrl_c-child-ctrl_break.txt"); finished_utc=(Get-Date).ToUniversalTime().ToString('o'); host=$env:COMPUTERNAME} | ConvertTo-Json | Set-Content -Encoding utf8 "$d\ctrl_c-result.json"; "exit_code=$rc"; Get-Content "$d\ctrl_c-parent.txt"
```

預期：畫面出現 `test cap_interactive_desktop__exec_or_wait_ctrl_c ... ok` 與 `1 passed`，最後一行 `exit_code=0`，其後列出 `ctrl_c-parent.txt`（最後一列 `PASS`）；全程約 6 秒。

若 `exec_delegate.exe` 不存在或 sha256 不符（暫存目錄被清理），請通知 agent 以上表的建置指令重建並重新放置，sha256 會隨之更新於本文件。

## 執行後（agent 處理）

判定範圍（已知限制，2026-10-28 雲端審查 `69b5a91_9d74511-opus.md` d0b8ad7#1）：Ctrl+C 圈可證明「孫行程被終止且 exit code 經 exec_or_wait 照傳」（產品在建立孫行程後對委派者自身忽略 Ctrl+C）；**Ctrl+Break 圈只證明行程樹會被終止，不證明照傳**——產品只對委派者忽略 Ctrl+C、不忽略 Ctrl+Break，委派者本身也以 `0xC000013A` 結束，該圈無法區分兩者。不改為「孫行程攔截 Ctrl+Break 並以特徵碼結束」的理由（2026-10-28 依雲端審查 `9d74511_32f6dd1-opus.md` 6c9a512#1 更正）：Python 基線的 Windows release（PyInstaller one-file）的 bootloader 對 Ctrl+C／Ctrl+Break 都忽略，並照傳子行程結束碼；Rust 委派者只忽略 Ctrl+C。由於 `sw_core` 沒有 SIGBREAK 處理器，兩者對使用者可觀察的結束碼目前相同，所以本輪不改案例形狀。這項差異登記為已知差距（addenda `CHG-S1X-WIN-DELEGATE-CTRLBREAK-GAP-01`），交擁有者裁定是否讓委派者一併忽略 Ctrl+Break（若裁定要改，就採用前輪 d0b8ad7#1 的特徵碼 77 案例）。登記 PASS 時須照此範圍描述，不得寫成兩圈都證明照傳。

1. 將 `%LOCALAPPDATA%\Temp\sw-od29\` 下的 `ctrl_c-result.json`、`ctrl_c-transcript.txt`、`ctrl_c-parent.txt`、`ctrl_c-child-*.txt` 複製到 `~/b-log/rust-refactor/od29/`（附 `SHA256SUMS`），核對 `exe_sha256` 等於上表值、`exit_code` 為 0、transcript 或 `ctrl_c-parent.txt` 內有 `1 passed`／`PASS`，且兩個子診斷檔都存在（證明子行程確實進入子分支）。
2. PASS：在 `docs/rust-refactor/owner-decisions-addenda.md` 的 OD-29 實跑紀錄列把本項改為「已實證（OD-29）」，附結果檔 sha256；FAIL 照實回報，不得改判或重跑到通過為止。

## 第 3 次執行（目前測試原始碼，待擁有者實跑）

雲端審查指出 ready 標記只證明子行程進入測試分支，不能證明 `exec_or_wait` 已啟動受測孫行程。測試現改由受測孫行程本身寫 `ctrl_c-delegate-ctrl_c.txt`／`ctrl_c-delegate-ctrl_break.txt`（含 PID）；父行程解析該 PID、確認仍存活後才送控制事件，事件後並確認孫行程已終止。重入命令帶 `--ignored --exact`，子／父各自寫診斷檔。判定仍要求 Ctrl+C 與 Ctrl+Break 圈皆以 `0xC000013A` 終止，沒有刪除任一圈或改期望值。

目前 source sha256：`84b6291973d5c11f34c705b9326173a36347f37020ebfcc64f27f86da6fad69e`。Windows GNU 交叉建置已完成；exe 位於工作樹 `target/x86_64-pc-windows-gnu/debug/deps/exec_delegate-a1b706c7172f3476.exe`，sha256 `6fb3d7951eb67b84b6e4a159a8cff1e59d5867b58155ec7f7854e0b38d389780`。目前環境沒有 `cmd.exe`，尚未將其複製至 Windows throwaway 根，也沒有宣稱原生驗證通過。第 2 次執行的 PASS 仍是舊版測試的證據，不能替代此版重跑。

由擁有者在 Windows Terminal 的 PowerShell 分頁執行下列單一命令；它先從此 worktree 的 WSL UNC 路徑複製建置檔至隔離的 `%LOCALAPPDATA%\Temp\sw-od29`，核對 sha256，再於互動 console 執行。`ctrl_c-parent.txt`、`ctrl_c-child-*.txt`、`ctrl_c-delegate-*.txt` 與結果 JSON 均留在該 throwaway 根，transcript 未收錄原生輸出時仍可查核：

```powershell
$d="$env:LOCALAPPDATA\Temp\sw-od29"; New-Item -ItemType Directory -Force $d | Out-Null; Copy-Item -LiteralPath '\\wsl.localhost\arc-260404\home\paul_chen\prj_pri\serialwrap-worktrees\rust-refactor\target\x86_64-pc-windows-gnu\debug\deps\exec_delegate-a1b706c7172f3476.exe' -Destination "$d\exec_delegate.exe" -Force; $actual=(Get-FileHash "$d\exec_delegate.exe" -Algorithm SHA256).Hash.ToLowerInvariant(); if($actual -ne '6fb3d7951eb67b84b6e4a159a8cff1e59d5867b58155ec7f7854e0b38d389780'){throw "exe sha256 不符：$actual"}; $env:SW_PAL_CTRLC_DIAG_DIR=$d; Start-Transcript -Path "$d\ctrl_c-transcript.txt" -Force | Out-Null; & "$d\exec_delegate.exe" --ignored --exact cap_interactive_desktop__exec_or_wait_ctrl_c --nocapture; $rc=$LASTEXITCODE; Stop-Transcript | Out-Null; $h={param($f) if (Test-Path $f) {(Get-FileHash $f -Algorithm SHA256).Hash.ToLower()} else {$null}}; [ordered]@{test='cap_interactive_desktop__exec_or_wait_ctrl_c'; exit_code=$rc; exe_sha256=(& $h "$d\exec_delegate.exe"); parent_diag_sha256=(& $h "$d\ctrl_c-parent.txt"); child_ctrl_c_sha256=(& $h "$d\ctrl_c-child-ctrl_c.txt"); child_ctrl_break_sha256=(& $h "$d\ctrl_c-child-ctrl_break.txt"); delegate_ctrl_c_sha256=(& $h "$d\ctrl_c-delegate-ctrl_c.txt"); delegate_ctrl_break_sha256=(& $h "$d\ctrl_c-delegate-ctrl_break.txt"); finished_utc=(Get-Date).ToUniversalTime().ToString('o'); host=$env:COMPUTERNAME} | ConvertTo-Json | Set-Content -Encoding utf8 "$d\ctrl_c-result.json"; "exit_code=$rc"; Get-Content "$d\ctrl_c-parent.txt"
```

預期為 `1 passed`、`exit_code=0`、父診斷最後列 `PASS`，且兩個 delegate 診斷檔都存在並各含受測孫行程 PID。完成後由擁有者提供 `%LOCALAPPDATA%\Temp\sw-od29\` 下的結果 JSON、父診斷、兩個 child 診斷、兩個 delegate 診斷、transcript 與 `SHA256SUMS`，再逐字複製至 repo 證據目錄；本輪未寫入 Windows 根目錄，也未把本次交叉建置當成原生證據。

## OD-89：Windows `interactive_desktop` L1 證據

此流程由擁有者在 Windows Terminal 的 PowerShell 7 互動分頁執行。必須直接執行，不能從 WSL interop、排程工作、管線或輸出重新導向啟動。腳本會由目前 worktree 的 WSL UNC 路徑判定 distro，先用 WSL Cargo 只建置 `sw-pal` 的 `exec_delegate`、`lock` 兩個 Windows 測試執行檔，再複製到 `%LOCALAPPDATA%\Temp\sw-interactive-l1\<run-id>`，逐一於互動 console 執行鎖定的兩列。

兩列是：

- `cargo:sw-pal::exec_delegate::cap_interactive_desktop__exec_or_wait_ctrl_c`
- `cargo:sw-pal::lock::cap_interactive_desktop__ctrl_break_stops_wait`

這兩項只測 Windows console／process 控制，不開序列埠、不碰正式安裝或 Windows 系統設定。執行前須確保候選 HEAD 已提交且工作樹乾淨（`target/` 除外）；產生後不得換候選 HEAD。

### 執行命令

在 Windows Terminal PowerShell 7 分頁：

```powershell
Set-Location '\\wsl.localhost\arc-260404\home\paul_chen\prj_pri\serialwrap-worktrees\rust-refactor-p7'
& .\scripts\rust\win-interactive-l1.ps1
```

預期兩個測試各顯示 `... ok` 與 `1 passed; 0 failed`。結尾應顯示 `collected=2 passed=2 failed=0 finished=True`，並印出結果檔位置：

```text
target/win-interactive-l1/<run-id>/l1-results.json
```

結果是 `l1-results.v1`，`collected` 只含上列兩個 nodeid；`provenance.git.head` 和 `provenance.git.end.head` 都記錄候選 commit，兩端 `clean` 必須為 `true`。檔案另含 Windows build target、Rust toolchain、RUSTFLAGS 摘要及兩個受測 exe 的 SHA256。若任何測試失敗，照實保留 failed row 與非零 exit code，不得重跑直到變綠或改判。

Windows section gate 建立 run directory 後，在 WSL 匯入原始結果；`<candidate-commit>` 必須等於結果檔記錄的 HEAD：

```bash
python3 scripts/rust/slice21_gate_judge.py import-windows-section \
  --run-dir target/slice21-gate/<gate-run-id> \
  --name windows_cap_interactive_desktop \
  --candidate-commit <candidate-commit> \
  --source target/win-interactive-l1/<run-id>/l1-results.json
```

成功時 section 片段位於 `target/slice21-gate/<gate-run-id>/sections/windows_cap_interactive_desktop.json`，L1 副本位於該 run 的 `windows_cap_interactive_desktop/l1-results.json`。先確認匯入完成，再刪除該次 `%LOCALAPPDATA%\Temp\sw-interactive-l1\<run-id>` 目錄；保留 `target/win-interactive-l1/<run-id>/l1-results.json` 供 gate 使用。

## `supervisor_runtime` Windows task 實跑準備

以下命令另需擁有者明確授權；本次準備不執行排程工作。測試須使用自己的 `sw-p8-test-<pid>` 工作名。建置前檢查共用 D 槽可用空間至少 100 GB；不足時停止：

```bash
df -BG --output=avail /mnt/d
```

擁有者授權後，在此 worktree 的 WSL shell 執行：

```bash
export PATH="/mnt/c/Windows/System32:/mnt/c/Windows/System32/WindowsPowerShell/v1.0:$PATH"
command -v cmd.exe >/dev/null
command -v wslpath >/dev/null
cmd.exe /c ver
export TMPDIR="$(mktemp -d /tmp/sw.XXXXXX)"
trap 'rm -rf -- "$TMPDIR"' EXIT
export CARGO_TARGET_DIR="$PWD/target"
set -o pipefail
cargo build -p sw-daemon --features test-fakes --bin serialwrapd --target x86_64-pc-windows-gnu
cargo build -p sw-cli --features test-fakes --bin wintask_stop_harness --target x86_64-pc-windows-gnu
SW_TEST_ALLOW_SCHTASKS=1 \
SW_WIN_EXTRA_FILES="$PWD/target/x86_64-pc-windows-gnu/debug/serialwrapd.exe:$PWD/target/x86_64-pc-windows-gnu/debug/wintask_stop_harness.exe" \
  scripts/rust/win-test.sh --package sw-pal --caps supervisor_runtime -- --test wintask_runtime
```

`SW_WIN_EXTRA_FILES` 必須同時帶入 `serialwrapd.exe` 與 `wintask_stop_harness.exe`；後者由 `wintask_runtime.rs:8` 的實跑契約要求。預期該檔的 capability 列顯示 `1 passed`，`target/win-test-report/` 會留下 run report 與對應的 `l1-results.v1`。測試正常結束會刪除本次 `sw-p8-test-<pid>` 排程工作並由 `win-runner.sh` 清除 staging；確認本次工作已不存在後，只清理由該次測試建立且仍殘留的精確 `sw-p8-test-<pid>` 工作和 temp 目錄。不得清理其他名稱的排程工作。
