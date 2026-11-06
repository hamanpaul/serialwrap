#!/usr/bin/env bash
# A/B 黑箱執行腳本（P3-T18）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
PROFILE="debug"
L1_OUT=""

# ── PID namespace 隔離（#s1x）──────────────────────────────────────────────
# 宿主上的生產 serialwrapd（pipx／systemd）與容器 daemon 會被 CLI 的 /proc 多開偵測看見，
# 使 rig 誤判 DEVICE_STILL_HELD／DAEMON_ALREADY_RUNNING_ELSEWHERE。故整段 A 輪、釋放確認、B 輪
# 於同一個 user+pid+mount namespace 內執行（SW_PIDNS_OUTER 記錄外層 pid namespace 供內層比對）。
# 不可用時以 exit 3 拒絕（判定 INVALID，不以 PASS 放行，也不退回 --force-spawn）。
if [[ -z "${SW_PIDNS_OUTER:-}" ]]; then
  if ! command -v unshare >/dev/null 2>&1 || ! unshare -Upf --mount-proc --map-current-user true >/dev/null 2>&1; then
    echo "ab_blackbox.sh: INVALID（環境能力不足）：無法以非特權 unshare 建立 user+pid+mount namespace；本次 run 不得作為 A/B 證據" >&2
    exit 3
  fi
  OUTER_PIDNS="$(readlink "/proc/$$/ns/pid")"
  # RUN_ID 的 pid 後綴須取外層 pid：進入 namespace 後 $$ 恆為 1，會使所有 run 都以 _1 結尾。
  SW_RUN_STAMP="$(date +%Y%m%d_%H%M%S)_$$"
  exec env SW_PIDNS_OUTER="$OUTER_PIDNS" SW_RUN_STAMP="$SW_RUN_STAMP" unshare -Upf --mount-proc --map-current-user bash "$0" "$@"
fi

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

if [[ -n "${SW_TEST_COM_PAIR:-}" && "$SW_TEST_COM_PAIR" != "COM20:COM21" ]]; then
  echo "ab_blackbox.sh: 只允許 Stage 1A DUT-side COM pair COM20:COM21" >&2
  exit 2
fi

# 每次 run 於自己的目錄建 bundle（不使用共享的 target/mkbundle-dev）：舊版留下的無標記目錄不會讓 A/B 腳本中途 exit 2，
# 且 A/B 身分檔（identity）所記的 bundle 必與本次 run 同源。
RUN_ID="${SW_RUN_STAMP:-$(date +%Y%m%d_%H%M%S)_$$}"
RUN_DIR="$REPO_ROOT/target/ab-runs/$RUN_ID"
mkdir -p "$RUN_DIR" "$REPO_ROOT/target/ab-report"
BUNDLE_OUT="$RUN_DIR/bundle"
"$REPO_ROOT/scripts/rust/mkbundle.sh" --dev --profile "$PROFILE" --out "$BUNDLE_OUT"

BUNDLE_CLI="$BUNDLE_OUT/bin/serialwrap"
if [[ ! -f "$BUNDLE_CLI" ]]; then
  echo "錯誤: bundle CLI 不存在: $BUNDLE_CLI" >&2
  exit 1
fi
# B 輪 daemon 必須明定為本 run bundle 的 serialwrapd（未設時 harness 退回 repo Python daemon，
# deployed_content 記 daemon_env_missing，L1 證據不可用）
BUNDLE_DAEMON="$BUNDLE_OUT/bin/serialwrapd"
if [[ ! -f "$BUNDLE_DAEMON" ]]; then
  echo "錯誤: bundle daemon 不存在: $BUNDLE_DAEMON" >&2
  exit 1
fi

cd "$REPO_ROOT"

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
A_CONFIG="$RUN_DIR/a-config.jsonl"
B_CONFIG="$RUN_DIR/b-config.jsonl"
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

