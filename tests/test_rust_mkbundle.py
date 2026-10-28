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
import tempfile
import time
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
    """模組內共用的 dev bundle（`sentinel=True` 時為 `--delegate-sentinel` 扁平佈局）；第一次 `out()` 時以 mkbundle.sh 建立。"""

    def __init__(self, root: Path, *, sentinel: bool = False) -> None:
        self._root = root
        self._sentinel = sentinel
        self._out: Path | None = None

    def out(self) -> Path:
        _require_cargo()
        if self._out is None:
            out = self._root / ("sentinel bundle" if self._sentinel else "dev bundle")
            args = [str(MKBUNDLE), "--dev"]
            if self._sentinel:
                args.append("--delegate-sentinel")
            args += ["--out", str(out)]
            res = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=900,
                cwd=str(REPO),
                check=False,
            )
            assert res.returncode == 0, (
                f"mkbundle.sh {' '.join(args[1:])} 失敗（rc={res.returncode}）：{res.stderr.strip()}"
            )
            self._out = out
        return self._out


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> _LazyBundle:
    return _LazyBundle(tmp_path_factory.mktemp("mkbundle", numbered=False))


@pytest.fixture(scope="module")
def sentinel_bundle(tmp_path_factory) -> _LazyBundle:
    return _LazyBundle(tmp_path_factory.mktemp("mkbundle-sentinel", numbered=False), sentinel=True)


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
        [str(py), "-I", "-B", "-m", "sw_core.cli", "--version"],
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
    # dev bundle 的 Rust launcher 指向版本目錄內的 serialwrapd，且不含 wheel。
    launcher = out / "versions" / VERSION / "bin" / "serialwrapd"
    assert d["launcher"]["path"] == "bin/serialwrapd"
    assert d["launcher"]["sha256"] == _sha256(launcher)
    assert d["package"]["wheel_sha256"] is None
    assert d["runtime"]["python_version"] == _meta_python_version()


def test_bundle_runtime_contains_isolated_pyyaml(bundle, tmp_path):
    """daemon bundle 自帶 PyYAML，且隔離執行時不依賴宿主 site-packages。"""
    out = bundle.out()
    py_dir = out / "versions" / VERSION / "py"
    pyvenv_cfg = (py_dir / "pyvenv.cfg").read_text(encoding="utf-8").lower()
    assert "include-system-site-packages = false" in pyvenv_cfg
    python = py_dir / "bin" / "python3"
    result = subprocess.run(
        [str(python), "-I", "-B", "-c", "import pathlib, yaml; print(pathlib.Path(yaml.__file__).resolve())"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "host-site-packages")},
    )
    assert result.returncode == 0, result.stderr
    assert str(py_dir.resolve()) in result.stdout, result.stdout


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


