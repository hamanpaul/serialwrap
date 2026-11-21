"""Rust CI workflow 與 Stage 1A 出口 gate 結構測試（P3-T19）。

以 PyYAML 解析 .github/workflows/rust.yml 與 .github/workflows/tests.yml，
驗證 CI workflow 結構、雙平台 job 定義、依序步驟、固定版本與安全限制（無 continue-on-error）。
"""

from __future__ import annotations

import ast
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
    "cargo test --workspace --all-features --no-fail-fast",
    "python3 scripts/rust/gen_error_codes.py --check",
    "SW_REQUIRE_CARGO=1 python3 -m pytest -q tests/test_rust_*.py",
    "scripts/rust/mkbundle.sh --dev --out target/dev-bundle",
    "mkdir -p /mnt/sw-tmp/pt",
    (
        "SW_RESOURCE_REPORT=$PWD/target/ci-blackbox/stage1a-resource-report.json "
        "SERIALWRAP_CLI_EXE=$PWD/target/dev-bundle/bin/serialwrap "
        "SERIALWRAP_AB_LABEL=rust-1a "
        "python3 -m pytest -q tests/contract_blackbox/test_cli_no_delegation.py "
        '-m "rust_only" '
        "--confcutdir=tests/contract_blackbox -p no:cacheprovider --basetemp=/mnt/sw-tmp/pt/stage1a "
        "--junitxml=target/ci-blackbox/junit-stage1a.xml"
    ),
    "scripts/rust/mkbundle.sh --release-bundle --stage 2 --profile release --out target/s24-bundle",
    "source scripts/rust/gate_prereqs.sh --ci",
    (
        "SW_RESOURCE_REPORT=$PWD/target/ci-blackbox/resource-report.json "
        "SERIALWRAP_CLI_EXE=\"$S24_BUNDLE_VERSION_ROOT/bin/serialwrap\" "
        "SERIALWRAP_DAEMON_EXE=\"$S24_BUNDLE_VERSION_ROOT/bin/serialwrapd\" "
        "SW_BUNDLE=\"$SW_BUNDLE\" SERIALWRAP_AB_LABEL=rust-linux "
        "python3 -m pytest -q tests/contract_blackbox "
        '-m "core_portable or posix_only" '
        "--confcutdir=tests/contract_blackbox -p no:cacheprovider --basetemp=/mnt/sw-tmp/pt/stage2 -o tmp_path_retention_policy=failed "
        "--junitxml=target/ci-blackbox/junit.xml"
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


def _assert_runs_after_earlier_failure(step: dict[str, Any], where: str) -> None:
    """步驟必須在前面步驟失敗時仍執行（`!cancelled()` 取代預設的 `success()`），且不得以 continue-on-error 放行。

    PR #278 十輪審查：步驟串行、前一個紅就不跑後面，一輪只露一層。`!cancelled()` 不改變失敗步驟的結論，
    job 仍以失敗結束；`always()` 會連取消也照跑，不作為替代。
    """
    name = step.get("name", step.get("uses", "?"))
    cond = str(step.get("if", ""))
    assert "!cancelled()" in cond, f"{where} 的步驟 {name!r} 必須帶 if: !cancelled()（前面步驟失敗時仍執行）：{cond!r}"
    assert "always()" not in cond, f"{where} 的步驟 {name!r} 不得改用 always()：{cond!r}"
    assert "continue-on-error" not in step, f"{where} 的步驟 {name!r} 不得設定 continue-on-error"


def _call_name(node: ast.AST) -> str:
    """`f(...)`／`a.b(...)` 的被呼叫名稱（`f`／`a.b`）；其他節點回空字串。"""
    if not isinstance(node, ast.Call):
        return ""
    try:
        return ast.unparse(node.func)
    except Exception:  # pragma: no cover - ast.unparse 對合法 AST 不會失敗
        return ""


def _assert_supervision_restart_sync_points(source: str) -> None:
    """`test_stop_during_push_flushes_wal` 的三個 DUT RX 靜定同步點與位移判準（以 AST 的執行順序檢查，不釘字面次數／位置）。

    要保護的意圖（PR #278 第 7、8 輪）：
    1. 停機後先等 DUT RX 靜定，才取樣「已收到的 chunk」基線；
    2. 重啟前再等一次靜定並記下 DUT RX 位移；
    3. 重啟後保留 ≥1 秒觀察窗、再等一次靜定，判定只看該位移之後才到 DUT 的 bytes。
    測試內多加或少加其他呼叫、改註解、改排版都不影響本檢查；拿掉任何一個同步點或改回整段 rx_log 判定才會失敗。
    """
    tree = ast.parse(source)
    func = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "test_stop_during_push_flushes_wal"),
        None,
    )
    assert func is not None, "test_s24_supervision_systemd.py 缺少 test_stop_during_push_flushes_wal"

    def pos(node: ast.AST) -> tuple[int, int]:
        return (node.lineno, node.col_offset)

    calls = sorted((n for n in ast.walk(func) if isinstance(n, ast.Call)), key=pos)
    assigns = sorted((n for n in ast.walk(func) if isinstance(n, ast.Assign)), key=pos)
    settles = [c for c in calls if _call_name(c) == "_settled_rx_len"]

    stop = next((c for c in calls if _call_name(c) == "transient_unit.stop"), None)
    assert stop is not None, "測試必須呼叫 transient_unit.stop()"
    restart = next((c for c in calls if _call_name(c) == "transient_unit.start" and pos(c) > pos(stop)), None)
    assert restart is not None, "停機後必須以 transient_unit.start() 重啟"

    # 1. 停機後的 chunk 基線（讀 shell_target.commands() 的第一個賦值）之前，已有一次靜定等待。
    baseline = next(
        (a for a in assigns if pos(stop) < pos(a) < pos(restart)
         and any(_call_name(c) == "shell_target.commands" for c in ast.walk(a.value))),
        None,
    )
    assert baseline is not None, "停機後、重啟前必須以 shell_target.commands() 取樣已收到的 chunk 基線"
    assert any(pos(stop) < pos(c) < pos(baseline) for c in settles), (
        "重啟前的 no-auto-resend 基線必須在停機後 DUT RX 靜定才取樣"
    )

    # 2. 基線之後、重啟之前，把靜定後的 DUT RX 長度記到一個名稱（位移）。
    offset_assign = next(
        (a for a in assigns if pos(baseline) < pos(a) < pos(restart)
         and _call_name(a.value) == "_settled_rx_len"
         and len(a.targets) == 1 and isinstance(a.targets[0], ast.Name)),
        None,
    )
    assert offset_assign is not None, "重啟前必須再等一次 DUT RX 靜定並記下位移"
    offset_name = offset_assign.targets[0].id

    # 3. 重啟後：≥1 秒觀察窗 → 靜定 → 只取位移之後的 bytes 判定。
    sleep = next(
        (c for c in calls if pos(c) > pos(restart) and _call_name(c) == "time.sleep"
         and c.args and isinstance(c.args[0], ast.Constant)
         and isinstance(c.args[0].value, (int, float)) and c.args[0].value >= 1.0),
        None,
    )
    assert sleep is not None, "重啟後須保留至少 1 秒的 no-auto-resend 觀察窗"
    settle_after = next((c for c in settles if pos(c) > pos(sleep)), None)
    assert settle_after is not None, "重啟後的觀察窗之後須排空 DUT RX 再判定"

    def is_offset_slice(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Subscript)
            and _call_name(node.value) == "shell_target.rx_log"
            and isinstance(node.slice, ast.Slice)
            and isinstance(node.slice.lower, ast.Name) and node.slice.lower.id == offset_name
            and node.slice.upper is None
        )

    sliced = next(
        (a for a in assigns if pos(a) > pos(settle_after) and is_offset_slice(a.value)
         and len(a.targets) == 1 and isinstance(a.targets[0], ast.Name)),
        None,
    )
    assert sliced is not None, "重啟後判定須只看重啟前 DUT RX 位移之後的 bytes"
    sliced_name = sliced.targets[0].id
    sentinel_asserts = [
        n for n in ast.walk(func)
        if isinstance(n, ast.Assert) and pos(n) > pos(sliced)
        and any(isinstance(c, ast.Constant) and c.value == b"SW_XFER_CHUNK" for c in ast.walk(n.test))
    ]
    assert sentinel_asserts, "重啟後必須斷言 chunk sentinel 不出現"
    for node in sentinel_asserts:
        names = {c.id for c in ast.walk(node.test) if isinstance(c, ast.Name)}
        assert sliced_name in names, "chunk sentinel 的判定對象必須是位移之後的 bytes，不得改回整段 rx_log"

    # issue 282：停機後輸入列必須已收尾，重啟殘留四項都採零容忍硬斷言。
    assertions = {
        ast.dump(node.test, include_attributes=False)
        for node in ast.walk(func) if isinstance(node, ast.Assert)
    }
    strict_expressions = (
        'not rx_at_restart or rx_at_restart.endswith(b"\\n")',
        'residue["residual_input_bytes"] == 0',
        'residue["residual_submitted_after_restart"] is False',
        'residue["extra_chunk_commands"] == 0',
        'residue["prompt_unavailable_seen"] is False',
    )
    for expression in strict_expressions:
        expected = ast.dump(ast.parse(expression, mode="eval").body, include_attributes=False)
        assert expected in assertions, f"停止後殘留判準必須保留嚴格斷言：{expression}"
    calls = [node for node in ast.walk(func) if isinstance(node, ast.Call)]
    assert not any(_call_name(node) == "warnings.warn" for node in calls), "issue 282 判準不得降級為 warning"
    loose_bound = ast.dump(
        ast.parse('residue["extra_chunk_commands"] <= 1', mode="eval").body,
        include_attributes=False,
    )
    assert loose_bound not in assertions, "issue 282 不得接受一個額外 chunk"


