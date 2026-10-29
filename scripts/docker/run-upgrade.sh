#!/usr/bin/env bash
# OD-33 R2(b)：0.3.2 正式版 → 0.4.0 候選升級驗證（Linux，OD-16 (5) ubuntu:24.04 systemd 容器）。
# host 端：由 git tag v0.3.2 建 wheel、下載隔離安裝所需 PyYAML wheel、以 mkbundle.sh 建不含測試鉤子的 0.4.0 release bundle（或沿用 --wheel／--bundle）、
# 建升級映像，每種監管模式起一個全新容器（--network none、不帶任何裝置），以唯讀 bind mount 提供 wheel、bundle 與
# 容器內 driver，結果寫 <out>/<mode>/upgrade-report.json。不碰本機正式安裝、/dev/ttyUSB* 或其他容器。
# `--scenario install-sh`（OD-50）：另將 bundle 打包成 release 同名 tarball，並唯讀掛載 repo 的 install.sh，
# 驗證「0.3.2 pipx 已安裝且 daemon 已停止 → install.sh --bundle <tarball>」；預設 upgrade 情境的必要檢查不變。
# install-sh 情境只支援 systemd-user／on-demand；systemd-system（OD-48）只在預設 upgrade 情境以 `--mode systemd-system` 明確指定。
# 自建 bundle 比照 release workflow 的 publish-linux-bundle（release 前置第 2 輪 OWNER-BATCH #2）：以系統 /usr/bin/python3.12
# 建 build venv 再執行 mkbundle，bundle runtime 的 pyvenv.cfg 須為 `home = /usr/bin`（OD-50 使用者主機）；主機沒有
# /usr/bin/python3.12 時 fail-closed（rc=3），不回退其他 python。`--bundle` 提供的 bundle 同樣檢查 pyvenv.cfg。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DOCKERFILE="$REPO_ROOT/tools/docker/upgrade.Dockerfile"
OLD_TAG=v0.3.2
OLD_VERSION=0.3.2
NEW_VERSION=0.4.0
# SW_UPGRADE_SYSTEM_PYTHON 只供測試模擬主機缺件；實體不在 /usr/bin 或版本不符 contracts/cli_help/_meta.json 一律拒絕。
SYSTEM_PYTHON="${SW_UPGRADE_SYSTEM_PYTHON:-/usr/bin/python3.12}"
# 與 release workflow 的 build venv 同一釘版
BUILD_PIP_PINS=(pyyaml==6.0.3 jsonschema==4.10.3)
MODES=(systemd-user on-demand)
OUT=""
WHEEL=""
BUNDLE=""
DRY_RUN=0
SCENARIO=upgrade
while (($#)); do
    case "$1" in
        --mode)
            (($# >= 2)) || { echo "--mode 需要參數" >&2; exit 2; }
            case "$2" in
                # systemd-system（OD-48 SPEC-GAP #10）只驗直接 setup 成功路徑，須明確指定，不屬 all。
                systemd-user|on-demand|systemd-system) MODES=("$2") ;;
                all) MODES=(systemd-user on-demand) ;;
                *) echo "不支援的模式：$2" >&2; exit 2 ;;
            esac
            shift 2
            ;;
        --out) (($# >= 2)) || { echo "--out 需要目錄" >&2; exit 2; }; OUT="$2"; shift 2 ;;
        --wheel) (($# >= 2)) || { echo "--wheel 需要檔案" >&2; exit 2; }; WHEEL="$2"; shift 2 ;;
        --bundle) (($# >= 2)) || { echo "--bundle 需要目錄" >&2; exit 2; }; BUNDLE="$2"; shift 2 ;;
        --scenario)
            (($# >= 2)) || { echo "--scenario 需要參數" >&2; exit 2; }
            case "$2" in
                upgrade|install-sh) SCENARIO="$2" ;;
                *) echo "不支援的情境：$2" >&2; exit 2 ;;
            esac
            shift 2
            ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help)
            echo "用法：$0 [--mode systemd-user|on-demand|systemd-system|all] [--scenario upgrade|install-sh] [--out DIR] [--wheel serialwrap-0.3.2-*.whl] [--bundle DIR] [--dry-run]"
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
if [[ "$SCENARIO" == install-sh && " ${MODES[*]} " == *" systemd-system "* ]]; then
    echo "--scenario install-sh 不支援 systemd-system（該模式只驗 OD-48 直接 setup 成功路徑）" >&2
    exit 2
fi
WORK="$OUT/work"
IMAGE="serialwrap-upgrade:$RUN_ID"
TARBALL_NAME="serialwrap-$NEW_VERSION-linux-x86_64.tar.gz"
# install-sh 情境的額外掛載與 driver 參數
EXTRA_MOUNTS=()
EXTRA_DRIVER_ARGS=()
if [[ "$SCENARIO" == install-sh ]]; then
    EXTRA_MOUNTS=(-v "$REPO_ROOT/install.sh:/opt/sw-installer/install.sh:ro" -v "$WORK/dist:/opt/sw-dist:ro")
    EXTRA_DRIVER_ARGS=(--scenario install-sh --installer /opt/sw-installer/install.sh
        --bundle-tarball "/opt/sw-dist/$TARBALL_NAME")
fi

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
        -v "$SCRIPT_DIR:/opt/sw-driver:ro" -v "$OUT/$mode:/evidence" ${EXTRA_MOUNTS[@]+"${EXTRA_MOUNTS[@]}"}
        "$IMAGE" /sbin/init)
}

