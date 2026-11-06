#!/usr/bin/env python3
"""Windows 正式 bundle 的組裝輔助（由 scripts/rust/mkbundle-windows.ps1 呼叫）。

子命令：
  isolation    確認 yaml／serial／sw_core 由 bundle runtime 載入（以 bundle runtime `-I -B` 執行）
  fingerprint  印出 bundle 內 sw_core 的程式碼指紋（以 bundle runtime `-I -B` 執行）
  manifest     產生 release-manifest.json 與內層 SHA256SUMS（以 bundle runtime `-I -B` 執行）
  validate     以 contracts/release_manifest.schema.json 驗證 manifest、核對內層 SHA256SUMS 與禁用字串
               （以含 jsonschema 的建置機 Python 執行）
  zip          把 bundle 根目錄打成 zip（條目一律用 `/`、排除 mkbundle owner marker）

與 Linux 的 mkbundle.sh 組成對齊，但 Windows 版 layout 為 versions/<V>/{bin,py,release-manifest.json,SHA256SUMS}，
runtime 為 CPython embeddable（py/python.exe），sw_core 位於 py/sw_core（Rust 的 install_layout 固定讀此處）。
"""

from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import json
import os
import pathlib
import re
import sys
import zipfile

OWNER_MARKER = ".mkbundle-owner"
TARGET = "x86_64-pc-windows-gnu"
SHA256SUMS_LINE = re.compile(r"([0-9a-f]{64})  (.+)")
TEST_HOOK_MARKERS = (
    b"SERIALWRAP_TEST_",
    b"SW_TEST_UPDATE_PAUSE_AFTER",
    b"SW_TEST_UPDATE_FORCE_VERIFY_FAIL",
)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_dir(path: str) -> pathlib.Path:
    return pathlib.Path(path).resolve()


def cmd_fingerprint(args: argparse.Namespace) -> int:
    vdir = _version_dir(args.version_dir)
    import sw_core.version as version

    loaded = pathlib.Path(version.__file__).resolve().parent
    if loaded != vdir / "py" / "sw_core":
        raise SystemExit(f"bundle runtime 載入了非預期 sw_core：{loaded}")
    fingerprint = version.compute_code_fingerprint(str(loaded))
    if not fingerprint or not re.fullmatch(r"[0-9a-f]{12}", fingerprint):
        raise SystemExit(f"程式碼指紋格式不符：{fingerprint!r}")
    print(fingerprint)
    return 0


def cmd_isolation(args: argparse.Namespace) -> int:
    """依賴與 sw_core 都必須由 bundle 載入，sys.path 不得含 bundle 之外的路徑。"""
    import serial
    import sw_core
    import yaml

    root = pathlib.Path(args.py_dir).resolve()
    for name, module in (("yaml", yaml), ("serial", serial), ("sw_core", sw_core)):
        path = pathlib.Path(module.__file__).resolve()
        if not path.is_relative_to(root):
            raise SystemExit(f"{name} 未由 bundle runtime 載入：{path}")
    for entry in sys.path:
        if entry and not pathlib.Path(entry).resolve().is_relative_to(root):
            raise SystemExit(f"sys.path 含 bundle 之外的路徑：{entry}")
    print("runtime isolation ok:", yaml.__file__)
    return 0


