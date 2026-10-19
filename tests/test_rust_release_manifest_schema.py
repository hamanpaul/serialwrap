"""P4-T11：release manifest schema 對 `mkbundle.sh --dev` 產物的新增 optional 欄位（§9.5、SC-25）。

紅燈階段（TDD）：`scripts/rust/mkbundle.sh` 尚未寫入 `components.daemon.state_schema`（`"python-state-v0"`）
與 `components.supervisor.task_xml_template_sha256`，故本測試應以「欄位缺漏」的斷言失敗，
不得因 import、路徑或 bundle 建置錯誤失敗（建置本身須成功）。
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "contracts" / "release_manifest.schema.json"
MKBUNDLE = REPO_ROOT / "scripts" / "rust" / "mkbundle.sh"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

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


@pytest.mark.xfail(strict=True, reason="P4-T11 紅燈")
def test_manifest_schema_accepts_new_optional_fields(tmp_path: Path) -> None:
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    manifest = _build_dev_bundle(tmp_path / "bundle")

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

    # state_schema 為 optional：去掉後 schema 仍須通過。
    without_state_schema = copy.deepcopy(manifest)
    del without_state_schema["components"]["daemon"]["state_schema"]
    validator.validate(without_state_schema)
