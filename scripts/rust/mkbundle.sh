#!/usr/bin/env bash
# 建立 Stage 1A 自含式開發 bundle；`--delegate-sentinel` 保留後續管理面測試用的精簡 bundle。
set -euo pipefail

usage() {
  cat <<'EOF'
用法：
  scripts/rust/mkbundle.sh --dev --out <目錄> [--target <triple>] [--profile debug|release] [--version-override <版本>] [--with-com0com <signed zip>] [--with-com0com-fixture <fixture zip>] [--print-dir]
  scripts/rust/mkbundle.sh --dev --delegate-sentinel --out <目錄> [其他選項]

選項：
  --dev                    建立開發 bundle（目前唯一支援的 bundle 類型）
  --delegate-sentinel      將委派入口改為 P4 測試 sentinel
  --target <triple>        指定 Rust target；Windows 正式 bundle 由擁有者 runner 建置
  --profile <name>         選擇 debug 或 release，預設為 debug
  --version-override <v>   以同一編譯輸入覆寫 CLI 與 daemon bundle 版本
  --with-com0com <zip>     驗證上游簽章套件雜湊並抽取 x64 driver payload
  --with-com0com-fixture <zip> 僅供測試：略過上游雜湊驗證，仍檢查完整 x64 payload
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
VERSION_OVERRIDE=""
COM0COM_ZIP=""
COM0COM_FIXTURE=0

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
    --version-override)
      if [[ $# -lt 2 ]]; then
        echo "mkbundle.sh: --version-override 缺少版本參數" >&2
        exit 2
      fi
      VERSION_OVERRIDE="$2"
      shift 2
      ;;
    --with-com0com)
      if [[ $# -lt 2 ]]; then
        echo "mkbundle.sh: --with-com0com 缺少 signed zip" >&2
        exit 2
      fi
      COM0COM_ZIP="$2"
      shift 2
      ;;
    --with-com0com-fixture)
      if [[ $# -lt 2 ]]; then
        echo "mkbundle.sh: --with-com0com-fixture 缺少 zip" >&2
        exit 2
      fi
      COM0COM_ZIP="$2"
      COM0COM_FIXTURE=1
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
if [[ -n "$COM0COM_ZIP" && ! -f "$COM0COM_ZIP" ]]; then
  echo "mkbundle.sh: com0com zip 不存在：$COM0COM_ZIP" >&2
  exit 2
fi
if [[ "$SENTINEL" -eq 1 && -n "$COM0COM_ZIP" ]]; then
  echo "mkbundle.sh: com0com 需使用完整 --dev bundle，不能與 --delegate-sentinel 合併" >&2
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

VERSION="${VERSION_OVERRIDE:-$(tr -d ' \n' < "$REPO_ROOT/VERSION")}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "mkbundle.sh: bundle version 必須符合 MAJOR.MINOR.PATCH" >&2
  exit 2
fi

BUILD_ARGS=(build -p sw-cli -p sw-daemon)
if [[ -n "$COM0COM_ZIP" ]]; then
  BUILD_ARGS+=(-p sw-com0com-helper)
fi
if [[ "$PROFILE" == release ]]; then
  BUILD_ARGS+=(--release)
fi
if [[ "$TARGET" != "$HOST_TRIPLE" ]]; then
  BUILD_ARGS+=(--target "$TARGET")
fi
(cd "$REPO_ROOT" && SW_BUNDLE_VERSION="$VERSION" CARGO_TARGET_DIR="$REPO_ROOT/target" cargo "${BUILD_ARGS[@]}" --quiet)

BUILD_DIR="$REPO_ROOT/target"
if [[ "$TARGET" != "$HOST_TRIPLE" ]]; then
  BUILD_DIR+="/$TARGET"
fi
BUILD_DIR+="/$PROFILE"
BIN_SRC="$BUILD_DIR/serialwrap"
DAEMON_SRC="$BUILD_DIR/serialwrapd"
CLI_NAME=serialwrap
DAEMON_NAME=serialwrapd
if [[ ! -x "$BIN_SRC" && -x "$BUILD_DIR/serialwrap.exe" ]]; then
  BIN_SRC="$BUILD_DIR/serialwrap.exe"
  DAEMON_SRC="$BUILD_DIR/serialwrapd.exe"
  CLI_NAME=serialwrap.exe
  DAEMON_NAME=serialwrapd.exe
fi
if [[ ! -x "$BIN_SRC" || ! -x "$DAEMON_SRC" ]]; then
  echo "mkbundle.sh: cargo build 後仍找不到 $BIN_SRC" >&2
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
  # 輸出位置可能是指向版本檔的 symlink；先移除 symlink，避免覆寫其目標。
  cp --remove-destination "$BIN_SRC" "$OUT/bin/$CLI_NAME"
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
  if [[ -e "$VERSION_DIR" ]]; then
    rm -rf -- "$VERSION_DIR"
  fi
  mkdir -p "$VERSION_DIR/bin" "$OUT/bin"
  cp "$BIN_SRC" "$VERSION_DIR/bin/$CLI_NAME"
  cp "$DAEMON_SRC" "$VERSION_DIR/bin/$DAEMON_NAME"
  chmod +x "$VERSION_DIR/bin/$CLI_NAME"
  chmod +x "$VERSION_DIR/bin/$DAEMON_NAME"

  HELPER_NAME=sw-com0com-helper
  HELPER_MANIFEST_PATH=""
  COM0COM_SHA=""
  if [[ -n "$COM0COM_ZIP" ]]; then
    if [[ "$TARGET" == *-windows-* ]]; then
      HELPER_NAME+=.exe
    fi
    HELPER_SRC="$BUILD_DIR/$HELPER_NAME"
    if [[ ! -x "$HELPER_SRC" ]]; then
      echo "mkbundle.sh: cargo build 後仍找不到 com0com helper：$HELPER_SRC" >&2
      exit 1
    fi
    cp "$HELPER_SRC" "$VERSION_DIR/bin/$HELPER_NAME"
    chmod +x "$VERSION_DIR/bin/$HELPER_NAME"
    HELPER_MANIFEST_PATH="bin/$HELPER_NAME"

    COM0COM_DIR="$VERSION_DIR/optional/com0com"
    mkdir -p "$COM0COM_DIR"
    EXTRACT_ARGS=("$COM0COM_ZIP" "$COM0COM_DIR" --provenance "$REPO_ROOT/packaging/com0com/PROVENANCE.json")
    if [[ "$COM0COM_FIXTURE" -eq 1 ]]; then
      EXTRACT_ARGS+=(--fixture)
    fi
    python3 "$REPO_ROOT/scripts/rust/extract_com0com.py" "${EXTRACT_ARGS[@]}"
    cp "$REPO_ROOT/packaging/com0com/LICENSE-GPLv2.txt" "$COM0COM_DIR/"
    cp "$REPO_ROOT/packaging/com0com/SOURCE.md" "$COM0COM_DIR/"
    cp "$REPO_ROOT/packaging/com0com/PROVENANCE.json" "$COM0COM_DIR/"
    COM0COM_SHA="$(python3 - "$VERSION_DIR" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
files = sorted(
    (path for path in (root / "optional/com0com").rglob("*") if path.is_file()),
    key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
)
content = bytearray()
for path in files:
    content.extend(path.relative_to(root).as_posix().encode("utf-8"))
    content.append(0)
    content.extend(path.read_bytes())
print(hashlib.sha256(content).hexdigest())
PY
    )"
    COM0COM_HELPER_SHA="$(sha256sum "$VERSION_DIR/$HELPER_MANIFEST_PATH" | cut -d ' ' -f 1)"
  fi

  PY_DIR="$VERSION_DIR/py"
  python3 -m venv --copies --system-site-packages "$PY_DIR"
  if [[ -L "$PY_DIR/lib64" ]]; then
    rm -- "$PY_DIR/lib64"
  fi
  PYTHON="$PY_DIR/bin/python3"
  PY_SITE="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
  mkdir -p "$PY_SITE"

  python3 - "$REPO_ROOT/sw_core" "$PY_DIR/sw_core" "$PY_SITE/sw_core" \
    "$REPO_ROOT/serialwrapd.py" "$PY_DIR/serialwrapd.py" "$VERSION" \
    "$PY_DIR/VERSION" "$PY_SITE/VERSION" <<'PY'
import os
import pathlib
import shutil
import sys

source, bundled_package, installed_package, daemon_source, daemon_target, version, *version_paths = sys.argv[1:]
source_path = pathlib.Path(source)
for root, dirs, files in os.walk(source_path, followlinks=False):
    for name in dirs + files:
        path = pathlib.Path(root) / name
        if path.is_symlink():
            raise SystemExit(f"sw_core 含 symlink，無法建立自含 bundle：{path}")

ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
shutil.copytree(source_path, bundled_package, ignore=ignore)
shutil.copytree(source_path, installed_package, ignore=ignore)
shutil.copy2(daemon_source, daemon_target)
for path in map(pathlib.Path, version_paths):
    path.write_text(version + "\n", encoding="utf-8")
PY

  PYTHON_VERSION="$("$PYTHON" -I -B -c 'import platform; print(platform.python_version())')"
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
  "$PYTHON" -I -B - "$REPO_ROOT/contracts/golden/tree_sha256.ndjson" "$VERSION_DIR" <<'PY'
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

  PACKAGE_TREE_SHA="$("$PYTHON" -I -B -c 'import sw_core.release_manifest as m; print(m.package_tree_sha256())')"
  PROFILE_TREE_SHA="$("$PYTHON" -I -B -c 'import sw_core.release_manifest as m, sys; print(m.tree_sha256(sys.argv[1]))' "$REPO_ROOT/sw_core/assets/profiles")"
  CLI_SHA="$(sha256_file "$VERSION_DIR/bin/$CLI_NAME")"
  LAUNCHER_SHA="$(sha256_file "$VERSION_DIR/bin/$DAEMON_NAME")"
  RUNTIME_REALPATH="$(realpath "$PYTHON")"
  RUNTIME_SHA="$(sha256_file "$RUNTIME_REALPATH")"
  SKILL_SHA="$(sha256_file "$REPO_ROOT/sw_core/assets/skill/SKILL.md")"
  SKILL_WINDOWS_SHA="$(sha256_file "$REPO_ROOT/sw_core/assets/skill/SKILL_WINDOWS.md")"
  SUPERVISOR_SHA="$(sha256_file "$REPO_ROOT/sw_core/systemd_units.py")"
  TASK_XML_TEMPLATE_SHA="$("$PYTHON" -I -B - "$REPO_ROOT/crates/sw-pal/src/service_ctl.rs" <<'PY'
import ast
import hashlib
import pathlib
import re
import sys

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
start = source.index("pub const TASK_XML_TEMPLATE: &str = concat!(")
match = re.search(r"concat!\((.*?)\n\s*\);", source[start:], re.S)
if match is None:
    raise SystemExit("找不到 TASK_XML_TEMPLATE")
parts = re.findall(r'^\s*("(?:\\.|[^"\\])*")\s*,?\s*$', match.group(1), re.M)
if not parts:
    raise SystemExit("render_task_xml 樣板為空")
template = "".join(ast.literal_eval(part) for part in parts)
print(hashlib.sha256(template.encode("utf-8")).hexdigest())
PY
  )"
  GIT_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  CONTRACT_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD:contracts)"
  MANIFEST="$VERSION_DIR/release-manifest.json"

  python3 - "$MANIFEST" "$VERSION" "$TARGET" "$GIT_REVISION" "$CONTRACT_REVISION" \
    "$CLI_NAME" "$CLI_SHA" "$PACKAGE_TREE_SHA" "$PYTHON_VERSION" "$LAUNCHER_SHA" \
    "$RUNTIME_SHA" "$SKILL_SHA" "$SKILL_WINDOWS_SHA" "$PROFILE_TREE_SHA" "$SUPERVISOR_SHA" \
    "$TASK_XML_TEMPLATE_SHA" "$COM0COM_SHA" "$HELPER_MANIFEST_PATH" "${COM0COM_HELPER_SHA:-}" <<'PY'
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
    task_xml_template_sha,
    com0com_sha,
    com0com_helper,
    com0com_helper_sha,
) = sys.argv[1:20]
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
            "entry": ["py/bin/python3", "-I", "-B", "py/serialwrapd.py"],
            "state_schema": "python-state-v0",
            "launcher": {"path": "bin/serialwrapd", "sha256": launcher_sha},
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
            "task_xml_template_sha256": task_xml_template_sha,
        },
    },
    "pairing": {
        "rule": "exact",
        "cli_version": version,
        "daemon_version": version,
    },
}
if com0com_sha:
    manifest["components"]["com0com"] = {
        "optional": True,
        "default_selected": False,
        "requires_elevation": True,
        "experimental": True,
        "version": "3.0.0.0",
        "path": "optional/com0com/",
        "sha256": com0com_sha,
        "license": "GPL-2.0",
        "license_file": "optional/com0com/LICENSE-GPLv2.txt",
        "source_offer_file": "optional/com0com/SOURCE.md",
        "provenance_file": "optional/com0com/PROVENANCE.json",
        "helper": {"path": com0com_helper, "sha256": com0com_helper_sha},
        "default_pair": ["COM20", "COM21"],
    }
