#!/usr/bin/env bash
# A/B 黑箱執行腳本（P3-T18）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
PROFILE="debug"
L1_OUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --l1-out)
      L1_OUT="$2"
      shift 2
      ;;
    -h|--help)
      echo "用法: scripts/rust/ab_blackbox.sh [--profile debug|release] [--l1-out <dir>]"
      exit 0
      ;;
    *)
      echo "未知選項: $1" >&2
      exit 2
      ;;
  esac
done

BUNDLE_OUT="$REPO_ROOT/target/mkbundle-dev"
"$REPO_ROOT/scripts/rust/mkbundle.sh" --dev --profile "$PROFILE" --out "$BUNDLE_OUT"

BUNDLE_CLI="$BUNDLE_OUT/bin/serialwrap"
if [[ ! -f "$BUNDLE_CLI" ]]; then
  echo "錯誤: bundle CLI 不存在: $BUNDLE_CLI" >&2
  exit 1
fi

cd "$REPO_ROOT"

RUN_ID="$(date +%Y%m%d_%H%M%S)_$$"
RUN_DIR="$REPO_ROOT/target/ab-runs/$RUN_ID"
mkdir -p "$RUN_DIR" "$REPO_ROOT/target/ab-report"

# 1. 產生必要案例清單 (required) - 僅保留黑箱 pytest 案例
REQUIRED_CASES="$RUN_DIR/required_cases.txt"
python3 -c "
import json
from pathlib import Path

root = Path('$REPO_ROOT')
cases_lock = root / 'tests' / 'acceptance' / 'cases.lock.json'
cov_json = root / 'tests' / 'contract_blackbox' / 'data' / 'coverage_1a.json'

c1a = set()
if cases_lock.is_file():
    data = json.loads(cases_lock.read_text(encoding='utf-8'))
    c1a.update(data.get('sets', {}).get('cli_1A', []))

extra = set()
if cov_json.is_file():
    cov = json.loads(cov_json.read_text(encoding='utf-8'))
    extra.update(cov.get('p3_extra', []))

req = sorted(c for c in (c1a | extra) if ('.py::' in c or c.startswith('test_bb_')))
Path('$REQUIRED_CASES').write_text('\n'.join(req) + '\n', encoding='utf-8')
"

A_PIDS="$RUN_DIR/a.pids"
A_XML="$RUN_DIR/a.xml"
ID_A="$RUN_DIR/id-a.json"
ID_B="$RUN_DIR/id-b.json"

if [[ -n "$L1_OUT" ]]; then
  mkdir -p "$L1_OUT/linux/blackbox"
  B_XML="$L1_OUT/linux/blackbox/junit.xml"
  B_RESOURCE_REPORT="$L1_OUT/linux/blackbox/resource-report.json"
else
  B_XML="$RUN_DIR/b.xml"
  B_RESOURCE_REPORT="$RUN_DIR/b-resource-report.json"
fi

# 2. 執行 A 輪（Python 基線）
echo "=== 開始 A 輪（Python 基線）==="
touch "$A_PIDS"
set +e
env -u SERIALWRAP_CLI_EXE -u SERIALWRAP_DAEMON_EXE -u SERIALWRAP_AB_LABEL \
  SW_AB_PIDLOG="$A_PIDS" python3 -m pytest tests/contract_blackbox \
  -m "core_portable or posix_only" \
  --confcutdir=tests/contract_blackbox \
  -p no:cacheprovider \
  -o xfail_strict=true \
  --junitxml="$A_XML"
a_rc=$?
set -e
if [[ "$a_rc" -ge 2 ]]; then
  echo "A 輪 pytest 執行異常 (exit $a_rc)" >&2
  exit "$a_rc"
fi


# 3. 釋放確認（規格 §15.9.1 第 3 步）
echo "=== 執行 A 輪釋放確認 ==="
RELEASE_CHECK="$RUN_DIR/release-check.json"
python3 -c "
import fcntl, json, os, sys, time
from pathlib import Path

run_dir = Path('$RUN_DIR')
pids_file = Path('$A_PIDS')
release_check = Path('$RELEASE_CHECK')

pids = []
sockets = []
locks = []
workdirs = []
parse_errors = []

if not pids_file.is_file():
    parse_errors.append(f'pids_file 不存在: {pids_file}')
