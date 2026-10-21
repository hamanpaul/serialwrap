"""Rust CI workflow 與 Stage 1A 出口 gate 結構測試（P3-T19）。

以 PyYAML 解析 .github/workflows/rust.yml 與 .github/workflows/tests.yml，
驗證 CI workflow 結構、雙平台 job 定義、依序步驟、固定版本與安全限制（無 continue-on-error）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_YML = REPO_ROOT / ".github" / "workflows" / "rust.yml"
TESTS_YML = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# rust-linux job 中必須依序出現的命令字串
RUST_LINUX_REQUIRED_COMMANDS = [
    "python3 scripts/rust/check_cfg_boundary.py",
    "python3 scripts/rust/check_dep_direction.py",
    "cargo check --workspace --all-targets",
    "cargo test --workspace --all-features",
    "python3 scripts/rust/gen_error_codes.py --check",
    "SW_REQUIRE_CARGO=1 python3 -m pytest -q tests/test_rust_*.py",
    "scripts/rust/mkbundle.sh --dev --out target/dev-bundle",
    (
        "SERIALWRAP_CLI_EXE=$PWD/target/dev-bundle/bin/serialwrap "
        "SERIALWRAP_AB_LABEL=rust-1a python3 -m pytest -q tests/contract_blackbox "
        "--confcutdir=tests/contract_blackbox -p no:cacheprovider"
    ),
    (
        "SERIALWRAP_CLI_EXE=$PWD/target/dev-bundle/bin/serialwrap "
        "python3 func-test/runner.py --mode pty --case sm-01-attach-to-ready"
    ),
]


def _load_yaml(path: Path) -> dict[str, Any]:
    """載入 YAML 檔並確保根節點為 mapping。若檔案不存在則觸發斷言失敗。"""
    assert path.is_file(), f"工作流程檔案不存在: {path}"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"{path} 根節點必須是 mapping"
    return data


def test_rust_workflow_exists_and_triggers_on_pull_request():
    """驗證 .github/workflows/rust.yml 存在且觸發條件含 pull_request。"""
    doc = _load_yaml(RUST_YML)
    # PyYAML 將未加引號的 on: 解析為布林 True（YAML 1.1 規範）
    on_trigger = doc.get("on") if "on" in doc else doc.get(True)
    assert on_trigger is not None, "rust.yml 缺少 'on' 觸發條件"
    if isinstance(on_trigger, list):
        assert "pull_request" in on_trigger, f"'on' 觸發條件未包含 pull_request: {on_trigger}"
    elif isinstance(on_trigger, dict):
        assert "pull_request" in on_trigger, f"'on' 觸發條件未包含 pull_request: {list(on_trigger.keys())}"
    elif isinstance(on_trigger, str):
        assert on_trigger == "pull_request", f"'on' 觸發條件未包含 pull_request: {on_trigger}"
    else:
        pytest.fail(f"無法解析的 'on' 觸發條件型態: {type(on_trigger)}")


def test_rust_linux_job_toolchain_and_python():
    """驗證 rust-linux job 之 toolchain 釘為 1.96.1（含 Windows target 與 mingw）且 Python 釘為 3.12.3。"""
    doc = _load_yaml(RUST_YML)
    jobs = doc.get("jobs", {})
    assert "rust-linux" in jobs, "rust.yml 缺少 'rust-linux' job"
    job = jobs["rust-linux"]
    assert job.get("runs-on") == "ubuntu-latest", f"rust-linux runs-on 必須為 ubuntu-latest: {job.get('runs-on')}"

    steps = job.get("steps", [])
    assert steps, "rust-linux job 缺少 steps"

    job_str = yaml.dump(job)
    # toolchain 1.96.1
    assert "1.96.1" in job_str, "rust-linux 必須安裝/指定 toolchain 1.96.1"
    assert "x86_64-pc-windows-gnu" in job_str, "rust-linux 必須含 x86_64-pc-windows-gnu target"
    assert "gcc-mingw-w64-x86-64" in job_str, "rust-linux 必須安裝 gcc-mingw-w64-x86-64"

    # Python 3.12.3
    assert "3.12.3" in job_str, "rust-linux 必須指定 Python 3.12.3"
    assert "pyyaml" in job_str.lower(), "rust-linux 必須安裝 PyYAML"
    assert "pytest" in job_str.lower(), "rust-linux 必須安裝 pytest"
    assert "jsonschema" in job_str.lower(), "rust-linux 必須安裝 jsonschema"


def test_rust_linux_job_ordered_commands():
    """驗證 rust-linux 的步驟依序含指定命令字串（逐項 in 斷言）。"""
    doc = _load_yaml(RUST_YML)
    job = doc.get("jobs", {}).get("rust-linux", {})
    steps = job.get("steps", [])
    assert steps, "rust-linux job 缺少 steps"

    # 驗證 Windows target linker 注入
    job_str = yaml.dump(job)
    assert "CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER" in job_str, (
        "rust-linux 必須注入 CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER"
    )

    step_runs = [step.get("run", "") for step in steps if isinstance(step, dict) and "run" in step]

    # 逐項 in 斷言與順序檢查
    current_step_idx = 0
    current_char_offset = 0

    for cmd in RUST_LINUX_REQUIRED_COMMANDS:
        # 逐項 in 斷言：命令必須出現在某個步驟中
        assert any(cmd in r for r in step_runs), f"命令 {cmd!r} 未在 rust-linux steps 中出現"

        # 順序檢查：命令必須依序出現
        found = False
        for step_idx in range(current_step_idx, len(step_runs)):
            text = step_runs[step_idx]
            start_pos = current_char_offset if step_idx == current_step_idx else 0
            pos = text.find(cmd, start_pos)
            if pos != -1:
                current_step_idx = step_idx
                current_char_offset = pos + len(cmd)
                found = True
                break
        assert found, f"命令 {cmd!r} 未在 rust-linux steps 中依序找到"


def test_rust_windows_hosted_job():
    """驗證 rust-windows-hosted 的 runs-on 為 windows-latest 且步驟含 cargo test --workspace。"""
    doc = _load_yaml(RUST_YML)
    jobs = doc.get("jobs", {})
    assert "rust-windows-hosted" in jobs, "rust.yml 缺少 'rust-windows-hosted' job"
    job = jobs["rust-windows-hosted"]
    assert job.get("runs-on") == "windows-latest", (
        f"rust-windows-hosted runs-on 必須為 windows-latest: {job.get('runs-on')}"
    )

    steps = job.get("steps", [])
    assert steps, "rust-windows-hosted 缺少 steps"
    step_runs = [step.get("run", "") for step in steps if isinstance(step, dict) and "run" in step]
    assert any("cargo test --workspace" in r for r in step_runs), (
        "rust-windows-hosted 步驟必須包含 'cargo test --workspace'"
    )


def test_rust_windows_com_job():
    """驗證 rust-windows-com 的 runs-on 含 self-hosted 且 if: 條件含 run-windows-com。"""
    doc = _load_yaml(RUST_YML)
    jobs = doc.get("jobs", {})
    assert "rust-windows-com" in jobs, "rust.yml 缺少 'rust-windows-com' job"
    job = jobs["rust-windows-com"]

    runs_on = job.get("runs-on")
    if isinstance(runs_on, list):
        assert "self-hosted" in runs_on, f"rust-windows-com runs-on 必須包含 'self-hosted': {runs_on}"
    elif isinstance(runs_on, str):
        assert "self-hosted" in runs_on, f"rust-windows-com runs-on 必須包含 'self-hosted': {runs_on}"
    else:
        pytest.fail(f"無法識別的 runs-on 型態: {type(runs_on)}")

    if_cond = job.get("if")
    assert if_cond is not None, "rust-windows-com 缺少 if: 條件保護"
    assert "run-windows-com" in str(if_cond), (
        f"rust-windows-com 的 if: 條件必須包含 'run-windows-com'（自架 runner 安全保護）: {if_cond}"
    )


def test_no_continue_on_error():
    """驗證 rust.yml 中任何 job 或 step 皆未設定 continue-on-error: true。"""
    doc = _load_yaml(RUST_YML)
    jobs = doc.get("jobs", {})
    assert jobs, "rust.yml 缺少 jobs"

    for job_name, job_data in jobs.items():
        if not isinstance(job_data, dict):
            continue
        job_coe = job_data.get("continue-on-error")
        assert not (job_coe is True or str(job_coe).strip().lower() == "true"), (
            f"Job {job_name} 不得以 continue-on-error: true 放行"
        )
        for idx, step in enumerate(job_data.get("steps", [])):
            if not isinstance(step, dict):
                continue
            step_coe = step.get("continue-on-error")
            step_name = step.get("name", f"step-{idx}")
            assert not (step_coe is True or str(step_coe).strip().lower() == "true"), (
                f"Job {job_name} 的步驟 {step_name} 不得以 continue-on-error: true 放行"
            )


def test_tests_workflow_python_version():
    """驗證 tests.yml 的 python-version 改釘為 '3.12.3'（§0.7 argparse 方言基準）。"""
    doc = _load_yaml(TESTS_YML)
    jobs = doc.get("jobs", {})
    assert "pytest" in jobs, "tests.yml 缺少 'pytest' job"
    steps = jobs["pytest"].get("steps", [])

    setup_python_steps = [
        step
        for step in steps
        if isinstance(step, dict) and "setup-python" in step.get("uses", "")
    ]
    assert setup_python_steps, "tests.yml 缺少 setup-python 步驟"

    for step in setup_python_steps:
        py_ver = step.get("with", {}).get("python-version")
        assert py_ver == "3.12.3", (
            f"tests.yml 的 python-version 必須釘為 '3.12.3'（目前為 {py_ver!r}）"
        )
