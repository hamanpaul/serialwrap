#!/usr/bin/env bash
# 建立 Stage 1A 開發 bundle；`--delegate-sentinel` 提供後續管理面測試使用的精簡 bundle。
set -euo pipefail

usage() {
  cat <<'EOF'
用法：
  scripts/rust/mkbundle.sh --dev --out <目錄> [--target <triple>] [--profile debug|release] [--print-dir]
  scripts/rust/mkbundle.sh --dev --delegate-sentinel --out <目錄> [其他選項]

選項：
  --dev                    建立開發 bundle（目前唯一支援的 bundle 類型）
  --delegate-sentinel      將委派入口改為 P4 測試 sentinel
  --target <triple>        指定 Rust target；Windows bundle 由 P3-T17 承接
  --profile <name>         選擇 debug 或 release，預設為 debug
  --out <目錄>             指定輸出目錄
  --print-dir              保留後續階段介面；成功時 stdout 一律輸出絕對目錄
  -h, --help               顯示此說明
EOF
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
DEV=0
SENTINEL=0
PROFILE=debug
TARGET=""
OUT="$REPO_ROOT/target/mkbundle-dev"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --dev) DEV=1; shift ;;
    --delegate-sentinel) SENTINEL=1; shift ;;
    --print-dir) shift ;;
    --out)
      if [[ $# -lt 2 ]]; then
        echo "mkbundle.sh: --out 缺少目錄參數" >&2
        exit 2
      fi
      OUT="$2"
      shift 2
      ;;
    --target)
      if [[ $# -lt 2 ]]; then
        echo "mkbundle.sh: --target 缺少 target triple" >&2
        exit 2
      fi
      TARGET="$2"
      shift 2
      ;;
    --profile)
      if [[ $# -lt 2 ]]; then
        echo "mkbundle.sh: --profile 缺少 profile 名稱" >&2
        exit 2
      fi
      PROFILE="$2"
      shift 2
      ;;
    *) echo "mkbundle.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done

if [[ "$DEV" -ne 1 ]]; then
  echo "mkbundle.sh: 目前只支援 --dev（正式 bundle 由後續階段承接）" >&2
  exit 2
fi
if [[ "$PROFILE" != debug && "$PROFILE" != release ]]; then
  echo "mkbundle.sh: --profile 僅支援 debug 或 release" >&2
  exit 2
fi

HOST_TRIPLE="$(rustc -vV | awk '/^host:/ {print $2}')"
if [[ -z "$TARGET" ]]; then
  TARGET="$HOST_TRIPLE"
fi
case "$TARGET" in
  *-windows-*)
    echo "mkbundle.sh: Windows bundle 由 P3-T17 承接，拒絕產生不完整的 dev bundle" >&2
    exit 2
    ;;
esac

BUILD_ARGS=(build -p sw-cli)
if [[ "$PROFILE" == release ]]; then
  BUILD_ARGS+=(--release)
fi
if [[ "$TARGET" != "$HOST_TRIPLE" ]]; then
  BUILD_ARGS+=(--target "$TARGET")
fi
(cd "$REPO_ROOT" && CARGO_TARGET_DIR="$REPO_ROOT/target" cargo "${BUILD_ARGS[@]}" --quiet)

BUILD_DIR="$REPO_ROOT/target"
if [[ "$TARGET" != "$HOST_TRIPLE" ]]; then
  BUILD_DIR+="/$TARGET"
fi
BUILD_DIR+="/$PROFILE"
BIN_SRC="$BUILD_DIR/serialwrap"
CLI_NAME=serialwrap
if [[ ! -x "$BIN_SRC" && -x "$BUILD_DIR/serialwrap.exe" ]]; then
  BIN_SRC="$BUILD_DIR/serialwrap.exe"
  CLI_NAME=serialwrap.exe
fi
if [[ ! -x "$BIN_SRC" ]]; then
  echo "mkbundle.sh: cargo build 後仍找不到 $BIN_SRC" >&2
  exit 1
fi

VERSION="$(tr -d ' \n' < "$REPO_ROOT/VERSION")"
if [[ -z "$VERSION" ]]; then
  echo "mkbundle.sh: VERSION 不得為空" >&2
  exit 1
fi
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd -P)"

sha256_file() {
  sha256sum -- "$1" | awk '{print $1}'
}

if [[ "$SENTINEL" -eq 1 ]]; then
  # P4 管理面測試需要在 bundle bin 下直接執行 sentinel，維持既有精簡格式。
  mkdir -p "$OUT/bin" "$OUT/py/bin"
  cp "$BIN_SRC" "$OUT/bin/$CLI_NAME"
  chmod +x "$OUT/bin/$CLI_NAME"
  PY_SRC="$(command -v python3)"
  ln -sf "$(readlink -f "$PY_SRC")" "$OUT/py/bin/python3"

  SENTINEL_PATH="$REPO_ROOT/tests/contract_blackbox/delegate_sentinel.py"
  CLI_SHA="$(sha256_file "$OUT/bin/$CLI_NAME")"
  REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  python3 - "$OUT/release-manifest.json" "$VERSION" "$REVISION" "$TARGET" "$CLI_SHA" "$CLI_NAME" "$SENTINEL_PATH" <<'PY'
import json
import sys

path, version, revision, target, cli_sha, cli_name, sentinel = sys.argv[1:8]
manifest = {
    "schema_version": 1,
    "bundle_version": version,
    "stage": "rust-1b-dev",
    "target": target,
    "git_revision": revision,
    "components": {
        "cli": {
            "implementation": "rust",
            "version": version,
            "path": f"bin/{cli_name}",
            "sha256": cli_sha,
        },
        "python_cli_delegate": {
            "entry": ["py/bin/python3", "-I", sentinel],
        },
    },
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
else
  VERSION_DIR="$OUT/versions/$VERSION"
  mkdir -p "$VERSION_DIR/bin" "$OUT/bin"
  cp "$BIN_SRC" "$VERSION_DIR/bin/$CLI_NAME"
  chmod +x "$VERSION_DIR/bin/$CLI_NAME"

  PY_DIR="$VERSION_DIR/py"
  python3 -m venv --system-site-packages "$PY_DIR"
  PYTHON="$PY_DIR/bin/python3"
  PY_SITE="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
  mkdir -p "$PY_SITE"
  printf '%s\n' "$REPO_ROOT" > "$PY_SITE/serialwrap_repo.pth"

  PYTHON_VERSION="$("$PYTHON" -I -c 'import platform; print(platform.python_version())')"
  EXPECTED_PYTHON_VERSION="$(python3 - "$REPO_ROOT/contracts/cli_help/_meta.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["python_version"])
PY
)"
  if [[ "$PYTHON_VERSION" != "$EXPECTED_PYTHON_VERSION" ]]; then
    echo "mkbundle.sh: Python 版本不符，預期 $EXPECTED_PYTHON_VERSION，實際 $PYTHON_VERSION" >&2
    exit 1
  fi

  # 以 P1 的正式實作先核對 golden，再計算 bundle 內的 Python package tree。
  "$PYTHON" -I - "$REPO_ROOT/contracts/golden/tree_sha256.ndjson" "$VERSION_DIR" <<'PY'
