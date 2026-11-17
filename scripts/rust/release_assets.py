#!/usr/bin/env python3
"""驗證並封裝 Stage 2 RC／正式版 release assets。"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

TAG_RE = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?:-rc\.(?P<rc>[1-9][0-9]*))?$")
SHA256_ROW_RE = re.compile(r"^(?P<sha>[0-9a-f]{64})  (?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)$")
BUNDLE_PLATFORMS = ("linux", "windows")


class ReleaseValidationError(ValueError):
    """release tag 或資產不符合 Stage 2 發布條件。"""


@dataclasses.dataclass(frozen=True)
class ReleaseTag:
    tag: str
    version: str
    kind: str
    rc_number: int | None = None


def parse_tag(tag: str) -> ReleaseTag:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleaseValidationError(f"release tag 格式不合法：{tag!r}；僅接受 v<VERSION> 或 v<VERSION>-rc.N")
    rc_text = match.group("rc")
    return ReleaseTag(
        tag=tag,
        version=match.group("version"),
        kind="rc" if rc_text is not None else "release",
        rc_number=int(rc_text) if rc_text is not None else None,
    )


def read_version_file(path: Path) -> str:
    try:
        version = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ReleaseValidationError(f"無法讀取 VERSION：{exc}") from exc
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ReleaseValidationError(f"VERSION 格式不合法：{version!r}")
    return version


def check_tag_version(tag: str, version_file: Path) -> ReleaseTag:
    parsed = parse_tag(tag)
    expected = read_version_file(version_file)
    if parsed.version != expected:
        raise ReleaseValidationError(f"tag {tag} 的版本 {parsed.version} 與 VERSION {expected} 不符")
    return parsed


def rc_candidates(releases: Sequence[Mapping[str, Any]], version: str) -> list[str]:
    """回傳同一版本且已發布為 prerelease 的 RC tag，按 RC 序號由新至舊排列。"""
    found: dict[int, str] = {}
    for item in releases:
        if item.get("isDraft") is True or item.get("isPrerelease") is not True:
            continue
        tag = item.get("tagName")
        if not isinstance(tag, str):
            continue
        try:
            parsed = parse_tag(tag)
        except ReleaseValidationError:
            continue
        if parsed.kind == "rc" and parsed.version == version and parsed.rc_number is not None:
            found[parsed.rc_number] = tag
    return [found[number] for number in sorted(found, reverse=True)]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_asset_name(version: str, platform: str) -> str:
    if platform == "linux":
        return f"serialwrap-{version}-linux-x86_64.tar.gz"
    if platform == "windows":
        return f"serialwrap-{version}-windows-x86_64.zip"
    raise ReleaseValidationError(f"不支援的 bundle 平台：{platform}")


def _validate_manifest(manifest_raw: bytes, *, version: str, tag_commit: str, source: str) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(f"{source} release-manifest.json 無法解析：{exc}") from exc
    if not isinstance(manifest, dict):
        raise ReleaseValidationError(f"{source} release-manifest.json 根節點不是 object")
    if manifest.get("bundle_version") != version:
        raise ReleaseValidationError(
            f"{source} manifest bundle_version={manifest.get('bundle_version')!r}，預期 {version}"
        )
    if manifest.get("stage") != "rust-2":
        raise ReleaseValidationError(
            f"{source} manifest 不是 Stage 2：stage={manifest.get('stage')!r}（預期 'rust-2'）"
        )
    if manifest.get("git_revision") != tag_commit:
        raise ReleaseValidationError(
            f"{source} manifest git_revision={manifest.get('git_revision')!r}，tag commit={tag_commit!r}"
        )
    components = manifest.get("components")
    if not isinstance(components, dict):
        raise ReleaseValidationError(f"{source} manifest 缺少 components object")
    for component_name in ("cli", "daemon"):
        component = components.get(component_name)
        if not isinstance(component, dict):
            raise ReleaseValidationError(f"{source} manifest 缺少 components.{component_name}")
        if component.get("implementation") != "rust" or component.get("version") != version:
            raise ReleaseValidationError(
                f"{source} components.{component_name} 必須為 Rust {version}：{component}"
            )
    return manifest


def _safe_archive_path(name: str, *, source: str) -> PurePosixPath:
    if "\\" in name:
        raise ReleaseValidationError(f"{source} archive 含非 POSIX 路徑：{name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseValidationError(f"{source} archive 含不安全路徑：{name!r}")
    return path


def _manifest_from_linux_tar(archive: Path, version: str) -> bytes:
    expected = PurePosixPath(f"serialwrap-{version}-linux-x86_64/versions/{version}/release-manifest.json")
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            matches: list[tarfile.TarInfo] = []
            names: set[str] = set()
            for member in tar.getmembers():
                path = _safe_archive_path(member.name, source=archive.name)
                if member.name in names:
                    raise ReleaseValidationError(f"{archive.name} archive 有重複路徑：{member.name}")
                names.add(member.name)
                if path == expected:
                    matches.append(member)
            if len(matches) != 1 or not matches[0].isfile():
                raise ReleaseValidationError(f"{archive.name} 缺少唯一一般檔 {expected}")
            handle = tar.extractfile(matches[0])
            if handle is None:
                raise ReleaseValidationError(f"{archive.name} 無法讀取 {expected}")
            return handle.read()
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseValidationError(f"Linux bundle 無法讀取：{exc}") from exc


def _manifest_from_windows_zip(archive: Path, version: str) -> bytes:
    expected = PurePosixPath(f"serialwrap-{version}-windows-x86_64/versions/{version}/release-manifest.json")
    try:
        with zipfile.ZipFile(archive) as zipped:
            matches: list[zipfile.ZipInfo] = []
            names: set[str] = set()
            for member in zipped.infolist():
                path = _safe_archive_path(member.filename.rstrip("/"), source=archive.name)
                if member.filename in names:
                    raise ReleaseValidationError(f"{archive.name} archive 有重複路徑：{member.filename}")
                names.add(member.filename)
                if path == expected:
                    mode = member.external_attr >> 16
                    if mode & 0o170000 == 0o120000:
                        raise ReleaseValidationError(f"{archive.name} manifest 不得為 symlink")
                    matches.append(member)
            if len(matches) != 1:
                raise ReleaseValidationError(f"{archive.name} 缺少唯一 {expected}")
            return zipped.read(matches[0])
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseValidationError(f"Windows bundle 無法讀取：{exc}") from exc


def _validate_archive_manifest(path: Path, platform: str, *, version: str, tag_commit: str) -> dict[str, Any]:
    if platform == "linux":
        raw = _manifest_from_linux_tar(path, version)
    else:
        raw = _manifest_from_windows_zip(path, version)
    return _validate_manifest(raw, version=version, tag_commit=tag_commit, source=platform)


def _parse_sums(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseValidationError(f"SHA256SUMS 無法讀取：{exc}") from exc
    rows: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        match = SHA256_ROW_RE.fullmatch(line)
        if match is None:
            raise ReleaseValidationError(f"SHA256SUMS 第 {line_number} 行格式錯誤：{line!r}")
        name = match.group("name")
        if name in rows:
            raise ReleaseValidationError(f"SHA256SUMS 有重複檔名：{name}")
        rows[name] = match.group("sha")
    return rows


def validate_assets(*, tag: str, version_file: Path, tag_commit: str, assets_dir: Path) -> dict[str, Any]:
    parsed = check_tag_version(tag, version_file)
    if re.fullmatch(r"[0-9a-f]{40}", tag_commit) is None:
        raise ReleaseValidationError(f"tag commit 必須是完整 40 位小寫 Git SHA：{tag_commit!r}")
    if assets_dir.is_symlink() or not assets_dir.is_dir():
        raise ReleaseValidationError(f"release assets 目錄不存在或不是一般目錄：{assets_dir}")
    root = assets_dir.resolve()
    expected_names = {
        _bundle_asset_name(parsed.version, "linux"),
        _bundle_asset_name(parsed.version, "windows"),
        "RC-INSTALL.md",
        "SHA256SUMS",
    }
    actual_paths = list(root.iterdir())
    if any(not path.is_file() or path.is_symlink() for path in actual_paths):
        raise ReleaseValidationError("release assets 目錄只允許一般檔案")
    actual_names = {path.name for path in actual_paths}
    if actual_names != expected_names:
        raise ReleaseValidationError(
            f"release asset 集合不符：missing={sorted(expected_names - actual_names)} extra={sorted(actual_names - expected_names)}"
        )
    sums = _parse_sums(root / "SHA256SUMS")
    asset_names = expected_names - {"SHA256SUMS"}
    if set(sums) != asset_names:
        raise ReleaseValidationError(
            f"SHA256SUMS 列表不符：missing={sorted(asset_names - set(sums))} extra={sorted(set(sums) - asset_names)}"
        )
    for name in sorted(asset_names):
        actual_sha = sha256_file(root / name)
        if sums[name] != actual_sha:
            raise ReleaseValidationError(
                f"SHA256 不符：{name} expected={sums[name]} actual={actual_sha}"
            )
    manifests = {
        platform: _validate_archive_manifest(
            root / _bundle_asset_name(parsed.version, platform),
            platform,
            version=parsed.version,
            tag_commit=tag_commit,
        )
        for platform in BUNDLE_PLATFORMS
    }
    return {
        "kind": parsed.kind,
        "version": parsed.version,
        "tag": parsed.tag,
        "tag_commit": tag_commit,
        "git_revision": tag_commit,
        "assets": sorted(asset_names),
        "manifest_stages": {platform: manifests[platform]["stage"] for platform in BUNDLE_PLATFORMS},
    }


def _source_manifest(bundle: Path, *, version: str, tag_commit: str, platform: str) -> dict[str, Any]:
    if bundle.is_symlink():
        raise ReleaseValidationError(f"{platform} bundle 根目錄不得為 symlink：{bundle}")
    root = bundle.resolve()
    if not root.is_dir() or root.name != f"serialwrap-{version}-{platform}-x86_64":
        raise ReleaseValidationError(f"{platform} bundle 根目錄名稱或型別錯誤：{bundle}")
    version_root = root / "versions" / version
    manifest_path = version_root / "release-manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ReleaseValidationError(f"{platform} bundle 缺少一般檔 versions/{version}/release-manifest.json")
    return _validate_manifest(manifest_path.read_bytes(), version=version, tag_commit=tag_commit, source=platform)


def _ensure_bundle_paths_in_root(root: Path, bundle: Path, platform: str) -> list[Path]:
    entries = [root, *sorted(bundle.rglob("*"), key=lambda item: item.relative_to(bundle).as_posix())]
    for entry in entries:
        if entry == bundle / ".mkbundle-owner":
            continue
        if entry.is_symlink():
            if platform == "windows":
                raise ReleaseValidationError(f"Windows bundle 不得含 symlink：{entry}")
            try:
                entry.resolve(strict=True).relative_to(bundle.resolve())
            except (OSError, ValueError) as exc:
                raise ReleaseValidationError(f"Linux bundle symlink 無效或逸出 bundle：{entry}") from exc
        elif not entry.is_file() and not entry.is_dir():
            raise ReleaseValidationError(f"bundle 含非一般檔案：{entry}")
    return entries


def _package_linux_bundle(bundle: Path, destination: Path) -> None:
    root = bundle.resolve()
    entries = _ensure_bundle_paths_in_root(root, bundle.resolve(), "linux")
    with tarfile.open(destination, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        tar.add(root, arcname=root.name, recursive=False)
        for entry in entries[1:]:
            if entry == root / ".mkbundle-owner":
                continue
            tar.add(entry, arcname=f"{root.name}/{entry.relative_to(root).as_posix()}", recursive=False)


def _package_windows_bundle(bundle: Path, destination: Path) -> None:
    root = bundle.resolve()
    entries = _ensure_bundle_paths_in_root(root, root, "windows")
    with zipfile.ZipFile(destination, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
        for entry in entries[1:]:
            if entry == root / ".mkbundle-owner" or entry.is_dir():
                continue
            arcname = f"{root.name}/{entry.relative_to(root).as_posix()}"
            zipped.write(entry, arcname)


def prepare_rc_assets(
    *,
    tag: str,
    version_file: Path,
    tag_commit: str,
    linux_bundle: Path,
    windows_bundle: Path,
    guide: Path,
    out_dir: Path,
) -> dict[str, Any]:
    parsed = check_tag_version(tag, version_file)
    if parsed.kind != "rc":
        raise ReleaseValidationError(f"RC assets 只能為 v<VERSION>-rc.N tag：{tag}")
    if re.fullmatch(r"[0-9a-f]{40}", tag_commit) is None:
        raise ReleaseValidationError(f"tag commit 必須是完整 40 位小寫 Git SHA：{tag_commit!r}")
    for platform, bundle in (("linux", linux_bundle), ("windows", windows_bundle)):
        _source_manifest(bundle, version=parsed.version, tag_commit=tag_commit, platform=platform)
    if not guide.is_file() or guide.is_symlink():
        raise ReleaseValidationError(f"RC 試裝說明不存在或不是一般檔案：{guide}")
    if out_dir.exists():
        raise ReleaseValidationError(f"輸出目錄已存在，拒絕覆寫：{out_dir}")

    out_dir.mkdir(parents=True)
    try:
        linux_name = _bundle_asset_name(parsed.version, "linux")
        windows_name = _bundle_asset_name(parsed.version, "windows")
        _package_linux_bundle(linux_bundle, out_dir / linux_name)
        _package_windows_bundle(windows_bundle, out_dir / windows_name)
        shutil.copyfile(guide, out_dir / "RC-INSTALL.md")
        asset_names = sorted((linux_name, windows_name, "RC-INSTALL.md"))
        (out_dir / "SHA256SUMS").write_text(
            "".join(f"{sha256_file(out_dir / name)}  {name}\n" for name in asset_names),
            encoding="ascii",
            newline="\n",
        )
        return validate_assets(tag=tag, version_file=version_file, tag_commit=tag_commit, assets_dir=out_dir)
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise


def _write_github_output(path: Path | None, values: Mapping[str, str]) -> None:
    if path is not None:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def _cmd_check_tag(args: argparse.Namespace) -> int:
    parsed = check_tag_version(args.tag, args.version_file)
    output = {"kind": parsed.kind, "version": parsed.version, "rc_number": str(parsed.rc_number or "")}
    _write_github_output(args.github_output, output)
    print(json.dumps({"tag": parsed.tag, **output}, sort_keys=True))
    return 0


def _cmd_list_rc(args: argparse.Namespace) -> int:
    version = read_version_file(args.version_file)
    try:
        releases = json.loads(args.releases_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(f"GitHub release 清單無法解析：{exc}") from exc
    if not isinstance(releases, list):
        raise ReleaseValidationError("GitHub release 清單必須是 JSON array")
    for tag in rc_candidates(releases, version):
        print(tag)
    return 0


def _cmd_validate_assets(args: argparse.Namespace) -> int:
    report = validate_assets(
        tag=args.tag,
        version_file=args.version_file,
        tag_commit=args.tag_commit,
        assets_dir=args.assets_dir,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    report = prepare_rc_assets(
        tag=args.tag,
        version_file=args.version_file,
        tag_commit=args.tag_commit,
        linux_bundle=args.linux_bundle,
        windows_bundle=args.windows_bundle,
        guide=args.guide,
        out_dir=args.out_dir,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="驗證 Stage 2 RC／正式版 release assets")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check-tag", help="解析 tag 並核對 VERSION")
    check.add_argument("--tag", required=True)
    check.add_argument("--version-file", type=Path, required=True)
    check.add_argument("--github-output", type=Path)
    check.set_defaults(handler=_cmd_check_tag)

    list_rc = commands.add_parser("list-rc", help="從 gh release list JSON 選出同版已發布 prerelease")
    list_rc.add_argument("--version-file", type=Path, required=True)
    list_rc.add_argument("--releases-json", type=Path, required=True)
    list_rc.set_defaults(handler=_cmd_list_rc)

    validate = commands.add_parser("validate-assets", help="核對外層 SHA256SUMS 與 Linux／Windows manifest")
    validate.add_argument("--tag", required=True)
    validate.add_argument("--version-file", type=Path, required=True)
    validate.add_argument("--tag-commit", required=True)
    validate.add_argument("--assets-dir", type=Path, required=True)
    validate.set_defaults(handler=_cmd_validate_assets)

    prepare = commands.add_parser("prepare-rc-assets", help="封裝 gate 候選 bundle 為待發布 RC assets，不呼叫 gh")
    prepare.add_argument("--tag", required=True)
    prepare.add_argument("--version-file", type=Path, required=True)
    prepare.add_argument("--tag-commit", required=True)
    prepare.add_argument("--linux-bundle", type=Path, required=True)
    prepare.add_argument("--windows-bundle", type=Path, required=True)
    prepare.add_argument("--guide", type=Path, required=True)
    prepare.add_argument("--out-dir", type=Path, required=True)
    prepare.set_defaults(handler=_cmd_prepare)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except ReleaseValidationError as exc:
        print(f"release_assets: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