with open(path, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY

  ln -sfn "../versions/$VERSION/bin/$CLI_NAME" "$OUT/bin/$CLI_NAME"
  ln -sfn "../versions/$VERSION/bin/$DAEMON_NAME" "$OUT/bin/$DAEMON_NAME"
  if [[ "$(realpath "$OUT/bin/$CLI_NAME")" != "$(realpath "$VERSION_DIR/bin/$CLI_NAME")" ]]; then
    echo "mkbundle.sh: bin/$CLI_NAME symlink 未指向 versions/$VERSION/bin/$CLI_NAME" >&2
    exit 1
  fi
  if [[ "$(realpath "$OUT/bin/$DAEMON_NAME")" != "$(realpath "$VERSION_DIR/bin/$DAEMON_NAME")" ]]; then
    echo "mkbundle.sh: bin/$DAEMON_NAME symlink 未指向 versions/$VERSION/bin/$DAEMON_NAME" >&2
    exit 1
  fi

  if find "$VERSION_DIR" -type l -print -quit | grep -q .; then
    echo "mkbundle.sh: dev bundle 不得含 symlink：$(find "$VERSION_DIR" -type l -print -quit)" >&2
    exit 1
  fi

  python3 - "$VERSION_DIR" <<'PY'
import hashlib
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
rows = []
for directory, names, files in os.walk(root, followlinks=False):
    for name in names + files:
        path = pathlib.Path(directory) / name
        if path.is_symlink():
            raise SystemExit(f"dev bundle 不得含 symlink：{path}")
    for name in files:
        path = pathlib.Path(directory) / name
        relative = path.relative_to(root).as_posix()
        if relative in {"release-manifest.json", "SHA256SUMS"}:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append((relative, digest))
rows.sort(key=lambda item: item[0].encode("utf-8"))
with (root / "SHA256SUMS").open("w", encoding="utf-8", newline="\n") as handle:
    for relative, digest in rows:
        handle.write(f"{digest}  {relative}\n")
PY

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