import json
import pathlib
import sys
import tempfile

import sw_core.release_manifest as manifest

golden_path = pathlib.Path(sys.argv[1])
temp_root = pathlib.Path(sys.argv[2])
for number, line in enumerate(golden_path.read_text(encoding="utf-8").splitlines(), 1):
    fixture = json.loads(line)
    with tempfile.TemporaryDirectory(prefix=".tree-sha256-", dir=temp_root) as raw_root:
        root = pathlib.Path(raw_root)
        for relative, content in fixture["files"].items():
            path = root.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        actual = manifest.tree_sha256(str(root))
        if actual != fixture["tree_sha256"]:
            raise SystemExit(f"tree_sha256 golden #{number} 不符：{actual}")
PY

  PACKAGE_TREE_SHA="$("$PYTHON" -I -c 'import sw_core.release_manifest as m; print(m.package_tree_sha256())')"
  PROFILE_TREE_SHA="$("$PYTHON" -I -c 'import sw_core.release_manifest as m, sys; print(m.tree_sha256(sys.argv[1]))' "$REPO_ROOT/sw_core/assets/profiles")"
  CLI_SHA="$(sha256_file "$VERSION_DIR/bin/$CLI_NAME")"
  LAUNCHER_SHA="$(sha256_file "$REPO_ROOT/serialwrapd.py")"
  RUNTIME_REALPATH="$(realpath "$PYTHON")"
  RUNTIME_SHA="$(sha256_file "$RUNTIME_REALPATH")"
  SKILL_SHA="$(sha256_file "$REPO_ROOT/sw_core/assets/skill/SKILL.md")"
  SKILL_WINDOWS_SHA="$(sha256_file "$REPO_ROOT/sw_core/assets/skill/SKILL_WINDOWS.md")"
  SUPERVISOR_SHA="$(sha256_file "$REPO_ROOT/sw_core/systemd_units.py")"
  GIT_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  CONTRACT_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD:contracts)"
  MANIFEST="$VERSION_DIR/release-manifest.json"

  python3 - "$MANIFEST" "$VERSION" "$TARGET" "$GIT_REVISION" "$CONTRACT_REVISION" \
    "$CLI_NAME" "$CLI_SHA" "$PACKAGE_TREE_SHA" "$PYTHON_VERSION" "$LAUNCHER_SHA" \
    "$RUNTIME_SHA" "$SKILL_SHA" "$SKILL_WINDOWS_SHA" "$PROFILE_TREE_SHA" "$SUPERVISOR_SHA" \
    "$REPO_ROOT" <<'PY'
