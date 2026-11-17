"""Slice 2.1 出口 gate 報告結構與 BLOCKED-OWNER 清單契約（P5-T22，紅燈）。

對應計畫 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 22 Step 1：
- `tests/acceptance/slice21_gate_report.schema.json`：報告 schema（BLOCKED-OWNER 不可為 PASS、
  移除計數門檻 `expected_min_passed`、identity 必填欄位）。
- `scripts/rust/slice21_gate.sh --dry-run`：列出 BO-S21-01～16、verdict 永不為 READY、
  `windows_cap_windows_com` 在未提供 `SW_TEST_COM_PAIR` 時為 EVIDENCE_MISSING。
- `docs/rust-refactor/slice21-owner-runbook.md`：每個 BO 項目有 `### BO-S21-NN` 錨點。
- `tests/acceptance/slice21_bo.yaml`：機器可讀 BO 清單，含 Windows 凍結案例 `case_ids`。

紅燈原因：上述檔案與 gate 腳本尚未存在（FileNotFoundError／bash 非零結束），屬缺少行為，非語法錯誤。
本檔只跑 POSIX（gate 為 bash 腳本，Windows 側證據由 win-test 另行產生）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _schema():
    return json.loads((ROOT / "tests/acceptance/slice21_gate_report.schema.json").read_text(encoding="utf-8"))


def test_blocked_owner_items_can_never_be_pass():
    s = _schema()
    assert s["properties"]["blocked_owner"]["items"]["properties"]["status"]["const"] == "BLOCKED-OWNER"
    assert set(s["properties"]["sections"]["items"]["properties"]["status"]["enum"]) == {"PASS", "FAIL", "EVIDENCE_MISSING"}
    assert set(s["properties"]["verdict"]["enum"]) == {"READY_FOR_OWNER_EVIDENCE", "INCOMPLETE", "FAIL"}
    sec = s["properties"]["sections"]["items"]["required"]
    assert {"skipped", "deselected", "required_set", "l1_results", "l1_verdict", "ignored"} <= set(sec)
    assert "linux_cap_posix_pty_workspace" in s["properties"]["sections"]["items"]["properties"]["name"]["enum"]
    assert "expected_min_passed" not in s["properties"]["sections"]["items"]["properties"]   # R5-F3：計數門檻已移除
    assert {"cli_sha256", "daemon_sha256", "daemon_implementation", "contracts_revision", "cases_lock_sha256", "tool_revision", "platform"} <= set(s["properties"]["identity"]["required"])


@pytest.mark.parametrize("dirty_kind", ["untracked", "tracked"])
def test_formal_gate_rejects_dirty_worktree_before_writing_outputs(tmp_path, dirty_kind):
    fake_root = tmp_path / "repo"
    script = fake_root / "scripts/rust/slice21_gate.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/rust/slice21_gate.sh", script)
    subprocess.run(["git", "init", "-q", str(fake_root)], check=True)
    subprocess.run(["git", "-C", str(fake_root), "config", "user.name", "Gate Test"], check=True)
    subprocess.run(["git", "-C", str(fake_root), "config", "user.email", "gate-test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(fake_root), "add", "scripts/rust/slice21_gate.sh"], check=True)
    subprocess.run(["git", "-C", str(fake_root), "commit", "-qm", "test fixture"], check=True)
    if dirty_kind == "untracked":
        (fake_root / "untracked-output.json").write_text("dirty\n", encoding="utf-8")
    else:
        script.write_text(script.read_text(encoding="utf-8") + "\n# dirty\n", encoding="utf-8")

    output_root = tmp_path / "outputs"
    proc = subprocess.run(
        ["bash", str(script), "--run-id", "dirty", "--out", str(output_root)],
        cwd=fake_root, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "工作樹不乾淨" in proc.stderr
    assert "git status --porcelain --untracked-files=normal" in proc.stderr
    assert not output_root.exists(), "拒絕開跑前不得建立 run 輸出"


def test_gate_dry_run_lists_all_blocked_owner_items(tmp_path):
    r = subprocess.run(["bash", "scripts/rust/slice21_gate.sh", "--dry-run", "--run-id", "t", "--out", str(tmp_path)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    rep = json.loads((tmp_path / "t" / "report.json").read_text(encoding="utf-8"))
    ids = [b["id"] for b in rep["blocked_owner"]]
    assert ids == [f"BO-S21-{i:02d}" for i in range(1, 17)]
    assert all(b["status"] == "BLOCKED-OWNER" for b in rep["blocked_owner"])
    assert rep["verdict"] == "INCOMPLETE"                            # dry-run 永不 READY
    posix_pty = [s for s in rep["sections"] if s["name"] == "linux_cap_posix_pty_workspace"][0]
    assert posix_pty["required_set"] == "s21_gate:cap_posix_pty"
    assert posix_pty["status"] == "EVIDENCE_MISSING"
    parity_76_80 = [s for s in rep["sections"] if s["name"] == "py_parity_76_80"][0]
    assert parity_76_80["required_set"] == "s21_gate:py_parity_76_80"
    assert parity_76_80["status"] == "EVIDENCE_MISSING"
    com = [s for s in rep["sections"] if s["name"] == "windows_cap_windows_com"][0]
    assert com["status"] == "EVIDENCE_MISSING"                      # 未提供 SW_TEST_COM_PAIR
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    selector = lock["sets"]["s21_gate:windows_cap_windows_com"]["windows"]["selector"]["argv"]
    command = shlex.split(com["command"])
    start = command.index("scripts/rust/win-test.sh")
    assert command[start + 1:] == ["--caps", "windows_com", *selector]
    assert "SW_TEST_CONSOLE_PAIR" in command[0], command

    check_env = dict(os.environ)
    check_env.pop("SW_TEST_SERIALWRAP_NO_FAKES", None)
    missing = subprocess.run(["bash", "scripts/rust/gate_prereqs.sh", "--check-env"],
                              cwd=ROOT, env=check_env, capture_output=True, text=True, timeout=30)
    assert missing.returncode == 3, missing.stdout + missing.stderr
    assert "EVIDENCE_MISSING" in missing.stdout and "SW_TEST_SERIALWRAP_NO_FAKES" in missing.stdout
    prereqs = (ROOT / "scripts/rust/gate_prereqs.sh").read_text(encoding="utf-8")
    assert 'export SW_TEST_SERIALWRAP_NO_FAKES=' in prereqs
    assert 'target/s2-par/gpz/prereqs' in prereqs and 'no-fakes-target' in prereqs
    assert 'awk -v avg="$load"' in prereqs


def test_runbook_has_anchor_per_item():
    text = (ROOT / "docs/rust-refactor/slice21-owner-runbook.md").read_text(encoding="utf-8")
    for i in range(1, 17):
        assert f"### BO-S21-{i:02d}" in text


def test_slice24_prereq_failure_is_recorded_before_exit(tmp_path):
    """M6：gate_prereqs 非零須寫入 driver.tsv 並停止後續收集。"""
    script = (ROOT / "scripts/rust/slice24_exit.sh").read_text(encoding="utf-8")
    match = re.search(
        r'(?ms)^if source "\$ROOT/scripts/rust/gate_prereqs\.sh" --gate .*; then\n.*?^fi$', script
    )
    assert match, "slice24_exit.sh 必須明確檢查 gate_prereqs 的來源返回值"
    assert script.index('DRIVER_TSV="$RUN_DIR/driver.tsv"') < match.start()

    fake_root = tmp_path / "repo"
    fake_prereqs = fake_root / "scripts/rust/gate_prereqs.sh"
    fake_prereqs.parent.mkdir(parents=True)
    fake_prereqs.write_text("return 37\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    driver_tsv = tmp_path / "driver.tsv"
    marker = tmp_path / "continued"
    shell = (
        f'ROOT="{fake_root}"; RUN_DIR="{run_dir}"; DRIVER_TSV="{driver_tsv}"; : >"$DRIVER_TSV";\n'
        f'{match.group(0)}\n'
        f'touch "{marker}"\n'
    )
    proc = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 37, proc.stdout + proc.stderr
    assert driver_tsv.read_text(encoding="utf-8") == "gate_prereqs\t37\n"
    assert not marker.exists(), "前置失敗後不得繼續 gate 收集"

    # slice21 與 P7 的 `set -e` 會在 source 非零時立即終止，不存在 slice24 的無 -e 靜默續跑問題。
    for path in ("scripts/rust/slice21_gate.sh", "scripts/rust/p7_gate.sh"):
        other = (ROOT / path).read_text(encoding="utf-8")
        assert "set -euo pipefail" in other
        assert other.index('source "$ROOT/scripts/rust/gate_prereqs.sh" --gate') < len(other)


def test_bo_case_ids_cover_frozen_windows_cases():
    import yaml

    items = yaml.safe_load((ROOT / "tests/acceptance/slice21_bo.yaml").read_text(encoding="utf-8"))["items"]
    rep_ids = {c for b in items for c in b["case_ids"]}
    assert {"win-com-open-exclusive", "win-com-txrx-bridge", "win-com-cancel-close", "win-wal-av-rotate", "win-wal-av-unlink",
            "win-wal-av-reader-handle", "win-wal-f12-progress"} <= rep_ids


def _sys_admin_evidence(tmp_path, *, evidence_head=None, probe=True, cap_eff=None, selector=None, finished=True):
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    required = lock["sets"]["s21_gate:cap_sys_admin"]["linux"]
    current_head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
    evidence_head = evidence_head or current_head
    locked_selector = required["selector"]["argv"]
    nodeids = required["nodeids"]
    l1 = {
        "schema_version": 2, "format": "l1-results.v1", "runner": "cargo", "platform": "linux",
        "argv": locked_selector, "markexpr": "", "counts": {"all": {"passed": len(nodeids), "failed": 0, "skipped": 0}},
        "resources": {}, "uart_kind": "native", "not_collected_platform": [], "collected": nodeids,
        "deselected": [],
        "results": [{"nodeid": nodeid, "outcome": "passed", "skip_kind": None,
                     "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}
                    for nodeid in nodeids],
        "exit_code": 0, "finished": finished, "core_portable_platform_skips": [], "deployed_content": None,
        "provenance": {"schema": "cargo-l1-provenance.v1", "platform": "linux",
                       "git": {"head": evidence_head, "clean": True,
                               "end": {"head": evidence_head, "clean": True}}},
    }
    evidence = {
        "schema": "slice21-sys-admin-evidence.v1", "candidate_commit": evidence_head,
        "required_set": "s21_gate:cap_sys_admin", "platform": "linux", "selector": selector or locked_selector,
        "caps": ["sys_admin"], "caps_only": True, "cargo_exit_code": 0,
        "image": "ubuntu:24.04", "image_id": "sha256:" + "a" * 64,
        "container_os": {"id": "ubuntu", "version_id": "24.04"},
        "cargo_target_dir": "/tmp/slice21-sys-admin-target",
    }
    if probe:
        evidence["probe"] = {"euid": 0, "cap_eff": cap_eff or format(1 << 21, "x")}
    source = tmp_path / "sys-admin-results"
    source.mkdir()
    (source / "l1-results.json").write_text(json.dumps(l1), encoding="utf-8")
    (source / "sys-admin-evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    (source / "container.log").write_text("cap_sys_admin test completed\n", encoding="utf-8")
    return source, current_head, required


def _import_sys_admin(tmp_path, results, candidate_head):
    return subprocess.run(
        ["python3", "scripts/rust/slice21_sys_admin_evidence.py", "import-section",
         "--results", str(results), "--run-dir", str(tmp_path / "run"),
         "--candidate-commit", candidate_head],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )


def test_sys_admin_import_rejects_missing_probe(tmp_path):
    results, head, _ = _sys_admin_evidence(tmp_path, probe=False)
    proc = _import_sys_admin(tmp_path, results, head)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    section = json.loads((tmp_path / "run/sections/linux_cap_sys_admin.json").read_text(encoding="utf-8"))
    assert section["section_verdict"] == "EVIDENCE_MISSING"
    assert section["capability_probe"]["available"] is False
    assert section["l1_results"] is None
    assert any("探測" in reason for reason in section["reasons"])


def test_sys_admin_import_rejects_head_mismatch(tmp_path):
    results, head, _ = _sys_admin_evidence(tmp_path, evidence_head="0" * 40)
    proc = _import_sys_admin(tmp_path, results, head)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    section = json.loads((tmp_path / "run/sections/linux_cap_sys_admin.json").read_text(encoding="utf-8"))
    assert section["section_verdict"] == "EVIDENCE_MISSING"
    assert section["capability_probe"]["available"] is False
    assert section["l1_results"] is None
    assert any("HEAD" in reason or "head" in reason for reason in section["reasons"])


def test_sys_admin_import_rejects_cap_eff_without_sys_admin(tmp_path):
    results, head, _ = _sys_admin_evidence(tmp_path, cap_eff="1")
    proc = _import_sys_admin(tmp_path, results, head)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    section = json.loads((tmp_path / "run/sections/linux_cap_sys_admin.json").read_text(encoding="utf-8"))
    assert section["section_verdict"] == "EVIDENCE_MISSING"
    assert section["capability_probe"]["available"] is False
    assert section["l1_results"] is None


def test_sys_admin_import_rejects_selector_mismatch(tmp_path):
    results, head, _ = _sys_admin_evidence(tmp_path, selector=["--workspace", "--features", "test-fakes"])
    proc = _import_sys_admin(tmp_path, results, head)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    section = json.loads((tmp_path / "run/sections/linux_cap_sys_admin.json").read_text(encoding="utf-8"))
    assert section["section_verdict"] == "EVIDENCE_MISSING"
    assert section["capability_probe"]["available"] is False
    assert section["l1_results"] is None
    assert any("selector" in reason for reason in section["reasons"])


def test_sys_admin_import_rejects_incomplete_l1(tmp_path):
    results, head, _ = _sys_admin_evidence(tmp_path, finished=False)
    proc = _import_sys_admin(tmp_path, results, head)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    section = json.loads((tmp_path / "run/sections/linux_cap_sys_admin.json").read_text(encoding="utf-8"))
    assert section["section_verdict"] == "EVIDENCE_MISSING"
    assert section["capability_probe"]["available"] is False
    assert section["l1_results"] is None
    assert any("尚未完整結束" in reason for reason in section["reasons"])


def test_sys_admin_import_preserves_locked_l1_and_probe(tmp_path):
    results, head, required = _sys_admin_evidence(tmp_path)
    proc = _import_sys_admin(tmp_path, results, head)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    run_dir = tmp_path / "run"
    section = json.loads((run_dir / "sections/linux_cap_sys_admin.json").read_text(encoding="utf-8"))
    assert section["required_set"] == "s21_gate:cap_sys_admin"
    assert section["capability_probe"] == {
        "cap": "sys_admin", "available": True,
        "evidence": f"euid=0 CapEff={format(1 << 21, 'x')}",
    }
    assert section["run_euid"] == 0
    assert section["l1_results"] == "linux_cap_sys_admin/l1-results.json"
    l1_path = run_dir / section["l1_results"]
    l1 = json.loads(l1_path.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("l1_judge_sys_admin_test", ROOT / "tests/acceptance/l1_judge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.judge(l1, required["nodeids"], platform="linux", selector=required["selector"])
    assert result["verdict"] == "PASS", result
    assert (run_dir / "logs/linux_cap_sys_admin.log").read_text(encoding="utf-8") == "cap_sys_admin test completed\n"


def test_slice21_gate_routes_external_sys_admin_results_to_importer():
    text = (ROOT / "scripts/rust/slice21_gate.sh").read_text(encoding="utf-8")
    external = text.index('if [[ "$cap" == "sys_admin" && -n "${SW_GATE_SYS_ADMIN_RESULTS:-}" ]]')
    fallback = text.index('probe="$(cap_probe "$cap")"', external)
    assert "slice21_sys_admin_evidence.py" in text[external:fallback]
    assert "import-section" in text[external:fallback]
    assert "continue" in text[external:fallback]
    assert "SW_GATE_SYS_ADMIN_RESULTS" in text


def test_sys_admin_docker_driver_uses_only_disposable_root_container():
    script = ROOT / "scripts/rust/slice21_sys_admin_docker.sh"
    text = script.read_text(encoding="utf-8")
    assert "docker run" in text and "--rm" in text and "--cap-add SYS_ADMIN" in text
    assert "ubuntu:24.04" in text
    assert 'target=/workspace,readonly' in text
    assert "target=/host-rustup,readonly" in text
    assert "CARGO_TARGET_DIR=/tmp/" in text
    assert "apt-get update -qq >&2" in text
    assert "pkg-config libudev-dev >&2" in text
    assert "--device" not in text and "--privileged" not in text and "/dev/ttyUSB" not in text


def test_sys_admin_docker_driver_mounts_linked_worktree_git_metadata_readonly():
    """容器內的 L1 collector 必須能讀到 linked worktree 的 HEAD 與乾淨狀態。"""
    text = (ROOT / "scripts/rust/slice21_sys_admin_docker.sh").read_text(encoding="utf-8")
    assert 'GIT_DIR_HOST="$(git -C "$ROOT" rev-parse --absolute-git-dir)"' in text
    assert 'GIT_COMMON_DIR_HOST="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"' in text
    assert 'target=/host-gitdir,readonly' in text
    assert 'target=/host-git-common,readonly' in text
    assert '--env "GIT_DIR=/host-gitdir"' in text
    assert '--env "GIT_COMMON_DIR=/host-git-common"' in text
    assert '--env "GIT_WORK_TREE=/workspace"' in text
    assert '--env "GIT_OPTIONAL_LOCKS=0"' in text