else:
    raw_lines = pids_file.read_text(encoding='utf-8').splitlines()
    if not raw_lines:
        parse_errors.append('pids_file 為空，未記錄任何資源')
    for idx, line in enumerate(raw_lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict):
                parse_errors.append(f'第 {idx} 行非 JSON 物件')
                continue
            if 'pid' in rec and rec['pid']:
                pids.append(rec['pid'])
            if 'children' in rec and isinstance(rec['children'], list):
                pids.extend(rec['children'])
            if 'socket' in rec and rec['socket']:
                sockets.append(rec['socket'])
            if 'lock' in rec and rec['lock']:
                locks.append(rec['lock'])
            if 'run_dir' in rec and rec['run_dir']:
                workdirs.append(rec['run_dir'])
            if 'state_dir' in rec and rec['state_dir']:
                workdirs.append(rec['state_dir'])
        except Exception as e:
            parse_errors.append(f'解析第 {idx} 行失敗: {e}')

if not pids and not parse_errors:
    parse_errors.append('未記錄任何有效 pid')

# 為每個進程獨立設置等待上限 (最多 10 秒)
pids_reaped = True
unreaped_pids = []
for pid in set(pids):
    pid_deadline = time.monotonic() + 10.0
    while time.monotonic() < pid_deadline:
        proc_stat = Path(f'/proc/{pid}/status')
        if not proc_stat.exists():
            break
        try:
            stat_text = proc_stat.read_text(encoding='utf-8')
            if 'State:\tZ (zombie)' in stat_text:
                break
        except Exception:
            break
        time.sleep(0.05)
    else:
        pids_reaped = False
        unreaped_pids.append(pid)

# 檢查 socket 檔案不存在
sockets_cleaned = True
remaining_sockets = []
for s in set(sockets):
    p = s[7:] if s.startswith('unix://') else s
    if os.path.exists(p):
        sockets_cleaned = False
        remaining_sockets.append(p)

