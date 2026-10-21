"""P4-T11：驗證 mkbundle 產物含可選的 release manifest metadata（§9.5、SC-25）。"""

from __future__ import annotations

import copy
import ast
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "contracts" / "release_manifest.schema.json"
MKBUNDLE = REPO_ROOT / "scripts" / "rust" / "mkbundle.sh"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TASK_XML_SOURCE = REPO_ROOT / "crates" / "sw-pal" / "src" / "service_ctl.rs"

# mkbundle.sh 為 bash 腳本且以子行程建置（與 tests/test_rust_mkbundle.py 相同標記）。
pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.process_rpc,
]


def _build_dev_bundle(out: Path) -> dict:
    """以 `mkbundle.sh --dev` 建立開發 bundle，回傳其 `versions/<VERSION>/release-manifest.json`。"""
    proc = subprocess.run(
        ["bash", str(MKBUNDLE), "--dev", "--out", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, f"mkbundle.sh --dev 失敗：{proc.stderr}"
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    manifest_path = out / "versions" / version / "release-manifest.json"
    assert manifest_path.is_file(), f"缺少 release-manifest.json：{manifest_path}"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _task_xml_template_sha256() -> str:
    """從 sw-pal 的權威 const 重算未代入 Task XML 樣板摘要。"""
    source = TASK_XML_SOURCE.read_text(encoding="utf-8")
    start = source.index("pub const TASK_XML_TEMPLATE: &str = concat!(")
    match = re.search(r"concat!\((.*?)\n\s*\);", source[start:], re.S)
    assert match is not None, "找不到 TASK_XML_TEMPLATE"
    parts = re.findall(r'^\s*("(?:\\.|[^"\\])*")\s*,?\s*$', match.group(1), re.M)
    assert parts, "TASK_XML_TEMPLATE 為空"
    template = "".join(ast.literal_eval(part) for part in parts)
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def test_manifest_schema_accepts_new_optional_fields(tmp_path: Path) -> None:
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    bundle_root = tmp_path / "bundle"
    manifest = _build_dev_bundle(bundle_root)

    # schema 必須接受 mkbundle 產物（新增欄位存在或不存在皆然）。
    validator.validate(manifest)

    # P4-T11 新增的 optional 欄位必須由 mkbundle 寫入。
    daemon = manifest["components"]["daemon"]
    supervisor = manifest["components"]["supervisor"]
    assert "state_schema" in daemon, "mkbundle 未寫入 components.daemon.state_schema"
    assert daemon["state_schema"] == "python-state-v0"
    assert "task_xml_template_sha256" in supervisor, (
        "mkbundle 未寫入 components.supervisor.task_xml_template_sha256"
    )
    assert HEX64.match(supervisor["task_xml_template_sha256"]), "task_xml_template_sha256 必須為 64 位小寫 hex"
    assert supervisor["task_xml_template_sha256"] == _task_xml_template_sha256(), (
        "manifest 摘要必須涵蓋完整 sw-pal Task XML 樣板"
    )

    # 兩個 metadata 欄位皆為 optional，舊版 manifest 仍符合 schema。
    without_optional_fields = copy.deepcopy(manifest)
    del without_optional_fields["components"]["daemon"]["state_schema"]
    del without_optional_fields["components"]["supervisor"]["task_xml_template_sha256"]
    validator.validate(without_optional_fields)

    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    real_bundle = bundle_root / "versions" / version
    env = os.environ.copy()
    env["SERIALWRAP_TEST_MKBUNDLE_DIR"] = str(real_bundle)
    proc = subprocess.run(
        [
            "cargo",
            "test",
            "-p",
            "sw-cli",
            "--test",
            "mgmt_layout",
            "real_mkbundle_dev_output_verifies",
            "--",
            "--ignored",
            "--exact",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, (
        "Rust verify_bundle 必須接受真實 mkbundle.sh --dev 產物："
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
