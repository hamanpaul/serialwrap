#!/usr/bin/env bash
# Windows 本機 staging 與子行程監管的共用函式庫（由 win-func-test.sh、win-spike3.sh 以 source 載入）。
#
# 不變量（S1X G3）：
# - staging 內容等於 HEAD 的 git archive（不混入工作樹未提交變更）；子集以外的追蹤檔以 skip-worktree 標記，
#   使 Windows 端 git status 只反映子集本身，spike3 的 artifact.git_clean 才能如實判定。
# - Windows 子行程只以「PID＋行程起始時間」識別，停止時 taskkill /PID /T /F；不以映像名稱搜尋，
#   不觸碰既有的隔離排程工作或其 serialwrapd，也不開啟任何序列埠（COM 由呼叫的 wrapper 經 argv 指定）。
# - 只有 wrapper 呼叫本檔函式；source 本檔時不產生任何副作用。

# staging 子集（HEAD 路徑）。runner 與 spike3 的 import 依賴需與此清單同步；
# tests/test_rust_win_func_test.py 會斷言此清單涵蓋的必要路徑。
SW_WIN_SUBSET=(
  func-test
  realhw
  sw_core
  tools/uart_stdio_bridge
  contracts
  tests/acceptance
  scripts/stage0/spike3_win_wal.py
  scripts/stage0/spike3_evidence.schema.json
  scripts/stage0/wal_microbench.py
  serialwrap.py
  serialwrapd.py
  VERSION
)

SW_WIN_LAD_UNIX=""
SW_WIN_RUN_UNIX=""
SW_WIN_RUN_WIN=""
SW_WIN_STAGE_UNIX=""
SW_WIN_LAUNCHER_UNIX=""
SW_WIN_LAUNCHER_WIN=""
SW_WIN_HEAD=""
SW_WIN_TREE=""
SW_WIN_GIT_CLEAN=""
SW_WIN_CHILD_RC=""

sw_win_die() {
  printf '%s: %s\n' "${SW_WIN_TOOL:-win-stage}" "$*" >&2
  exit 2
}

# 確認 WSL interop 可用並取得 Windows LOCALAPPDATA 的 WSL 路徑；不可用即結束（證據不足，不得改以 WSL 代跑）。
sw_win_require_interop() {
  local lad
  if ! command -v cmd.exe >/dev/null 2>&1 || ! command -v powershell.exe >/dev/null 2>&1 \
    || ! command -v wslpath >/dev/null 2>&1; then
    sw_win_die "需要 WSL interop（cmd.exe、powershell.exe、wslpath）；證據不足"
  fi
  lad="$(cd /mnt/c && cmd.exe /c 'echo %LOCALAPPDATA%' 2>/dev/null | tr -d '\r' | tail -n 1)" || lad=""
  if [[ -z "$lad" || "$lad" == '%LOCALAPPDATA%' ]]; then
    sw_win_die "無法讀取 Windows LOCALAPPDATA；證據不足"
  fi
  SW_WIN_LAD_UNIX="$(wslpath -u "$lad")" || sw_win_die "無法轉換 LOCALAPPDATA 路徑：$lad"
}

# sw_win_stage_create <run_id>
# 建立 %LOCALAPPDATA%\Temp\sw-rust-test\<run_id>\{repo,tmp,report,logs}；repo 為 HEAD 子集並以 Windows git 可判定乾淨。
# 需先設定 ROOT（repo 根目錄）。設定 SW_WIN_RUN_UNIX／SW_WIN_RUN_WIN／SW_WIN_STAGE_UNIX／SW_WIN_HEAD／SW_WIN_TREE／SW_WIN_GIT_CLEAN。
sw_win_stage_create() {
  local run_id="$1"
  local listed all skip
  SW_WIN_RUN_UNIX="$SW_WIN_LAD_UNIX/Temp/sw-rust-test/$run_id"
  SW_WIN_STAGE_UNIX="$SW_WIN_RUN_UNIX/repo"
  mkdir -p "$SW_WIN_STAGE_UNIX" "$SW_WIN_RUN_UNIX/tmp" "$SW_WIN_RUN_UNIX/logs" "$SW_WIN_RUN_UNIX/report"
  SW_WIN_RUN_WIN="$(wslpath -w "$SW_WIN_RUN_UNIX")"

  git -C "$ROOT" archive --format=tar HEAD -- "${SW_WIN_SUBSET[@]}" \
    | tar -x -C "$SW_WIN_STAGE_UNIX" || sw_win_die "git archive HEAD 子集失敗"
  git -C "$SW_WIN_STAGE_UNIX" init -q
  git -C "$SW_WIN_STAGE_UNIX" config --local core.autocrlf false
  git -C "$SW_WIN_STAGE_UNIX" config --local core.filemode false
  git -C "$SW_WIN_STAGE_UNIX" fetch -q --no-tags "$ROOT" HEAD
  git -C "$SW_WIN_STAGE_UNIX" update-ref HEAD FETCH_HEAD
  git -C "$SW_WIN_STAGE_UNIX" read-tree HEAD

  # 子集以外的追蹤檔標 skip-worktree：不在 staging 中，但不應被 git status 報為刪除。
  listed="$SW_WIN_RUN_UNIX/.subset.z"
  all="$SW_WIN_RUN_UNIX/.index.z"
  skip="$SW_WIN_RUN_UNIX/.skip.z"
  git -C "$ROOT" ls-tree -r -z --name-only HEAD -- "${SW_WIN_SUBSET[@]}" | LC_ALL=C sort -z > "$listed"
  git -C "$SW_WIN_STAGE_UNIX" ls-files -z | LC_ALL=C sort -z > "$all"
  LC_ALL=C comm -z -23 "$all" "$listed" > "$skip"
  git -C "$SW_WIN_STAGE_UNIX" update-index -z --skip-worktree --stdin < "$skip"
  rm -f -- "$listed" "$all" "$skip"

  SW_WIN_HEAD="$(git -C "$ROOT" rev-parse HEAD)"
  SW_WIN_TREE="$(git -C "$ROOT" rev-parse 'HEAD^{tree}')"
  if [[ "$(git -C "$SW_WIN_STAGE_UNIX" rev-parse HEAD)" != "$SW_WIN_HEAD" ]]; then
    sw_win_die "staging HEAD 與受測 HEAD 不符"
  fi
  if [[ -z "$(git -C "$SW_WIN_STAGE_UNIX" status --porcelain --untracked-files=normal)" ]]; then
    SW_WIN_GIT_CLEAN=true
  else
    SW_WIN_GIT_CLEAN=false
  fi
}