# 1.5 preflight（#s1x）：必須位於隔離 pid namespace，且此 namespace 內不得看見任何非本 run 的 serialwrapd。
#     任一不成立即判定 INVALID（preflight.json 記錄 result／原因），不進入 A／B 輪。
PREFLIGHT="$RUN_DIR/preflight.json"
set +e
python3 -I - "$PREFLIGHT" <<'PY'
import json, os, sys
from pathlib import Path

out = Path(sys.argv[1])
outer = os.environ.get("SW_PIDNS_OUTER", "")
try:
    current = os.readlink("/proc/self/ns/pid")
except OSError:
    current = None
isolated = bool(outer) and current is not None and current != outer
foreign = []
me = {os.getpid(), os.getppid()}
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit() or int(entry.name) in me:
        continue
    try:
        parts = (entry / "cmdline").read_bytes().split(b"\0")
    except OSError:
        continue
    names = {os.path.basename(p.decode("utf-8", errors="replace")) for p in parts if p}
    if names & {"serialwrapd", "serialwrapd.py"}:
        foreign.append({"pid": int(entry.name), "cmdline": b" ".join(p for p in parts if p).decode("utf-8", errors="replace")[:200]})
problems = []
if not isolated:
    problems.append("未位於隔離 pid namespace（SW_PIDNS_OUTER 缺漏或與目前相同）")
if foreign:
    problems.append("隔離 namespace 內仍可見非本 run 的 serialwrapd")
result = {
    "result": "INVALID" if problems else "OK",
    "pid_namespace_isolated": isolated,
    "outer_pidns": outer,
    "current_pidns": current,
    "foreign_serialwrapd": foreign,
    "problems": problems,
}
out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
sys.exit(4 if problems else 0)
PY
preflight_rc=$?
set -e
if [[ "$preflight_rc" -ne 0 ]]; then
  echo "preflight INVALID（見 $PREFLIGHT）：本 run 不得作為 A/B 證據" >&2
  exit "$preflight_rc"
fi

# 2. 執行 A 輪（Python 基線）
echo "=== 開始 A 輪（Python 基線）==="
touch "$A_PIDS"
set +e
env -u SERIALWRAP_CLI_EXE -u SERIALWRAP_DAEMON_EXE -u SERIALWRAP_AB_LABEL \
  SW_AB_PIDLOG="$A_PIDS" SW_AB_CONFIGLOG="$A_CONFIG" SW_AB_STOPLOG="$RUN_DIR/a.stoplog.ndjson" python3 -m pytest tests/contract_blackbox \
  -m "core_portable or posix_only" \
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
  SERIALWRAP_DAEMON_EXE="$BUNDLE_DAEMON"
  SW_BUNDLE="$BUNDLE_OUT"   # test_cli_mgmt_update／doctor 以 SW_BUNDLE 找本次 run 的 bundle（不得回退到未建置的 target/mkbundle-pytest）
  SERIALWRAP_AB_LABEL="rust-1a"
  SW_AB_PIDLOG="$B_PIDS"
  SW_AB_CONFIGLOG="$B_CONFIG"
  # S1X Y3：daemon stop 各階段耗時與升級（SIGTERM／SIGKILL）紀錄，teardown「stop 後仍存活」時供歸因。
  SW_AB_STOPLOG="$RUN_DIR/b.stoplog.ndjson"
)
if [[ -n "$L1_OUT" ]]; then
  b_env+=(SW_RESOURCE_REPORT="$B_RESOURCE_REPORT")
fi

set +e
env "${b_env[@]}" python3 -m pytest tests/contract_blackbox \
  -m "core_portable or posix_only" \
  --junitxml="$B_XML"
b_rc=$?
set -e
if [[ "$b_rc" -ge 2 ]]; then
  echo "B 輪 pytest 執行異常 (exit $b_rc)" >&2
  exit "$b_rc"
fi

# 5. 產生身分 JSON (identity-a and identity-b)
python3 -c "
import hashlib, importlib.util, json, os, subprocess, sys
from pathlib import Path

