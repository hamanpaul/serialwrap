#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/tools/docker/compose.l15.yaml"
source "$SCRIPT_DIR/common.sh"
MODE=systemd-user
SCENARIO=hold
DRY_RUN=0
OUT=""
BUNDLE_OLD=""
BUNDLE_NEW=""
while (($#)); do
    case "$1" in
        --mode)
            (($# >= 2)) || { echo "--mode 需要參數" >&2; exit 2; }
            MODE="$2"
            shift 2
            ;;
        --scenario)
            (($# >= 2)) || { echo "--scenario 需要參數" >&2; exit 2; }
            SCENARIO="$2"
            shift 2
            ;;
        --bundle-old)
            (($# >= 2)) || { echo "--bundle-old 需要目錄" >&2; exit 2; }
            BUNDLE_OLD="$2"
            shift 2
            ;;
        --bundle-new)
            (($# >= 2)) || { echo "--bundle-new 需要目錄" >&2; exit 2; }
            BUNDLE_NEW="$2"
            shift 2
            ;;
        --dry-run) DRY_RUN=1; shift ;;
        --out)
            (($# >= 2)) || { echo "--out 需要目錄" >&2; exit 2; }
            OUT="$2"
            shift 2
            ;;
        -h|--help)
            echo "用法：$0 [--mode systemd-user|systemd-system] [--scenario hold|update-exit1] [--bundle-old DIR --bundle-new DIR] [--dry-run] [--out DIR]"
            exit 0
            ;;
        *) echo "不支援的參數：$1" >&2; exit 2 ;;
    esac
done

resolve_bundle_manifest() {
    python3 - "$1" <<'PY'
import json
import pathlib
import sys

bundle = pathlib.Path(sys.argv[1])
current_path = bundle / "bin" / "current-release.json"
if current_path.is_file():
    current = json.loads(current_path.read_text(encoding="utf-8"))
    version = current.get("bundle_version")
    if not isinstance(version, str) or not version:
        raise SystemExit(f"current-release.json 缺少 bundle_version：{current_path}")
    manifest = bundle / "versions" / version / "release-manifest.json"
elif (bundle / "release-manifest.json").is_file():
    manifest = bundle / "release-manifest.json"
else:
    manifests = sorted((bundle / "versions").glob("*/release-manifest.json"))
    if len(manifests) != 1:
        raise SystemExit(f"無法唯一定位 bundle manifest：{bundle}")
    manifest = manifests[0]
if not manifest.is_file():
    raise SystemExit(f"bundle 缺少 manifest：{manifest}")
print(manifest)
PY
}

case "$MODE" in
    systemd-user|systemd-system) ;;
    *) echo "不支援的 systemd 模式：$MODE" >&2; exit 2 ;;
esac
case "$SCENARIO" in
    hold) ;;
    update-exit1)
        [[ "$MODE" == "systemd-user" ]] || { echo "update-exit1 目前只支援 systemd-user" >&2; exit 2; }
        [[ -d "$BUNDLE_OLD" && -d "$BUNDLE_NEW" ]] || { echo "update-exit1 必須提供 --bundle-old 與 --bundle-new 目錄" >&2; exit 2; }
        BUNDLE_OLD="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$BUNDLE_OLD")"
        BUNDLE_NEW="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$BUNDLE_NEW")"
        BUNDLE_OLD_MANIFEST="$(resolve_bundle_manifest "$BUNDLE_OLD")"
        BUNDLE_NEW_MANIFEST="$(resolve_bundle_manifest "$BUNDLE_NEW")"
        python3 - "$BUNDLE_OLD_MANIFEST" "$BUNDLE_NEW_MANIFEST" <<'PY'
import json
import pathlib
import sys

for path, expected in zip(sys.argv[1:], ("0.4.0", "0.4.1"), strict=True):
    manifest = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if manifest.get("bundle_version") != expected:
        raise SystemExit(f"bundle 版本應為 {expected}：{path}")
PY
        ;;
    *) echo "不支援的 systemd 情境：$SCENARIO" >&2; exit 2 ;;
esac
# P1／P1-OC 前置只約束長跑與監管 runner（計畫 §0 前置表）；systemd 生命週期驗證照常執行。

RUN_ID="$(printenv SW_DOCKER_RUN_ID 2>/dev/null || true)"
if [[ -z "$RUN_ID" ]]; then RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"; fi
RUN_ID="$(printf '%s' "$RUN_ID" | tr '[:upper:]' '[:lower:]' | tr -c '[:alnum:]_-' '-')"
PROJECT="sw-systemd-$MODE-$RUN_ID"
CONTAINER_NAME="$PROJECT-systemd"
CONTAINER_ID=""
SYSTEMD_IMAGE="serialwrap-systemd:$RUN_ID"
SERIALWRAP_IMAGE="serialwrap-l15:$RUN_ID"
DUT_SERVICE_IMAGE="serialwrap-dut-sim-l15:$RUN_ID"
if [[ -z "$OUT" ]]; then OUT="$REPO_ROOT/evidence/stage0/docker/systemd-${MODE#systemd-}"; fi
OUT="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$OUT")"
if [[ -s "$OUT/l15-docker-report.json" ]]; then
    echo "拒絕覆寫既有證據：$OUT/l15-docker-report.json" >&2
    exit 2
