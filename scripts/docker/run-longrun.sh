#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/tools/docker/compose.l15.yaml"
source "$SCRIPT_DIR/common.sh"
HOURS=""
DRY_RUN=0
OUT=""
while (($#)); do
    case "$1" in
        --hours) (($# >= 2)) || { echo "--hours 需要正整數" >&2; exit 2; }; HOURS="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --out) (($# >= 2)) || { echo "--out 需要目錄" >&2; exit 2; }; OUT="$2"; shift 2 ;;
        -h|--help) echo "用法：$0 --hours N [--dry-run] [--out DIR]"; exit 0 ;;
        *) echo "不支援的參數：$1" >&2; exit 2 ;;
    esac
done
if [[ ! "$HOURS" =~ ^[1-9][0-9]*$ ]]; then echo "--hours 必須為正整數" >&2; exit 2; fi
sw_require_p1_oc "$REPO_ROOT"

RUN_ID="$(printenv SW_DOCKER_RUN_ID 2>/dev/null || true)"
if [[ -z "$RUN_ID" ]]; then RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"; fi
RUN_ID="$(printf '%s' "$RUN_ID" | tr '[:upper:]' '[:lower:]' | tr -c '[:alnum:]_-' '-')"
PROJECT="sw-longrun-$RUN_ID"
SERIALWRAP_IMAGE="serialwrap-l15:$RUN_ID"
DUT_IMAGE="serialwrap-dut-sim-l15:$RUN_ID"
SYSTEMD_IMAGE="serialwrap-systemd:$RUN_ID"
if [[ -z "$OUT" ]]; then OUT="$REPO_ROOT/evidence/stage0/docker/longrun-linux"; fi
OUT="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$OUT")"
VOLUME_ROOT="$OUT/volumes"
if [[ -e "$OUT/l15-docker-report.json" && -s "$OUT/l15-docker-report.json" ]]; then
    echo "拒絕覆寫既有證據：$OUT/l15-docker-report.json" >&2
    exit 2
fi

print_command() { printf '+ '; printf '%q ' "$@"; printf '\n'; }
if ((DRY_RUN)); then
    print_command docker compose -f "$COMPOSE_FILE" -p "$PROJECT" build serialwrap dut-sim
    print_command docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile systemd-user build systemd
    print_command docker compose -f "$COMPOSE_FILE" -p "$PROJECT" run --rm --no-deps -T \
        -v "$VOLUME_ROOT:/tmp/sw-t17" -v "$OUT:/evidence" -v "$OUT:$OUT" -e "SW_HOST_EVIDENCE_DIR=$OUT" \
        -e "SW_DOCKER_RUN_ID=od15-$RUN_ID" -e "SW_HOURS=$HOURS" -e "SW_DUT_IMAGE=$DUT_IMAGE" \
        serialwrap python3 scripts/docker/run_longrun_container.py
    echo "+ gate=BC-01..05,SC-34,SC-35,DRAIN_STALLED；不足時在 dry-run 前 exit 3"
    echo "+ evidence_layer=L1.5-docker producer=run-longrun.sh topology=docker-2c uart_kind=pty-fake"
    exit 0
fi

sw_prepare_empty_out "$OUT"
mkdir -p "$VOLUME_ROOT"
for subdir in state run wal config blog events.d events-rt by-id by-path isolation; do
    mkdir -p "$VOLUME_ROOT/$subdir"
done
export SW_CONTAINER_UID="$(id -u)"
export SW_CONTAINER_GID="$(id -g)"
export SW_DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
export SW_SERIALWRAP_IMAGE="$SERIALWRAP_IMAGE"
export SW_DUT_SERVICE_IMAGE="$DUT_IMAGE"
export SW_DUT_IMAGE="$DUT_IMAGE"
export SW_SYSTEMD_IMAGE="$SYSTEMD_IMAGE"
export SW_DOCKER_RUN_ID="od15-$RUN_ID"
export SW_HOURS="$HOURS"

cleanup() {
    docker ps -aq --filter "name=sw-dut-$SW_DOCKER_RUN_ID" | xargs -r docker rm -f >/dev/null 2>&1 || true
    docker compose -f "$COMPOSE_FILE" -p "$PROJECT" down --remove-orphans --rmi all >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker compose -f "$COMPOSE_FILE" -p "$PROJECT" build serialwrap dut-sim
docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile systemd-user build systemd
docker compose -f "$COMPOSE_FILE" -p "$PROJECT" run --rm --no-deps -T \
    -v "$VOLUME_ROOT:/tmp/sw-t17" -v "$OUT:/evidence" -v "$OUT:$OUT" -e SW_HOST_EVIDENCE_DIR="$OUT" \
    -e SW_DOCKER_RUN_ID -e SW_HOURS -e SW_DUT_IMAGE \
    -e SW_SERIALWRAP_IMAGE -e SW_SYSTEMD_IMAGE \
    serialwrap python3 scripts/docker/run_longrun_container.py