# sw_win_write_launcher：寫入 Windows 端啟動器（PowerShell）。啟動器以 PID＋起始時間記錄子行程，逾時以 taskkill /T /F 結束。
sw_win_write_launcher() {
  SW_WIN_LAUNCHER_UNIX="$SW_WIN_RUN_UNIX/win-child.ps1"
  SW_WIN_LAUNCHER_WIN="$SW_WIN_RUN_WIN\\win-child.ps1"
  cat > "$SW_WIN_LAUNCHER_UNIX" <<'PS'
param(
    [Parameter(Mandatory = $true)][string]$RunDir,
    [int]$TimeoutSeconds = 0,
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $RunDir 'child.pid'
$exitFile = Join-Path $RunDir 'child.exit'

function Get-ChildState {
    param([int]$ProcId, [long]$Ticks)
    try {
        $proc = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
        if ($null -eq $proc) { return 'STOPPED' }
        if ($proc.StartTime.ToUniversalTime().Ticks -eq $Ticks) { return 'RUNNING' }
        return 'REUSED'
    } catch {
        return 'UNKNOWN'
    }
}

if ($Stop) {
    if (-not (Test-Path -LiteralPath $pidFile)) { exit 0 }
    $identity = [IO.File]::ReadAllLines($pidFile)
    $targetId = [int]$identity[0].Trim()
    $targetTicks = [long]$identity[1].Trim()
    $state = Get-ChildState $targetId $targetTicks
    if ($state -eq 'STOPPED' -or $state -eq 'REUSED') { exit 0 }
    if ($state -eq 'RUNNING') {
        & taskkill.exe /PID $targetId /T /F | Out-Null
        for ($i = 0; $i -lt 10; $i++) {
            $state = Get-ChildState $targetId $targetTicks
            if ($state -ne 'RUNNING') { break }
            Start-Sleep -Milliseconds 500
        }
    }
    $state = Get-ChildState $targetId $targetTicks
    if ($state -eq 'STOPPED' -or $state -eq 'REUSED') { exit 0 }
    exit 1
}

foreach ($line in [IO.File]::ReadAllLines((Join-Path $RunDir 'child-env.txt'), [Text.Encoding]::UTF8)) {
    if ($line -match '^([^=]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
$argLine = [IO.File]::ReadAllText((Join-Path $RunDir 'child-args.txt'), [Text.Encoding]::UTF8)
Remove-Item -LiteralPath $pidFile, $exitFile -Force -ErrorAction SilentlyContinue
try {
    $proc = Start-Process -FilePath 'py' -ArgumentList $argLine `
        -WorkingDirectory (Join-Path $RunDir 'repo') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $RunDir 'child.stdout') `
        -RedirectStandardError (Join-Path $RunDir 'child.stderr')
} catch {
    [IO.File]::WriteAllText($exitFile, '125')
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 125
}
$null = $proc.Handle
[IO.File]::WriteAllText($pidFile, ("{0}`n{1}" -f $proc.Id, $proc.StartTime.ToUniversalTime().Ticks))
if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
    & taskkill.exe /PID $proc.Id /T /F | Out-Null
    $null = $proc.WaitForExit(5000)
    [IO.File]::WriteAllText($exitFile, '124')
    exit 124
}
$code = $proc.ExitCode
[IO.File]::WriteAllText($exitFile, [string]$code)
exit $code
PS
  SW_WIN_LAUNCHER_WIN="$(wslpath -w "$SW_WIN_LAUNCHER_UNIX")"
}

# sw_win_write_env <NAME=VALUE>...：寫入子行程環境；只傳入列出的變數（不經 WSLENV 轉送呼叫者的環境）。
sw_win_write_env() {
  local kv
  : > "$SW_WIN_RUN_UNIX/child-env.txt"
  for kv in "$@"; do
    [[ "$kv" == *=* ]] || sw_win_die "環境變數格式錯誤：$kv"
    printf '%s\n' "$kv" >> "$SW_WIN_RUN_UNIX/child-env.txt"
  done
}

# sw_win_run_child <timeout_s> <py 參數...>
# 以 Windows py 執行並等待結束；結果寫入 SW_WIN_CHILD_RC（逾時 124、啟動失敗 125）。
sw_win_run_child() {
  local timeout_s="$1"
  local ps_rc=0
  shift
  [[ "$timeout_s" =~ ^[1-9][0-9]*$ ]] || sw_win_die "逾時秒數必須為正整數"
  python3 -c 'import subprocess, sys; sys.stdout.write(subprocess.list2cmdline(sys.argv[1:]))' "$@" \
    > "$SW_WIN_RUN_UNIX/child-args.txt"
  rm -f -- "$SW_WIN_RUN_UNIX/child.exit"
  (cd /mnt/c && WSLENV= powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass \
    -File "$SW_WIN_LAUNCHER_WIN" -RunDir "$SW_WIN_RUN_WIN" -TimeoutSeconds "$timeout_s") \
    > "$SW_WIN_RUN_UNIX/launcher.stdout" 2> "$SW_WIN_RUN_UNIX/launcher.stderr" || ps_rc=$?
  if [[ -s "$SW_WIN_RUN_UNIX/child.exit" ]]; then
    SW_WIN_CHILD_RC="$(tr -d '\r\n' < "$SW_WIN_RUN_UNIX/child.exit")"
    [[ "$SW_WIN_CHILD_RC" =~ ^[0-9]+$ ]] || SW_WIN_CHILD_RC=125
  else
    SW_WIN_CHILD_RC=125
    printf '%s: 啟動器未留下子行程退出碼（powershell rc=%s）\n' "${SW_WIN_TOOL:-win-stage}" "$ps_rc" >&2
  fi
}

# sw_win_collect_child_logs <目的前綴>：把本次子行程的輸出移到 <前綴>.stdout／.stderr（啟動器 stderr 另存 .launcher.stderr）。
sw_win_collect_child_logs() {
  local dest="$1"
  mkdir -p -- "$(dirname "$dest")"
  [[ -e "$SW_WIN_RUN_UNIX/child.stdout" ]] && mv -f -- "$SW_WIN_RUN_UNIX/child.stdout" "$dest.stdout"
  [[ -e "$SW_WIN_RUN_UNIX/child.stderr" ]] && mv -f -- "$SW_WIN_RUN_UNIX/child.stderr" "$dest.stderr"
  [[ -e "$SW_WIN_RUN_UNIX/launcher.stderr" ]] && mv -f -- "$SW_WIN_RUN_UNIX/launcher.stderr" "$dest.launcher.stderr"
  return 0
}

# sw_win_stop_child：若 child.pid 記錄的子行程仍在執行（PID＋起始時間相符）則結束其行程樹；回傳 0 表示已確認不在執行。
sw_win_stop_child() {
  [[ -n "$SW_WIN_LAUNCHER_WIN" && -e "$SW_WIN_RUN_UNIX/child.pid" ]] || return 0
  (cd /mnt/c && WSLENV= powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass \
    -File "$SW_WIN_LAUNCHER_WIN" -RunDir "$SW_WIN_RUN_WIN" -Stop) >/dev/null 2>&1
}

# sw_win_cleanup：EXIT 陷阱。先確認子行程已終止；無法確認即保留 staging。SW_WIN_KEEP=1 保留 staging。
sw_win_cleanup() {
  local rc=$?
  local preserve=0
  trap - EXIT INT TERM
  if [[ -n "$SW_WIN_RUN_UNIX" && -d "$SW_WIN_RUN_UNIX" ]]; then
    if ! sw_win_stop_child; then
      printf '%s: 無法確認 Windows 子行程已終止，保留 staging %s\n' "${SW_WIN_TOOL:-win-stage}" "$SW_WIN_RUN_UNIX" >&2
      preserve=1
      [[ "$rc" -ne 0 ]] || rc=125
    fi
  fi
  # 只刪除本次 staging：路徑必須位於 %LOCALAPPDATA%\Temp\sw-rust-test 之下，防止變數錯置時誤刪其他目錄。
  if [[ -n "$SW_WIN_RUN_UNIX" && -d "$SW_WIN_RUN_UNIX" && "$preserve" -eq 0 && "${SW_WIN_KEEP:-0}" != 1 \
    && "$SW_WIN_RUN_UNIX" == "$SW_WIN_LAD_UNIX/Temp/sw-rust-test/"?* ]]; then
    if ! (cd / && rm -rf -- "$SW_WIN_RUN_UNIX"); then
      printf '%s: 警告：無法移除 staging %s\n' "${SW_WIN_TOOL:-win-stage}" "$SW_WIN_RUN_UNIX" >&2
    fi
  fi
  exit "$rc"
}
