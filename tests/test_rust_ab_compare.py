"""A/B 比較器（scripts/rust/ab_compare.py）的分類、身分欄位與整體判定契約（P3-T18）。

紅燈階段：scripts/rust/ab_compare.py 為 stub（exit 2、不產生報告），本檔所有測試以
strict xfail（raises：NotImplementedError、AssertionError）標示；實作完成的同一 commit 移除標記。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CMP = ROOT / "scripts" / "rust" / "ab_compare.py"
pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.process_rpc,
]

INNER = {
    "pass": "",
    "fail": "<failure message='x'/>",
    "error": "<error message='x'/>",
    "skip": "<skipped message='cap_missing:posix_pty'/>",
    "skip_rust_only": "<skipped message='rust-only contract'/>",
    "skip_posix_only": "<skipped message='posix_only'/>",
    "xfail": "<skipped type='pytest.xfail' message='x'/>",
}


def junit(path: Path, cases: list[tuple[str, str]]) -> Path:
    body = []
    for cid, st in cases:
        cls, name = cid.rsplit("::", 1)
        body.append(f'<testcase classname="{cls}" name="{name}">{INNER[st]}</testcase>')
    path.write_text(f'<testsuites><testsuite name="bb">{"".join(body)}</testsuite></testsuites>', encoding="utf-8")
    return path


def identity(tmp_path: Path, side: str, **over) -> Path:
    cli = tmp_path / f"cli-{side}"
    cli.write_bytes(side.encode())
    base = {
        "cli_path": str(cli),
        "cli_sha256": hashlib.sha256(side.encode()).hexdigest(),
        "cli_implementation": "python" if side == "a" else "rust",
        "daemon_version": "0.3.2",
        "daemon_git_revision": "a" * 40,
        "daemon_tree_sha256": "t" * 64,
        "daemon_runtime_sha256": "r" * 64,
        "contract_revision": "c" * 40,
        "case_list_sha256": "",
        "tool_revision": "x",
        "platform": "linux",
        "config_digest": "d" * 64,
        "dut_identity": "stub",
        "evidence_files": [],
    }
    base.update(over)
    p = tmp_path / f"id-{side}.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    return p


def run(tmp_path, a, b, platform="linux", id_a=None, id_b=None):
    appl = tmp_path / "appl.json"
    appl.write_text(
        json.dumps({"posix_only": {"applies_to": "windows"}, "rust-only contract": {"applies_to": "python"}}),
        encoding="utf-8",
    )
    out = tmp_path / "r.json"
    r = subprocess.run(
        [
            sys.executable,
            str(CMP),
            "--a",
            str(junit(tmp_path / "a.xml", a)),
            "--b",
            str(junit(tmp_path / "b.xml", b)),
            "--a-label",
            "python",
            "--b-label",
            "rust-1a",
            "--out",
            str(out),
            "--applicability",
            str(appl),
            "--identity-a",
            str(id_a or identity(tmp_path, "a", platform=platform)),
            "--identity-b",
            str(id_b or identity(tmp_path, "b", platform=platform)),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.exists(), f"ab_compare 未產生報告（rc={r.returncode}，stderr={r.stderr.strip()!r}）"
    return r.returncode, json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "a, b, cls, rc",
    [
        ("pass", "pass", "ok", 0),
        ("pass", "fail", "regression", 1),
        ("pass", "error", "regression", 1),
        ("fail", "pass", "baseline-fail", 1),
        ("pass", "skip", "evidence-insufficient", 1),
        ("skip_rust_only", "pass", "rust-only-ok", 0),
        ("skip_rust_only", "fail", "regression", 1),
        ("skip", "skip", "evidence-insufficient", 1),  # 未登記原因（缺 posix_pty）不得當 not-applicable
        ("xfail", "pass", "xfail-unapproved", 1),
        ("pass", "xfail", "xfail-unapproved", 1),
    ],
)
def test_classification(tmp_path, a, b, cls, rc):
    code, rep = run(tmp_path, [("t.mod::c1", a)], [("t.mod::c1", b)])
    assert code == rc
    assert rep["cases"][0]["class"] == cls
    assert rep["result"] == ("pass" if rc == 0 else "fail")


def test_registered_skip_is_not_applicable_only_on_matching_platform(tmp_path):
    code, rep = run(tmp_path, [("t::c", "skip_posix_only")], [("t::c", "skip_posix_only")], platform="windows")
    assert (code, rep["cases"][0]["class"]) == (0, "not-applicable")
    code, rep = run(tmp_path, [("t::c", "skip_posix_only")], [("t::c", "skip_posix_only")], platform="linux")
    assert (code, rep["cases"][0]["class"]) == (1, "evidence-insufficient")


def test_approved_rust_only_skips_accept_runner_config_digest(tmp_path):
    """A 輪全為核准 rust-only skip 時，runner 摘要可與 B 夾具摘要不同。"""
    identity_a = identity(tmp_path, "a", config_digest="a" * 64)
    identity_b = identity(tmp_path, "b", config_digest="b" * 64)
    code, report = run(
        tmp_path,
        [("t_cfg.py::test_one", "skip_rust_only")],
        [("t_cfg.py::test_one", "pass")],
        id_a=identity_a,
        id_b=identity_b,
    )
    assert code == 0
    assert report["result"] == "pass"
    assert report["cases"][0]["class"] == "rust-only-ok"
    assert report["identity"]["a"]["config_digest"] != report["identity"]["b"]["config_digest"]


def test_case_set_mismatch(tmp_path):
    code, rep = run(tmp_path, [("t::a", "pass")], [("t::b", "pass")])
    assert code == 1
    assert {c["class"] for c in rep["cases"]} == {"case-set-mismatch"}


@pytest.mark.parametrize("a", [[], [("t::a", "pass"), ("t::a", "pass")]], ids=["empty", "duplicate"])
def test_empty_or_duplicate_report_fails(tmp_path, a):
    code, rep = run(tmp_path, a, [("t::a", "pass")])
    assert code == 1 and rep["result"] == "fail" and rep["errors"]


@pytest.mark.parametrize("field", ["cli_sha256", "daemon_tree_sha256", "contract_revision", "dut_identity"])
def test_identity_required_fields(tmp_path, field):
    code, rep = run(tmp_path, [("t::a", "pass")], [("t::a", "pass")], id_b=identity(tmp_path, "b", **{field: ""}))
    assert code == 1 and any(field in e for e in rep["errors"])


def test_identity_cli_sha_recomputed_and_sides_differ(tmp_path):
    code, rep = run(tmp_path, [("t::a", "pass")], [("t::a", "pass")], id_b=identity(tmp_path, "b", cli_sha256="0" * 64))
    assert code == 1
    same = identity(tmp_path, "b", cli_implementation="python")
    code, _ = run(tmp_path, [("t::a", "pass")], [("t::a", "pass")], id_b=same)
    assert code == 1
