"""P2-T14 red：Windows 能力式 SKIP、regression 報告 deployed_content、U-Boot frontend、throwaway 隔離 env。

對應 docs/rust-refactor/plans/03-stage0-test-infra.md Task 14 T14-1。
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))

pytestmark = [pytest.mark.core_portable]

# 本檔以字串 "tmux" 比對 requires 的舊能力名（只做斷言，從不啟動 tmux）。
RESOURCE_AUDIT_WAIVERS = {"interactive_desktop": "只比對 requires 中的 tmux 能力字串，從不啟動 tmux"}


def _make_install(root: Path) -> Path:
    bindir = root / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / "serialwrap"
    exe.write_bytes(b"cli")
    exe.chmod(0o755)
    (bindir / "version.dll").write_bytes(b"dll")
    manifest = {"schema_version": "release-manifest.v1",
                "components": {"cli": {"sha256": hashlib.sha256(b"cli").hexdigest()}}}
    (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


@pytest.mark.memory_only
def test_regression_report_deployed_content(tmp_path):
    from realhw import deployed_content as dc
    from serialwrap_regression.reporter import RegressionReporter

    inst = _make_install(tmp_path / "v1")
    exe = inst / "bin" / "serialwrap"
    start = dc.measure(exe)

    def _plugin(start_value, report_dir):
        run_meta = {"version": "serialwrap 0.3.2", "git": "", "tiers": "regression",
                    "started_at": "260101-000000", "preflight_notes": [], "boards": [],
                    "allow_destructive": False}
        if start_value is not None:
            run_meta["deployed_content_start"] = start_value
        return SimpleNamespace(
            run_results=[],
            run_meta=run_meta,
            ctx=SimpleNamespace(report_dir=report_dir, cfg={"serialwrap_exe": str(exe)}),
            _load_cfg=lambda: {"serialwrap_exe": str(exe)},
        )

    # (1) start 取自 run_meta["deployed_content_start"]，end 於 build_reports 內量測
    out1 = tmp_path / "out1"
    RegressionReporter(plugin=_plugin(start, tmp_path / "rep1")).build_reports(
        SimpleNamespace(artifacts={}, artifact_dir=out1, run_id="r1", fw_ver="", cases=[]))
    dcn = json.loads((out1 / "report.json").read_text(encoding="utf-8"))["meta"]["deployed_content"]
    assert dcn["start"] == start
    assert dcn["end"]["bundle_tree_sha256"] == dc.tree_digests(inst)[0]
    assert "daemon_live" in dcn

    # (2) run_meta 無 start → start 標 not_measured，三個 digest 為 None（鍵仍存在）
    out2 = tmp_path / "out2"
    RegressionReporter(plugin=_plugin(None, tmp_path / "rep2")).build_reports(
        SimpleNamespace(artifacts={}, artifact_dir=out2, run_id="r2", fw_ver="", cases=[]))
    dcn2 = json.loads((out2 / "report.json").read_text(encoding="utf-8"))["meta"]["deployed_content"]
    assert dcn2["start"]["error"] == "not_measured"
    for key in ("bundle_tree_sha256", "runtime_tree_sha256", "entry_dir_sha256"):
        assert dcn2["start"][key] is None


@pytest.mark.memory_only
def test_platform_missing_caps_windows():
    from realhw import preflight

    caps = preflight.platform_missing_caps(os_name="nt", frontend_ok=True)
    assert set(caps) == {"tmux", "posix_throwaway_daemon", "usbipd", "supervisor_runtime"}
    for key, value in caps.items():
        assert value == f"cap_missing:{key}"

    caps_no_frontend = preflight.platform_missing_caps(os_name="nt", frontend_ok=False)
    assert caps_no_frontend["human_frontend"] == "cap_missing:human_frontend"


@pytest.mark.memory_only
def test_regression_no_os_name_hard_skip():
    path = REPO_ROOT / "regression" / "serialwrap_regression" / "plugin.py"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    os_name_refs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "name"
        and isinstance(node.value, ast.Name) and node.value.id == "os"
    ]
    assert os_name_refs == []
    assert "windows_case_unsupported" not in src


@pytest.mark.memory_only
def test_regression_windows_skip_is_capability_based():
    from realhw import preflight
    from serialwrap_regression import core

    cfg = {"boards": [{"com": "COM0", "alias": "dut", "serial": "S0", "platform": "prpl"}]}
    cases = {c["id"]: c for c in core.build_case_dicts(core.load_registry(), cfg)}
    caps = preflight.platform_missing_caps(os_name="nt", frontend_ok=True)

    f5 = cases["f5-raw-ownership-survives-agent-rounds"]["metadata"]
    assert core.runtime_skip(f5, caps, None, allow_destructive=False) is None

    f8 = cases["f8-second-daemon-detected"]["metadata"]
    skip = core.runtime_skip(f8, caps, None, allow_destructive=False)
    assert skip is not None and skip[0] == "cap_missing:posix_throwaway_daemon"

    f3 = cases["f3-fail-error-code"]["metadata"]
    assert core.runtime_skip(f3, caps, None, allow_destructive=False) is None


@pytest.mark.memory_only
def test_regression_case_requires_updated():
    from serialwrap_regression import core

    reg = {case.id: case for case in core.load_registry()}
    human_cases = [
        "f5-raw-ownership-survives-agent-rounds",
        "f5-deferred-bytes-flushed",
        "f5-console-peer-gone-recycled",
        "f5-second-console-linebuffer",
        "f9-uboot-readonly-and-console-kept",
        "f9-spontaneous-reboot-agent-gated",
    ]
    for cid in human_cases:
        assert tuple(reg[cid].requires) == ("human_frontend",), cid
    for cid in ("f10-unresolved-creds-terminal", "f10-creds-fixed-then-ready",
                "f8-second-daemon-detected"):
        assert "posix_throwaway_daemon" in reg[cid].requires, cid


@pytest.mark.memory_only
def test_throwaway_env_isolates_all_dirs(tmp_path):
    from serialwrap_regression import guards

    work = tmp_path / "ta"
    run = tmp_path / "run"
    by_id = work / "by-id"
    env = guards.throwaway_env(work, by_id, run)

    # 對齊 tests/conftest.py 的 9 個目錄變數，外加 PROFILE_DIR
    isolated = {
        "SERIALWRAP_STATE_DIR", "SERIALWRAP_RUN_DIR", "SERIALWRAP_WAL_DIR",
        "SERIALWRAP_CONFIG_DIR", "SERIALWRAP_LOG_DIR", "SERIALWRAP_EVENTS_DIR",
        "SERIALWRAP_EVENTS_RUNTIME_DIR", "SERIALWRAP_BY_ID_DIR", "SERIALWRAP_BY_PATH_DIR",
        "SERIALWRAP_PROFILE_DIR",
    }
    assert isolated <= set(env)
    for key in isolated:
        resolved = Path(env[key]).resolve()
        assert resolved.is_relative_to(work.resolve()) or resolved.is_relative_to(run.resolve()), key


@pytest.mark.memory_only
def test_uboot_console_uses_frontend():
    from serialwrap_regression import guards

    class _Frontend:
        kind = "fake"

        def __init__(self) -> None:
            self.sent: list[str] = []

        def send_line(self, text: str) -> None:
            self.sent.append(text)

        def capture(self) -> str:
            return "=> printenv\r\n=> "

    fe = _Frontend()
    ctx = SimpleNamespace(tmux=None, cfg={}, sw=None)
    console = guards.UBootConsole(ctx, "COM0", fe)

    console.readonly_cmd("printenv", settle_s=0)
    assert fe.sent == ["printenv"]

    with pytest.raises(guards.UBootGuardError):
        console.readonly_cmd("saveenv", settle_s=0)
    assert fe.sent == ["printenv"]  # 被拒的命令不得送出 UART


@pytest.mark.memory_only
def test_ported_cases_require_human_frontend():
    import realhw.cases  # noqa: F401  觸發 register()
    from realhw import harness

    reg = {case.id: case for case in harness.REGISTRY}
    ported = [
        "p0-console-raw",
        "p1-con-fanout", "p1-con-defer", "p1-con-busy", "p1-con-softpreempt",
        "p1-con-liveness", "p1-con-orphan", "p1-con-second",
        "p1-wal-reset", "p1-wal-fullrun", "p1-rst-reboot", "lr-mixed",
    ]
    for cid in ported:
        req = tuple(reg[cid].requires)
        assert "human_frontend" in req and "tmux" not in req, (cid, req)
    assert "tmux" in reg["p0-blog-clean"].requires
    assert "tmux" in reg["p1-ho-cycle"].requires


@pytest.mark.memory_only
def test_realhw_requires_reflect_dependencies():
    import realhw.cases  # noqa: F401
    from realhw import harness, preflight

    reg = {case.id: case for case in harness.REGISTRY}
    expected = {
        "p1-rst-daemon": {"supervisor_runtime"},
        "p1-ho-cycle": {"tmux"},
        "p1-ho-persist": {"supervisor_runtime"},
        "p1-hp-cycle": {"usbipd"},
        "p1-hp-reorder": {"usbipd", "supervisor_runtime"},
    }
    caps = preflight.platform_missing_caps(os_name="nt", frontend_ok=True)
    for cid, needed in expected.items():
        req = set(reg[cid].requires)
        assert needed <= req, (cid, sorted(req))
        lacking = [r for r in reg[cid].requires if r in caps]
        assert lacking, cid  # Windows 上此案必須被能力式 SKIP，不得實際執行
        assert caps[lacking[0]].startswith("cap_missing:"), cid