# 檢查 locks 可被 flock -n 取得
locks_free = True
held_locks = []
for lk in set(locks):
    if os.path.exists(lk):
        try:
            fd = os.open(lk, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        except (BlockingIOError, OSError):
            locks_free = False
            held_locks.append(lk)

# 檢查 /proc/*/fd 是否有開啟 run_dir、state_dir 或 A 輪行程 PTY
fds_released = True
held_fds = []
workdir_strs = [os.path.realpath(str(w)) for w in set(workdirs) if os.path.exists(w)]
try:
    for proc_dir in Path('/proc').iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid_num = int(proc_dir.name)
        if pid_num == os.getpid():
            continue
        fd_dir = proc_dir / 'fd'
        if not fd_dir.is_dir():
            continue
        try:
            for fd_link in fd_dir.iterdir():
                try:
                    target = os.path.realpath(os.readlink(fd_link))
                    for wd in workdir_strs:
                        if target.startswith(wd):
                            fds_released = False
                            held_fds.append(f'pid {pid_num} 持有 {target}')
                            break
                    if target.startswith('/dev/pts/') and pid_num in set(pids):
                        fds_released = False
                        held_fds.append(f'A 輪 pid {pid_num} 仍持有 PTY {target}')
                except OSError:
                    pass
        except OSError:
            pass
except Exception as e:
    parse_errors.append(f'掃描 /proc/*/fd 異常: {e}')

ok = pids_reaped and sockets_cleaned and locks_free and fds_released and not parse_errors
res = {
    'ok': ok,
    'pids_reaped': pids_reaped,
    'sockets_cleaned': sockets_cleaned,
    'locks_free': locks_free,
    'fds_released': fds_released,
    'parse_errors': parse_errors,
    'unreaped_pids': unreaped_pids,
    'remaining_sockets': remaining_sockets,
    'held_locks': held_locks,
    'held_fds': held_fds,
}
release_check.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
if not ok:
    print(f'釋放確認失敗: {res}', file=sys.stderr)
    sys.exit(1)
"

# 4. 執行 B 輪（Rust CLI）
echo "=== 開始 B 輪（Rust CLI）==="
B_PIDS="$RUN_DIR/b.pids"
touch "$B_PIDS"
b_env=(
  SERIALWRAP_CLI_EXE="$BUNDLE_CLI"
  SERIALWRAP_AB_LABEL="rust-1a"
  SW_AB_PIDLOG="$B_PIDS"
)
if [[ -n "$L1_OUT" ]]; then
  b_env+=(SW_RESOURCE_REPORT="$B_RESOURCE_REPORT")
fi

set +e
env "${b_env[@]}" python3 -m pytest tests/contract_blackbox \
  -m "core_portable or posix_only" \
  --confcutdir=tests/contract_blackbox \
  -p no:cacheprovider \
  -o xfail_strict=true \
  --junitxml="$B_XML"
b_rc=$?
set -e
if [[ "$b_rc" -ge 2 ]]; then
  echo "B 輪 pytest 執行異常 (exit $b_rc)" >&2
  exit "$b_rc"
fi

# 5. 產生身分 JSON (identity-a and identity-b)
python3 -c "
import hashlib, json, subprocess, sys
from pathlib import Path

root = Path('$REPO_ROOT')
version = (root / 'VERSION').read_text(encoding='utf-8').strip()

def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def dir_sha256(p: Path) -> str:
    if not p.is_dir():
        return hashlib.sha256(b'').hexdigest()
    h = hashlib.sha256()
    for f in sorted(p.rglob('*')):
        if f.is_file():
            h.update(f.relative_to(p).as_posix().encode())
            h.update(f.read_bytes())
    return h.hexdigest()

# 讀取 manifest
version_dir = root / 'target' / 'mkbundle-dev' / 'versions' / version
manifest_path = version_dir / 'release-manifest.json'
if not manifest_path.is_file():
    manifest_path = root / 'target' / 'mkbundle-dev' / 'release-manifest.json'

manifest = json.loads(manifest_path.read_text(encoding='utf-8'))

git_rev = manifest.get('git_revision') or subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
contract_rev = manifest.get('contract_revision', git_rev)
daemon_tree = manifest.get('components', {}).get('daemon', {}).get('package', {}).get('tree_sha256', '0'*64)
daemon_runtime = manifest.get('components', {}).get('daemon', {}).get('runtime', {}).get('sha256', '0'*64)

# CLI A: Python 入口
py_cli = str((root / 'serialwrap').resolve()) if (root / 'serialwrap').is_file() else sys.executable
py_sha = file_sha256(Path(py_cli))

# CLI B: Rust bundle
rust_cli = str(Path('$BUNDLE_CLI').resolve())
rust_sha = file_sha256(Path(rust_cli))

# 計算 tool_revision 與 config_digest
bb_tree_sha = dir_sha256(root / 'tests' / 'contract_blackbox')
tool_rev = f'{git_rev}+{bb_tree_sha}'
cfg_digest = dir_sha256(root / 'target' / 'mkbundle-dev')

# 建立 evidence_files 清單
evidence_files = []
for p in (Path('$A_XML'), Path('$B_XML'), Path('$RELEASE_CHECK'), Path('$REQUIRED_CASES')):
    if p.is_file():
        evidence_files.append({'path': str(p.resolve()), 'sha256': file_sha256(p)})

id_a = {
    'cli_path': py_cli,
    'cli_sha256': py_sha,
    'cli_implementation': 'python',
    'daemon_version': version,
    'daemon_git_revision': git_rev,
    'daemon_tree_sha256': daemon_tree,
    'daemon_runtime_sha256': daemon_runtime,
    'contract_revision': contract_rev,
    'case_list_sha256': '',
    'tool_revision': tool_rev,
    'platform': 'linux',
    'config_digest': cfg_digest,
    'dut_identity': 'FakeTarget',
    'evidence_files': evidence_files,
}
Path('$ID_A').write_text(json.dumps(id_a, indent=2) + '\n', encoding='utf-8')

id_b = {
    'cli_path': rust_cli,
    'cli_sha256': rust_sha,
    'cli_implementation': 'rust',
    'daemon_version': version,
    'daemon_git_revision': git_rev,
    'daemon_tree_sha256': daemon_tree,
    'daemon_runtime_sha256': daemon_runtime,
    'contract_revision': contract_rev,
    'case_list_sha256': '',
    'tool_revision': tool_rev,
    'platform': 'linux',
    'config_digest': cfg_digest,
    'dut_identity': 'FakeTarget',
    'evidence_files': evidence_files,
}
Path('$ID_B').write_text(json.dumps(id_b, indent=2) + '\n', encoding='utf-8')
"


# 6. 執行 ab_compare.py
REPORT_OUT="$REPO_ROOT/target/ab-report/$RUN_ID.json"
echo "=== 執行 A/B 比較器 ==="
python3 "$REPO_ROOT/scripts/rust/ab_compare.py" \
  --a "$A_XML" \
  --b "$B_XML" \
  --a-label "python" \
  --b-label "rust-1a" \
  --out "$REPORT_OUT" \
  --identity-a "$ID_A" \
  --identity-b "$ID_B" \
  --applicability "$REPO_ROOT/tests/contract_blackbox/data/applicability.json" \
  --required "$REQUIRED_CASES"

echo "A/B 比較完成：$REPORT_OUT"
