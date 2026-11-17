#!/usr/bin/env bash
# 以可丟棄 Ubuntu 24.04 root 容器收集 Slice 2.1 的 sys_admin L1 證據。
set -euo pipefail
export CARGO_INCREMENTAL=0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)
      [[ $# -ge 2 ]] || { echo "slice21_sys_admin_docker.sh: --out 缺少值" >&2; exit 2; }
      OUT_DIR="$2"; shift 2 ;;
    -h|--help)
      echo "用法：scripts/rust/slice21_sys_admin_docker.sh --out <空的結果目錄>"
      exit 0 ;;
    *) echo "slice21_sys_admin_docker.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done
[[ -n "$OUT_DIR" ]] || { echo "slice21_sys_admin_docker.sh: 必須指定 --out" >&2; exit 2; }

if [[ ! -r /etc/os-release ]]; then
  echo "slice21_sys_admin_docker.sh: 無法確認主機 Ubuntu 版本" >&2
  exit 2
fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 ]]; then
  echo "slice21_sys_admin_docker.sh: 僅支援 Ubuntu 24.04 主機（目前 ${ID:-unknown} ${VERSION_ID:-unknown}）" >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "slice21_sys_admin_docker.sh: 找不到 docker" >&2
  exit 2
fi
IMAGE="ubuntu:24.04"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null)" || {
  echo "slice21_sys_admin_docker.sh: 本機缺少 $IMAGE image；請先由擁有者準備 image" >&2
  exit 2
}
if [[ ! "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "slice21_sys_admin_docker.sh: 無法核對 $IMAGE image digest：$IMAGE_ID" >&2
  exit 2
fi

HEAD="$(git -C "$ROOT" rev-parse HEAD)"
WORKTREE_STATUS="$(git -C "$ROOT" status --porcelain --untracked-files=normal -- . ':(exclude)target')"
if [[ -n "$WORKTREE_STATUS" ]]; then
  printf 'slice21_sys_admin_docker.sh: 正式證據要求乾淨工作樹（排除 target/）：\n%s\n' "$WORKTREE_STATUS" >&2
  exit 4
fi

GIT_DIR_HOST="$(git -C "$ROOT" rev-parse --absolute-git-dir)"
GIT_COMMON_DIR_HOST="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
CARGO_HOME_HOST="$(realpath "${CARGO_HOME:-$HOME/.cargo}")"
RUSTUP_HOME_HOST="$(realpath "${RUSTUP_HOME:-$HOME/.rustup}")"
CARGO_BIN_HOST="$(realpath "$CARGO_HOME_HOST/bin")"
CARGO_REGISTRY_HOST="$(realpath "$CARGO_HOME_HOST/registry")"
if [[ ! -d "$CARGO_REGISTRY_HOST" || ! -d "$RUSTUP_HOME_HOST/toolchains" || ! -x "$CARGO_BIN_HOST/rustup" ]]; then
  echo "slice21_sys_admin_docker.sh: 主機 Rust toolchain／Cargo registry 不完整" >&2
  exit 2
fi

PLAN="$(python3 "$ROOT/scripts/rust/slice21_gate_judge.py" caps-plan --section linux_cap_sys_admin)"
PLAN_CHECK="$(python3 -c 'import json,sys; p=json.loads(sys.argv[1]); print(int(p.get("caps")==["sys_admin"] and p.get("caps_only") is True and isinstance(p.get("argv"),list) and bool(p["argv"])) )' "$PLAN")"
if [[ "$PLAN_CHECK" != 1 ]]; then
  echo "slice21_sys_admin_docker.sh: 凍結 selector 不符合 sys_admin 能力集合：$PLAN" >&2
  exit 2
fi
SELECTOR_JSON="$(python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1])["argv"],separators=(",",":")))' "$PLAN")"

OUT_DIR="$(python3 - "$OUT_DIR" <<'PY'
import pathlib, sys
print(pathlib.Path(sys.argv[1]).expanduser().resolve())
PY
)"
if [[ -e "$OUT_DIR" ]]; then
  echo "slice21_sys_admin_docker.sh: 結果目錄已存在，拒絕覆寫：$OUT_DIR" >&2
  exit 2
fi
mkdir -p "$OUT_DIR"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sw-s21-sys-admin.XXXXXX")"
trap 'rm -rf -- "$TMP_ROOT"' EXIT
ARCHIVE="$TMP_ROOT/evidence.tar"
DOCKER_STDERR="$TMP_ROOT/docker.stderr.log"
printf '候選 HEAD：%s\nimage：%s (%s)\n輸出：%s\n' "$HEAD" "$IMAGE" "$IMAGE_ID" "$OUT_DIR"

CONTAINER_SCRIPT=$(cat <<'CONTAINER'
set -euo pipefail
source /etc/os-release
export ID VERSION_ID
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then
  echo "container image is not Ubuntu 24.04: $ID $VERSION_ID" >&2
  exit 90
fi
if ! command -v python3 >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1 || ! command -v cc >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >&2
  apt-get install -y --no-install-recommends python3 git ca-certificates build-essential pkg-config libudev-dev >&2
  apt-get clean
  rm -rf /var/lib/apt/lists/*
fi

export PATH="/host-cargo-bin:$PATH"
export RUSTUP_HOME=/host-rustup
export CARGO_HOME=/tmp/sw-sys-admin-cargo-home
export CARGO_TARGET_DIR=/tmp/sw-sys-admin-target
export CARGO_INCREMENTAL=0
export CARGO_NET_OFFLINE=true
export GIT_DIR="$SW_SYS_ADMIN_GIT_DIR"
export GIT_COMMON_DIR="$SW_SYS_ADMIN_GIT_COMMON_DIR"
export GIT_WORK_TREE="$SW_SYS_ADMIN_ROOT"
export GIT_OPTIONAL_LOCKS=0
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="$SW_SYS_ADMIN_ROOT"
mkdir -p "$CARGO_HOME"
cp -a /host-cargo-registry "$CARGO_HOME/registry"

WORK=/tmp/sw-sys-admin-evidence
mkdir -p "$WORK"
python3 - "$WORK/probe.json" <<'PY'
import json, os, pathlib, re, sys
text = pathlib.Path("/proc/self/status").read_text(encoding="ascii")
match = re.search(r"^CapEff:\s*([0-9a-fA-F]+)\s*$", text, re.MULTILINE)
if not match:
    raise SystemExit("/proc/self/status 缺少 CapEff")
pathlib.Path(sys.argv[1]).write_text(json.dumps({"euid": os.geteuid(), "cap_eff": match.group(1)},
                                                  sort_keys=True) + "\n", encoding="utf-8")
PY

RUN_RC=77
if python3 - "$WORK/probe.json" <<'PY'
import json, pathlib, sys
probe = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if probe["euid"] == 0 and int(probe["cap_eff"], 16) & (1 << 21) else 1)
PY
then
  printf 'probe: ' >"$WORK/container.log"
  python3 - "$WORK/probe.json" >>"$WORK/container.log" <<'PY'
import json, pathlib, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"euid={p['euid']} CapEff={p['cap_eff']}")
PY
  printf '+ python3 scripts/rust/cargo_l1_results.py --platform linux --caps-only --caps sys_admin -- <locked selector>\n' >>"$WORK/container.log"
  mapfile -t SELECTOR_ARGV < <(python3 -c 'import json,os; print("\n".join(json.loads(os.environ["SW_SYS_ADMIN_SELECTOR_JSON"])))')
  set +e
  python3 scripts/rust/cargo_l1_results.py --platform linux --out "$WORK/l1-results.json" \
    --caps-only --caps sys_admin -- "${SELECTOR_ARGV[@]}" >>"$WORK/container.log" 2>&1
  RUN_RC=$?
  set -e
else
  printf 'sys_admin probe unavailable: ' >"$WORK/container.log"
  python3 - "$WORK/probe.json" >>"$WORK/container.log" <<'PY'
import json, pathlib, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"euid={p['euid']} CapEff={p['cap_eff']}")
PY
fi
export SW_SYS_ADMIN_RUN_RC="$RUN_RC"

python3 - "$WORK/probe.json" "$WORK/sys-admin-evidence.json" <<'PY'
import json, os, pathlib, sys
probe = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
evidence = {
    "schema": "slice21-sys-admin-evidence.v1",
    "candidate_commit": os.environ["SW_SYS_ADMIN_HEAD"],
    "required_set": "s21_gate:cap_sys_admin",
    "platform": "linux",
    "selector": json.loads(os.environ["SW_SYS_ADMIN_SELECTOR_JSON"]),
    "caps": ["sys_admin"],
    "caps_only": True,
    "probe": probe,
    "image": "ubuntu:24.04",
    "image_id": os.environ["SW_SYS_ADMIN_IMAGE_ID"],
    "container_os": {"id": os.environ["ID"], "version_id": os.environ["VERSION_ID"]},
    "cargo_target_dir": os.environ["CARGO_TARGET_DIR"],
    "cargo_exit_code": int(os.environ["SW_SYS_ADMIN_RUN_RC"]),
}
pathlib.Path(sys.argv[2]).write_text(json.dumps(evidence, ensure_ascii=False, sort_keys=True) + "\n",
                                     encoding="utf-8")
PY

FILES=(sys-admin-evidence.json container.log)
[[ -f "$WORK/l1-results.json" ]] && FILES+=(l1-results.json)
tar -C "$WORK" -cf - "${FILES[@]}"
exit "$RUN_RC"
CONTAINER
)

set +e
docker run --rm --cap-add SYS_ADMIN \
  --mount "type=bind,source=$GIT_COMMON_DIR_HOST,target=$GIT_COMMON_DIR_HOST,readonly" \
  --mount "type=bind,source=$ROOT,target=$ROOT,readonly" \
  --mount "type=bind,source=$GIT_DIR_HOST,target=$GIT_DIR_HOST,readonly" \
  --mount "type=bind,source=$CARGO_BIN_HOST,target=/host-cargo-bin,readonly" \
  --mount "type=bind,source=$CARGO_REGISTRY_HOST,target=/host-cargo-registry,readonly" \
  --mount "type=bind,source=$RUSTUP_HOME_HOST,target=/host-rustup,readonly" \
  --workdir "$ROOT" \
  --env "SW_SYS_ADMIN_HEAD=$HEAD" \
  --env "SW_SYS_ADMIN_SELECTOR_JSON=$SELECTOR_JSON" \
  --env "SW_SYS_ADMIN_IMAGE_ID=$IMAGE_ID" \
  --env "SW_SYS_ADMIN_ROOT=$ROOT" \
  --env "SW_SYS_ADMIN_GIT_DIR=$GIT_DIR_HOST" \
  --env "SW_SYS_ADMIN_GIT_COMMON_DIR=$GIT_COMMON_DIR_HOST" \
  --env "GIT_DIR=$GIT_DIR_HOST" \
  --env "GIT_COMMON_DIR=$GIT_COMMON_DIR_HOST" \
  --env "GIT_WORK_TREE=$ROOT" \
  --env "GIT_OPTIONAL_LOCKS=0" \
  --entrypoint /bin/bash "$IMAGE" -euc "$CONTAINER_SCRIPT" >"$ARCHIVE" 2>"$DOCKER_STDERR"
DOCKER_RC=$?
set -e
cp "$DOCKER_STDERR" "$OUT_DIR/docker.stderr.log"

if [[ ! -s "$ARCHIVE" ]]; then
  echo "slice21_sys_admin_docker.sh: Docker 未產出證據封包（exit=$DOCKER_RC）；詳見 $OUT_DIR/docker.stderr.log" >&2
  exit "${DOCKER_RC:-1}"
fi
mapfile -t ARCHIVE_MEMBERS < <(tar -tf "$ARCHIVE")
for member in "${ARCHIVE_MEMBERS[@]}"; do
  case "$member" in
    sys-admin-evidence.json|container.log|l1-results.json) ;;
    *) echo "slice21_sys_admin_docker.sh: 封包含非預期路徑：$member" >&2; exit 1 ;;
  esac
done
if [[ ! " ${ARCHIVE_MEMBERS[*]} " =~ ' sys-admin-evidence.json ' || ! " ${ARCHIVE_MEMBERS[*]} " =~ ' container.log ' ]]; then
  echo "slice21_sys_admin_docker.sh: 證據封包缺少 metadata 或 container.log" >&2
  exit 1
fi
tar -xf "$ARCHIVE" -C "$OUT_DIR" --no-same-owner --no-same-permissions

set +e
python3 "$ROOT/scripts/rust/slice21_sys_admin_evidence.py" validate \
  --results "$OUT_DIR" --candidate-commit "$HEAD" >"$OUT_DIR/validation.json" 2>"$OUT_DIR/validation.stderr.log"
VALIDATION_RC=$?
set -e
cat "$OUT_DIR/validation.json" 2>/dev/null || true
if [[ "$VALIDATION_RC" -ne 0 ]]; then
  echo "slice21_sys_admin_docker.sh: 證據未通過 gate 匯入前檢查（docker exit=$DOCKER_RC）；詳見 $OUT_DIR" >&2
  exit 1
fi
if [[ "$DOCKER_RC" -ne 0 ]]; then
  echo "slice21_sys_admin_docker.sh: L1 測試失敗（exit=$DOCKER_RC）；失敗證據已保留於 $OUT_DIR" >&2
  exit "$DOCKER_RC"
fi
echo "sys_admin 證據已產生並通過 HEAD／selector／euid／CapEff 檢查：$OUT_DIR"