fi

print_command() {
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
}
compose_run=(docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile "$MODE" run -d --no-deps
    --name "$CONTAINER_NAME" -v "$OUT/volumes:/tmp/sw-t17" -v "$OUT:/evidence" -v "$OUT:$OUT")
if [[ "$SCENARIO" == "update-exit1" ]]; then
    compose_run+=(-v "$BUNDLE_OLD:/tmp/sw-bundle-old:ro" -v "$BUNDLE_NEW:/tmp/sw-bundle-new:ro")
fi
compose_run+=(systemd /sbin/init)
if ((DRY_RUN)); then
    print_command docker compose -f "$COMPOSE_FILE" -p "$PROJECT" build serialwrap dut-sim
    print_command docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile "$MODE" build systemd
    print_command "${compose_run[@]}"
    if [[ "$MODE" == "systemd-user" ]]; then
        print_command docker exec "$CONTAINER_NAME" runuser -u swtest -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user show serialwrap -p Restart -p RefuseManualStart
        print_command docker exec "$CONTAINER_NAME" runuser -u swtest -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus journalctl --user -u serialwrap -o cat --no-pager
    else
        print_command docker exec "$CONTAINER_NAME" systemctl show serialwrap -p Restart -p RefuseManualStart
        print_command docker exec "$CONTAINER_NAME" journalctl -u serialwrap -o cat --no-pager
    fi
    echo '+ assert Restart=no RefuseManualStart=yes; hold semantics are harness evidence'
    echo "+ systemd unit 由 serialwrap setup 安裝；P1 SC-12 reconcile 由產品 CLI 執行"
    echo "+ evidence_layer=L1.5-docker producer=run-systemd.sh topology=docker-2c uart_kind=pty-fake"
    if [[ "$SCENARIO" == "update-exit1" ]]; then
        echo '+ run systemd update-exit1 using mounted 0.4.0／0.4.1 dev bundles'
    fi
    exit 0
fi

sw_prepare_empty_out "$OUT"
mkdir -p "$OUT/volumes"
export SW_SYSTEMD_IMAGE="$SYSTEMD_IMAGE"
export SW_SERIALWRAP_IMAGE="$SERIALWRAP_IMAGE"
export SW_DUT_SERVICE_IMAGE="$DUT_SERVICE_IMAGE"
cleanup() {
    docker ps -aq --filter "name=sw-dut-od15-$RUN_ID" | xargs -r docker rm -f >/dev/null 2>&1 || true
    if [[ -n "$CONTAINER_ID" ]]; then
        docker exec "$CONTAINER_ID" /bin/bash -lc \
            "systemctl stop serialwrap 2>/dev/null || true; runuser -u swtest -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user stop serialwrap 2>/dev/null || true" \
            >/dev/null 2>&1 || true
        docker rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true
    fi
    docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile "$MODE" down --remove-orphans --rmi all >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

cd "$REPO_ROOT"
docker compose -f "$COMPOSE_FILE" -p "$PROJECT" build serialwrap dut-sim
docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile "$MODE" build systemd
CONTAINER_ID="$("${compose_run[@]}")"

wait_systemd_ready() {
    local state
    for _ in $(seq 1 600); do
        state="$(docker exec "$CONTAINER_ID" systemctl is-system-running 2>/dev/null || true)"
        if [[ "$state" == running || "$state" == degraded ]]; then
            return 0
        fi
        sleep 0.2
    done
    echo "systemd 容器未在 120 秒內完成開機" >&2
    docker exec "$CONTAINER_ID" systemctl --failed --no-pager >&2 || true
    return 1
}

wait_systemd_ready
container_args=(docker exec -e SW_HOST_UID="$(id -u)" -e SW_HOST_GID="$(id -g)"
    -e SW_SERIALWRAP_IMAGE="$SERIALWRAP_IMAGE" -e SW_DUT_IMAGE="$DUT_SERVICE_IMAGE"
    -e SW_SYSTEMD_IMAGE="$SYSTEMD_IMAGE" -e PYTHONPATH=/opt/serialwrap
    -e SW_HOST_EVIDENCE_DIR="$OUT" -i "$CONTAINER_ID"
    /opt/serialwrap/.venv/bin/python scripts/docker/run_systemd_container.py
    --mode "$MODE" --run-id "od15-$RUN_ID" --scenario "$SCENARIO")
if [[ "$SCENARIO" == "update-exit1" ]]; then
    container_args+=(--bundle-old /tmp/sw-bundle-old --bundle-new /tmp/sw-bundle-new)
fi
"${container_args[@]}"
