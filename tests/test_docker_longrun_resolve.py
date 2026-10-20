"""run_longrun_container 的 CLI／daemon 路徑解析（OD-15 docker 48 h 長跑）。

長跑在無監管的 serialwrap image（系統 pip 安裝）執行；systemd image 才有 /opt/serialwrap/.venv。
解析順序：SW_BUNDLE → venv（存在時）→ PATH；皆無則明確失敗，不得默默使用不存在的路徑。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

_P = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "docker" / "run_longrun_container.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_longrun_container", _P)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_env_wins(tmp_path):
    mod = _load()
    cli, daemon = mod.resolve_artifacts({"SW_BUNDLE": str(tmp_path)}, venv=tmp_path / "nope", which=lambda n: None)
    assert cli == tmp_path / "bin" / "serialwrap" and daemon == tmp_path / "bin" / "serialwrapd"


def test_venv_used_when_present(tmp_path):
    mod = _load()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "serialwrap").write_text("")
    (tmp_path / "bin" / "serialwrapd").write_text("")
    cli, daemon = mod.resolve_artifacts({}, venv=tmp_path, which=lambda n: "/usr/local/bin/" + n)
    assert cli == tmp_path / "bin" / "serialwrap" and daemon == tmp_path / "bin" / "serialwrapd"


def test_path_fallback_when_no_venv(tmp_path):
    mod = _load()
    cli, daemon = mod.resolve_artifacts({}, venv=tmp_path / "nope", which=lambda n: "/usr/local/bin/" + n)
    assert cli == pathlib.Path("/usr/local/bin/serialwrap") and daemon == pathlib.Path("/usr/local/bin/serialwrapd")


def test_missing_everywhere_fails_loudly(tmp_path):
    mod = _load()
    with pytest.raises(FileNotFoundError):
        mod.resolve_artifacts({}, venv=tmp_path / "nope", which=lambda n: None)


def test_serialwrap_compose_user_has_dialout_group():
    """Compose 以主機 UID 執行時，需將 dialout 加入該行程的補充群組。"""
    import yaml

    compose = pathlib.Path(__file__).resolve().parents[1] / "tools" / "docker" / "compose.l15.yaml"
    services = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]
    assert "dialout" in services["serialwrap"].get("group_add", [])
    dockerfile = compose.parent / "serialwrap.Dockerfile"
    assert "usermod -aG dialout root" not in dockerfile.read_text(encoding="utf-8")


def test_serialwrap_image_installs_git_for_realhw_metadata():
    dockerfile = pathlib.Path(__file__).resolve().parents[1] / "tools" / "docker" / "serialwrap.Dockerfile"
    install = next(line for line in dockerfile.read_text(encoding="utf-8").splitlines()
                   if "apt-get install -y --no-install-recommends" in line)
    assert "git" in install.split()


def test_capture_doctor_saves_json_from_isolated_environment(tmp_path, monkeypatch):
    mod = _load()
    env = {
        "SERIALWRAP_CLI_EXE": "/usr/local/bin/serialwrap",
        "SERIALWRAP_STATE_DIR": "/tmp/sw-t17/state",
    }
    expected = {"ok": False, "checks": [{"check": "dialout", "ok": False}]}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(expected), "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    output = tmp_path / "doctor.json"

    assert mod.capture_doctor(env, output) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == expected
    assert 'capture_doctor(env, OUT / "doctor.json")' in _P.read_text(encoding="utf-8")
    assert calls == [(
        ["/usr/local/bin/serialwrap", "doctor"],
        {"env": env, "capture_output": True, "text": True, "timeout": 30},
    )]


def test_observer_sampling_interval_includes_sample_work(tmp_path, monkeypatch):
    """取樣命令耗時應計入週期，避免長跑樣本數隨 RPC 延遲持續流失。"""
    from realhw.acceptance import observer

    now = [0.0]
    waits = []

    class FakeStop:
        def is_set(self):
            return len(waits) >= 2

        def wait(self, timeout):
            waits.append(timeout)
            now[0] += timeout
            return False

    class FakeSw:
        def run(self, *args):
            if args == ("session", "list"):
                return {"sessions": []}
            raise AssertionError(args)

    def fake_sample(_sw, _boards, *, procscan):
        started = now[0]
        now[0] += 2.0
        return {"t_mono": started, "daemon": {}, "sessions": {}, "procscan": {}}

    monkeypatch.setattr(observer, "_platform_name", lambda: "linux")
    monkeypatch.setattr(observer, "sample", fake_sample)
    monkeypatch.setattr(observer.time, "monotonic", lambda: now[0])

    assert observer.run_observer(
        FakeSw(), [], interval_s=10.0, out=tmp_path / "observer.ndjson",
        stop=FakeStop(), duration_s=None,
    ) == 0
    assert waits == [8.0, 8.0]
