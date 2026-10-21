"""P4-T19 Windows 安裝的 com0com 選用元件黑箱與契約測試（SC-36，OD-15）。"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import zipfile
from pathlib import Path

import pytest

from bb_harness import repo_root

MKBUNDLE = repo_root() / "scripts" / "rust" / "mkbundle.sh"
# 計畫 Task 19 與 PROVENANCE.json 的上游 signed zip md5（SourceForge 一致）。
GPL_MD5 = "a07aa80024cca837dacc75d3573285ff"
# OD-14 相容原則：既有 setup 旗標只新增不改。
PRE_EXISTING_SETUP_FLAGS = {"--user", "--system", "--on-demand", "--force", "--with-sudo"}
REQUIRED_FLAGS = {"--with-com0com", "--remove-com0com"}
pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


def _fixture_zip(tmp_path: Path) -> Path:
    """組出含 x64／x86 NSIS 安裝程式名稱的隔離 zip fixture。"""
    zip_path = tmp_path / "fixture-com0com-3.0.0.0.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Setup_com0com_v3.0.0.0_W7_x86_signed.exe", b"fixture-x86-installer")
        zf.writestr("Setup_com0com_v3.0.0.0_W7_x64_signed.exe", b"fixture-x64-installer")
    return zip_path


def _seven_zip(tmp_path: Path, *, omit: str | None = None) -> Path:
    """建立只供 fixture 使用的 7z 替身，輸出 x64 driver payload 檔案。"""
    executable = tmp_path / "7z"
    names = [
        "setupc.exe",
        "setup.dll",
        "com0com.inf",
        "cncport.inf",
        "comport.inf",
        "com0com.sys",
        "com0com.cat",
    ]
    if omit is not None:
        names.remove(omit)
    lines = [
        "#!/usr/bin/env python3",
        "import pathlib, sys",
        "destination = pathlib.Path(next(arg[2:] for arg in sys.argv if arg.startswith('-o'))) ",
        "destination.mkdir(parents=True, exist_ok=True)",
    ]
    lines.extend(
        f"(destination / {name!r}).write_bytes({('fixture-' + name).encode()!r})"
        for name in names
    )
    executable.write_text("\n".join(lines) + "\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _extract_fixture(
    tmp_path: Path, *, omit: str | None = None, verify_source_hash: bool = False
) -> subprocess.CompletedProcess[str]:
    archive = _fixture_zip(tmp_path)
    seven_zip = _seven_zip(tmp_path, omit=omit)
    destination = tmp_path / "extracted"
    provenance = repo_root() / "packaging" / "com0com" / "PROVENANCE.json"
    if verify_source_hash:
        provenance = tmp_path / "provenance.json"
        provenance.write_text(
            json.dumps({"md5": "0" * 32, "sha256": "0" * 64}),
            encoding="utf-8",
        )
    command = [
        "python3",
        str(repo_root() / "scripts" / "rust" / "extract_com0com.py"),
        str(archive),
        str(destination),
        "--provenance",
        str(provenance),
    ]
    if not verify_source_hash:
        command.append("--fixture")
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": f"{seven_zip.parent}:{os.environ.get('PATH', '')}"},
    )


@pytest.mark.case("sc36-win-installer-com0com-optional")
def test_bundle_license_files_present(tmp_path: Path) -> None:
    seven_zip = _seven_zip(tmp_path)
    res = subprocess.run(
        [
            "bash",
            str(MKBUNDLE),
            "--dev",
            "--with-com0com-fixture",
            str(_fixture_zip(tmp_path)),
            "--out",
            str(tmp_path / "bundle"),
            "--print-dir",
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        env={**os.environ, "PATH": f"{seven_zip.parent}:{os.environ.get('PATH', '')}"},
    )
    assert res.returncode == 0, res.stderr
    bundle_root = Path(res.stdout.strip().splitlines()[-1])
    # 完整 dev bundle 的 print-dir 是 installer root；manifest 與相對路徑資產位於版本 payload。
    version = (repo_root() / "VERSION").read_text(encoding="utf-8").strip()
    comp = bundle_root / "versions" / version / "optional" / "com0com"
    for filename in (
        "setupc.exe",
        "setup.dll",
        "com0com.inf",
        "cncport.inf",
        "comport.inf",
        "com0com.sys",
        "com0com.cat",
    ):
        assert (comp / filename).is_file(), f"bundle 缺少 x64 payload：{filename}"

    manifest = json.loads(
        (bundle_root / "versions" / version / "release-manifest.json").read_text(encoding="utf-8")
    )
    helper = manifest["components"]["com0com"]["helper"]
    assert helper["path"] in {"bin/sw-com0com-helper", "bin/sw-com0com-helper.exe"}
    assert re.fullmatch(r"[a-f0-9]{64}", helper["sha256"])
    helper_file = bundle_root / "versions" / version / helper["path"]
    assert hashlib.sha256(helper_file.read_bytes()).hexdigest() == helper["sha256"]

    # GPLv2 原文將標題與版本分列兩行，兩者皆須存在。
    license_lines = (comp / "LICENSE-GPLv2.txt").read_text(encoding="utf-8").splitlines()
    non_empty_license_lines = [line.strip() for line in license_lines if line.strip()]
    first, second = non_empty_license_lines[:2]
    assert "GNU GENERAL PUBLIC LICENSE" in first, first
    assert "Version 2" in second, second

    # SOURCE.md：原始碼取得方式（SourceForge 專案 URL、3.0.0.0 原始碼）與書面提供承諾。
    source = (comp / "SOURCE.md").read_text(encoding="utf-8")
    assert re.search(r"https?://[^\s)]*sourceforge\.net", source), "SOURCE.md 缺 SourceForge 專案 URL"
    assert "3.0.0.0" in source
    assert "書面提供" in source, "SOURCE.md 缺書面提供原始碼承諾"

    # PROVENANCE.json：上游檔名、md5（與 SourceForge 一致）與 Authenticode 簽署者。
    provenance = json.loads((comp / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert provenance["md5"] == GPL_MD5
    assert "CyberCircuits" in str(provenance["authenticode_signer"])


def test_installer_only_zip_extracts_complete_x64_payload(tmp_path: Path) -> None:
    result = _extract_fixture(tmp_path)
    assert result.returncode == 0, result.stderr
    destination = tmp_path / "extracted"
    assert {path.name for path in destination.iterdir()} == {
        "setupc.exe",
        "setup.dll",
        "com0com.inf",
        "cncport.inf",
        "comport.inf",
        "com0com.sys",
        "com0com.cat",
    }
    assert not list(destination.glob(".com0com-extract-*")), "暫存安裝程式必須清除"


def test_incomplete_installer_payload_fails_closed(tmp_path: Path) -> None:
    result = _extract_fixture(tmp_path, omit="com0com.cat")
    assert result.returncode != 0
    assert "payload 不完整" in result.stderr
    assert "com0com.cat" in result.stderr
    assert not list((tmp_path / "extracted").glob("*")), "不完整 payload 不得複製到目的目錄"


def test_signed_zip_hash_mismatch_fails_before_extraction(tmp_path: Path) -> None:
    result = _extract_fixture(tmp_path, verify_source_hash=True)
    assert result.returncode != 0
    assert "上游 zip 雜湊不符" in result.stderr
    assert not (tmp_path / "extracted").exists(), "雜湊錯誤時不得開始抽取"


@pytest.mark.case("sc36-win-installer-com0com-optional")
def test_cli_schema_flags_rust_only() -> None:
    schema = json.loads((repo_root() / "contracts" / "cli_schema.json").read_text(encoding="utf-8"))
    setup = next(group for group in schema["x-groups"] if group["name"] == "setup")
    delegate_flags = {flag for opt in setup["options"] for flag in opt["flags"]}

    # 只新增不改：既有 setup 旗標必須仍在 delegate-1a 的 options 中。
    assert PRE_EXISTING_SETUP_FLAGS <= delegate_flags

    # 已實作選項列於 x-rust-only；x-pending 只供未實作變更暫存，C17 禁止保留 IMPLEMENTED 項目。
    rust_only = [item for item in schema.get("x-rust-only", []) if item.get("change") == "SC-36"]
    added = {
        item["option"][1]
        for item in rust_only
        if item.get("option") and item["option"][0] == "setup"
    }
    assert added == REQUIRED_FLAGS, f"x-rust-only SC-36 應登記 setup 的兩個旗標：{added}"
    assert not (REQUIRED_FLAGS & delegate_flags), "Rust-only 旗標不得列入 delegate-1a options"
    assert not any(item.get("change") == "SC-36" for item in schema.get("x-pending", []))


@pytest.mark.case("sc36-win-installer-com0com-optional")
def test_readme_com0com_both_languages() -> None:
    text = (repo_root() / "README.md").read_text(encoding="utf-8")
    en_start = text.index("\n## English\n")
    zh_start = text.index("\n## 繁體中文\n", en_start)
    sections = {"English": text[en_start:zh_start], "繁體中文": text[zh_start:]}
    for name, section in sections.items():
        for needle in ("com0com", "GPL", "--with-com0com"):
            assert needle in section, f"README 的 {name} 章缺少 {needle}"
