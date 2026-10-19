"""P3-T16：`scripts/rust/mkbundle.sh --dev --out` 產生的完整 dev bundle 結構與 manifest 契約。

- bundle 於第一個需要它的測試內惰性建立（同一模組內只建一次）；建立失敗以 AssertionError 呈現。
- manifest 位置：`<out>/versions/<VERSION>/release-manifest.json`（與 Rust／Python 的 bundle 根定位一致）。
- schema 與 golden 來自 P1（`contracts/release_manifest.schema.json`、`contracts/cli_help/_meta.json`），缺檔即失敗、不得跳過。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MKBUNDLE = REPO / "scripts" / "rust" / "mkbundle.sh"
SCHEMA = REPO / "contracts" / "release_manifest.schema.json"
META = REPO / "contracts" / "cli_help" / "_meta.json"
VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.process_rpc,
]


def _require_cargo() -> None:
    if shutil.which("cargo") is None:
        if os.environ.get("SW_REQUIRE_CARGO") == "1":
            pytest.fail("SW_REQUIRE_CARGO=1 但找不到 cargo（證據不足，非 PASS）")
        pytest.skip("cargo 不存在（證據不足，非 PASS）")


class _LazyBundle:
    """模組內共用的 dev bundle；第一次 `out()` 時以 mkbundle.sh 建立。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._out: Path | None = None

    def out(self) -> Path:
        _require_cargo()
        if self._out is None:
            out = self._root / "dev bundle"
            res = subprocess.run(
                [str(MKBUNDLE), "--dev", "--out", str(out)],
                capture_output=True,
                text=True,
                timeout=900,
                cwd=str(REPO),
                check=False,
            )
            assert res.returncode == 0, (
                f"mkbundle.sh --dev --out 失敗（rc={res.returncode}）：{res.stderr.strip()}"
            )
            self._out = out
        return self._out


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> _LazyBundle:
    return _LazyBundle(tmp_path_factory.mktemp("mkbundle", numbered=False))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(out: Path) -> dict:
    mpath = out / "versions" / VERSION / "release-manifest.json"
    assert mpath.exists(), f"manifest 不存在：{mpath}"
    return json.loads(mpath.read_text(encoding="utf-8"))


def _meta_python_version() -> str:
    return json.loads(META.read_text(encoding="utf-8"))["python_version"]


def test_manifest_validates_against_p1_schema(bundle):
    assert SCHEMA.exists(), "P1 contracts 缺少：contracts/release_manifest.schema.json"
    import jsonschema

    out = bundle.out()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    errors = sorted(validator.iter_errors(_manifest(out)), key=lambda e: list(e.absolute_path))
    assert not errors, "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


def test_bin_serialwrap_is_symlink_into_versions(bundle):
    out = bundle.out()
    link = out / "bin" / "serialwrap"
    assert link.is_symlink(), f"bin/serialwrap 必須為 symlink：{link}"
    assert Path(os.path.realpath(link)) == Path(
        os.path.realpath(out / "versions" / VERSION / "bin" / "serialwrap")
    )


def test_out_path_with_spaces(bundle):
    assert " " in str(bundle.out())


def test_venv_python_runs_cli_version(bundle):
    out = bundle.out()
    py = out / "versions" / VERSION / "py" / "bin" / "python3"
    r = subprocess.run(
        [str(py), "-I", "-m", "sw_core.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"serialwrap {VERSION}"


def test_cli_sha256_matches_binary_file(bundle):
    out = bundle.out()
    cli = _manifest(out)["components"]["cli"]
    assert cli["implementation"] == "rust"
    assert cli["path"] == "bin/serialwrap"
    assert cli["sha256"] == _sha256(out / "versions" / VERSION / "bin" / "serialwrap")


def test_tree_sha256_matches_p1_package_tree(bundle):
    out = bundle.out()
    comp = _manifest(out)["components"]
    expected = subprocess.run(
        [sys.executable, "-c", "import sw_core.release_manifest as m; print(m.package_tree_sha256())"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    assert expected.returncode == 0, expected.stderr
    tree = expected.stdout.strip()
    assert comp["python_cli_delegate"]["tree_sha256"] == tree
    assert comp["daemon"]["package"]["tree_sha256"] == tree


def test_python_cli_delegate_entry_and_runtime_version(bundle):
    out = bundle.out()
    pcd = _manifest(out)["components"]["python_cli_delegate"]
    assert pcd["entry"] == ["py/bin/python3", "-I", "-B", "-m", "sw_core.cli"]
    assert pcd["runtime_python_version"] == _meta_python_version()


def test_daemon_block_fields_nonempty_and_dev_bundle_shape(bundle):
    out = bundle.out()
    d = _manifest(out)["components"]["daemon"]
    assert d["git_revision"]
    assert d["launcher"]["sha256"] and d["runtime"]["sha256"] and d["package"]["tree_sha256"]
    # dev bundle 沒有 Rust launcher 與 wheel：launcher 指向 repo 的 serialwrapd.py，wheel_sha256 為 null
    assert Path(d["launcher"]["path"]) == REPO / "serialwrapd.py"
    assert d["launcher"]["sha256"] == _sha256(REPO / "serialwrapd.py")
    assert d["package"]["wheel_sha256"] is None
    assert d["runtime"]["python_version"] == _meta_python_version()


def test_manifest_top_level_and_pairing(bundle):
    out = bundle.out()
    m = _manifest(out)
    assert m["schema_version"] == 1
    assert m["stage"] == "1A"
    assert m["bundle_version"] == VERSION
    assert m["pairing"] == {"rule": "exact", "cli_version": VERSION, "daemon_version": VERSION}


def test_doctor_via_bundle_reports_version_pairing(bundle, tmp_path):
    out = bundle.out()
    harness_dir = str(REPO / "tests" / "contract_blackbox")
    if harness_dir not in sys.path:
        sys.path.insert(0, harness_dir)
    from bb_harness import isolated_env

    env = isolated_env(tmp_path)
    r = subprocess.run(
        [str(out / "bin" / "serialwrap"), "doctor"],
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )
    report = json.loads(r.stdout)
    checks = report if isinstance(report, list) else report.get("checks", [])
    version_pairing = next(
        (item for item in checks if item.get("check") == "version_pairing"), None
    )
    assert version_pairing is not None, (
        f"doctor 輸出缺少 version_pairing：{r.stdout!r}；stderr={r.stderr!r}"
    )
    assert version_pairing["mismatches"] == ["daemon_unreachable"]


def test_windows_target_is_rejected_before_build(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    cargo_called = tmp_path / "cargo-called"
    fake_rustc = fake_bin / "rustc"
    fake_rustc.write_text(
        "#!/bin/sh\nprintf 'host: x86_64-unknown-linux-gnu\\n'\n",
        encoding="utf-8",
    )
    fake_rustc.chmod(0o755)
    fake_cargo = fake_bin / "cargo"
    fake_cargo.write_text(
        "#!/bin/sh\n: > \"$SW_CARGO_CALLED\"\nexit 0\n",
        encoding="utf-8",
    )
    fake_cargo.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        SW_CARGO_CALLED=str(cargo_called),
    )

    result = subprocess.run(
        [
            str(MKBUNDLE),
            "--dev",
            "--target",
            "x86_64-pc-windows-gnu",
            "--out",
            str(tmp_path / "windows-bundle"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 2, result.stderr
    assert "P3-T17" in result.stderr
    assert not cargo_called.exists()


def test_help_lists_supported_options_without_building():
    result = subprocess.run(
        [str(MKBUNDLE), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for option in ("--target", "--profile", "--out", "--print-dir"):
        assert option in result.stdout