import datetime
import json
import sys

(
    path,
    version,
    target,
    revision,
    contract_revision,
    cli_name,
    cli_sha,
    package_tree,
    python_version,
    launcher_sha,
    runtime_sha,
    skill_sha,
    skill_windows_sha,
    profiles_sha,
    supervisor_sha,
    repo_root,
) = sys.argv[1:17]
python_entry = ["py/bin/python3", "-I", "-B", "-m", "sw_core.cli"]
manifest = {
    "schema_version": 1,
    "bundle_version": version,
    "stage": "1A",
    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "target": target,
    "git_revision": revision,
    "contract_revision": contract_revision,
    "components": {
        "cli": {
            "implementation": "rust",
            "version": version,
            "git_revision": revision,
            "path": f"bin/{cli_name}",
            "sha256": cli_sha,
        },
        "python_cli_delegate": {
            "entry": python_entry,
            "tree_sha256": package_tree,
            "runtime_python_version": python_version,
        },
        "daemon": {
            "implementation": "python",
            "version": version,
            "git_revision": revision,
            "entry": ["py/bin/python3", "-I", "-B", f"{repo_root}/serialwrapd.py"],
            "launcher": {"path": f"{repo_root}/serialwrapd.py", "sha256": launcher_sha},
            "runtime": {
                "path": "py/bin/python3",
                "python_version": python_version,
                "sha256": runtime_sha,
            },
            "package": {
                "name": "serialwrap",
                "version": version,
                "wheel_sha256": None,
                "tree_sha256": package_tree,
            },
        },
        "assets": {
            "skill_sha256": skill_sha,
            "skill_windows_sha256": skill_windows_sha,
            "profiles_sha256": profiles_sha,
        },
        "supervisor": {
            "default_mode": "on-demand",
            "definition_sha256": supervisor_sha,
        },
    },
    "pairing": {
        "rule": "exact",
        "cli_version": version,
        "daemon_version": version,
    },
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY

  ln -sfn "../versions/$VERSION/bin/$CLI_NAME" "$OUT/bin/$CLI_NAME"
  if [[ "$(realpath "$OUT/bin/$CLI_NAME")" != "$(realpath "$VERSION_DIR/bin/$CLI_NAME")" ]]; then
    echo "mkbundle.sh: bin/$CLI_NAME symlink 未指向 versions/$VERSION/bin/$CLI_NAME" >&2
    exit 1
  fi

  SCHEMA="$REPO_ROOT/contracts/release_manifest.schema.json"
  if [[ ! -f "$SCHEMA" ]]; then
    echo "mkbundle.sh: P1 contracts 缺少 $SCHEMA" >&2
    exit 1
  fi
  python3 - "$SCHEMA" "$MANIFEST" <<'PY'
import json
import sys

import jsonschema

with open(sys.argv[1], encoding="utf-8") as handle:
    schema = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    document = json.load(handle)
validator = jsonschema.validators.validator_for(schema)(schema)
errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
if errors:
    for error in errors:
        print(f"{list(error.absolute_path)}: {error.message}", file=sys.stderr)
    raise SystemExit(1)
PY
fi

printf '%s\n' "$OUT"
