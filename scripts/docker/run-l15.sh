#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/common.sh"
COMPOSE_FILE="$REPO_ROOT/tools/docker/compose.l15.yaml"
SUITE=""
if (($#)); then SUITE="$1"; shift; fi
RUN_ID="$(printenv SW_DOCKER_RUN_ID 2>/dev/null || true)"
if [[ -z "$RUN_ID" ]]; then RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"; fi
RUN_ID="$(printf '%s' "$RUN_ID" | tr '[:upper:]' '[:lower:]' | tr -c '[:alnum:]_-' '-')"
DRY_RUN=0
BUNDLE=""
OUT=""
CASE_ID=""
usage() {
    echo "用法：$0 <pytest-physical-uart|func-test-bridge|realhw-p0p1|regression-nondestructive> [--dry-run] [--bundle DIR] [--out DIR] [--case ID]" >&2
}
while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --bundle) (($# >= 2)) || { usage; exit 2; }; BUNDLE="$2"; shift 2 ;;
        --out) (($# >= 2)) || { usage; exit 2; }; OUT="$2"; shift 2 ;;
        --case) (($# >= 2)) || { usage; exit 2; }; CASE_ID="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "不支援的參數：$1" >&2; usage; exit 2 ;;
    esac
done
case "$SUITE" in
    pytest-physical-uart|func-test-bridge|realhw-p0p1|regression-nondestructive) ;;
    *) usage; exit 2 ;;
esac
if [[ -n "$BUNDLE" && "$SUITE" == "pytest-physical-uart" ]]; then
    echo "pytest-physical-uart 直接測試 Python 測試樹，不會啟動 bundle；請移除 --bundle 或改用會呼叫 serialwrap CLI 的 suite" >&2
    exit 2
fi
PROJECT="sw-l15-$RUN_ID"
SERIALWRAP_IMAGE="serialwrap-l15:$RUN_ID"
DUT_IMAGE="serialwrap-dut-sim-l15:$RUN_ID"
SYSTEMD_IMAGE="serialwrap-systemd:$RUN_ID"
if [[ -z "$OUT" ]]; then OUT="$REPO_ROOT/evidence/stage0/docker/run-l15-$RUN_ID"; fi
OUT="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$OUT")"
if [[ -s "$OUT/l15-docker-report.json" ]]; then
    echo "拒絕覆寫既有證據：$OUT/l15-docker-report.json" >&2
    exit 2
fi
if [[ -n "$BUNDLE" ]]; then
    BUNDLE="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$BUNDLE")"
    if [[ ! -d "$BUNDLE" ]]; then echo "bundle 目錄不存在：$BUNDLE" >&2; exit 2; fi
    PYTHONPATH="$REPO_ROOT" python3 - "$BUNDLE" <<'PY'
import os
import pathlib
import sys

from realhw.deployed_content import tree_digests
from sw_core.release_manifest import file_sha256, load_release_manifest

root = pathlib.Path(sys.argv[1]).resolve()
manifest_path = root / "release-manifest.json"
try:
    manifest = load_release_manifest(str(manifest_path))
except ValueError as exc:
    print(f"bundle manifest 無效：{exc}", file=sys.stderr)
    raise SystemExit(2)
for name in ("serialwrap", "serialwrapd"):
    binary = root / "bin" / name
    if not binary.is_file() or not os.access(binary, os.X_OK):
        print(f"bundle 缺少可執行檔：{binary}", file=sys.stderr)
        raise SystemExit(2)
if (manifest["components"]["cli"].get("implementation") != "rust"
        or manifest["components"]["daemon"].get("implementation") != "rust"):
    print("bundle CLI／daemon 未標記為 Rust 實作", file=sys.stderr)
    raise SystemExit(2)
for rel, expected in (
    (manifest["components"]["cli"]["path"], manifest["components"]["cli"]["sha256"]),
    (manifest["components"]["daemon"]["launcher"]["path"],
     manifest["components"]["daemon"]["launcher"]["sha256"]),
):
    candidate = (root / rel).resolve()
    if root not in candidate.parents or not candidate.is_file() or file_sha256(str(candidate)) != expected:
        print(f"bundle manifest artifact 摘要不符：{rel}", file=sys.stderr)
        raise SystemExit(2)
tree_digests(root)
PY
fi

print_command() {
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
}
if ((DRY_RUN)); then
    print_command docker compose -f "$COMPOSE_FILE" -p "$PROJECT" build serialwrap dut-sim
    print_command docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile systemd-user build systemd
    printf '+ docker compose -f %q -p %q run --rm --no-deps -T -v %q:%q -v %q:%q' \
        "$COMPOSE_FILE" "$PROJECT" /tmp/sw-l15-run /tmp/sw-l15-run "$OUT" "$OUT"
    if [[ -n "$BUNDLE" ]]; then printf ' -v %q:/opt/sw-bundle:ro' "$BUNDLE"; fi
    printf ' serialwrap python3 scripts/docker/run_l15_suite.py\n'
    case "$SUITE" in
        func-test-bridge)
            print_command uart-stdio-bridge selftest --endpoint posix-pty --out "$OUT/func-test/selftest"
            if [[ -n "$CASE_ID" && -f "$REPO_ROOT/func-test/scenarios/$CASE_ID.yaml" ]]; then
                print_command python3 func-test/runner.py --mode bridge --dut-mode docker --dut-image "$DUT_IMAGE" --scenarios --case "$CASE_ID" --selftest-report "$OUT/func-test/selftest/selftest-report.json" --report-dir "$OUT/func-test/scenario"
            else
                if [[ -n "$CASE_ID" ]]; then
                    print_command python3 func-test/runner.py --mode bridge --dut-mode docker --dut-image "$DUT_IMAGE" --case "$CASE_ID" --selftest-report "$OUT/func-test/selftest/selftest-report.json" --report-dir "$OUT/func-test/case"
                else
                    print_command python3 func-test/runner.py --mode bridge --dut-mode docker --dut-image "$DUT_IMAGE" --selftest-report "$OUT/func-test/selftest/selftest-report.json" --report-dir "$OUT/func-test/case"
                fi
            fi
            if [[ -z "$CASE_ID" ]]; then
                print_command python3 func-test/runner.py --mode bridge --dut-mode docker --dut-image "$DUT_IMAGE" --scenarios --selftest-report "$OUT/func-test/selftest/selftest-report.json" --report-dir "$OUT/func-test/scenario"
            fi
            ;;
        pytest-physical-uart)
            print_command python3 -m pytest -q -m posix_pty tests/ --junitxml "$OUT/pytest/junit.xml"
            ;;
        realhw-p0p1)
            print_command python3 -m realhw --tier p0,p1 --config /tmp/sw-l15-run/realhw-config.json --report-dir "$OUT/realhw"
            ;;
        regression-nondestructive)
            print_command testpilot --root /opt/serialwrap run serialwrap_regression --case '<each-nondestructive-case>'
            ;;
    esac
    echo "+ report metadata --evidence-layer L1.5-docker --producer run-l15.sh --topology docker-2c --uart-kind pty-fake"
    echo "+ container env SERIALWRAP_BY_ID_DIR=/run/sw-fake/by-id SERIALWRAP_BY_PATH_DIR=/run/sw-fake/by-path SW_UART_KIND=pty-fake"
    if [[ -n "$BUNDLE" ]]; then echo "+ bundle mount=$BUNDLE:/opt/sw-bundle:ro；啟動內容由 manifest 與實際檔案摘要核對"; fi
    exit 0