def _task_xml_template_sha(service_ctl: pathlib.Path) -> str:
    source = service_ctl.read_text(encoding="utf-8")
    start = source.index("pub const TASK_XML_TEMPLATE: &str = concat!(")
    match = re.search(r"concat!\((.*?)\n\s*\);", source[start:], re.S)
    if match is None:
        raise SystemExit("找不到 TASK_XML_TEMPLATE")
    parts = re.findall(r'^\s*("(?:\\.|[^"\\])*")\s*,?\s*$', match.group(1), re.M)
    if not parts:
        raise SystemExit("render_task_xml 樣板為空")
    template = "".join(ast.literal_eval(part) for part in parts)
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def cmd_manifest(args: argparse.Namespace) -> int:
    import sw_core.release_manifest as rm
    import sw_core.version as sv

    vdir = _version_dir(args.version_dir)
    py = vdir / "py"
    package = py / "sw_core"
    source = pathlib.Path(args.source_root).resolve()
    version = args.version
    if pathlib.Path(rm.__file__).resolve().parent != package:
        raise SystemExit(f"bundle runtime 載入了非預期 sw_core：{rm.__file__}")

    # tree_sha256 golden：與 Linux mkbundle 相同，先確認實作對得上 contracts。
    import tempfile

    golden = source / "contracts" / "golden" / "tree_sha256.ndjson"
    for number, line in enumerate(golden.read_text(encoding="utf-8").splitlines(), 1):
        fixture = json.loads(line)
        with tempfile.TemporaryDirectory(prefix=".tree-sha256-", dir=vdir) as raw_root:
            root = pathlib.Path(raw_root)
            for relative, content in fixture["files"].items():
                path = root.joinpath(*relative.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="")
            actual = rm.tree_sha256(str(root))
            if actual != fixture["tree_sha256"]:
                raise SystemExit(f"tree_sha256 golden #{number} 不符：{actual}")

    expected_py = json.loads((source / "contracts" / "cli_help" / "_meta.json").read_text(encoding="utf-8"))["python_version"]
    py_version = ".".join(str(part) for part in sys.version_info[:3])
    if py_version != expected_py:
        raise SystemExit(f"Python 版本不符，預期 {expected_py}，實際 {py_version}")

    fingerprint = sv.compute_code_fingerprint(str(package))
    if fingerprint != args.expected_fingerprint:
        raise SystemExit(f"編譯時嵌入的指紋 {args.expected_fingerprint} 與 bundle sw_core 指紋 {fingerprint} 不一致")

    package_tree = rm.package_tree_sha256()
    profiles_tree = rm.tree_sha256(str(package / "assets" / "profiles"))
    cli = vdir / "bin" / "serialwrap.exe"
    launcher = vdir / "bin" / "serialwrapd.exe"
    runtime = py / "python.exe"
    manifest = {
        "schema_version": 1,
        "bundle_version": version,
        "stage": "1A",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "target": TARGET,
        "git_revision": args.git_revision,
        "contract_revision": args.contract_revision,
        "components": {
            "cli": {
                "implementation": "rust",
                "version": version,
                "git_revision": args.git_revision,
                "path": "bin/serialwrap.exe",
                "sha256": sha256_file(cli),
            },
            "python_cli_delegate": {
                "entry": ["py/python.exe", "-I", "-B", "-m", "sw_core.cli"],
                "tree_sha256": package_tree,
                "runtime_python_version": py_version,
            },
            "daemon": {
                "implementation": "python",
                "version": version,
                "git_revision": args.git_revision,
                "entry": ["py/python.exe", "-I", "-B", "py/serialwrapd.py"],
                "state_schema": "python-state-v0",
                "code_fingerprint": fingerprint,
                "launcher": {"path": "bin/serialwrapd.exe", "sha256": sha256_file(launcher)},
                "runtime": {
                    "path": "py/python.exe",
                    "python_version": py_version,
                    "sha256": sha256_file(runtime),
                },
                "package": {
                    "name": "serialwrap",
                    "version": version,
                    "wheel_sha256": None,
                    "tree_sha256": package_tree,
                },
            },
            "assets": {
                "skill_sha256": sha256_file(package / "assets" / "skill" / "SKILL.md"),
                "skill_windows_sha256": sha256_file(package / "assets" / "skill" / "SKILL_WINDOWS.md"),
                "profiles_sha256": profiles_tree,
            },
            "supervisor": {
                "default_mode": "on-demand",
                "definition_sha256": sha256_file(package / "systemd_units.py"),
                "task_xml_template_sha256": _task_xml_template_sha(source / "crates" / "sw-pal" / "src" / "service_ctl.rs"),
            },
        },
        "pairing": {"rule": "exact", "cli_version": version, "daemon_version": version},
    }
    with (vdir / "release-manifest.json").open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    rows = []
    for directory, names, files in os.walk(vdir, followlinks=False):
        for name in files:
            path = pathlib.Path(directory) / name
            relative = path.relative_to(vdir).as_posix()
            if relative in {"release-manifest.json", "SHA256SUMS"}:
                continue
            rows.append((relative, sha256_file(path)))
    rows.sort(key=lambda item: item[0].encode("utf-8"))
    with (vdir / "SHA256SUMS").open("w", encoding="utf-8", newline="\n") as handle:
        for relative, digest in rows:
            handle.write(f"{digest}  {relative}\n")
    print(f"manifest ok; {len(rows)} files in SHA256SUMS; fingerprint {fingerprint}")
    return 0


def parse_sha256sums(text: str) -> tuple[dict[str, str], list[str]]:
    """解析內層 SHA256SUMS；格式錯誤或重複路徑都回報，不靜默覆寫。"""
    listed: dict[str, str] = {}
    errors: list[str] = []
    for line in text.splitlines():
        matched = SHA256SUMS_LINE.fullmatch(line)
        if matched is None:
            errors.append(f"SHA256SUMS 格式不合法：{line!r}")
            continue
        digest, relative = matched.groups()
        if "\\" in relative or relative.startswith("/") or ".." in relative.split("/"):
            errors.append(f"SHA256SUMS 路徑不合法：{relative}")
            continue
        if relative in listed:
            errors.append(f"SHA256SUMS 路徑重複：{relative}")
            continue
        listed[relative] = digest
    return listed, errors