def test_restart_residue_report_with_fake_observations():
    """以替身 bytes／command list 驗證殘留 helper，不啟動 daemon 或 DUT。"""
    source = (REPO_ROOT / "tests/contract_blackbox/test_s24_supervision_systemd.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    helper = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_restart_residue_report"),
        None,
    )
    assert helper is not None, "test_s24_supervision_systemd.py 缺少純計算 helper"
    namespace: dict[str, Any] = {}
    module = ast.Module(body=[helper], type_ignores=[])
    exec(compile(module, "test_s24_supervision_systemd.py", "exec"), namespace)
    report = namespace["_restart_residue_report"]

    clean = report(b"chunk\n", b"\n$ ", 2, ["SW_XFER_CHUNK_0", "SW_XFER_CHUNK_1"], [])
    assert clean["residual_input_bytes"] == 0
    assert clean["residual_submitted_after_restart"] is False
    assert clean["extra_chunk_commands"] == 0
    assert clean["prompt_unavailable_seen"] is False

    dirty = report(
        b"chunk\nprintf '%s' 'partial", b"\n$ ", 2,
        ["SW_XFER_CHUNK_0", "SW_XFER_CHUNK_1", "SW_XFER_CHUNK_2"], ["PROMPT_UNAVAILABLE"],
    )
    assert dirty["residual_input_bytes"] > 0
    assert dirty["residual_submitted_after_restart"] is True
    assert dirty["extra_chunk_commands"] == 1
    assert dirty["prompt_unavailable_seen"] is True

    cleared = report(b"chunk\nprintf '%s' 'partial", b"\x15\n$ ", 2,
                     ["SW_XFER_CHUNK_0", "SW_XFER_CHUNK_1"], [])
    assert cleared["residual_submitted_after_restart"] is False


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
    assert job.get("runs-on") == "ubuntu-24.04", f"rust-linux runs-on 必須為 ubuntu-24.04（setup-python 3.12.3）: {job.get('runs-on')}"

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

    # 一輪露出全部（PR #278）：第一個檢查步驟之後的每個 run 步驟，在前面步驟失敗時仍須執行。
    first_check = next(i for i, step in enumerate(steps) if "check_cfg_boundary.py" in str(step.get("run", "")))
    later_runs = [step for step in steps[first_check + 1:] if isinstance(step, dict) and "run" in step]
    assert len(later_runs) >= 13, f"rust-linux 檢查步驟數量異常：{len(later_runs)}"
    for step in later_runs:
        _assert_runs_after_earlier_failure(step, "rust-linux")
    cargo_test = next(r for r in step_runs if "cargo test --workspace" in r)
    assert "--no-fail-fast" in cargo_test, "Linux cargo test 必須帶 --no-fail-fast（一個 test binary 紅時仍跑完其餘）"

    # systemd stop 後須等 DUT PTY relay 排空停機前已送出的 TX，才能建立重啟前基線（黑箱步驟所跑測試的同步點）。
    _assert_supervision_restart_sync_points(
        (REPO_ROOT / "tests/contract_blackbox/test_s24_supervision_systemd.py").read_text(encoding="utf-8")
    )


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
    assert any("cargo test --workspace --no-fail-fast" in r for r in step_runs), (
        "rust-windows-hosted 的 cargo test 必須帶 --no-fail-fast"
    )
    # cargo test 紅時仍執行 release 建置與匯入 DLL 檢查（一輪露出全部，PR #278）。
    imports_step = next(step for step in steps if "check_win_imports.py" in str(step.get("run", "")))
    _assert_runs_after_earlier_failure(imports_step, "rust-windows-hosted")


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
    """驗證 rust.yml 中任何 job 或 step 皆未設定 continue-on-error: true；
    tests.yml 的 pytest job 同樣不得以 continue-on-error 放行，且第一遍失敗時仍須跑第二遍與 L1 鎖檢查。"""
    pytest_job = _load_yaml(TESTS_YML)["jobs"]["pytest"]
    assert "continue-on-error" not in pytest_job, "tests.yml 的 pytest job 不得設定 continue-on-error"
    pytest_steps = pytest_job["steps"]
    assert all("continue-on-error" not in step for step in pytest_steps), "pytest job 的步驟不得設定 continue-on-error"
    full_runs = [i for i, step in enumerate(pytest_steps) if "python3 -m pytest -q tests/" in str(step.get("run", ""))]
    assert len(full_runs) == 2, f"pytest job 必須有無 re2／有 re2 兩遍全套：{full_runs}"
    re2_install = next(i for i, step in enumerate(pytest_steps) if "pip install" in str(step.get("run", ""))
                       and "google-re2" in str(step.get("run", "")))
    lock_check = next(i for i, step in enumerate(pytest_steps) if "regen_lock.py --check" in str(step.get("run", "")))
    assert full_runs[0] < re2_install < full_runs[1], "google-re2 必須在第一遍之後、第二遍之前安裝"
    assert "if" not in pytest_steps[full_runs[0]], "第一遍全套不得帶條件"
    # PR #278 十輪內第二遍只執行 4 次、鎖檢查 0 次：第一遍紅時三個步驟仍須執行，job 結論仍為失敗。
    for index in (re2_install, full_runs[1], lock_check):
        _assert_runs_after_earlier_failure(pytest_steps[index], "tests.yml pytest")
    assert "github.event_name == 'pull_request'" in str(pytest_steps[lock_check]["if"]), (
        "L1 鎖只增檢查仍只在 pull_request 事件執行（需要 PR base）"
    )

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


