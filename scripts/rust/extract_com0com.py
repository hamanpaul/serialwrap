#!/usr/bin/env python3
"""驗證並抽取 com0com x64 安裝程式內的完整 driver payload。"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

INSTALLER = "Setup_com0com_v3.0.0.0_W7_x64_signed.exe"
REQUIRED_FILES = (
    "setupc.exe",
    "setup.dll",
    "com0com.inf",
    "cncport.inf",
    "comport.inf",
    "com0com.sys",
    "com0com.cat",
)


def _fail(message: str) -> "NoReturn":
    raise SystemExit(f"extract_com0com.py: {message}")


def _archive_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    candidates = [
        member
        for member in archive.infolist()
        if PurePosixPath(member.filename.replace("\\", "/")).name == INSTALLER
    ]
    if len(candidates) != 1:
        _fail(f"上游 zip 必須恰有一個 x64 安裝程式 {INSTALLER}，找到 {len(candidates)} 個")
    return candidates[0]


def _verify_archive(archive_path: Path, provenance_path: Path, fixture: bool) -> None:
    if fixture:
        return
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        expected_md5 = provenance["md5"]
        expected_sha256 = provenance["sha256"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        _fail(f"PROVENANCE.json 無法讀取預期雜湊：{error}")
    body = archive_path.read_bytes()
    actual_md5 = hashlib.md5(body).hexdigest()
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if actual_md5 != expected_md5 or actual_sha256 != expected_sha256:
        _fail(
            "上游 zip 雜湊不符："
            f"md5={actual_md5}（預期 {expected_md5}），"
            f"sha256={actual_sha256}（預期 {expected_sha256}）"
        )


def extract(archive_path: Path, destination: Path, provenance_path: Path, fixture: bool) -> None:
    _verify_archive(archive_path, provenance_path, fixture)
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as error:
        _fail(f"無法讀取 com0com zip：{error}")
    with archive:
        member = _archive_member(archive)
        if member.is_dir():
            _fail(f"x64 安裝程式不是一般檔案：{member.filename}")
        destination.mkdir(parents=True, exist_ok=True)
        seven_zip = shutil.which("7z")
        if seven_zip is None:
            _fail("找不到 7z；請安裝 7-Zip 後重試，未產生 com0com 套件")
        with tempfile.TemporaryDirectory(prefix=".com0com-extract-", dir=destination) as temporary:
            temporary_path = Path(temporary)
            installer_path = temporary_path / INSTALLER
            installer_path.write_bytes(archive.read(member))
            payload_path = temporary_path / "payload"
            payload_path.mkdir()
            result = subprocess.run(
                [seven_zip, "x", "-y", f"-o{payload_path}", str(installer_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                _fail(
                    "7z 無法解開 x64 安裝程式："
                    f"rc={result.returncode}；{result.stderr.strip() or result.stdout.strip()}"
                )
            by_name: dict[str, list[Path]] = {}
            for path in payload_path.rglob("*"):
                if path.is_file():
                    by_name.setdefault(path.name.casefold(), []).append(path)
            missing = [name for name in REQUIRED_FILES if name not in by_name]
            duplicates = [name for name in REQUIRED_FILES if len(by_name.get(name, [])) > 1]
            if missing or duplicates:
                details = []
                if missing:
                    details.append("缺少 " + ", ".join(missing))
                if duplicates:
                    details.append("檔名重複 " + ", ".join(duplicates))
                _fail("x64 安裝程式 payload 不完整：" + "；".join(details))
            for name in REQUIRED_FILES:
                shutil.copyfile(by_name[name][0], destination / name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--fixture", action="store_true", help="僅供測試，略過上游 zip 雜湊比對")
    args = parser.parse_args(argv)
    try:
        extract(args.archive, args.destination, args.provenance, args.fixture)
    except (OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        _fail(f"抽取失敗：{error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