def test_release_bundle_build_does_not_enable_test_fakes(tmp_path):
    """正式 bundle 的 cargo invocation 不得啟用 setup 故障注入 feature。"""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    cargo_args = tmp_path / "cargo-args"
    fake_rustc = fake_bin / "rustc"
    fake_rustc.write_text("#!/bin/sh\nprintf 'host: x86_64-unknown-linux-gnu\\n'\n", encoding="utf-8")
    fake_rustc.chmod(0o755)
    fake_cargo = fake_bin / "cargo"
    fake_cargo.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$SW_CARGO_ARGS\"\nexit 0\n", encoding="utf-8")
    fake_cargo.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}", SW_CARGO_ARGS=str(cargo_args),
               CARGO_TARGET_DIR=str(tmp_path / "cargo-target"))

    result = subprocess.run(
        [str(MKBUNDLE), "--release-bundle", "--profile", "release", "--out", str(tmp_path / "release")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert cargo_args.is_file(), result.stderr
    args = cargo_args.read_text(encoding="utf-8")
    assert "sw-cli/test-fakes" not in args
    assert "--release" in args
    assert result.returncode != 0, "fake cargo 未產生 binaries，後續 bundle 組裝應 fail-closed"


def test_help_lists_supported_options_without_building():
    result = subprocess.run(
        [str(MKBUNDLE), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for option in ("--target", "--profile", "--release-bundle", "--out", "--print-dir"):
        assert option in result.stdout


def test_mkbundle_respects_cargo_target_dir_override(tmp_path: Path) -> None:
    _require_cargo()
    result = subprocess.run(
        [
            "cargo",
            "test",
            "-p",
            "sw-cli",
            "--test",
            "mgmt_layout",
            "mkbundle_respects_cargo_target_dir_override",
            "--",
            "--ignored",
            "--exact",
        ],
        cwd=REPO,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert result.returncode == 0, (
        "Rust mkbundle target-dir 回歸測試失敗："
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "1 passed" in result.stdout, (
        "pytest 必須確實執行 mkbundle_respects_cargo_target_dir_override："
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def _build_bundle(out: Path, version_override: str | None = None) -> Path:
    """以 `mkbundle.sh --dev` 建立指定目錄的新 bundle；失敗以 AssertionError 呈現（含 stderr）。"""
    _require_cargo()
    args = [str(MKBUNDLE), "--dev", "--out", str(out)]
    if version_override is not None:
        args += ["--version-override", version_override]
    res = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=900,
        cwd=str(REPO),
        check=False,
    )
    assert res.returncode == 0, (
        f"mkbundle.sh {' '.join(args[1:])} 失敗（rc={res.returncode}）：{res.stderr.strip()}"
    )
    return out


def _vdir(out: Path, version: str = VERSION) -> Path:
    return out / "versions" / version


def _full_listing(root: Path) -> dict[str, str]:
    """相對路徑 → 內容 sha256（含 __pycache__ 與 *.pyc；symlink 記為 `symlink:<目標>`；目錄不列）。"""
    listing: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                listing[rel] = "symlink:" + os.readlink(p)
            elif p.is_file():
                listing[rel] = _sha256(p)
    return listing


def _is_bytecode(rel: str) -> bool:
    return "__pycache__" in rel.split("/") or rel.endswith(".pyc")


def _tree_sha256_stdlib(root: Path, subtree: str | None = None) -> str:
    """與 `sw_core.release_manifest.tree_sha256` 同演算法（規格 §9.5）：排除 __pycache__ 與 *.pyc；
    relpath 以 `/` 分隔、相對於 root、依 UTF-8 位元組序；每列 `<relpath>\\0<sha256>\\n` 的 sha256。"""
    base = root / subtree if subtree else root
    rows: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in filenames:
            if name.endswith(".pyc"):
                continue
            p = Path(dirpath) / name
            rows.append((p.relative_to(root).as_posix(), _sha256(p)))
    rows.sort(key=lambda kv: kv[0].encode("utf-8"))
    body = "".join(f"{rel}\0{digest}\n" for rel, digest in rows)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _harness_env(tmp: Path) -> dict[str, str]:
    harness_dir = str(REPO / "tests" / "contract_blackbox")
    if harness_dir not in sys.path:
        sys.path.insert(0, harness_dir)
    from bb_harness import isolated_env

    env = isolated_env(tmp)
    # 本檔要觀察 entry 的 `-B` 是否生效；不能由測試環境的 PYTHONDONTWRITEBYTECODE 代勞。
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return env


def _send_rpc(endpoint: str, method: str, params: dict):
    harness_dir = str(REPO / "tests" / "contract_blackbox")
    if harness_dir not in sys.path:
        sys.path.insert(0, harness_dir)
    from bb_daemon import _send_rpc as send

    return send(endpoint, method, params, timeout_s=1.0)


def _wait_ready(endpoint: str, proc: subprocess.Popen, timeout_s: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"daemon 提早結束 rc={proc.returncode}")
        res = _send_rpc(endpoint, "health.ping", {})
        if res and res.get("ok") is True:
            return res
        time.sleep(0.2)
    raise AssertionError(f"health.ping 於 {timeout_s} s 內未回 ok：{endpoint}")


def _count_procs_with(token: str) -> int:
    count = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = Path("/proc", entry, "cmdline").read_bytes()
        except OSError:
            continue
        if token.encode("utf-8") in raw:
            count += 1
    return count


def _run_daemon_session(bundle_root: Path, env: dict, work: Path) -> None:
    """以 `bin/serialwrapd`（launcher）啟動 daemon，經 Rust CLI 委派一次 `session list`，最後 `daemon stop`。"""
    launcher = bundle_root / "bin" / "serialwrapd"
    assert launcher.is_file(), f"bin/serialwrapd（Stage 1 launcher）缺失：{launcher}"
    work.mkdir(parents=True, exist_ok=True)
    sockdir = Path(tempfile.mkdtemp(prefix="swd-", dir=tempfile.gettempdir()))
    proc: subprocess.Popen | None = None
    try:
        sock = sockdir / "s"
        with open(work / "daemon.log", "w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [
                    str(launcher),
                    "--socket", str(sock),
                    "--lock", str(sockdir / "l"),
                    "--profile-dir", str(work / "profiles"),
                ],
                env=env,
                cwd=str(work),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            _wait_ready(str(sock), proc)
            cli_env = dict(env, SERIALWRAP_ENDPOINT=str(sock))
            listed = subprocess.run(
                [str(bundle_root / "bin" / "serialwrap"), "session", "list"],
                env=cli_env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert listed.returncode == 0, f"委派命令 session list 失敗：{listed.stderr.strip()}"
            assert listed.stderr == "", f"session list 不應出現錯誤的版本警告：{listed.stderr!r}"
            stopped = subprocess.run(
                [str(bundle_root / "bin" / "serialwrap"), "daemon", "stop"],
                env=cli_env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert stopped.returncode == 0, f"daemon stop 失敗：{stopped.stderr.strip()}"
            proc.wait(timeout=20)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        shutil.rmtree(sockdir, ignore_errors=True)


def test_bundle_python_run_leaves_tree_unchanged(tmp_path):
    out = _build_bundle(tmp_path / "bundle dir")
    vdir = _vdir(out)
    symlinks = sorted(p.relative_to(vdir).as_posix() for p in vdir.rglob("*") if p.is_symlink())
    assert not symlinks, f"自含式 bundle 不得含 symlink（bundle 指紋對 symlink 一律拒絕）：{symlinks}"

    env = _harness_env(tmp_path / "iso")
    before = _full_listing(vdir)
    before_tree = _tree_sha256_stdlib(vdir)
    _run_daemon_session(out, env, tmp_path / "main")
    after = _full_listing(vdir)

    new_bytecode = sorted(k for k in set(after) - set(before) if _is_bytecode(k))
    assert not new_bytecode, f"啟動並執行 Python daemon 後不得新增位元組碼（entry 缺 -B）：{new_bytecode[:10]}"
    changed = sorted(k for k in before if after.get(k) != before[k])
    assert not changed, f"執行後既有檔案內容不得改變：{changed[:10]}"
    assert _tree_sha256_stdlib(vdir) == before_tree, "執行後 bundle_tree_sha256 不得改變"

    # 對照組：複本 bundle 的 daemon entry 去掉 -B，同一流程必須產生位元組碼，證明上方檢測有效。
    ctrl = tmp_path / "control bundle"
    shutil.copytree(out, ctrl, symlinks=True)
    ctrl_manifest = _vdir(ctrl) / "release-manifest.json"
    ctrl_doc = json.loads(ctrl_manifest.read_text(encoding="utf-8"))
    ctrl_doc["components"]["daemon"]["entry"] = [
        item for item in ctrl_doc["components"]["daemon"]["entry"] if item != "-B"
    ]
    ctrl_manifest.write_text(json.dumps(ctrl_doc, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    ctrl_before = _full_listing(_vdir(ctrl))
    _run_daemon_session(ctrl, env, tmp_path / "control")
    ctrl_after = _full_listing(_vdir(ctrl))
    ctrl_new = [k for k in set(ctrl_after) - set(ctrl_before) if _is_bytecode(k)]
    assert ctrl_new, "對照組（entry 無 -B）未產生位元組碼：上方檢測無法證明有效"


def test_bundle_py_is_self_contained(tmp_path):
    out = _build_bundle(tmp_path / "bundle dir")
    py_dir = _vdir(out) / "py"
    pths = sorted(p.relative_to(py_dir).as_posix() for p in py_dir.rglob("*.pth"))
    assert not pths, f"自含式 bundle 的 py/ 不得含 .pth（指向 repo）：{pths}"

    py = py_dir / "bin" / "python3"
    assert not py.is_symlink(), "py/bin/python3 必須為一般檔（venv --copies）"
    r = subprocess.run(
        [str(py), "-I", "-c", "import sw_core, os; print(os.path.dirname(os.path.abspath(sw_core.__file__)))"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )
    assert r.returncode == 0, r.stderr
    loaded = Path(r.stdout.strip()).resolve()
    assert loaded.is_relative_to(py_dir.resolve()), f"sw_core 必須由 bundle 內的 py/ 提供，實際載入 {loaded}"
    assert loaded.name == "sw_core" and loaded.parent.name == "site-packages", (
        f"隔離模式實際載入的 sw_core 應位於 bundle site-packages：{loaded}"
    )
    manifest = _manifest(out)
    package_hash = manifest["components"]["daemon"]["package"]["tree_sha256"]
    assert _tree_sha256_stdlib(loaded.parent, "sw_core") == package_hash, (
        "實際匯入的 site-packages/sw_core 必須與 manifest 套件樹指紋相同"
    )


def test_bundle_two_versions_have_distinct_tree_sha256(tmp_path):
    versions = ("0.4.0", "0.4.1")
    fingerprints: dict[str, str] = {}
    for version in versions:
        out = _build_bundle(tmp_path / f"bundle-{version}", version_override=version)
        vdir = _vdir(out, version)

        cli = subprocess.run(
            [str(out / "bin" / "serialwrap"), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert cli.returncode == 0 and cli.stdout.strip() == f"serialwrap {version}", cli.stderr

        py = subprocess.run(
            [str(vdir / "py" / "bin" / "python3"), "-I", "-m", "sw_core.cli", "--version"],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            check=False,
        )
        assert py.returncode == 0 and py.stdout.strip() == f"serialwrap {version}", py.stderr

        m = json.loads((vdir / "release-manifest.json").read_text(encoding="utf-8"))
        assert m["bundle_version"] == version
        assert m["pairing"] == {"rule": "exact", "cli_version": version, "daemon_version": version}
        fingerprints[version] = _tree_sha256_stdlib(vdir)
        if version == "0.4.0":
            _run_daemon_session(out, _harness_env(tmp_path / "override-iso"), tmp_path / "override-daemon")

    assert fingerprints["0.4.0"] != fingerprints["0.4.1"], "不同版本的 bundle_tree_sha256 必須不同"


def test_bundle_daemon_components_and_sha256sums(bundle):
    out = bundle.out()
    vdir = _vdir(out)
    d = _manifest(out)["components"]["daemon"]
    assert d["implementation"] == "python"
    assert d["version"] == VERSION
    assert d["entry"] == ["py/bin/python3", "-I", "-B", "py/serialwrapd.py"]
    assert d["launcher"]["path"] == "bin/serialwrapd"
    assert d["launcher"]["sha256"] == _sha256(vdir / "bin" / "serialwrapd")
    assert d["runtime"]["path"] == "py/bin/python3"
    assert d["runtime"]["sha256"] == _sha256(vdir / "py" / "bin" / "python3")
    assert d["runtime"]["python_version"] == _meta_python_version()
    assert d["package"]["name"] == "serialwrap"
    assert d["package"]["version"] == VERSION
    assert d["package"]["tree_sha256"] == _tree_sha256_stdlib(vdir / "py", "sw_core")

    sums = vdir / "SHA256SUMS"
    assert sums.is_file(), f"缺少 SHA256SUMS：{sums}"
    listed: list[str] = []
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, sep, rel = line.partition("  ")
        assert sep and len(digest) == 64, f"SHA256SUMS 列格式須為 `<sha256>  <relpath>`：{line!r}"
        assert _sha256(vdir / rel) == digest, f"SHA256SUMS 與實際檔案不符：{rel}"
        listed.append(rel)
    assert listed == sorted(listed), "SHA256SUMS 必須依 relpath 排序"
    deployed = {
        p.relative_to(vdir).as_posix()
        for p in vdir.rglob("*")
        if p.is_file() and p.name not in ("SHA256SUMS", "release-manifest.json")
    }
    missing = sorted(deployed - set(listed))
    assert not missing, f"SHA256SUMS 漏列部署檔：{missing[:10]}"


def _build_layout(out: Path, *, sentinel: bool) -> None:
    """以 `mkbundle.sh --dev`（可選 `--delegate-sentinel`）建立或重建 `out`；失敗以 AssertionError 呈現（含 stderr）。"""
    _require_cargo()
    args = [str(MKBUNDLE), "--dev"]
    if sentinel:
        args.append("--delegate-sentinel")
    args += ["--out", str(out)]
    res = subprocess.run(args, capture_output=True, text=True, timeout=900, cwd=str(REPO), check=False)
    assert res.returncode == 0, f"mkbundle.sh {' '.join(args[1:])} 失敗（rc={res.returncode}）：{res.stderr.strip()}"


def test_refuses_nonempty_out_not_created_by_mkbundle(tmp_path):
    """非空且非 mkbundle 建立的目錄：exit 2、不寫入任何檔案（於 cargo 建置前即拒絕，不需 cargo）。"""
    out = tmp_path / "foreign"
    out.mkdir()
    notes = out / "operator-notes.txt"
    notes.write_text("keep", encoding="utf-8")
    res = subprocess.run(
        [str(MKBUNDLE), "--dev", "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert res.returncode == 2, res.stderr
    assert "拒絕" in res.stderr
    assert sorted(p.name for p in out.iterdir()) == ["operator-notes.txt"]
    assert notes.read_text(encoding="utf-8") == "keep"


def test_refuses_legacy_layout_without_owner_marker(tmp_path):
    """無 owner marker 的舊版 sentinel 頂層 manifest 同樣視為非 mkbundle 建立：拒絕並原樣保留。"""
    out = tmp_path / "legacy"
    out.mkdir()
    stale = out / "release-manifest.json"
    stale.write_text("{}", encoding="utf-8")
    res = subprocess.run(
        [str(MKBUNDLE), "--dev", "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert res.returncode == 2, res.stderr
    assert stale.read_text(encoding="utf-8") == "{}"
    assert not (out / ".mkbundle-owner").exists()


def test_refuses_out_path_that_is_a_regular_file(tmp_path):
    target = tmp_path / "not-a-dir"
    target.write_text("x", encoding="utf-8")
    res = subprocess.run(
        [str(MKBUNDLE), "--dev", "--out", str(target)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert res.returncode == 2, res.stderr
    assert target.read_text(encoding="utf-8") == "x"


def test_sentinel_then_dev_into_same_out_leaves_single_layout(tmp_path):
    """sentinel 與 dev 建到同一目錄：第二次建置須整個重建，最終只剩單一佈局（不殘留頂層 manifest／py／前次檔案）。"""
    out = tmp_path / "shared out"
    _build_layout(out, sentinel=True)
    assert (out / "release-manifest.json").is_file(), "前置條件：sentinel 佈局應寫入頂層 manifest"
    stale = out / "stale-from-sentinel.txt"
    stale.write_text("stale", encoding="utf-8")

    _build_layout(out, sentinel=False)

    assert not stale.exists(), "mkbundle 自有目錄須整個重建，不得保留前次檔案"
    assert not (out / "release-manifest.json").exists(), "dev 建置後殘留 sentinel 頂層 manifest（bundle 定位會取到過期清單）"
    assert not (out / "py").exists(), "dev 建置後殘留 sentinel 的頂層 py/"
    assert (out / "versions" / VERSION / "release-manifest.json").is_file()
    assert (out / "bin" / "serialwrap").is_symlink()
    assert sorted(p.name for p in (out / "versions").iterdir()) == [VERSION]

    _build_layout(out, sentinel=True)

    assert not (out / "versions").exists(), "sentinel 建置後殘留 dev 的 versions/"
    manifest = json.loads((out / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["stage"] == "rust-1b-dev"


def test_bundle_launcher_starts_python_daemon(bundle, tmp_path):
    out = bundle.out()
    launcher = out / "bin" / "serialwrapd"
    assert launcher.is_file(), f"bin/serialwrapd（Stage 1 launcher）缺失：{launcher}"
    env = _harness_env(tmp_path / "iso")
    sockdir = Path(tempfile.mkdtemp(prefix="swl-", dir=tempfile.gettempdir()))
    proc: subprocess.Popen | None = None
    try:
        sock = sockdir / "s"
        with open(tmp_path / "daemon.log", "w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [
                    str(launcher),
                    "--socket", str(sock),
                    "--lock", str(sockdir / "l"),
                    "--profile-dir", str(tmp_path / "profiles"),
                ],
                env=env,
                cwd=str(tmp_path),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            ping = _wait_ready(str(sock), proc)
            assert ping.get("ok") is True
            instances = _count_procs_with(str(sock))
            assert instances == 1, (
                f"launcher 應以 execve 取代自身，socket {sock} 只能對應 1 個 daemon 行程"
                f"（multi_open 規則 2；實際 {instances}）"
            )
            _send_rpc(str(sock), "daemon.stop", {})
            proc.wait(timeout=20)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        shutil.rmtree(sockdir, ignore_errors=True)


_CHECKSUM_EXEMPT = frozenset({"release-manifest.json", "SHA256SUMS"})


def _assert_sha256sums_lists_every_file(root: Path) -> None:
    """除 release-manifest.json 與 SHA256SUMS 外，`root` 下每個一般檔都必須有 checksum 列且數值相符。

    與 Rust 端 `verify_bundle`（crates/sw-cli/src/install_layout.rs）採同一不變量：缺列即
    CHECKSUM_VERIFY_FAILED（`sha256:<檔名> missing`）。`.mkbundle-owner` 於 sentinel 扁平佈局落在根目錄，
    不得以排除清單豁免。
    """
    sums = root / "SHA256SUMS"
    assert sums.is_file(), f"缺少 SHA256SUMS：{sums}"
    listed: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, sep, rel = line.partition("  ")
        assert sep and len(digest) == 64, f"SHA256SUMS 列格式須為 `<sha256>  <relpath>`：{line!r}"
        listed[rel] = digest
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    unlisted = sorted(actual - set(listed) - _CHECKSUM_EXEMPT)
    assert not unlisted, f"SHA256SUMS 漏列檔案（Rust 驗證器將回 CHECKSUM_VERIFY_FAILED）：{unlisted[:10]}"
    stale = sorted(set(listed) - actual)
    assert not stale, f"SHA256SUMS 列出不存在的檔案：{stale[:10]}"
    mismatched = sorted(rel for rel, digest in listed.items() if _sha256(root / rel) != digest)
    assert not mismatched, f"SHA256SUMS 與實際檔案不符：{mismatched[:10]}"


def test_sha256sums_lists_every_file_dev(bundle):
    _assert_sha256sums_lists_every_file(_vdir(bundle.out()))


def test_sha256sums_lists_every_file_sentinel(sentinel_bundle):
    out = sentinel_bundle.out()
    assert (out / ".mkbundle-owner").is_file(), "前置條件：sentinel 扁平佈局的 owner marker 應位於根目錄"
    _assert_sha256sums_lists_every_file(out)


def _isolated_pidns_prefix() -> list[str]:
    """`setup` 的 safety check 以 `/proc` 掃描 serialwrapd；無 `--socket` 的宿主生產 daemon 會被視為占用（DEVICE_STILL_HELD）。

    本測試只驗證 bundle 的組裝與 setup 本身，故與 contract_blackbox 的 `pidns_rig` 採同一隔離手段：
    以非特權 user+pid+mount namespace 執行，宿主行程不可見。能力不足時 skip（絕不 PASS）。
    """
    harness_dir = str(REPO / "tests" / "contract_blackbox")
    if harness_dir not in sys.path:
        sys.path.insert(0, harness_dir)
    from bb_l15 import unshare_pidns_argv

    prefix = unshare_pidns_argv()
    if prefix is None:
        pytest.skip("無法以非特權 unshare 建立 pid namespace（環境能力不足，非 PASS）")
    return prefix


def test_setup_on_demand_from_sentinel_bundle_isolated_home(sentinel_bundle, tmp_path):
    out = sentinel_bundle.out()
    env = _harness_env(tmp_path / "iso")
    res = subprocess.run(
        [*_isolated_pidns_prefix(), str(out / "bin" / "serialwrap"), "setup", "--on-demand"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert res.returncode == 0, (
        f"sentinel bundle 的 setup --on-demand 失敗（rc={res.returncode}）：{res.stdout}{res.stderr}"
    )
    assert (Path(env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml").is_file()
