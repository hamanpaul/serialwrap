#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/tools/docker/compose.l15.yaml"
source "$SCRIPT_DIR/common.sh"
HOURS=""
MODE=systemd-user
DRY_RUN=0
OUT=""
while (($#)); do
    case "$1" in
        --hours) (($# >= 2)) || { echo "--hours 需要正整數" >&2; exit 2; }; HOURS="$2"; shift 2 ;;
        --mode) (($# >= 2)) || { echo "--mode 需要 systemd-user 或 systemd-system" >&2; exit 2; }; MODE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --out) (($# >= 2)) || { echo "--out 需要目錄" >&2; exit 2; }; OUT="$2"; shift 2 ;;
        -h|--help) echo "用法：$0 --hours N --mode systemd-user|systemd-system [--dry-run] [--out DIR]"; exit 0 ;;
        *) echo "不支援的參數：$1" >&2; exit 2 ;;
    esac
done
if [[ ! "$HOURS" =~ ^[1-9][0-9]*$ ]]; then echo "--hours 必須為正整數" >&2; exit 2; fi
case "$MODE" in systemd-user|systemd-system) ;; *) echo "不支援的 systemd 模式：$MODE" >&2; exit 2 ;; esac

# P1 與 P1-OC 均為啟動前 gate，dry-run 也不得繞過。
sw_require_p1_oc "$REPO_ROOT"

RUN_ID="$(printenv SW_DOCKER_RUN_ID 2>/dev/null || true)"
if [[ -z "$RUN_ID" ]]; then RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"; fi
RUN_ID="$(printf '%s' "$RUN_ID" | tr '[:upper:]' '[:lower:]' | tr -c '[:alnum:]_-' '-')"
PROJECT="sw-supervision-$MODE-$RUN_ID"
CONTAINER_NAME="$PROJECT-systemd"
SERIALWRAP_IMAGE="serialwrap-l15:$RUN_ID"
DUT_IMAGE="serialwrap-dut-sim-l15:$RUN_ID"
SYSTEMD_IMAGE="serialwrap-systemd:$RUN_ID"
if [[ -z "$OUT" ]]; then OUT="$REPO_ROOT/evidence/stage0/docker/supervision-${MODE#systemd-}"; fi
OUT="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$OUT")"
VOLUME_ROOT="$OUT/volumes"
REPORT="$OUT/l15-docker-report.json"
if [[ -s "$REPORT" ]]; then echo "拒絕覆寫既有證據：$REPORT" >&2; exit 2; fi

print_command() { printf '+ '; printf '%q ' "$@"; printf '\n'; }
if ((DRY_RUN)); then
    print_command docker compose -f "$COMPOSE_FILE" -p "$PROJECT" build serialwrap dut-sim
    print_command docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile "$MODE" build systemd
    print_command docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile "$MODE" run -d --name "$CONTAINER_NAME" \
        -v "$VOLUME_ROOT:/tmp/sw-t17" -v "$OUT:/evidence" systemd /sbin/init
    print_command docker exec -e "SW_HOST_UID=$(id -u)" -e "SW_HOST_GID=$(id -g)" \
        -e "SW_SERIALWRAP_IMAGE=$SERIALWRAP_IMAGE" -e "SW_DUT_IMAGE=$DUT_IMAGE" \
        -e "SW_SYSTEMD_IMAGE=$SYSTEMD_IMAGE" "$CONTAINER_NAME" \
        python3 scripts/docker/run_supervision_container.py --mode "$MODE" --hours "$HOURS" --run-id "od15-$RUN_ID"
    echo "+ systemd 就緒等待：最多 120 秒；服務 READY 後才啟動 observer"
    echo "+ observer PASS 條件：觀測跨度至少 ${HOURS}h-20s、樣本數足夠、最大間隔不超過 30s、所有樣本健康"
    echo "+ evidence_layer=L1.5-docker producer=run-supervision.sh topology=docker-2c uart_kind=pty-fake"
    exit 0
fi

sw_prepare_empty_out "$OUT"
mkdir -p "$VOLUME_ROOT"
export SW_CONTAINER_UID="$(id -u)"
export SW_CONTAINER_GID="$(id -g)"
export SW_DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
export SW_SERIALWRAP_IMAGE="$SERIALWRAP_IMAGE"
export SW_DUT_SERVICE_IMAGE="$DUT_IMAGE"
export SW_SYSTEMD_IMAGE="$SYSTEMD_IMAGE"
export SW_DUT_IMAGE="$DUT_IMAGE"
export SW_DOCKER_RUN_ID="od15-$RUN_ID"
export SW_HOST_UID="$(id -u)"
export SW_HOST_GID="$(id -g)"
CONTAINER_ID=""
cleanup() {
    docker ps -aq --filter "name=sw-dut-$SW_DOCKER_RUN_ID" | xargs -r docker rm -f >/dev/null 2>&1 || true
    if [[ -n "$CONTAINER_ID" ]]; then docker rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true; fi
    docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile "$MODE" down --remove-orphans --rmi all >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

cd "$REPO_ROOT"
docker compose -f "$COMPOSE_FILE" -p "$PROJECT" build serialwrap dut-sim
docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile "$MODE" build systemd
CONTAINER_ID="$(docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --profile "$MODE" run -d --no-deps \
    --name "$CONTAINER_NAME" -v "$VOLUME_ROOT:/tmp/sw-t17" -v "$OUT:/evidence" systemd /sbin/init)"

wait_systemd_ready() {
    local state
    for _ in $(seq 1 600); do
        state="$(docker exec "$CONTAINER_ID" systemctl is-system-running 2>/dev/null || true)"
        if [[ "$state" == running || "$state" == degraded ]]; then return 0; fi
        sleep 0.2
    done
    echo "systemd 容器未在 120 秒內完成開機" >&2
    docker exec "$CONTAINER_ID" systemctl --failed --no-pager >&2 || true
    return 1
}

wait_systemd_ready
docker exec -e SW_HOST_UID -e SW_HOST_GID -e SW_SERIALWRAP_IMAGE -e SW_DUT_IMAGE -e SW_SYSTEMD_IMAGE \
    -i "$CONTAINER_ID" python3 scripts/docker/run_supervision_container.py \
    --mode "$MODE" --hours "$HOURS" --run-id "$SW_DOCKER_RUN_ID"