def test_pytest_windows_provisions_pinned_cargo():
    """臨時 workspace 不會繼承 repo override，hosted job 必須明確指定工具鏈並強制 cargo。"""
    job = _load_yaml(TESTS_YML)["jobs"]["pytest-windows"]
    assert job["env"]["RUSTUP_TOOLCHAIN"] == "1.96.1"
    assert job["env"]["SW_REQUIRE_CARGO"] == "1"
    steps = job["steps"]
    rust_index = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("dtolnay/rust-toolchain@"))
    assert steps[rust_index]["uses"] == "dtolnay/rust-toolchain@1.96.1"
    assert steps[rust_index]["with"]["toolchain"] == "1.96.1"
    assert set(steps[rust_index]["with"]["targets"].split(",")) == {
        "x86_64-unknown-linux-gnu", "x86_64-pc-windows-gnu",
    }
    pytest_index = next(i for i, step in enumerate(steps) if "python -m pytest" in step.get("run", ""))
    assert rust_index < pytest_index


def test_pytest_windows_excludes_only_exit_driver_integrations():
    """hosted CI 不生產出口證據；排除名單不得含凍結 Windows 必要節點，出口 driver 仍跑完整集合。"""
    import json
    import shlex

    job = _load_yaml(TESTS_YML)["jobs"]["pytest-windows"]
    command = next(step["run"] for step in job["steps"] if "python -m pytest" in step.get("run", ""))
    tokens = shlex.split(command)
    excluded = [tokens[i + 1] for i, token in enumerate(tokens) if token == "--deselect"]
    assert set(excluded) == {
        "tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native",
        "tests/test_rust_bench_tools.py::test_ab_selection_follows_frozen_selector_per_platform",
        "tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence",
    }
    lock = json.loads((REPO_ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    for platforms in lock["sets"].values():
        assert not set(excluded).intersection(platforms.get("windows", {}).get("nodeids", []))
    driver = (REPO_ROOT / "scripts/rust/stage1_exit_driver.sh").read_text(encoding="utf-8")
    full_step = driver.split("step pytest_full_junit ", 1)[1].split("fresh_or_fail", 1)[0]
    assert "python3 -m pytest -q tests/" in full_step
    assert "--deselect" not in full_step


def test_golden_corpus_checkout_preserves_lf():
    """pyre oracle 拒絕 CRLF，Git 的 Windows 檢出必須保留原始 LF。"""
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    rules = [line.split() for line in attributes.splitlines() if line and not line.startswith("#")]
    assert ["scripts/rust/goldens/pyre_corpus.txt", "text", "eol=lf"] in rules
