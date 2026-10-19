"""P4-T19 Windows 安裝的 com0com 選用元件黑箱與契約測試（SC-36，OD-15）。

紅燈階段（TDD）：`scripts/rust/mkbundle.sh` 尚不接受 `--with-com0com`、`packaging/com0com/` 尚未建立、
`contracts/cli_schema.json` 尚未登記 `setup` 的兩個 Rust-only 旗標，故各案例以 `xfail(strict=True)` 標示；
實作 commit 須移除 marker（strict 會把漏移除轉為 FAIL）。
fixture zip 於測試內以 zipfile 產生，內容為替身檔（不是真的 com0com 套件）。
"""
from __future__ import annotations

import json
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


def _fixture_zip(tmp_path: Path) -> Path:
    """組出 `--with-com0com <zip>` 的輸入（替身檔案，不是真的 com0com 套件）。"""
    zip_path = tmp_path / "fixture-com0com-3.0.0.0.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("setupc.exe", b"fixture-setupc")
        zf.writestr("com0com.inf", b"fixture-inf")
    return zip_path


@pytest.mark.xfail(strict=True, reason="P4-T19 紅燈")
def test_bundle_license_files_present(tmp_path: Path) -> None:
    res = subprocess.run(
        [
            "bash",
            str(MKBUNDLE),
            "--dev",
            "--with-com0com",
            str(_fixture_zip(tmp_path)),
            "--out",
            str(tmp_path / "bundle"),
            "--print-dir",
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    bundle = Path(res.stdout.strip().splitlines()[-1])
    comp = bundle / "optional" / "com0com"

    # LICENSE-GPLv2.txt：首行（首個非空白行）含 GNU GENERAL PUBLIC LICENSE 與 Version 2。
    license_lines = (comp / "LICENSE-GPLv2.txt").read_text(encoding="utf-8").splitlines()
    first = next(line for line in license_lines if line.strip())
    assert "GNU GENERAL PUBLIC LICENSE" in first, first
    assert "Version 2" in first, first

    # SOURCE.md：原始碼取得方式（SourceForge 專案 URL、3.0.0.0 原始碼）與書面提供承諾。
    source = (comp / "SOURCE.md").read_text(encoding="utf-8")
    assert re.search(r"https?://[^\s)]*sourceforge\.net", source), "SOURCE.md 缺 SourceForge 專案 URL"
    assert "3.0.0.0" in source
    assert "書面提供" in source, "SOURCE.md 缺書面提供原始碼承諾"

    # PROVENANCE.json：上游檔名、md5（與 SourceForge 一致）與 Authenticode 簽署者。
    provenance = json.loads((comp / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert provenance["md5"] == GPL_MD5
    assert "CyberCircuits" in str(provenance["authenticode_signer"])


@pytest.mark.xfail(strict=True, reason="P4-T19 紅燈")
def test_cli_schema_flags_rust_only() -> None:
    schema = json.loads((repo_root() / "contracts" / "cli_schema.json").read_text(encoding="utf-8"))
    setup = next(group for group in schema["x-groups"] if group["name"] == "setup")
    delegate_flags = {flag for opt in setup["options"] for flag in opt["flags"]}

    # 只新增不改：既有 setup 旗標必須仍在 delegate-1a 的 options 中。
    assert PRE_EXISTING_SETUP_FLAGS <= delegate_flags

    # Rust-only 登記方式與 SC-23 相同：列於 x-pending（SC-36），不進入 delegate-1a 的 setup options。
    pending = [item for item in schema.get("x-pending", []) if item.get("id") == "SC-36"]
    added = {
        item["adds_option"][1]
        for item in pending
        if item.get("adds_option") and item["adds_option"][0] == "setup"
    }
    assert added == REQUIRED_FLAGS, f"x-pending SC-36 應登記 setup 的兩個旗標：{added}"
    assert not (REQUIRED_FLAGS & delegate_flags), "Rust-only 旗標不得列入 delegate-1a options"


@pytest.mark.xfail(strict=True, reason="P4-T19 紅燈")
def test_readme_com0com_both_languages() -> None:
    text = (repo_root() / "README.md").read_text(encoding="utf-8")
    en_start = text.index("\n## English\n")
    zh_start = text.index("\n## 繁體中文\n", en_start)
    sections = {"English": text[en_start:zh_start], "繁體中文": text[zh_start:]}
    for name, section in sections.items():
        for needle in ("com0com", "GPL", "--with-com0com"):
            assert needle in section, f"README 的 {name} 章缺少 {needle}"
