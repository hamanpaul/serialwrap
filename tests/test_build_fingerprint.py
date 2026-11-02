"""程式碼指紋（build fingerprint）：看得出 daemon 跑的是哪份程式碼，升級後讓 daemon 換新碼。

背景：版本只來自 ``VERSION``（release／分支 build／main 都是同一個字串），而
``pipx install --force`` 加 ``serialwrap setup --user`` 之後，已在跑的 systemd 服務不會被
重啟——daemon 靜默沿用舊碼，從外面完全看不出來。本檔涵蓋：

A. 指紋本身：穩定、檔案一改就變、只算 ``.py``、凍結包算不出時回 ``None``。
B. daemon 端：``daemon status``／每個 RPC 回應帶 ``code_fingerprint``（``code_path`` 只在 status）。
C. CLI 端：``--version`` 附指紋且為惰性；client↔daemon 指紋不一致時 stderr 一行、stdout JSON 不變。
D. ``serialwrap setup``：服務 active 且指紋不符才 restart；相同不動；各種不該動的情境。
E. ``reconcile(restart_daemon=...)`` 的指令序列（含 systemd-system 與 sudo pending）。
F. ``doctor`` 的 ``daemon_code_current`` 檢查。
"""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml

from sw_core import cli
from sw_core import version as version_module
from sw_core.service import SerialwrapService
from sw_core.setup_cmd import daemon_code_assessment, reconcile, service_is_active
from sw_core.sysenv import FakeEffects

_REPO_VERSION = (Path(__file__).parent.parent / "VERSION").read_text(encoding="utf-8").strip()
_FAKE_MULTI_OPEN = {"multi_open": False, "daemons": [], "holders": {}, "holders_status": "ok"}


# ───────────────────────────── A. 指紋本身 ─────────────────────────────