def cmd_validate(args: argparse.Namespace) -> int:
    import jsonschema

    root = pathlib.Path(args.bundle_root).resolve()
    vdir = root / "versions" / args.version
    source = pathlib.Path(args.source_root).resolve()
    errors: list[str] = []

    schema = json.loads((source / "contracts" / "release_manifest.schema.json").read_text(encoding="utf-8"))
    manifest = json.loads((vdir / "release-manifest.json").read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    for error in sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path)):
        errors.append(f"manifest schema {list(error.absolute_path)}: {error.message}")
    values = {
        "bundle_version": manifest.get("bundle_version"),
        "components.cli.version": manifest["components"]["cli"]["version"],
        "components.daemon.version": manifest["components"]["daemon"]["version"],
        "pairing.cli_version": manifest["pairing"]["cli_version"],
        "pairing.daemon_version": manifest["pairing"]["daemon_version"],
    }
    for key, value in values.items():
        if value != args.version:
            errors.append(f"manifest {key}={value!r}，預期 {args.version}")
    if manifest.get("stage") == "rust-1b-dev":
        errors.append("manifest 是 delegate-sentinel 測試 bundle")
    if manifest.get("target") != TARGET:
        errors.append(f"manifest target={manifest.get('target')!r}")

    sums_text = (vdir / "SHA256SUMS").read_bytes()
    if b"\r" in sums_text:
        errors.append("內層 SHA256SUMS 含 CR（必須 LF 行尾）")
    listed, sums_errors = parse_sha256sums(sums_text.decode("utf-8"))
    errors.extend(sums_errors)
    actual_files = {
        path.relative_to(vdir).as_posix()
        for path in vdir.rglob("*")
        if path.is_file() and path.name not in {"SHA256SUMS", "release-manifest.json"}
    }
    for relative in sorted(actual_files - set(listed)):
        errors.append(f"SHA256SUMS 缺列：{relative}")
    for relative in sorted(set(listed) - actual_files):
        errors.append(f"SHA256SUMS 列出不存在的檔案：{relative}")
    for relative in sorted(actual_files & set(listed)):
        if sha256_file(vdir / relative) != listed[relative]:
            errors.append(f"SHA256SUMS 雜湊不符：{relative}")

    for exe in sorted((vdir / "bin").glob("*.exe")) + sorted((root / "bin").glob("*.exe")):
        data = exe.read_bytes()
        for marker in TEST_HOOK_MARKERS:
            if marker in data:
                errors.append(f"{exe.relative_to(root).as_posix()} 含測試鉤子 {marker.decode()}")
    for path in root.rglob("delegate_sentinel*"):
        errors.append(f"bundle 含 delegate sentinel：{path}")
    for name in ("serialwrap.exe", "serialwrapd.exe"):
        if sha256_file(root / "bin" / name) != sha256_file(vdir / "bin" / name):
            errors.append(f"bin\\{name} 與 versions\\{args.version}\\bin\\{name} 內容不同")
    if (root / "bin" / "current-release.json").is_file():
        pointer = json.loads((root / "bin" / "current-release.json").read_text(encoding="utf-8"))
        if pointer != {"bundle_version": args.version, "dir": f"versions/{args.version}"}:
            errors.append(f"current-release.json 內容不符：{pointer}")
    else:
        errors.append("缺少 bin/current-release.json")
    if (vdir / "py" / "python312._pth").is_file():
        pth = (vdir / "py" / "python312._pth").read_text(encoding="utf-8")
        if re.search(r"[A-Za-z]:[\\/]|^/", pth, re.M):
            errors.append("python312._pth 含絕對路徑，bundle 不可搬移")
    else:
        errors.append("缺少 py/python312._pth")

    if errors:
        for message in errors:
            print(f"validate: {message}", file=sys.stderr)
        return 1
    print(f"validate ok: schema、{len(listed)} 項內層 SHA256SUMS、無測試鉤子、._pth 無絕對路徑")
    return 0


def cmd_zip(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.bundle_root).resolve()
    out = pathlib.Path(args.output).resolve()
    top = root.name
    entries = []
    for directory, names, files in os.walk(root, followlinks=False):
        names.sort()
        for name in sorted(files):
            path = pathlib.Path(directory) / name
            if path.is_symlink():
                raise SystemExit(f"bundle 不得含 symlink：{path}")
            if path.name == OWNER_MARKER and path.parent == root:
                continue
            entries.append((path, f"{top}/{path.relative_to(root).as_posix()}"))
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, arcname in entries:
            info = zipfile.ZipInfo.from_file(path, arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as handle:
                archive.writestr(info, handle.read(), compresslevel=9)
    print(f"zip ok: {out} ({len(entries)} entries)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fingerprint")
    p.add_argument("--version-dir", required=True)
    p.set_defaults(func=cmd_fingerprint)
    p = sub.add_parser("isolation")
    p.add_argument("--py-dir", required=True)
    p.set_defaults(func=cmd_isolation)
    p = sub.add_parser("manifest")
    p.add_argument("--version-dir", required=True)
    p.add_argument("--source-root", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--git-revision", required=True)
    p.add_argument("--contract-revision", required=True)
    p.add_argument("--expected-fingerprint", required=True)
    p.set_defaults(func=cmd_manifest)
    p = sub.add_parser("validate")
    p.add_argument("--bundle-root", required=True)
    p.add_argument("--source-root", required=True)
    p.add_argument("--version", required=True)
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("zip")
    p.add_argument("--bundle-root", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_zip)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
