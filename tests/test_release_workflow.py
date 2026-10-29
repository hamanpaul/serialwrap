"""release workflow 的 Linux Rust bundle job 結構與步驟行為測試（RELEASE-TODO-01）。

以 PyYAML 解析 .github/workflows/release.yml，驗證：
- 新 job `publish-linux-bundle` 於 tag push 觸發、`needs: publish`，既有 wheel／PyInstaller exe job 保留；
- tag↔VERSION（及 Cargo workspace 版號）檢查、cargo build、mkbundle 正式候選、打包、SHA256SUMS、smoke、上傳的步驟與順序；
- 取出步驟的 shell 腳本以 bash 實際執行（假 bundle）：版號不符、smoke 任一條件不成立都必須非零結束；
- bundle runtime 必須以 ubuntu-24.04 系統 `/usr/bin/python3.12` 建立（OD-50；不得用 actions/setup-python 的
  hosted toolcache），smoke 斷言 `py/pyvenv.cfg` 的 `home = /usr/bin`，並在乾淨 ubuntu:24.04 容器
  （`--network none`、只唯讀掛載解開的 bundle）內啟動 on-demand daemon 確認可用（S1-3185c6d-1）。
Windows bundle 由擁有者 runner 原生建置後附加（release 計畫 §2），workflow 不建。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

# 本檔混合 memory_only（YAML 結構）與 process_rpc（bash 執行步驟），依規則 2 模組層只放平台 marker
pytestmark = [pytest.mark.posix_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
JOB = "publish-linux-bundle"
BUNDLE_NAME = "serialwrap-${VERSION}-linux-x86_64"
TARBALL = f"dist/{BUNDLE_NAME}.tar.gz"
# 只在 `sw-cli/test-fakes` 編入的故障注入鉤子字串（crates/sw-cli/src/rollback.rs）；正式候選二進位不得含有。
TEST_FAKE_MARKERS = ("SW_TEST_UPDATE_PAUSE_AFTER", "SW_TEST_UPDATE_FORCE_VERIFY_FAIL")


def _load() -> dict[str, Any]:
    assert RELEASE_YML.is_file(), f"工作流程檔案不存在: {RELEASE_YML}"
    with open(RELEASE_YML, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    assert isinstance(doc, dict), "release.yml 根節點必須是 mapping"
    return doc


def _job() -> dict[str, Any]:
    jobs = _load().get("jobs", {})
    assert JOB in jobs, f"release.yml 缺少 {JOB!r} job"
    return jobs[JOB]


def _steps() -> list[dict[str, Any]]:
    steps = _job().get("steps", [])
    assert steps, f"{JOB} 缺少 steps"
    return steps


def _step(step_id: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("id") == step_id:
            return step
    pytest.fail(f"{JOB} 缺少 id={step_id!r} 的步驟")


def _step_index(step_id: str) -> int:
    return [step.get("id") for step in _steps()].index(step_id)


def _run_step(step_id: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """以 GitHub Actions 的預設 shell（bash -e -o pipefail）執行步驟的 run 腳本。"""
    script = _step(step_id)["run"]
    full_env = {"PATH": os.environ["PATH"], "LC_ALL": "C.UTF-8", **env}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.memory_only
def test_release_workflow_triggers_on_tag_push():
    doc = _load()
    on = doc.get("on") if "on" in doc else doc.get(True)
    assert isinstance(on, dict) and "push" in on, f"release.yml 必須由 push 觸發: {on}"
    assert on["push"].get("tags") == ["v*"], f"release.yml 必須只由 v* tag push 觸發: {on['push']}"
    assert "branches" not in on["push"], "release.yml 不得由分支 push 觸發"


@pytest.mark.memory_only
def test_existing_wheel_and_windows_exe_jobs_are_kept():
    jobs = _load()["jobs"]
    for name in ("project-policy", "publish", "publish-windows-exe"):
        assert name in jobs, f"既有 job {name!r} 不得移除"
    publish_runs = "\n".join(step.get("run", "") for step in jobs["publish"]["steps"])
    assert "python3 -m build --wheel" in publish_runs
    assert 'gh release upload "$TAG_NAME" dist/*.whl --clobber' in publish_runs
    exe_runs = "\n".join(step.get("run", "") for step in jobs["publish-windows-exe"]["steps"])
    assert "python -m PyInstaller --noconfirm serialwrap.spec" in exe_runs
    assert "dist/serialwrap.exe dist/serialwrapd.exe" in exe_runs


@pytest.mark.memory_only
def test_linux_bundle_job_needs_publish_and_pins_toolchain():
    job = _job()
    # OD-50：Linux 使用者皆為 Ubuntu 24.04 以上；bundle runtime 綁定該版系統 /usr/bin/python3.12，runner 須釘同版。
    assert job.get("runs-on") == "ubuntu-24.04", f"{JOB} 必須 runs-on: ubuntu-24.04（OD-50）: {job.get('runs-on')}"
    needs = job.get("needs")
    needs = [needs] if isinstance(needs, str) else needs
    assert needs == ["publish"], f"{JOB} 必須 needs: publish（等 GitHub Release 建立後才上傳）: {needs}"
    uses = [step.get("uses", "") for step in _steps()]
    assert any(u.startswith("actions/checkout@") for u in uses)
    assert "dtolnay/rust-toolchain@1.96.1" in uses, "Rust toolchain 必須釘 1.96.1（與 rust.yml、rust-toolchain.toml 一致）"


@pytest.mark.memory_only
def test_linux_bundle_job_builds_runtime_from_system_python():
    """S1-3185c6d-1：hosted toolcache Python 建的 venv 搬到使用者主機會缺 libpython／標準函式庫，daemon 無法啟動。"""
    uses = [step.get("uses", "") for step in _steps()]
    assert not any(u.startswith("actions/setup-python@") for u in uses), (
        f"{JOB} 不得用 actions/setup-python（hosted toolcache Python 不可搬移，S1-3185c6d-1）"
    )
    text = "\n".join(str(step.get("run", "")) for step in _steps())
    assert "hostedtoolcache" not in text, f"{JOB} 不得引用 hosted toolcache 的 Python"
    step = _step("system-python")
    assert _step_index("system-python") < _step_index("mkbundle"), "system-python 必須在 mkbundle 之前"
    run = step["run"]
    for needle in (
        "python3.12-venv",
        "/usr/bin/python3.12 -m venv",
        "contracts/cli_help/_meta.json",
        "pyyaml==6.0.3",
        "jsonschema==4.10.3",
        "GITHUB_PATH",
        "sys._base_executable",
    ):
        assert needle in run, f"system-python 步驟缺少 {needle!r}"
    for install in ("cargo-build", "mkbundle"):
        assert "pip install" not in _step(install)["run"]


@pytest.mark.memory_only
def test_linux_bundle_job_container_smoke_isolation():
    """容器 smoke：乾淨 ubuntu:24.04＋系統 python3.12、--network none、只唯讀掛載解開的 bundle，啟停 on-demand daemon。"""
    order = ["smoke", "container-smoke", "upload"]
    indexes = [_step_index(step_id) for step_id in order]
    assert indexes == sorted(indexes), f"{JOB} 步驟順序必須為 {order}: {indexes}"
    run = _step("container-smoke")["run"]
    assert "FROM ubuntu:24.04" in run
    assert "--no-install-recommends python3.12" in run
    for forbidden in ("python3-yaml", "python3-jsonschema", "python3-venv", "libpython3.12t64"):
        assert forbidden not in run, f"容器映像不得預裝 {forbidden}（bundle 必須自帶 runtime 依賴）"
    docker_run = [line for line in run.splitlines() if "docker run" in line]
    assert len(docker_run) == 1, f"container-smoke 只能有一個 docker run: {docker_run}"
    joined = _joined_command(run, "docker run")
    assert "--network none" in joined
    mounts = [tok for tok in joined.split() if tok in ("-v", "--volume", "--mount")]
    assert mounts == ["-v"], f"只能有一個 -v 掛載（解開的 bundle）: {joined}"
    assert '-v "$root:/opt/serialwrap:ro"' in joined, f"bundle 必須唯讀掛載: {joined}"
    for forbidden in ("GITHUB_WORKSPACE", "hostedtoolcache", "/dev/tty", "--device", "--privileged", "$PWD"):
        assert forbidden not in joined, f"docker run 不得帶入 {forbidden}: {joined}"
    for needle in (
        'root="$RUNNER_TEMP/smoke/serialwrap-${VERSION}-linux-x86_64"',
        "supervision_mode: on-demand",
        "SERIALWRAP_RUN_DIR",
        "SERIALWRAP_STATE_DIR",
        "SERIALWRAP_BY_ID_DIR",
        '"$B/bin/serialwrap" daemon start',
        '"$B/bin/serialwrap" daemon status',
        '"$B/bin/serialwrap" daemon stop',
        "-I -B -c 'import sw_core.daemon, yaml",
        "container smoke passed",
    ):
        assert needle in run, f"container-smoke 缺少 {needle!r}"


def _joined_command(script: str, start: str) -> str:
    """把以反斜線續行的 shell 命令接成一行，回傳以 `start` 開頭的那條命令。"""
    logical: list[str] = []
    buf = ""
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        logical.append(buf + stripped)
        buf = ""
    matches = [line for line in logical if start in line]
    assert len(matches) == 1, f"找不到唯一的 {start!r} 命令: {matches}"
    return matches[0]


@pytest.mark.memory_only
def test_linux_bundle_job_step_order():
    order = ["verify-version", "cargo-build", "mkbundle", "package", "smoke", "upload"]
    indexes = [_step_index(step_id) for step_id in order]
    assert indexes == sorted(indexes), f"{JOB} 步驟順序必須為 {order}: {indexes}"


@pytest.mark.memory_only
def test_linux_bundle_job_build_commands():
    assert "cargo build --release" in _step("cargo-build")["run"]
    mkbundle = _step("mkbundle")["run"]
    assert "scripts/rust/mkbundle.sh --release-bundle --profile release --out" in mkbundle
    assert "--version-override" not in mkbundle, "正式 bundle 不得以 --version-override 改版號（release 計畫 §2）"
    assert "--dev" not in mkbundle and "test-fakes" not in mkbundle
    assert BUNDLE_NAME in mkbundle


@pytest.mark.memory_only
def test_linux_bundle_job_packages_and_uploads_named_artifacts():
    package = _step("package")["run"]
    assert f"{BUNDLE_NAME}.tar.gz" in package
    assert "SHA256SUMS" in package and "sha256sum" in package
    upload = _step("upload")
    assert upload.get("env", {}).get("GH_TOKEN") == "${{ secrets.GITHUB_TOKEN }}"
    assert 'gh release upload "$TAG_NAME"' in upload["run"]
    assert f'"{TARBALL}"' in upload["run"] and "dist/SHA256SUMS" in upload["run"]
    assert "--clobber" in upload["run"]


@pytest.mark.memory_only
def test_linux_bundle_job_smoke_covers_required_checks():
    smoke = _step("smoke")["run"]
    for needle in ("--version", "--help", "release-manifest.json", "sha256sum -c", "py/pyvenv.cfg",
                   "home = /usr/bin", *TEST_FAKE_MARKERS):
        assert needle in smoke, f"smoke 步驟缺少 {needle!r}"


@pytest.mark.memory_only
def test_workflow_does_not_build_windows_bundle_and_has_no_continue_on_error():
    doc = _load()
    text = RELEASE_YML.read_text(encoding="utf-8")
    assert "windows-x86_64.zip" not in text, "Windows bundle 由擁有者 runner 建置後上傳，workflow 不建"
    for name, job in doc["jobs"].items():
        assert job.get("continue-on-error") not in (True, "true"), f"job {name} 不得 continue-on-error"
        for step in job.get("steps", []):
            assert step.get("continue-on-error") not in (True, "true"), f"job {name} 步驟不得 continue-on-error"
        if "windows" in str(job.get("runs-on", "")):
            runs = "\n".join(step.get("run", "") for step in job.get("steps", []))
            assert "mkbundle" not in runs, f"job {name} 不得在 workflow 內建 Windows bundle"


# ---- 步驟腳本行為（bash 實際執行） ----


def _verify_tree(tmp_path: Path, version: str, cargo_version: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "Cargo.toml").write_text(
        f'[workspace]\nmembers = []\n\n[workspace.package]\nversion = "{cargo_version}"\n', encoding="utf-8"
    )
    return root


@pytest.mark.process_rpc
@pytest.mark.parametrize(
    ("tag", "version", "cargo_version", "ok"),
    [
        ("v1.2.3", "1.2.3", "1.2.3", True),
        ("v1.2.4", "1.2.3", "1.2.3", False),
        ("1.2.3", "1.2.3", "1.2.3", False),
        ("v1.2.3", "1.2.3", "1.2.2", False),
    ],
)
def test_verify_version_step_fails_on_mismatch(tmp_path, tag, version, cargo_version, ok):
    root = _verify_tree(tmp_path, version, cargo_version)
    github_env = tmp_path / "github_env"
    github_env.write_text("", encoding="utf-8")
    res = _run_step("verify-version", root, {"TAG_NAME": tag, "GITHUB_ENV": str(github_env)})
    if ok:
        assert res.returncode == 0, res.stdout + res.stderr
        assert f"VERSION={version}" in github_env.read_text(encoding="utf-8").splitlines()
    else:
        assert res.returncode != 0, f"tag={tag} VERSION={version} Cargo={cargo_version} 必須失敗"
        assert "::error::" in res.stdout + res.stderr
        assert "VERSION=" not in github_env.read_text(encoding="utf-8")


FAKE_VERSION = "9.8.7"
# 以 ubuntu-24.04 系統 /usr/bin/python3.12 建立（經 build venv）的 runtime：home 指向 /usr/bin（OD-50）。
PYVENV_CFG = (
    "home = /usr/bin\n"
    "include-system-site-packages = false\n"
    "version = 3.12.3\n"
    "executable = /usr/bin/python3.12\n"
)
# actions/setup-python 建出的 runtime（S1-3185c6d-1 的失敗情境）。
TOOLCACHE_PYVENV_CFG = (
    "home = /opt/hostedtoolcache/Python/3.12.3/x64/bin\n"
    "include-system-site-packages = false\n"
    "version = 3.12.3\n"
    "executable = /opt/hostedtoolcache/Python/3.12.3/x64/bin/python3.12\n"
)


def _fake_bundle(runner_temp: Path, version: str = FAKE_VERSION) -> Path:
    """仿 mkbundle 正式候選佈局：bin/ symlink → versions/<V>/bin/，versions/<V>/ 含 manifest 與 SHA256SUMS。"""
    out = runner_temp / "bundle" / f"serialwrap-{version}-linux-x86_64"
    vdir = out / "versions" / version
    (vdir / "bin").mkdir(parents=True)
    (vdir / "py").mkdir()
    (vdir / "py" / "serialwrapd.py").write_text("# daemon\n", encoding="utf-8")
    (vdir / "py" / "pyvenv.cfg").write_text(PYVENV_CFG, encoding="utf-8")
    cli = vdir / "bin" / "serialwrap"
    cli.write_text(
        "#!/bin/sh\n"
        f'case "$1" in --version) echo "serialwrap {version}";; --help) echo "usage: serialwrap";; *) exit 2;; esac\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)
    daemon = vdir / "bin" / "serialwrapd"
    daemon.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    daemon.chmod(0o755)
    manifest = {
        "bundle_version": version,
        "stage": "1A",
        "components": {"cli": {"version": version}, "daemon": {"version": version}},
        "pairing": {"cli_version": version, "daemon_version": version},
    }
    (vdir / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_inner_sums(vdir)
    (out / "bin").mkdir()
    (out / "bin" / "serialwrap").symlink_to(f"../versions/{version}/bin/serialwrap")
    (out / "bin" / "serialwrapd").symlink_to(f"../versions/{version}/bin/serialwrapd")
    (out / ".mkbundle-owner").write_text("owner\n", encoding="utf-8")
    return out


def _write_inner_sums(vdir: Path) -> None:
    rows = []
    for path in sorted(p for p in vdir.rglob("*") if p.is_file()):
        rel = path.relative_to(vdir).as_posix()
        if rel in {"release-manifest.json", "SHA256SUMS"}:
            continue
        rows.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}\n")
    (vdir / "SHA256SUMS").write_text("".join(rows), encoding="utf-8")


def _edit_manifest(vdir: Path, change: Callable[[dict[str, Any]], None]) -> None:
    path = vdir / "release-manifest.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _wrong_cli_version(vdir: Path) -> None:
    cli = vdir / "bin" / "serialwrap"
    cli.write_text(cli.read_text(encoding="utf-8").replace(f"serialwrap {FAKE_VERSION}", "serialwrap 0.0.1"))
    _write_inner_sums(vdir)


def _broken_help(vdir: Path) -> None:
    cli = vdir / "bin" / "serialwrap"
    cli.write_text(cli.read_text(encoding="utf-8").replace('echo "usage: serialwrap"', "exit 3"))
    _write_inner_sums(vdir)


def _test_fake_marker(vdir: Path) -> None:
    cli = vdir / "bin" / "serialwrap"
    cli.write_text(cli.read_text(encoding="utf-8") + "# SW_TEST_UPDATE_PAUSE_AFTER\n")
    _write_inner_sums(vdir)


def _pyvenv_cfg(content: str | None) -> Callable[[Path], None]:
    def mutate(vdir: Path) -> None:
        cfg = vdir / "py" / "pyvenv.cfg"
        if content is None:
            cfg.unlink()
        else:
            cfg.write_text(content, encoding="utf-8")
        _write_inner_sums(vdir)

    return mutate


def _stale_inner_sums(vdir: Path) -> None:
    (vdir / "py" / "serialwrapd.py").write_text("# tampered\n", encoding="utf-8")


BUNDLE_MUTATIONS: dict[str, Callable[[Path], None]] = {
    "cli_version_mismatch": _wrong_cli_version,
    "help_fails": _broken_help,
    "manifest_missing": lambda vdir: (vdir / "release-manifest.json").unlink(),
    "manifest_bundle_version": lambda vdir: _edit_manifest(vdir, lambda d: d.update(bundle_version="0.0.1")),
    "manifest_cli_version": lambda vdir: _edit_manifest(vdir, lambda d: d["components"]["cli"].update(version="0.0.1")),
    "manifest_daemon_version": lambda vdir: _edit_manifest(
        vdir, lambda d: d["components"]["daemon"].update(version="0.0.1")
    ),
    "manifest_sentinel_stage": lambda vdir: _edit_manifest(vdir, lambda d: d.update(stage="rust-1b-dev")),
    "test_fake_marker": _test_fake_marker,
    "inner_sums_stale": _stale_inner_sums,
}

# runtime 可攜性（S1-3185c6d-1）：失敗訊息必須點名 pyvenv.cfg，避免因其他原因誤判為通過。
PYVENV_MUTATIONS: dict[str, Callable[[Path], None]] = {
    "pyvenv_missing": _pyvenv_cfg(None),
    "pyvenv_home_toolcache": _pyvenv_cfg(TOOLCACHE_PYVENV_CFG),
    "pyvenv_home_usr_local": _pyvenv_cfg(PYVENV_CFG.replace("home = /usr/bin", "home = /usr/local/bin")),
    "pyvenv_home_prefix_only": _pyvenv_cfg(PYVENV_CFG.replace("home = /usr/bin", "home = /usr/bin/extra")),
    "pyvenv_system_site_packages": _pyvenv_cfg(
        PYVENV_CFG.replace("include-system-site-packages = false", "include-system-site-packages = true")
    ),
}


def _package_and_smoke(tmp_path: Path, mutate: Callable[[Path], None] | None = None,
                       after_package: Callable[[Path], None] | None = None):
    work = tmp_path / "work"
    work.mkdir()
    runner_temp = tmp_path / "runner_temp"
    out = _fake_bundle(runner_temp)
    if mutate is not None:
        mutate(out / "versions" / FAKE_VERSION)
    env = {"VERSION": FAKE_VERSION, "RUNNER_TEMP": str(runner_temp)}
    package = _run_step("package", work, env)
    assert package.returncode == 0, package.stdout + package.stderr
    if after_package is not None:
        after_package(work)
    return work, _run_step("smoke", work, env)


@pytest.mark.process_rpc
def test_package_and_smoke_pass_on_well_formed_bundle(tmp_path):
    work, smoke = _package_and_smoke(tmp_path)
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    tarball = work / "dist" / f"serialwrap-{FAKE_VERSION}-linux-x86_64.tar.gz"
    sums = (work / "dist" / "SHA256SUMS").read_text(encoding="utf-8")
    assert sums == f"{hashlib.sha256(tarball.read_bytes()).hexdigest()}  {tarball.name}\n"
    with tarfile.open(tarball) as archive:
        names = archive.getnames()
        prefix = f"serialwrap-{FAKE_VERSION}-linux-x86_64"
        assert all(name == prefix or name.startswith(prefix + "/") for name in names)
        assert not any(name.endswith(".mkbundle-owner") for name in names), "owner marker 不得進入發佈 tarball"
        link = archive.getmember(f"{prefix}/bin/serialwrap")
        assert link.issym() and link.linkname == f"../versions/{FAKE_VERSION}/bin/serialwrap"


@pytest.mark.process_rpc
@pytest.mark.parametrize("mutation", sorted(BUNDLE_MUTATIONS))
def test_smoke_fails_on_broken_bundle(tmp_path, mutation):
    _, smoke = _package_and_smoke(tmp_path, mutate=BUNDLE_MUTATIONS[mutation])
    assert smoke.returncode != 0, f"{mutation}: smoke 必須失敗\n{smoke.stdout}{smoke.stderr}"


@pytest.mark.process_rpc
def test_smoke_fails_when_tarball_does_not_match_sha256sums(tmp_path):
    def tamper(work: Path) -> None:
        with open(work / "dist" / f"serialwrap-{FAKE_VERSION}-linux-x86_64.tar.gz", "ab") as handle:
            handle.write(b"\0")

    _, smoke = _package_and_smoke(tmp_path, after_package=tamper)
    assert smoke.returncode != 0, smoke.stdout + smoke.stderr


@pytest.mark.process_rpc
@pytest.mark.parametrize("mutation", sorted(PYVENV_MUTATIONS))
def test_smoke_fails_on_non_portable_python_runtime(tmp_path, mutation):
    _, smoke = _package_and_smoke(tmp_path, mutate=PYVENV_MUTATIONS[mutation])
    assert smoke.returncode != 0, f"{mutation}: smoke 必須失敗\n{smoke.stdout}{smoke.stderr}"
    assert "pyvenv.cfg" in smoke.stdout + smoke.stderr, f"{mutation}: 失敗原因須為 pyvenv.cfg\n{smoke.stdout}{smoke.stderr}"


# ---- container-smoke（以假 docker 驗證步驟對 docker 結果的判定） ----


def _fake_docker(bin_dir: Path, run_script: str) -> Path:
    """假 docker：build 成功；run 時記錄參數、吃掉 stdin，再執行 `run_script`（模擬容器內結果）。"""
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / "docker-args.log"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        "  build) cat > /dev/null; echo sha256:fake; exit 0;;\n"
        f"  run) cat > /dev/null; {run_script};;\n"
        "  *) exit 0;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return log


def _container_smoke(tmp_path: Path, run_script: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    work = tmp_path / "work"
    work.mkdir()
    runner_temp = tmp_path / "runner_temp"
    (runner_temp / "smoke" / f"serialwrap-{FAKE_VERSION}-linux-x86_64").mkdir(parents=True)
    log = _fake_docker(tmp_path / "fake-bin", run_script)
    env = {
        "VERSION": FAKE_VERSION,
        "RUNNER_TEMP": str(runner_temp),
        "PATH": f"{tmp_path / 'fake-bin'}{os.pathsep}{os.environ['PATH']}",
    }
    return _run_step("container-smoke", work, env), log


@pytest.mark.process_rpc
def test_container_smoke_passes_when_container_reports_success(tmp_path):
    res, log = _container_smoke(tmp_path, 'echo "container smoke passed for serialwrap-9.8.7"; exit 0')
    assert res.returncode == 0, res.stdout + res.stderr
    runs = [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run ")]
    assert len(runs) == 1, runs
    assert "--network none" in runs[0]
    assert f"{tmp_path}/runner_temp/smoke/serialwrap-{FAKE_VERSION}-linux-x86_64:/opt/serialwrap:ro" in runs[0]


@pytest.mark.process_rpc
@pytest.mark.parametrize(
    "run_script",
    [
        # 例：bundle runtime 在容器內載入失敗（S1-3185c6d-1）
        'echo "python3: error while loading shared libraries: libpython3.12.so.1.0" >&2; exit 127',
        # docker 成功結束卻沒有容器內的通過標記（腳本未實際執行）
        "exit 0",
    ],
    ids=["container_fails", "no_pass_marker"],
)
def test_container_smoke_fails_when_container_check_fails(tmp_path, run_script):
    res, _ = _container_smoke(tmp_path, run_script)
    assert res.returncode != 0, res.stdout + res.stderr
    assert "::error::" in res.stdout + res.stderr