def _make_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class TestFingerprintFunction:
    def test_stable_across_calls_and_hex_shaped(self, tmp_path):
        root = _make_tree(tmp_path, {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"})
        first = version_module.compute_code_fingerprint(str(root))
        assert first == version_module.compute_code_fingerprint(str(root))
        assert re.fullmatch(r"[0-9a-f]{%d}" % version_module.FINGERPRINT_HEX_LEN, first)

    def test_changes_when_a_file_changes(self, tmp_path):
        root = _make_tree(tmp_path, {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"})
        before = version_module.compute_code_fingerprint(str(root))
        (root / "pkg" / "b.py").write_text("y = 3\n", encoding="utf-8")
        assert version_module.compute_code_fingerprint(str(root)) != before

    def test_changes_when_a_file_is_added_removed_or_renamed(self, tmp_path):
        root = _make_tree(tmp_path, {"a.py": "x = 1\n"})
        base = version_module.compute_code_fingerprint(str(root))
        (root / "c.py").write_text("", encoding="utf-8")
        added = version_module.compute_code_fingerprint(str(root))
        assert added != base
        (root / "c.py").rename(root / "d.py")
        assert version_module.compute_code_fingerprint(str(root)) != added
        (root / "d.py").unlink()
        assert version_module.compute_code_fingerprint(str(root)) == base

    def test_content_shifting_between_files_changes_fingerprint(self, tmp_path):
        """檔界混入長度與路徑：把內容從一個檔挪到另一個檔，串接位元組相同也必須不同。"""
        one = _make_tree(tmp_path / "one", {"a.py": "AB", "b.py": "C"})
        two = _make_tree(tmp_path / "two", {"a.py": "A", "b.py": "BC"})
        assert version_module.compute_code_fingerprint(str(one)) != version_module.compute_code_fingerprint(str(two))

    def test_ignores_non_python_and_pycache(self, tmp_path):
        root = _make_tree(tmp_path, {"a.py": "x = 1\n"})
        base = version_module.compute_code_fingerprint(str(root))
        _make_tree(root, {"assets/skill/SKILL.md": "docs", "__pycache__/a.cpython-312.pyc": "bytecode",
                          "pkg/__pycache__/junk.py": "z = 0\n", "tools/run.sh": "echo"})
        assert version_module.compute_code_fingerprint(str(root)) == base

    def test_independent_of_filesystem_order_and_location(self, tmp_path):
        files = {"z.py": "1\n", "a.py": "2\n", "m/n.py": "3\n"}
        one = _make_tree(tmp_path / "one", files)
        two = _make_tree(tmp_path / "two", dict(reversed(list(files.items()))))
        assert version_module.compute_code_fingerprint(str(one)) == version_module.compute_code_fingerprint(str(two))

    def test_no_python_files_means_unknown(self, tmp_path):
        """PyInstaller 凍結包內沒有 .py：回 None（呼叫端視為無從比對）。"""
        _make_tree(tmp_path, {"readme.txt": "x"})
        assert version_module.compute_code_fingerprint(str(tmp_path)) is None
        assert version_module.compute_code_fingerprint(str(tmp_path / "missing")) is None

    def test_real_package_fingerprint_matches_cached_helper(self):
        fp = version_module.code_fingerprint()
        assert fp == version_module.compute_code_fingerprint()
        assert fp and len(fp) == version_module.FINGERPRINT_HEX_LEN

    def test_version_string_format(self):
        fp = version_module.code_fingerprint()
        assert version_module.format_version_with_fingerprint() == f"{_REPO_VERSION} (code {fp})"
        with mock.patch.object(version_module, "code_fingerprint", return_value=None):
            assert version_module.format_version_with_fingerprint() == _REPO_VERSION


# ───────────────────────────── B. daemon 端 ─────────────────────────────


class TestDaemonExposesFingerprint:
    def test_status_carries_fingerprint_and_code_path(self):
        svc = SerialwrapService([])
        with mock.patch("sw_core.service.detect_multi_open", return_value=_FAKE_MULTI_OPEN):
            st = svc.rpc("health.status", {})
        assert st["code_fingerprint"] == version_module.code_fingerprint()
        assert Path(st["code_path"]) == Path(version_module.__file__).parent

    def test_every_rpc_response_carries_fingerprint(self):
        svc = SerialwrapService([])
        assert svc.rpc("health.ping", {})["code_fingerprint"] == version_module.code_fingerprint()
        unknown = svc.rpc("no.such.method", {})
        assert unknown["ok"] is False and unknown["code_fingerprint"] == version_module.code_fingerprint()

    def test_fingerprint_is_computed_once_at_startup_not_per_rpc(self):
        """啟動時算一次常駐：之後磁碟上的程式碼再怎麼變，daemon 回報的仍是啟動當下那份。"""
        with mock.patch.object(version_module, "code_fingerprint", return_value="startupfp001"), \
                mock.patch("sw_core.service.code_fingerprint", return_value="startupfp001") as calc:
            svc = SerialwrapService([])
            assert calc.call_count == 1
            for _ in range(3):
                assert svc.rpc("health.ping", {})["code_fingerprint"] == "startupfp001"
            assert calc.call_count == 1

    def test_unknown_fingerprint_is_omitted_from_ordinary_responses(self):
        with mock.patch("sw_core.service.code_fingerprint", return_value=None):
            svc = SerialwrapService([])
        assert "code_fingerprint" not in svc.rpc("health.ping", {})

    def test_lightweight_stub_without_init_does_not_raise(self):
        """既有測試以 __new__ 繞過 __init__ 建 stub：rpc() wrapper 不得因缺屬性拋例外。"""
        svc = SerialwrapService.__new__(SerialwrapService)
        assert svc.rpc("health.ping", {}) == {"ok": True, "pong": True}


# ───────────────────────────── C. CLI 端 ─────────────────────────────

_CLI_FP = "cli0000000aa"
_DAEMON_FP = "daemon00000b"


class _StubClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

    def __call__(self, endpoint, method, params=None, timeout_s=5.0, retries=0):
        return self.responses.get(method, {"ok": True})


def _run_cli(argv, stub):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sw_core.cli.rpc_call", stub), redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def cli_fp(monkeypatch):
    """固定 CLI 這側算出的指紋，與 daemon 回報值分開控制。"""
    monkeypatch.setattr(cli, "_code_fingerprint", lambda: _CLI_FP)


class TestVersionFlag:
    def test_prints_version_and_fingerprint(self, capsys):
        with pytest.raises(SystemExit) as ei:
            cli.build_parser().parse_args(["--version"])
        assert ei.value.code == 0
        out = capsys.readouterr().out.strip()
        assert re.fullmatch(rf"serialwrap {re.escape(_REPO_VERSION)} \(code [0-9a-f]{{12}}\)", out)

    def test_other_commands_never_compute_fingerprint(self, monkeypatch):
        """指紋要讀全部原始碼：建 parser／跑一般子命令不得為它付出代價（只有 --version 才算）。"""
        calls = []
        monkeypatch.setattr(version_module, "compute_code_fingerprint", lambda *a, **k: calls.append(1) or "x")
        version_module.code_fingerprint.cache_clear()
        try:
            cli.build_parser()
            rc, _out, _err = _run_cli(["session", "list"], _StubClient({"session.list": {"ok": True, "sessions": []}}))
            assert rc == 0
            assert calls == []
        finally:
            version_module.code_fingerprint.cache_clear()


class TestFingerprintMismatchWarning:
    def _stub(self, **fields):
        return _StubClient({"session.list": {"ok": True, "sessions": [], "version": _REPO_VERSION, **fields}})

    def test_same_version_different_code_warns_on_stderr_only(self, cli_fp):
        rc, out, err = _run_cli(["session", "list"], self._stub(code_fingerprint=_DAEMON_FP))
        assert rc == 0, "勿擋：成功語意不受影響"
        assert _CLI_FP in err and _DAEMON_FP in err
        assert "serialwrap service restart" in err
        assert err.count("\n") == 1, "警告只有一行"
        assert "不一致" not in out and json.loads(out)["ok"] is True, "警告不得混進 stdout"

    def test_stdout_json_is_unchanged_by_the_warning(self, cli_fp):
        """同一個回應，有警告與沒警告時 stdout 逐位元組相同。"""
        resp = {"ok": True, "sessions": [], "version": _REPO_VERSION, "code_fingerprint": _DAEMON_FP}
        _rc, out_warn, err_warn = _run_cli(["session", "list"], _StubClient({"session.list": dict(resp)}))
        assert err_warn
        resp_same = dict(resp, code_fingerprint=_CLI_FP)
        _rc, out_clean, err_clean = _run_cli(["session", "list"], _StubClient({"session.list": resp_same}))
        assert err_clean == ""
        assert json.loads(out_warn) == resp and json.loads(out_clean) == resp_same
        assert out_warn.count("\n") == out_clean.count("\n") == 1, "stdout 仍是單一行 JSON"

    def test_matching_fingerprint_is_silent(self, cli_fp):
        rc, _out, err = _run_cli(["session", "list"], self._stub(code_fingerprint=_CLI_FP))
        assert rc == 0 and err == ""

    def test_daemon_without_fingerprint_is_silent_backward_compat(self, cli_fp):
        """欄位加入前部署的 daemon：無從比對，不印警告（setup／doctor 另行把它當舊碼處理）。"""
        rc, _out, err = _run_cli(["session", "list"], self._stub())
        assert rc == 0 and err == ""

    def test_client_without_fingerprint_is_silent(self, monkeypatch):
        monkeypatch.setattr(cli, "_code_fingerprint", lambda: None)
        _rc, _out, err = _run_cli(["session", "list"], self._stub(code_fingerprint=_DAEMON_FP))
        assert err == ""

    def test_version_mismatch_prints_only_the_version_line(self, cli_fp):
        stub = _StubClient({"session.list": {"ok": True, "version": "9.9.9", "code_fingerprint": _DAEMON_FP}})
        _rc, _out, err = _run_cli(["session", "list"], stub)
        assert "9.9.9" in err and _REPO_VERSION in err
        assert _DAEMON_FP not in err and err.count("\n") == 1

    def test_failure_response_keeps_error_line_and_adds_code_line(self, cli_fp):
        stub = _StubClient({"session.list": {"ok": False, "error_code": "SOME_ERROR",
                                             "version": _REPO_VERSION, "code_fingerprint": _DAEMON_FP}})
        rc, _out, err = _run_cli(["session", "list"], stub)
        assert rc == 2
        assert "failed: SOME_ERROR" in err and _DAEMON_FP in err

    def test_event_dispatch_path_also_warns(self, cli_fp):
        stub = _StubClient({"event.rule_list": {"ok": True, "rules": [], "version": _REPO_VERSION,
                                                "code_fingerprint": _DAEMON_FP}})
        rc, out, err = _run_cli(["event", "list"], stub)
        assert rc == 0 and _DAEMON_FP in err
        assert json.loads(out)["ok"] is True


# ───────────────────────────── D. setup（CLI 整合）─────────────────────────────

_INSTALLED_FP = "installed0001"
_STALE_FP = "stale00000002"
_ACTIVE_USER = ("systemctl", "--user", "is-active", "serialwrap")
_ACTIVE_SYSTEM = ("systemctl", "is-active", "serialwrap")
_RESTART_USER = ["systemctl", "--user", "restart", "serialwrap"]


class _RestartingEffects(FakeEffects):
    """會讓『daemon 指紋』隨 restart 指令改變的 effects：模擬 restart 後 daemon 載入新碼。"""

    def __init__(self, state, **kw):
        super().__init__(**kw)
        self._state = state

    def run(self, cmd, *, timeout_s=None):
        result = super().run(cmd, timeout_s=timeout_s)
        if "restart" in cmd and cmd[-1] == "serialwrap":
            self._state["restarts"] += 1
            self._state["daemon_fp"] = self._state["fp_after_restart"]
        return result


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    from sw_core import sysenv

    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "posix")
    monkeypatch.setattr(cli, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(cli, "SOCKET_PATH", str(tmp_path / "run" / "serialwrap.sock"))
    monkeypatch.setattr(cli, "_endpoint_alive", lambda endpoint: True)
    monkeypatch.setattr(cli, "_code_fingerprint", lambda: _INSTALLED_FP)
    monkeypatch.setattr(cli, "_RESTART_VERIFY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(cli, "_RESTART_VERIFY_INTERVAL_S", 0.01)

    state = {"reachable": True, "flashing": False, "daemon_fp": _STALE_FP,
             "fp_after_restart": _INSTALLED_FP, "restarts": 0}
    fx_holder: dict = {}

    def make_fx(*, active=("user",), system=True):
        commands = {}
        if "user" in active:
            commands[_ACTIVE_USER] = (0, "active\n", "")
        if "system" in active:
            commands[_ACTIVE_SYSTEM] = (0, "active\n", "")
        fx = _RestartingEffects(state, systemd=system, commands=commands)
        fx_holder["fx"] = fx
        monkeypatch.setattr(sysenv, "SystemEffects", lambda: fx)
        return fx

    make_fx()
    calls = []

    def rpc(endpoint, method, params, **kwargs):
        calls.append(method)
        assert method in {"health.ping", "mcu.status", "session.list"}
        if not state["reachable"]:
            raise OSError("隔離測試：daemon 連不上")
        resp = {"ok": True, "flashing": state["flashing"], "sessions": []}
        if state["daemon_fp"] is not None:
            resp["code_fingerprint"] = state["daemon_fp"]
        return resp

    monkeypatch.setattr(cli, "rpc_call", rpc)
    config = cfg_dir / "config.yaml"

    def write(mode):
        cfg_dir.mkdir(parents=True, exist_ok=True)
        config.write_text(yaml.safe_dump({"supervision_mode": mode}), encoding="utf-8")

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["setup", *argv])
        text = out.getvalue()
        return rc, (json.loads(text) if text.strip() else None), err.getvalue()

    yield SimpleNamespace(state=state, write=write, run=run, make_fx=make_fx,
                          fx=lambda: fx_holder["fx"], calls=calls)


class TestSetupRestartsStaleDaemon:
    def test_stale_daemon_with_active_service_is_restarted(self, setup_env):
        setup_env.write("systemd-user")
        rc, payload, _err = setup_env.run("--user")
        assert rc == 0
        calls = setup_env.fx().calls
        assert _RESTART_USER in calls
        assert calls.index(["systemctl", "--user", "daemon-reload"]) < calls.index(_RESTART_USER), \
            "先刷新 unit 再 restart，新 unit 一併生效"
        assert setup_env.state["restarts"] == 1
        assert payload["setup"]["daemon_restart"] == "ran"
        code = payload["daemon_code"]
        assert code["stale"] is True and code["reason"] == "fingerprint_mismatch"
        assert code["action"] == "restarted" and code["after"] == "match"
        assert code["installed"] == _INSTALLED_FP and code["daemon"] == _STALE_FP

    def test_matching_fingerprint_does_not_restart(self, setup_env):
        setup_env.write("systemd-user")
        setup_env.state["daemon_fp"] = _INSTALLED_FP
        rc, payload, _err = setup_env.run("--user")
        assert rc == 0
        assert not any("restart" in c for c in setup_env.fx().calls)
        assert _ACTIVE_USER not in map(tuple, setup_env.fx().calls), "指紋相同時連 is-active 都不必查"
        assert payload["daemon_code"]["stale"] is False and payload["daemon_code"]["action"] == "none"
        assert "daemon_restart" not in payload["setup"]

    def test_daemon_that_reports_no_fingerprint_is_treated_as_stale(self, setup_env):
        """升級到第一個帶指紋的版本時，舊 daemon 的回應沒有 code_fingerprint——這正是要重啟的情況。"""
        setup_env.write("systemd-user")
        setup_env.state["daemon_fp"] = None
        rc, payload, _err = setup_env.run("--user")
        assert rc == 0 and _RESTART_USER in setup_env.fx().calls
        assert payload["daemon_code"]["reason"] == "daemon_no_fingerprint"
        assert payload["daemon_code"]["action"] == "restarted"

    def test_unreachable_daemon_keeps_existing_behaviour(self, setup_env):
        setup_env.write("systemd-user")
        setup_env.state["reachable"] = False
        rc, payload, _err = setup_env.run("--user")
        assert rc == 0
        assert not any("restart" in c for c in setup_env.fx().calls)
        assert "daemon_code" not in payload and "daemon_restart" not in payload["setup"]

    def test_inactive_service_is_not_restarted(self, setup_env):
        """daemon 在回應、但不是 systemd 服務起的（例如手動 start）：不能用 systemctl 去蓋它。"""
        setup_env.write("systemd-user")
        setup_env.make_fx(active=())
        rc, payload, _err = setup_env.run("--user")
        assert rc == 0 and not any("restart" in c for c in setup_env.fx().calls)
        assert payload["daemon_code"]["action"] == "service_inactive"
        assert "hint" in payload["daemon_code"]

    def test_no_restart_flag_skips(self, setup_env):
        setup_env.write("systemd-user")
        rc, payload, _err = setup_env.run("--user", "--no-restart")
        assert rc == 0 and not any("restart" in c for c in setup_env.fx().calls)
        assert payload["daemon_code"]["stale"] is True
        assert payload["daemon_code"]["action"] == "skipped_no_restart_flag"

    def test_flashing_with_force_never_restarts(self, setup_env):
        setup_env.write("systemd-user")
        setup_env.state["flashing"] = True
        rc, payload, _err = setup_env.run("--user", "--force")
        assert rc == 0 and not any("restart" in c for c in setup_env.fx().calls)
        assert payload["daemon_code"]["action"] == "skipped_flashing"

    def test_on_demand_only_reports(self, setup_env):
        setup_env.write("on-demand")
        rc, payload, _err = setup_env.run("--on-demand")
        assert rc == 0 and setup_env.fx().calls == []
        assert payload["daemon_code"]["action"] == "manual_restart"
        assert "daemon stop" in payload["daemon_code"]["hint"]

    def test_mode_transition_does_not_add_a_restart(self, setup_env):
        """on-demand → systemd-user 本來就停舊起新，不再補 restart。"""
        setup_env.write("on-demand")
        rc, payload, _err = setup_env.run("--user")
        assert rc == 0 and payload["setup"]["transitioned"] is True
        assert not any("restart" in c for c in setup_env.fx().calls)
        assert payload["daemon_code"]["action"] == "mode_transition"

    def test_restart_that_still_mismatches_is_reported_not_swallowed(self, setup_env):
        """unit 的 ExecStart 指向別份安裝時，restart 後指紋仍不同——要明講，不能回報成功。"""
        setup_env.write("systemd-user")
        setup_env.state["fp_after_restart"] = _STALE_FP
        rc, payload, err = setup_env.run("--user")
        assert rc == 0
        assert payload["daemon_code"]["action"] == "restarted" and payload["daemon_code"]["after"] == "mismatch"
        assert "未回報與安裝版相同" in err and payload["daemon_code"]["hint"]

    def test_restart_that_never_comes_back_is_reported(self, setup_env):
        setup_env.write("systemd-user")
        original = setup_env.fx().run

        def fx_run(cmd, *, timeout_s=None):
            result = original(cmd, timeout_s=timeout_s)
            if "restart" in cmd:
                setup_env.state["reachable"] = False  # 重啟後 daemon 沒再起來
            return result

        setup_env.fx().run = fx_run
        rc, payload, _err = setup_env.run("--user")
        assert rc == 0 and payload["daemon_code"]["after"] == "unreachable"

    def test_stdout_is_a_single_json_document(self, setup_env):
        setup_env.write("systemd-user")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.main(["setup", "--user"])
        assert len(out.getvalue().strip().splitlines()) == 1
        json.loads(out.getvalue())

    def test_system_scope_with_sudo_restarts_after_enable(self, setup_env):
        setup_env.write("systemd-system")
        setup_env.make_fx(active=("system",))
        rc, payload, _err = setup_env.run("--system", "--with-sudo")
        assert rc == 0
        calls = setup_env.fx().calls
        restart = ["sudo", "systemctl", "restart", "serialwrap"]
        assert restart in calls
        assert calls.index(["sudo", "systemctl", "enable", "serialwrap"]) < calls.index(restart)
        assert payload["daemon_code"]["action"] == "restarted"

    def test_system_scope_without_sudo_lists_restart_as_pending(self, setup_env):
        setup_env.write("systemd-system")
        setup_env.make_fx(active=("system",))
        rc, payload, _err = setup_env.run("--system")
        assert rc == 0
        assert not any(c and c[0] == "sudo" for c in setup_env.fx().calls), "絕不靜默跑 sudo"
        assert ["sudo", "systemctl", "restart", "serialwrap"] in payload["pending_sudo"]
        assert payload["daemon_code"]["action"] == "pending_sudo"
        assert setup_env.state["restarts"] == 0


# ───────────────────────────── E. setup_cmd 純函式與 reconcile ─────────────────────────────


class TestAssessmentPureFunction:
    def test_cases(self):
        def assess(**kw):
            base = dict(daemon_running=True, daemon_fingerprint="aaa", installed_fingerprint="aaa")
            base.update(kw)
            return daemon_code_assessment(**base)

        assert assess()["stale"] is False and assess()["checked"] is True
        assert assess(daemon_fingerprint="bbb")["reason"] == "fingerprint_mismatch"
        assert assess(daemon_fingerprint=None)["reason"] == "daemon_no_fingerprint"
        assert assess(daemon_fingerprint="")["stale"] is True
        unreachable = assess(daemon_running=False, daemon_fingerprint=None)
        assert unreachable["checked"] is False and unreachable["stale"] is False
        no_local = assess(installed_fingerprint=None)
        assert no_local["checked"] is False and no_local["stale"] is False

    def test_service_is_active_requires_exact_active(self):
        for out, expected in [("active\n", True), ("activating\n", False), ("failed\n", False), ("", False)]:
            fx = FakeEffects(systemd=True, commands={_ACTIVE_USER: (0, out, "")})
            assert service_is_active(fx, "systemd-user") is expected
        assert service_is_active(FakeEffects(systemd=True, commands={_ACTIVE_USER: (3, "inactive\n", "")}),
                                 "systemd-user") is False
        assert service_is_active(FakeEffects(systemd=True), "on-demand") is False
        sysfx = FakeEffects(systemd=True, commands={_ACTIVE_SYSTEM: (0, "active\n", "")})
        assert service_is_active(sysfx, "systemd-system") is True


class TestReconcileRestartFlag:
    def test_default_same_mode_refresh_never_restarts(self, tmp_path):
        """沒帶 restart_daemon 時維持既有『冪等刷新、不打斷 daemon』。"""
        fx = FakeEffects(systemd=True)
        res = reconcile(old_mode="systemd-user", target_mode="systemd-user", fx=fx, home=tmp_path,
                        daemon_running=True)
        assert not any("restart" in c or "start" in c for c in fx.calls)
        assert "daemon_restart" not in res

    def test_restart_flag_adds_restart_after_refresh(self, tmp_path):
        fx = FakeEffects(systemd=True)
        res = reconcile(old_mode="systemd-user", target_mode="systemd-user", fx=fx, home=tmp_path,
                        daemon_running=True, restart_daemon=True)
        assert fx.calls[-1] == _RESTART_USER
        assert fx.calls.index(["systemctl", "--user", "enable", "serialwrap"]) < len(fx.calls) - 1
        assert res["daemon_restart"] == "ran"

    def test_failed_restart_is_reported(self, tmp_path):
        fx = FakeEffects(systemd=True, commands={tuple(_RESTART_USER): (1, "", "boom")})
        res = reconcile(old_mode="systemd-user", target_mode="systemd-user", fx=fx, home=tmp_path,
                        restart_daemon=True)
        assert res["daemon_restart"] == "failed"

    def test_system_with_sudo(self, tmp_path):
        fx = FakeEffects(systemd=True)
        res = reconcile(old_mode="systemd-system", target_mode="systemd-system", fx=fx, home=tmp_path,
                        with_sudo=True, restart_daemon=True)
        assert fx.calls[-1] == ["sudo", "systemctl", "restart", "serialwrap"]
        assert res["daemon_restart"] == "ran"

    def test_system_without_sudo_is_pending_only(self, tmp_path):
        fx = FakeEffects(systemd=True)
        res = reconcile(old_mode="systemd-system", target_mode="systemd-system", fx=fx, home=tmp_path,
                        with_sudo=False, restart_daemon=True)
        assert fx.calls == []
        assert res["pending_sudo"][-1] == ["sudo", "systemctl", "restart", "serialwrap"]
        assert res["daemon_restart"] == "pending_sudo"

    def test_transition_ignores_restart_flag(self, tmp_path):
        fx = FakeEffects(systemd=True)
        res = reconcile(old_mode="on-demand", target_mode="systemd-user", fx=fx, home=tmp_path,
                        daemon_running=True, restart_daemon=True)
        assert not any("restart" in c for c in fx.calls)
        assert res["daemon_restart"] is None

    def test_on_demand_same_mode_has_nothing_to_restart(self, tmp_path):
        fx = FakeEffects(systemd=True)
        res = reconcile(old_mode="on-demand", target_mode="on-demand", fx=fx, home=tmp_path, restart_daemon=True)
        assert fx.calls == [] and res["daemon_restart"] is None


# ───────────────────────────── F. doctor ─────────────────────────────


class TestDoctorDaemonCodeCurrent:
    def _item(self, monkeypatch, response, *, installed=_INSTALLED_FP):
        def fake_rpc(endpoint, method, params, timeout_s=0.5, **kw):
            assert method in ("health.ping", "health.status")
            return response

        monkeypatch.setattr("sw_core.client.rpc_call", fake_rpc)
        monkeypatch.setattr("sw_core.cli._safe_runtime_config", lambda: None)
        monkeypatch.setattr("sw_core.version.code_fingerprint", lambda: installed)
        from sw_core.doctor_cmd import run_doctor

        report = run_doctor(fx=FakeEffects(systemd=True, in_groups={"dialout"}), platform="linux")
        return next(i for i in report if i["check"] == "daemon_code_current")

    def test_same_code_is_ok(self, monkeypatch):
        item = self._item(monkeypatch, {"ok": True, "code_fingerprint": _INSTALLED_FP})
        assert item["ok"] is True and item["fix"] == "" and item["advisory"] is True

    def test_different_code_fails_with_restart_fix(self, monkeypatch):
        item = self._item(monkeypatch, {"ok": True, "code_fingerprint": _STALE_FP})
        assert item["ok"] is False and item["advisory"] is True
        assert _STALE_FP in item["detail"] and _INSTALLED_FP in item["detail"]
        assert "serialwrap service restart" in item["fix"]

    def test_daemon_without_fingerprint_counts_as_stale(self, monkeypatch):
        item = self._item(monkeypatch, {"ok": True})
        assert item["ok"] is False and "舊版 daemon" in item["detail"]

    def test_unreachable_daemon_is_informational(self, monkeypatch):
        item = self._item(monkeypatch, {"ok": False, "error_code": "SOCKET_ERROR"})
        assert item["ok"] is True and item["fix"] == ""

    def test_unknown_local_fingerprint_is_informational(self, monkeypatch):
        item = self._item(monkeypatch, {"ok": True, "code_fingerprint": _STALE_FP}, installed=None)
        assert item["ok"] is True

    def test_overall_doctor_ok_ignores_this_advisory_check(self):
        assert "daemon_code_current" in cli._DOCTOR_ADVISORY_CHECKS
        assert "daemon_code_current" in cli._DOCTOR_ADVISORY_CHECKS_WIN
