#!/usr/bin/env bash
set -euo pipefail

repo=$(git rev-parse --show-toplevel)
run_id="$(date +%y%m%d-%H%M%S)-$$"

if ! lad_win=$(cmd.exe /c 'echo %LOCALAPPDATA%' 2>/dev/null | tr -d '\r'); then
    printf '%s\n' 'win-pytest: 無法取得 Windows LOCALAPPDATA' >&2
    exit 125
fi
if [[ -z "$lad_win" ]]; then
    printf '%s\n' 'win-pytest: Windows LOCALAPPDATA 為空' >&2
    exit 125
fi

lad_wsl=$(wslpath -u "$lad_win")
run_dir="$lad_wsl/Temp/sw-stage0/$run_id"
stage="$run_dir/repo"
mkdir -p "$stage"

windows_pid=""
windows_started_at=""
powershell_pid=""
preserve_runner_artifacts=0
windows_pid_file="$run_dir/windows-pytest.pid"

read_windows_identity() {
    local identity=()
    [[ -s "$windows_pid_file" ]] || return 1
    mapfile -t identity < "$windows_pid_file"
    windows_pid="${identity[0]:-}"
    windows_pid="${windows_pid//$'\r'/}"
    windows_started_at="${identity[1]:-}"
    windows_started_at="${windows_started_at//$'\r'/}"
    [[ "$windows_pid" =~ ^[0-9]+$ && "$windows_started_at" =~ ^[0-9]+$ ]]
}

windows_process_state() {
    local pid=$1
    local started_at=$2
    local state
    if [[ ! "$pid" =~ ^[0-9]+$ || ! "$started_at" =~ ^[0-9]+$ ]]; then
        printf '%s' 'UNKNOWN'
        return 0
    fi
    state=$(powershell.exe -NoProfile -NonInteractive -Command \
        "\$ErrorActionPreference = 'Stop'; try { \$p = Get-Process -Id $pid -ErrorAction SilentlyContinue; if (\$null -eq \$p) { 'STOPPED' } elseif (\$p.StartTime.ToUniversalTime().Ticks -eq [long]$started_at) { 'RUNNING' } else { 'REUSED' } } catch { 'UNKNOWN' }" \
        2>/dev/null) || state=UNKNOWN
    state=${state//$'\r'/}
    case "$state" in
        *STOPPED*) printf '%s' 'STOPPED' ;;
        *REUSED*) printf '%s' 'REUSED' ;;
        *RUNNING*) printf '%s' 'RUNNING' ;;
        *) printf '%s' 'UNKNOWN' ;;
    esac
}

stop_windows_tree() {
    local pid=$1
    local started_at=$2
    local state
    state=$(windows_process_state "$pid" "$started_at")
    case "$state" in
        STOPPED|REUSED) return 0 ;;
        RUNNING) ;;
        *) return 1 ;;
    esac

    taskkill.exe /PID "$pid" /T /F >/dev/null 2>&1 || true
    for _ in {1..5}; do
        state=$(windows_process_state "$pid" "$started_at")
        case "$state" in
            STOPPED|REUSED) return 0 ;;
            UNKNOWN) return 1 ;;
        esac
        sleep 1
    done
    return 1
}

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    if [[ -z "$windows_pid" && -e "$windows_pid_file" ]]; then
        if ! read_windows_identity; then
            printf 'win-pytest: 無法確認 Windows pytest 行程識別資料，保留 %s 與 staging\n' \
                "$windows_pid_file" >&2
            preserve_runner_artifacts=1
            [[ "$rc" -ne 0 ]] || rc=125
        fi
    fi
    if [[ -n "$windows_pid" ]]; then
        if stop_windows_tree "$windows_pid" "$windows_started_at"; then
            rm -f -- "$windows_pid_file"
            windows_pid=""
            windows_started_at=""
        else
            printf 'win-pytest: 無法確認 Windows pytest 行程樹已終止，保留 PID、診斷資料與 staging（PID=%s）\n' \
                "$windows_pid" >&2
            preserve_runner_artifacts=1
            [[ "$rc" -ne 0 ]] || rc=125
        fi
    fi
    if [[ -n "$powershell_pid" ]] && kill -0 "$powershell_pid" 2>/dev/null; then
        kill "$powershell_pid" 2>/dev/null || true
        wait "$powershell_pid" 2>/dev/null || true
    fi
    if [[ "${SW_WIN_KEEP:-0}" != "1" && "$preserve_runner_artifacts" != "1" ]]; then
        cd /
        if ! rm -rf -- "$stage"; then
            printf 'win-pytest: 警告：無法移除 staging %s\n' "$stage" >&2
        fi
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 將版本庫已追蹤與未忽略的未追蹤檔複製到 Windows 本機磁碟。
manifest="$run_dir/files.list"
git -C "$repo" ls-files -z --cached --others --exclude-standard --deduplicate > "$manifest"
(
    cd "$repo"
    while IFS= read -r -d '' path; do
        if [[ ! -e "$path" && ! -L "$path" ]]; then
            continue
        fi
        cp --parents --target-directory="$stage" -- "$path"
    done < "$manifest"
)

