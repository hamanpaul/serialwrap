#!/usr/bin/env bash
# OD-33 R2(b)：0.3.2 正式版 → 0.4.0 候選升級驗證（Linux，OD-16 (5) ubuntu:24.04 systemd 容器）。
# host 端：由 git tag v0.3.2 建 wheel、下載隔離安裝所需 PyYAML wheel、以 mkbundle.sh 建不含測試鉤子的 0.4.0 release bundle（或沿用 --wheel／--bundle）、
# 建升級映像，每種監管模式起一個全新容器（--network none、不帶任何裝置），以唯讀 bind mount 提供 wheel、bundle 與
# 容器內 driver，結果寫 <out>/<mode>/upgrade-report.json。不碰本機正式安裝、/dev/ttyUSB* 或其他容器。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DOCKERFILE="$REPO_ROOT/tools/docker/upgrade.Dockerfile"
OLD_TAG=v0.3.2
OLD_VERSION=0.3.2
NEW_VERSION=0.4.0
MODES=(systemd-user on-demand)
OUT=""
WHEEL=""
BUNDLE=""
DRY_RUN=0
while (($#)); do
    case "$1" in
        --mode)
            (($# >= 2)) || { echo "--mode 需要參數" >&2; exit 2; }
            case "$2" in
                systemd-user|on-demand) MODES=("$2") ;;
                all) MODES=(systemd-user on-demand) ;;
                *) echo "不支援的模式：$2" >&2; exit 2 ;;
            esac
            shift 2
            ;;
        --out) (($# >= 2)) || { echo "--out 需要目錄" >&2; exit 2; }; OUT="$2"; shift 2 ;;
        --wheel) (($# >= 2)) || { echo "--wheel 需要檔案" >&2; exit 2; }; WHEEL="$2"; shift 2 ;;
        --bundle) (($# >= 2)) || { echo "--bundle 需要目錄" >&2; exit 2; }; BUNDLE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help)
            echo "用法：$0 [--mode systemd-user|on-demand|all] [--out DIR] [--wheel serialwrap-0.3.2-*.whl] [--bundle DIR] [--dry-run]"
            exit 0
            ;;
        *) echo "不支援的參數：$1" >&2; exit 2 ;;
    esac
done

RUN_ID="$(printenv SW_DOCKER_RUN_ID 2>/dev/null || true)"
if [[ -z "$RUN_ID" ]]; then RUN_ID="$(date -u +%Y%m%dt%H%M%Sz)-$$"; fi
RUN_ID="$(printf '%s' "$RUN_ID" | tr '[:upper:]' '[:lower:]' | tr -c '[:alnum:]_-' '-')"
if [[ -z "$OUT" ]]; then OUT="$REPO_ROOT/target/upgrade-evidence/$RUN_ID"; fi
OUT="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$OUT")"
for mode in "${MODES[@]}"; do
    if [[ -e "$OUT/$mode/upgrade-report.json" ]]; then
        echo "拒絕覆寫既有證據：$OUT/$mode/upgrade-report.json" >&2
        exit 2
    fi
done
WORK="$OUT/work"
IMAGE="serialwrap-upgrade:$RUN_ID"

print_command() {
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
}

container_run() {
    local mode="$1" wheel_dir="$2" bundle="$3"
    run_cmd=(docker run -d --name "sw-upgrade-$RUN_ID-$mode" --network none --cgroupns host
        -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock --tmpfs /tmp
        -v "$wheel_dir:/opt/sw-old:ro" -v "$bundle:/opt/sw-new:ro"
        -v "$SCRIPT_DIR:/opt/sw-driver:ro" -v "$OUT/$mode:/evidence"
        "$IMAGE" /sbin/init)
}

if ((DRY_RUN)); then
    echo "+ git archive $OLD_TAG | tar -x -C $WORK/src-$OLD_VERSION"
    print_command python3 -m pip wheel --no-deps --no-build-isolation -w "$WORK/wheels" "$WORK/src-$OLD_VERSION"
    print_command python3 -m pip download --only-binary=:all: --dest "$WORK/wheels" 'PyYAML>=6'
    print_command "$REPO_ROOT/scripts/rust/mkbundle.sh" --release-bundle --profile release --version-override "$NEW_VERSION" --out "$WORK/bundle-$NEW_VERSION"
    print_command docker build -f "$DOCKERFILE" -t "$IMAGE" "$WORK/empty-context"
    for mode in "${MODES[@]}"; do
        container_run "$mode" "$WORK/wheels" "${BUNDLE:-$WORK/bundle-$NEW_VERSION}"
        print_command "${run_cmd[@]}"
        print_command docker exec "sw-upgrade-$RUN_ID-$mode" python3 /opt/sw-driver/run_upgrade_container.py \
            --mode "$mode" --wheel "/opt/sw-old/serialwrap-$OLD_VERSION-py3-none-any.whl" --bundle /opt/sw-new --out /evidence
    done
    echo "+ evidence_layer=L1.5-docker producer=run-upgrade.sh old=$OLD_VERSION new=$NEW_VERSION"
    exit 0
fi

mkdir -p "$WORK"
mkdir -p "$WORK/wheels"
CONTAINERS=()
cleanup() {
    for name in "${CONTAINERS[@]}"; do
        docker rm -f "$name" >/dev/null 2>&1 || true
    done
    docker image rm -f "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

if [[ -z "$WHEEL" ]]; then
    git -C "$REPO_ROOT" rev-parse -q --verify "refs/tags/$OLD_TAG" >/dev/null \
        || { echo "版本庫沒有 $OLD_TAG tag" >&2; exit 3; }
    rm -rf "$WORK/src-$OLD_VERSION" "$WORK/wheels"
    mkdir -p "$WORK/src-$OLD_VERSION" "$WORK/wheels"
    git -C "$REPO_ROOT" archive "$OLD_TAG" | tar -x -C "$WORK/src-$OLD_VERSION"
    python3 -m pip wheel --quiet --no-deps --no-build-isolation -w "$WORK/wheels" "$WORK/src-$OLD_VERSION"
    WHEEL="$WORK/wheels/serialwrap-$OLD_VERSION-py3-none-any.whl"
fi
WHEEL="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$WHEEL")"
[[ -f "$WHEEL" && "$(basename "$WHEEL")" == "serialwrap-$OLD_VERSION-py3-none-any.whl" ]] \
    || { echo "wheel 必須是 serialwrap-$OLD_VERSION-py3-none-any.whl：$WHEEL" >&2; exit 2; }
if [[ "$WHEEL" != "$WORK/wheels/serialwrap-$OLD_VERSION-py3-none-any.whl" ]]; then
    cp "$WHEEL" "$WORK/wheels/serialwrap-$OLD_VERSION-py3-none-any.whl"
fi
python3 -m pip download --quiet --only-binary=:all: --dest "$WORK/wheels" 'PyYAML>=6'
if [[ -z "$BUNDLE" ]]; then
    BUNDLE="$("$REPO_ROOT/scripts/rust/mkbundle.sh" --release-bundle --profile release --version-override "$NEW_VERSION" \
        --out "$WORK/bundle-$NEW_VERSION" --print-dir | tail -1)"
fi
BUNDLE="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$BUNDLE")"
python3 - "$BUNDLE/versions/$NEW_VERSION/release-manifest.json" "$NEW_VERSION" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
if manifest.get("bundle_version") != sys.argv[2]:
    raise SystemExit(f"bundle 版本應為 {sys.argv[2]}：{manifest.get('bundle_version')}")
PY

mkdir -p "$WORK/empty-context"
docker build -q -f "$DOCKERFILE" -t "$IMAGE" "$WORK/empty-context" >/dev/null
IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE")"

overall=0
for mode in "${MODES[@]}"; do
    mkdir -p "$OUT/$mode"
    container_run "$mode" "$WORK/wheels" "$BUNDLE"
    name="sw-upgrade-$RUN_ID-$mode"
    CONTAINERS+=("$name")
    "${run_cmd[@]}" >/dev/null
    state=""
    for _ in $(seq 1 600); do
        state="$(docker exec "$name" systemctl is-system-running 2>/dev/null || true)"
        [[ "$state" == running || "$state" == degraded ]] && break
        sleep 0.2
    done
    if [[ "$state" != running && "$state" != degraded ]]; then
        echo "[$mode] systemd 容器未在 120 秒內完成開機（$state）" >&2
        overall=1
        continue
    fi
    rc=0
    docker exec -e SW_HOST_UID="$(id -u)" -e SW_HOST_GID="$(id -g)" -e SW_UPGRADE_IMAGE="$IMAGE" \
        -e SW_UPGRADE_IMAGE_ID="$IMAGE_ID" "$name" python3 /opt/sw-driver/run_upgrade_container.py \
        --mode "$mode" --wheel "/opt/sw-old/$(basename "$WHEEL")" --bundle /opt/sw-new --out /evidence || rc=$?
    docker exec "$name" journalctl --no-pager -o short-iso > "$OUT/$mode/journal.txt" 2>/dev/null || true
    docker rm -f "$name" >/dev/null 2>&1 || true
    if ((rc != 0)); then overall=1; fi
    echo "[$mode] rc=$rc report=$OUT/$mode/upgrade-report.json"
done
exit "$overall"