root = Path('$REPO_ROOT')
_outer_pidns = os.environ.get('SW_PIDNS_OUTER', '')
PIDNS_ISOLATED = bool(_outer_pidns) and os.readlink('/proc/self/ns/pid') != _outer_pidns
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
version_dir = Path('$BUNDLE_OUT') / 'versions' / version
manifest_path = version_dir / 'release-manifest.json'
if not manifest_path.is_file():
    manifest_path = Path('$BUNDLE_OUT') / 'release-manifest.json'

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

# 計算 tool_revision；config_digest 由各輪實際建立的設定紀錄彙整（config_digest.py，逐案例摘要）
bb_tree_sha = dir_sha256(root / 'tests' / 'contract_blackbox')
tool_rev = f'{git_rev}+{bb_tree_sha}'
bundle_tree = dir_sha256(Path('$BUNDLE_OUT'))
cd_spec = importlib.util.spec_from_file_location('ab_config_digest', root / 'tests' / 'contract_blackbox' / 'config_digest.py')
cd_mod = importlib.util.module_from_spec(cd_spec)
cd_spec.loader.exec_module(cd_mod)
cfg_a = cd_mod.aggregate(Path('$A_CONFIG'))
cfg_b = cd_mod.aggregate(Path('$B_CONFIG'))
Path('$RUN_DIR/config-digest-a.json').write_text(json.dumps(cfg_a, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
Path('$RUN_DIR/config-digest-b.json').write_text(json.dumps(cfg_b, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')

# 建立 evidence_files 清單（共用部分；設定紀錄與摘要檔依輪分列）
evidence_common = []
for p in (Path('$A_XML'), Path('$B_XML'), Path('$RELEASE_CHECK'), Path('$REQUIRED_CASES')):
    if p.is_file():
        evidence_common.append({'path': str(p.resolve()), 'sha256': file_sha256(p)})
evidence_a = evidence_common + [{'path': str(p.resolve()), 'sha256': file_sha256(p)} for p in (Path('$A_CONFIG'), Path('$RUN_DIR/config-digest-a.json'))]
evidence_b = evidence_common + [{'path': str(p.resolve()), 'sha256': file_sha256(p)} for p in (Path('$B_CONFIG'), Path('$RUN_DIR/config-digest-b.json'))]

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
    'config_digest': cfg_a['digest'],
    'bundle_tree_sha256': bundle_tree,
    'dut_identity': 'FakeTarget',
    'pid_namespace_isolated': PIDNS_ISOLATED,
    'evidence_files': evidence_a,
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
    'config_digest': cfg_b['digest'],
    'bundle_tree_sha256': bundle_tree,
    'dut_identity': 'FakeTarget',
    'pid_namespace_isolated': PIDNS_ISOLATED,
    'evidence_files': evidence_b,
}
Path('$ID_B').write_text(json.dumps(id_b, indent=2) + '\n', encoding='utf-8')
"


# 6. 執行 ab_compare.py
REPORT_OUT="$REPO_ROOT/target/ab-report/$RUN_ID.json"
echo "=== 執行 A/B 比較器 ==="
# 比較器回非 0（例如 baseline-fail）時仍須輸出報告路徑，讓 exit driver 交給 gate 依報告內容判定；
# 不得因 set -e 提前結束而使 gate 誤判「缺少 ab-report」（S1X Y2）。exit code 照傳。
compare_rc=0
python3 "$REPO_ROOT/scripts/rust/ab_compare.py" \
  --a "$A_XML" \
  --b "$B_XML" \
  --a-label "python" \
  --b-label "rust-1a" \
  --out "$REPORT_OUT" \
  --identity-a "$ID_A" \
  --identity-b "$ID_B" \
  --applicability "$REPO_ROOT/tests/contract_blackbox/data/applicability.json" \
  --required "$REQUIRED_CASES" || compare_rc=$?

if [[ -f "$REPORT_OUT" ]]; then
  if (( compare_rc == 0 )); then
    echo "A/B 比較完成：$REPORT_OUT"
  else
    echo "A/B 比較器 exit $compare_rc（判定見報告）：$REPORT_OUT"
  fi
else
  echo "A/B 比較器未產生報告（exit $compare_rc）" >&2
fi
exit "$compare_rc"