cd "$stage"
pytest_exit=0
require_native=0
if ! cmd.exe /c py -3.12 -c "import pytest, yaml, serial, jsonschema"; then
    printf '%s\n' 'win-pytest: Windows Python 缺 pytest/PyYAML/pyserial/jsonschema' >&2
    pytest_exit=3
else
    # P2-T13 審查修正：不再以 AST 產生 --ignore 清單。posix_only 模組由 tests/conftest.py 的
    # pytest_ignore_collect（與 pytest_make_collect_report，命令列明確指定者）排除；指令參數須與鎖檔 selector.argv 一致。
    {

        stage_win=$(wslpath -w "$stage")
        run_win=$(wslpath -w "$run_dir")
        junit_win="$stage_win\\junit.xml"
        export SW_WIN_PID_FILE="$run_win\\windows-pytest.pid"
        export SW_WIN_EXIT_FILE="$run_win\\windows-pytest.exit"
        export SW_WIN_STDOUT="$run_win\\windows-pytest.stdout"
        export SW_WIN_STDERR="$run_win\\windows-pytest.stderr"
        export SW_RESOURCE_REPORT="$stage_win\\resource-report.json"
        export PYTHONUTF8=1

        append_wslenv() {
            local name=$1
            local entry
            local entries=()
            IFS=':' read -r -a entries <<< "${WSLENV:-}"
            for entry in "${entries[@]}"; do
                if [[ "${entry%%/*}" == "$name" ]]; then
                    return
                fi
            done
            if [[ -n "${WSLENV:-}" ]]; then
                WSLENV="$WSLENV:$name"
            else
                WSLENV="$name"
            fi
            export WSLENV
        }

        while IFS= read -r name; do
            case "$name" in
                SW_TEST_*|SW_WIN_*)
                    case "$name" in
                        SW_WIN_TIMEOUT|SW_WIN_KEEP) continue ;;
                    esac
                    append_wslenv "$name"
                    ;;
            esac
        done < <(compgen -e)
        for name in SW_WIN_PID_FILE SW_WIN_EXIT_FILE SW_WIN_STDOUT SW_WIN_STDERR SW_RESOURCE_REPORT; do
            append_wslenv "$name"
        done
        append_wslenv PYTHONUTF8

        # 只傳入呼叫者的參數（含 -q）與 junit 路徑；不另注入 -rs／-p no:cacheprovider（會使報告 argv 與鎖檔不符）。
        pytest_args=("--junitxml=$junit_win" "$@")
        export SW_WIN_PYTEST_ARGS_JSON
        SW_WIN_PYTEST_ARGS_JSON=$(python3 -c \
            'import json, sys; print(json.dumps(sys.argv[1:], separators=(",", ":")))' \
            "${pytest_args[@]}")
        append_wslenv SW_WIN_PYTEST_ARGS_JSON

        previous=""
        for arg in "$@"; do
            if [[ "$previous" == "-m" || "$previous" == "--markexpr" ]]; then
                if [[ "$arg" == *windows_native* || "$arg" == *windows_com* ]]; then
                    require_native=1
                fi
            fi
            case "$arg" in
                -m*windows_native*|-m*windows_com*|--markexpr=*windows_native*|--markexpr=*windows_com*)
                    require_native=1
                    ;;
            esac
            previous="$arg"
        done

        timeout_s="${SW_WIN_TIMEOUT:-900}"
        if [[ ! "$timeout_s" =~ ^[1-9][0-9]*$ ]]; then
            printf '%s\n' 'win-pytest: SW_WIN_TIMEOUT 必須是正整數' >&2
            pytest_exit=2
        else
            launcher="$run_dir/launch-pytest.ps1"
            cat > "$launcher" <<'PS'
