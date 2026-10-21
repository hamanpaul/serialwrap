"""P9-T5：乾淨 DUT 狀態與實例隔離（tools/stage3/dut_state.py）的測試（TDD）。

依 docs/rust-refactor/plans/10-stage3-canary.md Task 5 Step 1 撰寫（計畫所列 10 項測試名稱與斷言保持不變）；
另補充 l2／l3 乾淨狀態 fail-closed、模板渲染、profile 凍結與 l1.5 隔離等行為（標示於各測試的 docstring）。
POSIX 專屬行為（/tmp 暫存 RUN_DIR 與 run-dir.txt 記錄）見 test_stage3_dut_state_posix.py。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import stage3_fakes as fakes
from tools.stage3 import ab_manifest, dut_state, jsonio
from tools.stage3.reason_codes import HarnessError

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _load(tmp_path: Path, **overrides):
    return ab_manifest.load_manifest(fakes.write_ab_manifest(tmp_path, **overrides))


def _sessions(*rows: tuple[str, str]) -> str:
    return json.dumps({"ok": True, "sessions": [{"com": com, "state": state} for com, state in rows]})


def _session_result(*rows: tuple[str, str]) -> dict:
    return {"rc": 0, "stdout": _sessions(*rows)}


def _bench_env(tmp_path: Path) -> dict[str, str]:
    """l2／l3 不隔離目錄：以空的 bench state 目錄取代主機真實的 state.json，使測試與 $HOME 狀態無關。"""
    state_dir = tmp_path / "bench-state"
    state_dir.mkdir(exist_ok=True)
    return {"SERIALWRAP_STATE_DIR": str(state_dir)}


@pytest.fixture
def instances():
    """收集測試中建立的實例，結束時清理 POSIX 的 /tmp RUN_DIR（cleanup 須可重入）。"""
    items: list[dut_state.Instance] = []
    yield items
    for inst in items:
        dut_state.cleanup_instance(inst)


def test_b_instance_never_reuses_a_root(tmp_path, instances):
    m = _load(tmp_path)
    a = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out",
                                 base_env={"SERIALWRAP_ENDPOINT": "tcp://x"})
    instances.append(a)
    b = dut_state.fresh_instance(m, m.sides[1], round=1, base_dir=tmp_path / "out",
                                 base_env={"SERIALWRAP_ENDPOINT": "tcp://x"})
    instances.append(b)
    assert a.root != b.root
    for k in dut_state.ISOLATED_DIR_KEYS:
        assert a.env[k] != b.env[k]
    assert "SERIALWRAP_ENDPOINT" not in a.env   # base_env 帶入了此值，必須被移除（非空洞斷言）
    assert a.env["SERIALWRAP_DAEMON_EXE"] == str(m.sides[0].daemon_exe)
    assert b.env["SERIALWRAP_DAEMON_EXE"] == str(m.sides[1].daemon_exe)
    assert a.env["SERIALWRAP_CLI_EXE"] == str(m.sides[0].cli_exe)


def test_reused_root_rejected(tmp_path, instances):
    m = _load(tmp_path)
    instances.append(dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={}))
    with pytest.raises(HarnessError) as ei:
        dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={})
    assert ei.value.code == "HARNESS_DUT_NOT_CLEAN"


def test_l15_clean_passes(tmp_path, instances):
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={})
    instances.append(inst)
    assert dut_state.verify_clean(m, inst, m.sides[0]) == []


def test_leftover_state_json_detected(tmp_path, instances):
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={})
    instances.append(inst)
    (inst.root / "state" / "state.json").write_text("{}", encoding="utf-8")
    assert dut_state.verify_clean(m, inst, m.sides[0]) == ["state.json present"]


def test_l15_leftover_wal_and_run_detected(tmp_path, instances):
    """補充：wal 殘留檔與實際 RUN_DIR 的殘留 entry（dev/ 除外）皆須被偵測。"""
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={})
    instances.append(inst)
    (inst.root / "wal" / "raw.wal.ndjson").write_text("{}\n", encoding="utf-8")
    (Path(inst.env["SERIALWRAP_RUN_DIR"]) / "leftover.sock").write_text("", encoding="utf-8")
    violations = dut_state.verify_clean(m, inst, m.sides[0])
    assert "wal not empty" in violations
    assert "run dir not empty" in violations


def test_windows_allowlist_env(tmp_path, instances):
    m = _load(
        tmp_path,
        platform="windows",
        **{
            "dut.transport": "com0com-pair",
            "dut.com_pair": {"daemon_side": "COM20", "bridge_side": "COM21"},
            "test_device_allowlist": ["COM20"],
        },
    )
    inst = dut_state.fresh_instance(
        m, m.sides[0], round=1, base_dir=tmp_path / "out",
        base_env={"SERIALWRAP_TEST_DEVICE_ALLOWLIST": "COM99"},
    )
    instances.append(inst)
    assert inst.env["SERIALWRAP_TEST_DEVICE_ALLOWLIST"] == "COM20"


def test_hardware_fingerprint_mismatch(tmp_path):
    m = _load(
        tmp_path,
        layer="l2",
        **{
            "dut.boards": ["COM0"],
            "dut.fingerprint_argv": ["{cli_exe}", "rig", "fingerprint"],
            "dut.expected_fingerprint": {"uptime_s": "[0-9]{1,3}"},
        },
    )
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
    runner = fakes.RecordingRunner({
        "cli:rig fingerprint": [{"rc": 0, "stdout": json.dumps({"uptime_s": "9999"})}],
        "cli:session list": [_session_result(("COM0", "READY"))],
    })
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == ["fingerprint mismatch: uptime_s"]


def test_hardware_board_not_ready(tmp_path):
    m = _load(tmp_path, layer="l2", **{"dut.boards": ["COM0", "COM1"]})
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
    runner = fakes.RecordingRunner({
        "cli:session list": [_session_result(("COM0", "READY"), ("COM1", "ATTACHED"))],
    })
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == ["board not READY: COM1"]


def test_hardware_reset_failure(tmp_path):
    m = _load(tmp_path, layer="l2", **{"dut.boards": ["COM0"], "dut.reset_argv": ["{cli_exe}", "rig", "reset"]})
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
    runner = fakes.RecordingRunner({"cli:rig reset": [{"rc": 3}]})
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == ["dut reset failed rc=3"]


def test_hardware_clean_passes(tmp_path):
    """補充：reset、fingerprint 與 session list 全部符合時回傳空清單，且三個命令都帶本側 env。"""
    m = _load(
        tmp_path,
        layer="l2",
        **{
            "dut.boards": ["COM0"],
            "dut.reset_argv": ["{cli_exe}", "rig", "reset"],
            "dut.fingerprint_argv": ["{cli_exe}", "rig", "fingerprint"],
            "dut.expected_fingerprint": {"uptime_s": "[0-9]{1,3}"},
        },
    )
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
    runner = fakes.RecordingRunner({
        "cli:rig reset": [{"rc": 0}],
        "cli:rig fingerprint": [{"rc": 0, "stdout": json.dumps({"uptime_s": "42"})}],
        "cli:session list": [_session_result(("COM0", "READY"))],
    })
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == []
    assert [c["sub"] for c in runner.calls] == ["rig reset", "rig fingerprint", "session list"]
    assert all(c["side"] == "A" for c in runner.calls)


def test_hardware_state_polluted(tmp_path):
    """補充：bench state.json 的 bindings 含 /tmp/ 哨兵時判為污染（與 realhw `_state_polluted` 同規則）。"""
    m = _load(tmp_path, layer="l2", **{"dut.boards": ["COM0"]})
    state_dir = tmp_path / "bench-state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bindings": {"COM0": {"device": "/tmp/fake-ttyUSB"}}}), encoding="utf-8")
    inst = dut_state.fresh_instance(
        m, m.sides[0], round=1, base_dir=tmp_path / "out",
        base_env={"SERIALWRAP_STATE_DIR": str(state_dir)},
    )
    runner = fakes.RecordingRunner({"cli:session list": [_session_result(("COM0", "READY"))]})
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == ["state polluted"]


def test_side_env_marks_side():
    assert dut_state.side_env({}, side="B", round=2) == {"SW_STAGE3_SIDE": "B", "SW_STAGE3_ROUND": "2"}


def test_artifact_env_selects_side_artifacts(tmp_path):
    m = _load(tmp_path)
    assert dut_state.artifact_env(m, m.sides[1]) == {
        "SERIALWRAP_CLI_EXE": str(m.sides[1].cli_exe),
        "SERIALWRAP_DAEMON_EXE": str(m.sides[1].daemon_exe),
        "SERIALWRAP_AB_LABEL": f"{m.run_id}-B",
    }


def test_l15_isolated_env_points_under_root(tmp_path, instances):
    """補充：l1.5 的 9 個隔離目錄與 4 個顯式路徑皆落在實例 root 下；ENDPOINT 與外層 allowlist 不得殘留。"""
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(
        m, m.sides[0], round=1, base_dir=tmp_path / "out",
        base_env={"SERIALWRAP_ENDPOINT": "tcp://x", "SERIALWRAP_TEST_DEVICE_ALLOWLIST": "COM9"},
    )
    instances.append(inst)
    root = str(inst.root)
    for key in dut_state.ISOLATED_DIR_KEYS:
        if key == "SERIALWRAP_RUN_DIR":
            continue
        assert inst.env[key].startswith(root + os.sep), key
    assert inst.env["SERIALWRAP_PROFILE_DIR"] == str(inst.root / "profiles")
    assert inst.env["SERIALWRAP_DATA_DIR"] == str(inst.root / "data")
    assert inst.env["SERIALWRAP_TTYMCU_PATH"] == str(inst.root / "run" / "dev" / "ttyMCU")
    assert inst.env["SERIALWRAP_EVENTS_LOG_PATH"] == str(inst.root / "events" / "events.ndjson")
    assert "SERIALWRAP_ENDPOINT" not in inst.env
    assert "SERIALWRAP_TEST_DEVICE_ALLOWLIST" not in inst.env   # linux 非 windows：不重設、外層值已移除
    assert Path(inst.env["SERIALWRAP_RUN_DIR"]).is_dir()


def test_missing_profile_file_is_drift(tmp_path):
    """補充：凍結的 profile 檔遺失時拒絕建立實例，且已建立的 POSIX 暫存 RUN_DIR 須被清除。"""
    m = _load(tmp_path)
    (tmp_path / "profiles" / "default.yaml").unlink()
    base_dir = tmp_path / "out"
    with pytest.raises(HarnessError) as ei:
        dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=base_dir, base_env={})
    assert ei.value.code == "HARNESS_MANIFEST_DRIFT"
    if os.name != "nt":
        record = base_dir / "r1-A" / "run-dir.txt"
        assert not Path(record.read_text(encoding="utf-8").strip()).exists()


# ---------------------------------------------------------------------------
# 補充：審查後加強的 fail-closed、模板、凍結與隔離斷言
# ---------------------------------------------------------------------------


def test_profile_sha_mismatch_is_drift(tmp_path):
    """補充：profile 內容與凍結 sha256 不符 → HARNESS_MANIFEST_DRIFT（拒絕建立實例）。"""
    m = _load(tmp_path)
    (tmp_path / "profiles" / "default.yaml").write_text("changed: true\n", encoding="utf-8")
    with pytest.raises(HarnessError) as ei:
        dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={})
    assert ei.value.code == "HARNESS_MANIFEST_DRIFT"


def test_duplicate_profile_name_is_rejected(tmp_path):
    """補充：兩個不同目錄下同名的 profile 會互相覆寫 → HARNESS_MANIFEST_INVALID。"""
    manifest = fakes.write_ab_manifest(tmp_path)   # 先寫入假 repo（含 profiles/default.yaml）
    src = tmp_path / "profiles" / "default.yaml"
    other = tmp_path / "other" / "default.yaml"
    other.parent.mkdir()
    shutil.copyfile(src, other)
    digest = jsonio.sha256_file(src)
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    doc["profile_files"] = [
        {"path": "profiles/default.yaml", "sha256": digest},
        {"path": "other/default.yaml", "sha256": digest},
    ]
    manifest.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(HarnessError) as ei:
        m = ab_manifest.load_manifest(manifest)
        dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={})
    assert ei.value.code == "HARNESS_MANIFEST_INVALID"


def test_default_base_env_comes_from_process_env(tmp_path, instances, monkeypatch):
    """補充：base_env 為 None 時取行程環境；POPPED_KEYS 仍被移除，其他變數保留。"""
    monkeypatch.setenv("SERIALWRAP_ENDPOINT", "tcp://host")
    monkeypatch.setenv("SERIALWRAP_TEST_DEVICE_ALLOWLIST", "COM9")
    monkeypatch.setenv("SW_T5_PROBE", "kept")
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out")
    instances.append(inst)
    assert "SERIALWRAP_ENDPOINT" not in inst.env
    assert "SERIALWRAP_TEST_DEVICE_ALLOWLIST" not in inst.env
    assert inst.env["SW_T5_PROBE"] == "kept"
    assert inst.env["SW_STAGE3_SIDE"] == "A"


def test_l15_isolation_drops_inherited_overrides(tmp_path, instances):
    """補充：外層的 config／release manifest／測試鉤子覆寫不得穿透 l1.5 隔離；system config 指向實例內不存在的路徑。"""
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(
        m, m.sides[0], round=1, base_dir=tmp_path / "out",
        base_env={
            "SERIALWRAP_CONFIG": "/host/config.yaml",
            "SERIALWRAP_RELEASE_MANIFEST": "/host/release-manifest.json",
            "SERIALWRAP_TEST_KILL_AFTER_SAVE": "1",
            "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE": "1",
            "SERIALWRAP_TEST_SYSTEM_CONFIG_PATH": "/etc/serialwrap/config.yaml",
        },
    )
    instances.append(inst)
    for key in ("SERIALWRAP_CONFIG", "SERIALWRAP_RELEASE_MANIFEST",
                "SERIALWRAP_TEST_KILL_AFTER_SAVE", "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE"):
        assert key not in inst.env, key
    assert inst.env["SERIALWRAP_TEST_SYSTEM_CONFIG_PATH"] == str(inst.root / "config" / "system-config.yaml")


def test_repr_hides_instance_environment(tmp_path, instances):
    """補充：Instance 的 repr 不輸出 env（外層環境可能帶有認證類變數）。"""
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out",
                                    base_env={"SW_T5_SECRET_PROBE": "hunter2"})
    instances.append(inst)
    assert inst.env["SW_T5_SECRET_PROBE"] == "hunter2"
    assert "hunter2" not in repr(inst)


def test_expected_fingerprint_without_command_is_mismatch(tmp_path):
    """補充：宣告了 expected_fingerprint 卻未設 fingerprint_argv → 每鍵皆 mismatch（不得視為乾淨）。"""
    m = _load(tmp_path, layer="l2", **{"dut.boards": ["COM0"], "dut.expected_fingerprint": {"uptime_s": "[0-9]{1,3}"}})
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
    runner = fakes.RecordingRunner({"cli:session list": [_session_result(("COM0", "READY"))]})
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == ["fingerprint mismatch: uptime_s"]


@pytest.mark.parametrize("fingerprint_result", [
    {"rc": 3, "stdout": ""},
    {"rc": 0, "stdout": "not json"},
    {"rc": 0, "stdout": "[1, 2]"},
    {"rc": -1, "stdout": "", "timed_out": True},
], ids=["rc-nonzero", "non-json", "json-not-object", "timeout"])
def test_fingerprint_command_failure_is_not_clean(tmp_path, fingerprint_result):
    """補充：指紋命令 rc≠0、非 JSON、非 object 或逾時，且未宣告 expected 時回報 unavailable（不得判為乾淨）。"""
    m = _load(tmp_path, layer="l2", **{"dut.boards": ["COM0"],
                                       "dut.fingerprint_argv": ["{cli_exe}", "rig", "fingerprint"]})
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
    runner = fakes.RecordingRunner({
        "cli:rig fingerprint": [fingerprint_result],
        "cli:session list": [_session_result(("COM0", "READY"))],
    })
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == ["fingerprint unavailable"]


def test_fingerprint_failure_with_expected_reports_each_key(tmp_path):
    """補充：指紋命令 rc≠0 且有 expected 時，逐鍵回報 mismatch。"""
    m = _load(tmp_path, layer="l2", **{
        "dut.boards": ["COM0"],
        "dut.fingerprint_argv": ["{cli_exe}", "rig", "fingerprint"],
        "dut.expected_fingerprint": {"uptime_s": "[0-9]{1,3}"},
    })
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
    runner = fakes.RecordingRunner({
        "cli:rig fingerprint": [{"rc": 3, "stdout": ""}],
        "cli:session list": [_session_result(("COM0", "READY"))],
    })
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == ["fingerprint mismatch: uptime_s"]


@pytest.mark.parametrize("session_result", [
    {"rc": 2, "stdout": ""},
    {"rc": -1, "stdout": "", "timed_out": True},
], ids=["rc-nonzero", "timeout"])
def test_session_list_failure_is_not_clean(tmp_path, session_result):
    """補充：session list rc≠0 或逾時時，每塊板皆視為非 READY（fail closed）。"""
    m = _load(tmp_path, layer="l2", **{"dut.boards": ["COM0"]})
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
    runner = fakes.RecordingRunner({"cli:session list": [session_result]})
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == ["board not READY: COM0"]


def test_l3_session_list_passes_endpoint(tmp_path):
    """補充：l3 的 session list 以 `--endpoint <manifest endpoint>` 指定 daemon，且先於子命令。"""
    m = _load(tmp_path, layer="l3")
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
    runner = fakes.RecordingRunner({
        "cli:session list": [_session_result(("TEST0", "READY"), ("TEST1", "READY"))],
    })
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == []
    assert m.endpoint
    assert runner.calls[-1]["argv"][1:] == ["--endpoint", m.endpoint, "session", "list"]


def test_invalid_fingerprint_pattern_rejected_before_reset(tmp_path):
    """補充：壞的 expected_fingerprint pattern 須在任何 DUT 命令（含 reset）執行前被拒絕。"""
    runner = fakes.RecordingRunner()
    with pytest.raises(HarnessError) as ei:
        m = _load(tmp_path, layer="l2", **{
            "dut.boards": ["COM0"],
            "dut.reset_argv": ["{cli_exe}", "rig", "reset"],
            "dut.expected_fingerprint": {"uptime_s": "[0-9"},
        })
        inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
        dut_state.verify_clean(m, inst, m.sides[0], run=runner)
    assert ei.value.code == "HARNESS_MANIFEST_INVALID"
    assert runner.calls == []


def test_templates_render_repo_root_and_out_dir(tmp_path):
    """補充：{repo_root}、{out_dir} 為 TEMPLATE_KEYS 的合法鍵，須可渲染（不得拋 HARNESS_MANIFEST_INVALID）。"""
    m = _load(tmp_path, layer="l2", **{"dut.boards": ["COM0"],
                                       "dut.reset_argv": ["{repo_root}/r.sh", "{out_dir}"]})
    base_dir = tmp_path / "out"
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=base_dir, base_env=_bench_env(tmp_path))
    runner = fakes.RecordingRunner({"cli:session list": [_session_result(("COM0", "READY"))]})
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner) == []
    assert runner.calls[0]["argv"] == [f"{tmp_path.resolve()}/r.sh", str(base_dir)]


def test_explicit_repo_root_overrides_manifest_directory(tmp_path):
    """補充：verify_clean 的 repo_root 關鍵字覆寫預設的 manifest 所在目錄。"""
    m = _load(tmp_path, layer="l2", **{"dut.boards": ["COM0"], "dut.reset_argv": ["{repo_root}/r.sh"]})
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env=_bench_env(tmp_path))
    alt = tmp_path / "alt-repo"
    alt.mkdir()
    runner = fakes.RecordingRunner({"cli:session list": [_session_result(("COM0", "READY"))]})
    assert dut_state.verify_clean(m, inst, m.sides[0], run=runner, repo_root=alt) == []
    assert runner.calls[0]["argv"] == [f"{alt.resolve()}/r.sh"]