# 自建 bundle 的前置條件：SYSTEM_PYTHON 可執行、實體位於 /usr/bin、版本等於 contracts/cli_help/_meta.json。任一不符即
# rc=3，不回退 PATH 上的其他 python（release workflow 同一檢查；S1-3185c6d-1）。
require_system_python() {
    local real expected actual
    if [[ ! -x "$SYSTEM_PYTHON" ]]; then
        echo "主機沒有可執行的 $SYSTEM_PYTHON：自建 bundle 須比照 release workflow 以 /usr/bin/python3.12 建 build venv（OD-50），不回退使用其他 python" >&2
        exit 3
    fi
    real="$(realpath "$SYSTEM_PYTHON")"
    if [[ "$(dirname "$real")" != /usr/bin ]]; then
        echo "$SYSTEM_PYTHON 的實體為 $real，不在 /usr/bin：bundle runtime 須綁定 /usr/bin/python3.12（OD-50），不回退使用其他 python" >&2
        exit 3
    fi
    expected="$("$SYSTEM_PYTHON" -I -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["python_version"])' \
        "$REPO_ROOT/contracts/cli_help/_meta.json")"
    actual="$("$SYSTEM_PYTHON" -I -c 'import platform; print(platform.python_version())')"
    if [[ "$actual" != "$expected" ]]; then
        echo "$SYSTEM_PYTHON 為 $actual，contracts/cli_help/_meta.json 要求 $expected：不回退使用其他 python" >&2
        exit 3
    fi
}

# 以 SYSTEM_PYTHON 建 build venv（PyYAML 複製進 bundle runtime、jsonschema 驗 release manifest），PATH 首位改為 build venv
# 後執行 mkbundle：mkbundle 以 PATH 上的 python3 執行 `python3 -m venv --copies`，runtime 的 pyvenv.cfg home 因而為 /usr/bin。
build_bundle_with_system_python() {
    local venv="$WORK/build-venv" base
    rm -rf "$venv"
    "$SYSTEM_PYTHON" -m venv "$venv"
    "$venv/bin/python3" -m pip install --quiet --disable-pip-version-check "${BUILD_PIP_PINS[@]}"
    base="$("$venv/bin/python3" -I -c 'import os, sys; print(os.path.dirname(sys._base_executable))')"
    if [[ "$base" != /usr/bin ]]; then
        echo "build venv 的基底直譯器位於 $base，應為 /usr/bin：不回退使用其他 python" >&2
        exit 3
    fi
    BUNDLE="$(env "PATH=$venv/bin:$PATH" "$REPO_ROOT/scripts/rust/mkbundle.sh" --release-bundle --profile release \
        --version-override "$NEW_VERSION" --out "$WORK/bundle-$NEW_VERSION" --print-dir | tail -1)"
}

# 自建或 --bundle 提供的 bundle 一律檢查 runtime 綁定使用者主機的 /usr/bin/python3.12（OD-50；release workflow smoke 同一判準）。
check_bundle_runtime() {
    local cfg="$BUNDLE/versions/$NEW_VERSION/py/pyvenv.cfg" line
    if [[ ! -f "$cfg" ]]; then
        echo "bundle 缺 versions/$NEW_VERSION/py/pyvenv.cfg：無法確認 runtime 綁定 /usr/bin/python3.12（OD-50）" >&2
        exit 3
    fi
    for line in "home = /usr/bin" "include-system-site-packages = false"; do
        if ! grep -qxF "$line" "$cfg"; then
            echo "bundle runtime 的 pyvenv.cfg 必須含「$line」（OD-50、S1-3185c6d-1）；實際：$(grep -E '^(home|include-system-site-packages)' "$cfg" | tr '\n' ' ' || true)" >&2
            exit 3
        fi
    done
    echo "[run-upgrade] bundle runtime pyvenv.cfg：home = /usr/bin、include-system-site-packages = false（$cfg）"
}