fi

sw_prepare_empty_out "$OUT"
WORKSPACE="$(mktemp -d /tmp/sw-l15.XXXXXX)"
export SW_CONTAINER_UID="$(id -u)"
export SW_CONTAINER_GID="$(id -g)"
export SW_DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
export SW_SERIALWRAP_IMAGE="$SERIALWRAP_IMAGE"
export SW_DUT_SERVICE_IMAGE="$DUT_IMAGE"
export SW_SYSTEMD_IMAGE="$SYSTEMD_IMAGE"
export SW_DUT_IMAGE="$DUT_IMAGE"
export SW_OUT="$OUT"
export SW_REPORT="$OUT/l15-docker-report.json"
export SW_SUITE="$SUITE"
export SW_CASE="$CASE_ID"
export SW_HOST_UID="$(id -u)"
export SW_HOST_GID="$(id -g)"
export SW_DOCKER_RUN_ID="od15-$RUN_ID"

if [[ "$SUITE" == "regression-nondestructive" ]]; then
    CONTAINER_CLI=/usr/local/bin/serialwrap
    if [[ -n "$BUNDLE" ]]; then CONTAINER_CLI=/opt/sw-bundle/bin/serialwrap; fi
    python3 - "$WORKSPACE/testbed.yaml" "$CONTAINER_CLI" <<'PY'
import json
import sys

data = {
    "serialwrap_exe": sys.argv[2],
    "bench_lock_path": "/tmp/sw-l15-run/isolation/state/regression-bench.lock",
    "human_frontend": "tmux",
    "cross_os_probe_argv": [],
    "allow_destructive": False,
    "tmux_prefix": "swl15reg",
    "boards": [
        {"com": f"COM{index}", "alias": f"docker-fake-{index}",
         "serial": f"SW-FAKE-{index}", "platform": "prpl"}
        for index in range(2)
    ],
    "timeouts": {"ready_wait_s": 120, "boot_wait_s": 120, "cmd_timeout_s": 12},
}
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
    stream.write("\n")
PY
fi

cleanup() {
    docker ps -aq --filter "name=sw-dut-$SW_DOCKER_RUN_ID" | xargs -r docker rm -f >/dev/null 2>&1 || true
    docker compose -f "$COMPOSE_FILE" -p "$PROJECT" down --remove-orphans --rmi all >/dev/null 2>&1 || true
    rm -rf -- "$WORKSPACE"
}
trap cleanup EXIT INT TERM

docker compose -f "$COMPOSE_FILE" -p "$PROJECT" build serialwrap dut-sim
docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile systemd-user build systemd
compose_args=(-f "$COMPOSE_FILE" -p "$PROJECT" run --rm --no-deps -T
    -v "$WORKSPACE:/tmp/sw-l15-run" -v "$OUT:$OUT"
    -e SW_OUT -e SW_REPORT -e SW_SUITE -e SW_CASE -e SW_DUT_IMAGE
    -e SW_SERIALWRAP_IMAGE -e SW_SYSTEMD_IMAGE -e SW_HOST_UID -e SW_HOST_GID)
compose_args+=(-e SW_DOCKER_RUN_ID)
if [[ "$SUITE" == "regression-nondestructive" ]]; then
    compose_args+=(-v "$WORKSPACE/testbed.yaml:/opt/serialwrap/regression/serialwrap_regression/testbed.yaml:ro")
fi
if [[ -n "$BUNDLE" ]]; then
    export SW_BUNDLE=/opt/sw-bundle
    compose_args+=(-v "$BUNDLE:/opt/sw-bundle:ro" -e SW_BUNDLE)
fi
if docker compose "${compose_args[@]}" serialwrap python3 scripts/docker/run_l15_suite.py; then
    exit 0
else
    exit $?
fi