$ErrorActionPreference = "Stop"
$process = $null
try {
    $arguments = @(
        "-3.12",
        "-c",
        '"import json, os, pytest; raise SystemExit(pytest.main(json.loads(os.environ[''SW_WIN_PYTEST_ARGS_JSON''])))"'
    )
    $process = Start-Process -FilePath "py" -ArgumentList $arguments -PassThru -WindowStyle Hidden -RedirectStandardOutput $env:SW_WIN_STDOUT -RedirectStandardError $env:SW_WIN_STDERR
    $null = $process.Handle
    $identity = "$($process.Id)`n$($process.StartTime.ToUniversalTime().Ticks)"
    [System.IO.File]::WriteAllText($env:SW_WIN_PID_FILE, $identity)
    $process.WaitForExit()
    [System.IO.File]::WriteAllText($env:SW_WIN_EXIT_FILE, [string]$process.ExitCode)
    exit $process.ExitCode
} catch {
    if ($null -ne $process) {
        try {
            $process.Refresh()
            if (-not $process.HasExited) {
                & taskkill.exe /PID $process.Id /T /F | Out-Null
                $null = $process.WaitForExit(5000)
            }
        } catch {
        }
    }
    [Console]::Error.WriteLine($_.Exception.Message)
    [System.IO.File]::WriteAllText($env:SW_WIN_EXIT_FILE, "125")
    exit 125
}
PS
            launcher_win=$(wslpath -w "$launcher")
            powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$launcher_win" \
                > "$run_dir/powershell.stdout" 2> "$run_dir/powershell.stderr" &
            powershell_pid=$!

            started_at=$SECONDS
            while :; do
                if [[ -s "$run_dir/windows-pytest.exit" ]]; then
                    if wait "$powershell_pid"; then
                        :
                    else
                        launcher_rc=$?
                    fi
                    powershell_pid=""
                    pytest_exit=$(tr -d '\r\n' < "$run_dir/windows-pytest.exit")
                    [[ "$pytest_exit" =~ ^[0-9]+$ ]] || pytest_exit=125
                    rm -f -- "$windows_pid_file"
                    windows_pid=""
                    windows_started_at=""
                    break
                fi

                if ! kill -0 "$powershell_pid" 2>/dev/null; then
                    if wait "$powershell_pid"; then
                        launcher_rc=0
                    else
                        launcher_rc=$?
                    fi
                    powershell_pid=""
                    if [[ -s "$run_dir/windows-pytest.exit" ]]; then
                        pytest_exit=$(tr -d '\r\n' < "$run_dir/windows-pytest.exit")
                        [[ "$pytest_exit" =~ ^[0-9]+$ ]] || pytest_exit=125
                        rm -f -- "$windows_pid_file"
                        windows_pid=""
                        windows_started_at=""
                    else
                        pytest_exit=$launcher_rc
                        [[ "$pytest_exit" -ne 0 ]] || pytest_exit=125
                        if read_windows_identity; then
                            if stop_windows_tree "$windows_pid" "$windows_started_at"; then
                                rm -f -- "$windows_pid_file"
                                windows_pid=""
                                windows_started_at=""
                            else
                                printf 'win-pytest: PowerShell 提前退出且無法確認 pytest 行程已終止，保留 PID 與 staging（PID=%s）\n' \
                                    "$windows_pid" >&2
                                preserve_runner_artifacts=1
                                pytest_exit=125
                            fi
                        else
                            printf 'win-pytest: PowerShell 提前退出且無有效 PID 識別資料，保留診斷資料與 staging\n' >&2
                            preserve_runner_artifacts=1
                            pytest_exit=125
                        fi
                    fi
                    break
                fi

                if (( SECONDS - started_at >= timeout_s )); then
                    if read_windows_identity; then
                        if stop_windows_tree "$windows_pid" "$windows_started_at"; then
                            rm -f -- "$windows_pid_file"
                            windows_pid=""
                            windows_started_at=""
                        else
                            printf 'win-pytest: 逾時後無法確認 pytest 行程已終止，保留 PID 與 staging（PID=%s）\n' \
                                "$windows_pid" >&2
                            preserve_runner_artifacts=1
                            pytest_exit=125
                        fi
                    else
                        printf '%s\n' 'win-pytest: 逾時後沒有有效 PID 識別資料，保留診斷資料與 staging' >&2
                        preserve_runner_artifacts=1
                        pytest_exit=125
                    fi
                    for _ in {1..5}; do
                        if ! kill -0 "$powershell_pid" 2>/dev/null; then
                            break
                        fi
                        sleep 1
                    done
                    if kill -0 "$powershell_pid" 2>/dev/null; then
                        kill "$powershell_pid" 2>/dev/null || true
                    fi
                    wait "$powershell_pid" 2>/dev/null || true
                    powershell_pid=""
                    if [[ "$pytest_exit" != "125" ]]; then
                        pytest_exit=124
                    fi
                    break
                fi
                sleep 1
            done
        fi
    }
fi

if [[ -f "$stage/junit.xml" ]]; then
    cp -a -- "$stage/junit.xml" "$run_dir/junit.xml"
fi
if [[ -f "$stage/resource-report.json" ]]; then
    cp -a -- "$stage/resource-report.json" "$run_dir/resource-report.json"
fi
for output in windows-pytest.stdout windows-pytest.stderr powershell.stdout powershell.stderr; do
    if [[ -s "$run_dir/$output" ]]; then
        cat "$run_dir/$output"
    fi
done

summary_args=(
    --junit "$run_dir/junit.xml"
    --pytest-exit "$pytest_exit"
    --output "$run_dir/summary.json"
)
if [[ "$require_native" == "1" ]]; then
    summary_args+=(--require-native)
fi
if python3 "$repo/scripts/stage0/junit_summary.py" "${summary_args[@]}"; then
    final_rc=0
else
    final_rc=$?
fi

if [[ "$preserve_runner_artifacts" != "1" ]]; then
    rm -f -- "$manifest" "$run_dir/ignore-args.bin" "$run_dir/launch-pytest.ps1" \
        "$windows_pid_file" "$run_dir/windows-pytest.exit" \
        "$run_dir/windows-pytest.stdout" "$run_dir/windows-pytest.stderr" \
        "$run_dir/powershell.stdout" "$run_dir/powershell.stderr"
fi
printf 'win-pytest: summary=%s\n' "$run_dir/summary.json"
printf 'win-pytest: staging=%s exit=%s\n' "$stage" "$final_rc"
exit "$final_rc"