if ((DRY_RUN)); then
    echo "+ git archive $OLD_TAG | tar -x -C $WORK/src-$OLD_VERSION"
    print_command python3 -m pip wheel --no-deps --no-build-isolation -w "$WORK/wheels" "$WORK/src-$OLD_VERSION"
    print_command "$SYSTEM_PYTHON" -m venv "$WORK/build-venv"
    print_command "$WORK/build-venv/bin/python3" -m pip install --quiet --disable-pip-version-check "${BUILD_PIP_PINS[@]}"
    print_command env "PATH=$WORK/build-venv/bin:$PATH" "$REPO_ROOT/scripts/rust/mkbundle.sh" --release-bundle --profile release \
        --version-override "$NEW_VERSION" --out "$WORK/bundle-$NEW_VERSION"
    echo "+ require ${BUNDLE:-$WORK/bundle-$NEW_VERSION}/versions/$NEW_VERSION/py/pyvenv.cfg: home = /usr/bin, include-system-site-packages = false"
    print_command python3 -m pip download --only-binary=:all: --dest "$WORK/wheels" 'PyYAML>=6'
    if [[ "$SCENARIO" == install-sh ]]; then
        print_command tar -czf "$WORK/dist/$TARBALL_NAME" --transform "s,^\.,serialwrap-$NEW_VERSION-linux-x86_64,SH" \
            -C "${BUNDLE:-$WORK/bundle-$NEW_VERSION}" .
    fi
    print_command docker build -f "$DOCKERFILE" -t "$IMAGE" "$WORK/empty-context"
    for mode in "${MODES[@]}"; do
        container_run "$mode" "$WORK/wheels" "${BUNDLE:-$WORK/bundle-$NEW_VERSION}"
        print_command "${run_cmd[@]}"
        print_command docker exec "sw-upgrade-$RUN_ID-$mode" python3 /opt/sw-driver/run_upgrade_container.py \
            --mode "$mode" --wheel "/opt/sw-old/serialwrap-$OLD_VERSION-py3-none-any.whl" --bundle /opt/sw-new --out /evidence \
            ${EXTRA_DRIVER_ARGS[@]+"${EXTRA_DRIVER_ARGS[@]}"}
    done
    echo "+ evidence_layer=L1.5-docker producer=run-upgrade.sh old=$OLD_VERSION new=$NEW_VERSION"
    exit 0
fi

# 需自建 bundle 時，主機前置條件先於任何建置、下載或 docker 動作檢查
if [[ -z "$BUNDLE" ]]; then
    require_system_python
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
if [[ -z "$BUNDLE" ]]; then
    build_bundle_with_system_python
fi
BUNDLE="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$BUNDLE")"
python3 - "$BUNDLE/versions/$NEW_VERSION/release-manifest.json" "$NEW_VERSION" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
if manifest.get("bundle_version") != sys.argv[2]:
    raise SystemExit(f"bundle 版本應為 {sys.argv[2]}：{manifest.get('bundle_version')}")
PY
check_bundle_runtime
python3 -m pip download --quiet --only-binary=:all: --dest "$WORK/wheels" 'PyYAML>=6'
if [[ "$SCENARIO" == install-sh ]]; then
    # 與 release 同名的 tarball：頂層為 serialwrap-<版本>-linux-x86_64/（S／H：不改寫 symlink／hardlink 目標）
    rm -rf "$WORK/dist"
    mkdir -p "$WORK/dist"
    tar -czf "$WORK/dist/$TARBALL_NAME" --transform "s,^\.,serialwrap-$NEW_VERSION-linux-x86_64,SH" -C "$BUNDLE" .
fi

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
        --mode "$mode" --wheel "/opt/sw-old/$(basename "$WHEEL")" --bundle /opt/sw-new --out /evidence \
        ${EXTRA_DRIVER_ARGS[@]+"${EXTRA_DRIVER_ARGS[@]}"} || rc=$?
    docker exec "$name" journalctl --no-pager -o short-iso > "$OUT/$mode/journal.txt" 2>/dev/null || true
    docker rm -f "$name" >/dev/null 2>&1 || true
    if ((rc != 0)); then overall=1; fi
    echo "[$mode] rc=$rc report=$OUT/$mode/upgrade-report.json"
done
exit "$overall"
