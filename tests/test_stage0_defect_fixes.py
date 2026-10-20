"""Stage 0 P1-T16 回歸測試：缺陷修正群組 DF-01、DF-06、DF-07、DF-08、DF-09、DF-10、DF-11、DF-12、DF-13、DF-16。

依據：`docs/rust-refactor/plans/02-stage0-python-bc.md` Task T16（「修正表（凍結；每列一個測試）」）。

本檔鎖定各缺陷的外部行為，包含事件 ID 防護、非 JSON 回應、錯誤輸出、規則驗證與載入容錯、
state 遷移、WSL sudo 閘門、ran 命令清單、broker router 定位與 Windows skill 複製回退。

注意事項：
- 新名稱 `valid_rule_id`（P1-T16 新增）一律於測試函式內延遲 import，使本檔在 Stage A 仍可被 collection。
- 本檔混合 Linux／Windows 語意，因此不設模組層 `pytestmark`；每個測試函式各自標註
  恰一個平台 marker（`core_portable`／`posix_only`）與至少一個資源 marker。
- `SerialwrapService` 與 `cli.main` 的路徑皆以 import-time 常數或模組全域取得設定，本檔以
  `monkeypatch` 替換模組層屬性（而非依賴環境變數）以達成每測試獨立目錄。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from sw_core import cli as cli_mod
from sw_core import service as service_mod
from sw_core import setup_cmd
from sw_core import sysenv
from sw_core.event_engine import EngineDeps, EventEngine
from sw_core.event_engine.registry import RuleRegistry
from sw_core.service import SerialwrapService

# 壞掉的 YAML（DF-07）：`:` 開頭後接未閉合的 flow sequence，yaml.safe_load 必定擲 YAMLError。
_BAD_YAML = ":\n- ["

# DF-10 golden：`render_system_unit` 於下列參數下的輸出（實作前基線，逐位元組）。
# 本斷言屬回歸防護：DF-10 修正只改遷移目標，不得改動 system unit 文字。
_GOLDEN_SYSTEM_UNIT = """\
[Unit]
Description=serialwrap UART broker daemon (system)
After=multi-user.target

[Service]
Type=simple
User=ru_test
SupplementaryGroups=dialout
RuntimeDirectory=serialwrap
StateDirectory=serialwrap
ConfigurationDirectory=serialwrap
UMask=0117
Environment=SERIALWRAP_SOCKET_GROUP=dialout
ExecStart=/opt/sw/bin/serialwrapd --socket /run/serialwrap/serialwrapd.sock --profile-dir /etc/serialwrap/profiles
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target"""

# DF-01 counter 誘餌內容：若 event.reset 錯誤地對 "A/B" 執行 clear，此檔會被刪除。
_BAIT_COUNTER = '{"exhausted": false, "fires": 5, "last_fire_ts": null}'


class _NullBridge:
    """EventEngine 所需的 bridge 查詢替身（不接任何真實 session）。"""

    def active_cmd_id_for(self, com: str) -> None:
        return None

    def profile_for(self, com: str) -> None:
        return None

    def known_coms(self) -> list[str]:
        return ["COM0"]


class _NtOsProxy:
    """以 `os.name == "nt"` 模擬 Windows 分支；其餘屬性轉交真實 os 模組。"""

    name = "nt"

    def __getattr__(self, attr: str) -> Any:
        return getattr(os, attr)


def _rule_payload(owner: str, name: str, *, pattern: dict[str, Any] | None = None) -> dict[str, Any]:
    """建立最小合法 event 規則 payload（kind=tool、selector=COM0）。"""
    return {
        "schema_version": 1,
        "owner": owner,
        "name": name,
        "kind": "tool",
        "selectors": ["COM0"],
        "pattern": pattern if pattern is not None else {"kind": "contains", "value": "x"},
        "handler": {"exec": ["/bin/true"]},
    }


def _last_json_line(text: str) -> dict[str, Any]:
    """取 stdout 最後一行並解析為 JSON（CLI 的 `_print` 為單行輸出）。"""
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, "stdout 為空，預期至少一行 JSON 回應"
    obj = json.loads(lines[-1])
    assert isinstance(obj, dict)
    return obj


def _isolate_xdg_and_home(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """把 HOME、XDG 與明確 config 路徑導向各測試 tmp，避免 setup 寫入共享設定。"""
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg_cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "xdg_data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "xdg_state"))
    monkeypatch.setenv("SERIALWRAP_CONFIG", str(root / "config.yaml"))
    return home


def _patch_effects(monkeypatch: pytest.MonkeyPatch, *, wsl: bool, systemd: bool = False) -> sysenv.FakeEffects:
    """以 FakeEffects 取代 setup 內建立的 SystemEffects（避免本機 WSL 偵測觸發真實 sudo）。"""
    fx = sysenv.FakeEffects(wsl=wsl, systemd=systemd)
    monkeypatch.setattr(sysenv, "SystemEffects", lambda: fx)
    return fx


@pytest.fixture
def stage0_event_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SerialwrapService, Path, Path]:
    """建立隔離的 SerialwrapService 事件引擎，回傳 (svc, events_dir, runtime_dir)。

    `SerialwrapService.__init__` 以 sw_core.service 模組層常數 EVENTS_DIR／EVENTS_RUNTIME_DIR／
    EVENTS_LOG_PATH 建構 EventEngine（import-time 凍結，環境變數於此已無效），故以 monkeypatch
    替換模組層屬性；events_dir 故意置於兩層子目錄下，使 "../../x" 解析到 tmp 內部。
    """
    events_dir = tmp_path / "a" / "b" / "events.d"
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    monkeypatch.setattr(service_mod, "EVENTS_DIR", str(events_dir))
    monkeypatch.setattr(service_mod, "EVENTS_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(service_mod, "EVENTS_LOG_PATH", str(runtime_dir / "events.ndjson"))
    svc = SerialwrapService([], state_path=str(tmp_path / "state.json"))
    return svc, events_dir, runtime_dir


# ─────────────────────────────── DF-01：event 規則 ID 路徑穿越 ───────────────────────────────


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_rule_id_regex_examples() -> None:
    """RULE_ID_RE 代表性樣例：合法 `owner.name`（各段 1–64 字元）通過，其餘一律拒絕且不擲例外。"""
    from sw_core.event_engine.registry import valid_rule_id  # 延遲 import：P1-T16 新增名稱

    assert valid_rule_id("team.rule-1") is True
    # 註：`"a." + "b" * 64` 合法（段長上限 64），故以 65 字元作為超長樣例。
    for bad in ("../x", "A.b", "a", "a.b/c", "a." + "b" * 65, "a" * 65 + ".b", "-a.b", None, 3):
        assert valid_rule_id(bad) is False


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_rule_delete_rejects_traversal(stage0_event_service: tuple[SerialwrapService, Path, Path]) -> None:
    """DF-01：event.rule_delete 的 rule_id 含 `../` 時不得刪除 events.d 以外的檔案。"""
    svc, events_dir, _runtime_dir = stage0_event_service
    victim = events_dir.parent.parent / "x.json"  # events.d/../../x.json ＝ tmp/a/x.json
    victim.write_text("{}", encoding="utf-8")

    r = svc.rpc("event.rule_delete", {"rule_id": "../../x"})

    assert r["ok"] is True and r["deleted"] is False
    assert victim.exists()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_registry_delete_rejects_invalid_rule_id(tmp_path: Path) -> None:
    """DF-01：直接呼叫 RuleRegistry.delete 也不得讓非法 ID 越過 registry 根目錄。"""
    events_dir = tmp_path / "events.d"
    events_dir.mkdir()
    victim = tmp_path / "outside.json"
    victim.write_text("{}", encoding="utf-8")
    registry = RuleRegistry(str(events_dir))

    assert registry.delete("../outside") is False
    assert victim.exists()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_event_reset_rejects_invalid_id_without_counting(
    stage0_event_service: tuple[SerialwrapService, Path, Path],
) -> None:
    """DF-01：event.reset 收到非法 rule_id（含 `/`）時不得觸碰 counter 檔，回應重設數為 0。"""
    svc, _events_dir, runtime_dir = stage0_event_service
    # CounterStore 對 "A/B" 的路徑為 runtime/A/B.counter.json；預先放誘餌檔。
    bait = runtime_dir / "A" / "B.counter.json"
    bait.parent.mkdir(parents=True)
    bait.write_text(_BAIT_COUNTER, encoding="utf-8")

    r = svc.rpc("event.reset", {"rule_id": "A/B"})

    assert r["cleared"] == 0
    assert bait.exists()
    assert bait.read_text(encoding="utf-8") == _BAIT_COUNTER


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_valid_rule_id_still_deletes(stage0_event_service: tuple[SerialwrapService, Path, Path]) -> None:
    """DF-01 回歸防護：合法 rule_id（team.rule-1）仍可正常刪除。"""
    svc, _events_dir, _runtime_dir = stage0_event_service
    r = svc.rpc("event.rule_set", _rule_payload("team", "rule-1"))
    assert r["ok"] is True

    r = svc.rpc("event.rule_delete", {"rule_id": "team.rule-1"})

    assert r["ok"] is True and r["deleted"] is True


# ─────────────────────────────── DF-06：client 非 JSON 回應 ───────────────────────────────


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX 僅 POSIX 可用")
def test_client_non_json_response_invalid_response(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """DF-06：daemon 回覆非 JSON（`hello\\n`）時 CLI 回 INVALID_RESPONSE、exit 2，且不印 traceback。"""
    sock_path = tmp_path / "fake.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    srv.settimeout(10)

    def _serve_non_json() -> None:
        try:
            conn, _addr = srv.accept()
        except OSError:
            return
        try:
            conn.settimeout(5)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            conn.sendall(b"hello\n")
        finally:
            conn.close()

    worker = threading.Thread(target=_serve_non_json, daemon=True)
    worker.start()
    try:
        rc = cli_mod.main(["--endpoint", f"unix://{sock_path}", "session", "list"])
    finally:
        srv.close()
        worker.join(timeout=10)

    captured = capsys.readouterr()
    assert rc == 2
    assert "Traceback" not in captured.err
    obj = _last_json_line(captured.out)
    assert obj["ok"] is False
    assert obj["error_code"] == "INVALID_RESPONSE"
    assert set(obj) <= {"ok", "error_code", "message"}


# ─────────────────────────────── DF-07：traceback 改為凍結 JSON／單行 ───────────────────────────────


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_supervision_mode_bad_yaml_one_line_exit1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DF-07：config.yaml 壞掉時 `serialwrap supervision-mode` stderr 恰一行、exit 1、無 traceback。"""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(_BAD_YAML, encoding="utf-8")
    monkeypatch.setattr(cli_mod, "CONFIG_DIR", str(cfg_dir))

    rc = cli_mod.main(["supervision-mode"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Traceback" not in captured.err
    assert len(captured.err.splitlines()) == 1


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_service_bad_yaml_internal_error_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DF-07：config.yaml 壞掉時 `serialwrap service status` 回 INTERNAL_ERROR JSON、exit 1、無 traceback。"""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(_BAD_YAML, encoding="utf-8")
    monkeypatch.setattr(cli_mod, "CONFIG_DIR", str(cfg_dir))

    rc = cli_mod.main(["service", "status"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Traceback" not in captured.err
    obj = _last_json_line(captured.out)
    assert obj["ok"] is False
    assert obj["error_code"] == "INTERNAL_ERROR"
    assert isinstance(obj.get("message"), str) and obj["message"]
    assert set(obj) == {"ok", "error_code", "message"}


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("daemon_command", ["start", "stop", "restart", "status"])
def test_daemon_bad_yaml_fails_before_daemon_action(
    daemon_command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DF-07：daemon 子命令遇壞 YAML 回 INTERNAL_ERROR，且不進 RPC、service 或 spawn 路徑。"""
    _isolate_xdg_and_home(monkeypatch, tmp_path)
    Path(os.environ["SERIALWRAP_CONFIG"]).write_text(_BAD_YAML, encoding="utf-8")
    calls: list[str] = []
    for name, label in (
        ("_run_daemon_start", "start"),
        ("_run_daemon_stop", "stop"),
        ("_run_daemon_restart", "restart"),
    ):
        monkeypatch.setattr(cli_mod, name, lambda _args, label=label: calls.append(label) or 0)
    monkeypatch.setattr(cli_mod, "_run_rpc", lambda *_a, **_k: calls.append("rpc") or 0)
    monkeypatch.setattr(cli_mod, "service_action", lambda *_a, **_k: calls.append("service") or {"ok": True})
    monkeypatch.setattr(cli_mod.subprocess, "Popen", lambda *_a, **_k: calls.append("spawn"))

    rc = cli_mod.main(["daemon", daemon_command])

    captured = capsys.readouterr()
    assert rc == 1
    assert _last_json_line(captured.out)["error_code"] == "INTERNAL_ERROR"
    assert calls == []
    assert "Traceback" not in captured.err


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_load_all_invalid_utf8_is_failed_load(tmp_path: Path) -> None:
    """DF-09：規則 JSON 含無效 UTF-8 時 load_all 將它列為 failed，不中止載入。"""
    events_dir = tmp_path / "events.d"
    events_dir.mkdir()
    malformed = events_dir / "bad.encoding.json"
    malformed.write_bytes(b"\xff\xfe")

    result = RuleRegistry(str(events_dir)).load_all()

    assert result.rules == []
    assert [entry.path for entry in result.failed] == [str(malformed)]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_load_all_overflow_is_failed_load(tmp_path: Path) -> None:
    """DF-09：規則中的 Infinity 數值觸發 OverflowError 時也須列入 failed。"""
    events_dir = tmp_path / "events.d"
    events_dir.mkdir()
    malformed = events_dir / "bad.overflow.json"
    malformed.write_text(
        json.dumps(dict(_rule_payload("bad", "overflow"), cooldown_ms=float("inf"))),
        encoding="utf-8",
    )

    result = RuleRegistry(str(events_dir)).load_all()

    assert result.rules == []
    assert [entry.path for entry in result.failed] == [str(malformed)]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_setup_unexpected_exception_internal_error_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DF-07：setup 內部擲出未預期的 RuntimeError 時回 INTERNAL_ERROR JSON、exit 1、無 traceback。"""
    _isolate_xdg_and_home(monkeypatch, tmp_path)
    _patch_effects(monkeypatch, wsl=False)
    # 略過 daemon／flash 探測（避免任何 socket 連線）。
    monkeypatch.setattr(cli_mod, "_rpc_call_traced", lambda *_a, **_k: {"ok": False})

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("injected setup failure")

    monkeypatch.setattr(cli_mod, "materialize_assets", _boom)

    rc = cli_mod.main(["setup", "--on-demand"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Traceback" not in captured.err
    obj = _last_json_line(captured.out)
    assert obj["ok"] is False
    assert obj["error_code"] == "INTERNAL_ERROR"
    assert "injected setup failure" in obj["message"]
    assert set(obj) == {"ok", "error_code", "message"}


# ─────────────────────────────── DF-08：contains 規則 flags 驗證 ───────────────────────────────


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_contains_rule_flags_validated(stage0_event_service: tuple[SerialwrapService, Path, Path]) -> None:
    """DF-08：contains 規則帶不合法 flags（q）必須於 upsert 階段拒絕，且規則未落盤。"""
    svc, events_dir, _runtime_dir = stage0_event_service
    payload = _rule_payload("team", "flagged", pattern={"kind": "contains", "value": "x", "flags": "q"})

    r = svc.rpc("event.rule_set", payload)

    assert r["ok"] is False
    assert r["error_code"] == "INVALID_RULE_SCHEMA"
    assert "unsupported regex flag: q" in r["error"]
    assert not (events_dir / "team.flagged.json").exists()


# ─────────────────────────────── DF-09：load_all 容錯 ───────────────────────────────


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_load_all_value_error_is_failed_load(tmp_path: Path) -> None:
    """DF-09：規則檔觸發 ValueError／TypeError 時 load_all 列入 failed、不擲例外，engine.start() 成功。"""
    events_dir = tmp_path / "events.d"
    events_dir.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    good_path = events_dir / "ok.good.json"
    good_path.write_text(json.dumps(_rule_payload("ok", "good")), encoding="utf-8")
    # cooldown_ms="abc" → int() 擲 ValueError；timeout_ms=None → int(None) 擲 TypeError。
    bad_value_path = events_dir / "bad.value.json"
    bad_value_path.write_text(
        json.dumps(dict(_rule_payload("bad", "value"), cooldown_ms="abc")), encoding="utf-8"
    )
    bad_type_path = events_dir / "bad.type.json"
    bad_type_path.write_text(
        json.dumps(dict(_rule_payload("bad", "type"), timeout_ms=None)), encoding="utf-8"
    )

    result = RuleRegistry(str(events_dir)).load_all()

    failed_paths = {entry.path for entry in result.failed}
    assert str(bad_value_path) in failed_paths
    assert str(bad_type_path) in failed_paths
    assert [rule.rule_id for rule in result.rules] == ["ok.good"]

    engine = EventEngine(EngineDeps(
        events_dir=str(events_dir),
        runtime_dir=str(runtime_dir),
        log_path=str(runtime_dir / "events.ndjson"),
        bridge=_NullBridge(),
    ))
    try:
        engine.start()  # 單一壞規則不得中止引擎啟動
        assert [row["rule_id"] for row in engine.rule_list()] == ["ok.good"]
    finally:
        engine.stop()


# ─────────────────────────────── DF-10：system scope 遷移目標 ───────────────────────────────


@pytest.mark.posix_only
@pytest.mark.memory_only
@pytest.mark.skipif(sys.platform == "win32", reason="systemd system scope 僅適用 POSIX")
def test_system_scope_migration_targets_daemon_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DF-10：遷移到 systemd-system 時目標為 system unit 下 daemon 實際使用的 state 路徑（run_user 的家目錄），
    而非 /var/lib/serialwrap；system unit 文字維持實作前逐位元組相同。"""
    import pwd  # 延遲 import：僅 POSIX 測試使用

    run_user = "ru_test"
    run_home = tmp_path / "home_ru"
    caller_home = tmp_path / "home_caller"
    run_home.mkdir()
    caller_home.mkdir()
    monkeypatch.setenv("SUDO_USER", run_user)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_state"))
    monkeypatch.setattr(
        setup_cmd.shutil,
        "which",
        lambda name, *_a, **_k: "/opt/sw/bin/serialwrapd" if name == "serialwrapd" else None,
    )

    def _fake_getpwnam(name: str) -> types.SimpleNamespace:
        if name != run_user:
            raise KeyError(name)
        return types.SimpleNamespace(pw_dir=str(run_home))

    monkeypatch.setattr(pwd, "getpwnam", _fake_getpwnam)

    migrations: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        setup_cmd,
        "migrate_legacy_state",
        lambda src, dst: migrations.append((Path(src), Path(dst))),
    )

    fx = sysenv.FakeEffects()
    result = setup_cmd.reconcile(
        old_mode="on-demand",
        target_mode="systemd-system",
        fx=fx,
        home=caller_home,
        daemon_running=False,
        ping=lambda _endpoint: False,
        alive=lambda _pid: False,
        any_flashing=False,
        with_sudo=True,
        force=False,
        socket_path=setup_cmd.SYSTEM_SOCKET,
        config_path=tmp_path / "config.yaml",
    )

    assert result["applied"] is True and result["transitioned"] is True
    assert len(migrations) == 1
    _src, dst = migrations[0]
    assert dst == run_home / ".local" / "state" / "serialwrap" / "state.json"

    staged = (caller_home / ".local" / "share" / "serialwrap" / "serialwrap.service").read_text(encoding="utf-8")
    assert staged == _GOLDEN_SYSTEM_UNIT


# ─────────────────────────────── DF-11：WSL systemd 啟用需 --with-sudo ───────────────────────────────


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wsl_systemd_sudo_only_with_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DF-11：WSL 且 systemd 未啟用、未帶 --with-sudo 時不得執行任何 sudo；改於 pending_sudo 回報寫 /etc/wsl.conf 的命令。"""
    _isolate_xdg_and_home(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_mod, "_rpc_call_traced", lambda *_a, **_k: {"ok": False})
    fx = _patch_effects(monkeypatch, wsl=True, systemd=False)

    cli_mod.main(["setup", "--system"])

    captured = capsys.readouterr()
    assert [c for c in fx.calls if c and c[0] == "sudo"] == []
    payload = _last_json_line(captured.out)
    pending = payload.get("pending_sudo") or []
    assert any(
        isinstance(cmd, list) and cmd[:2] == ["sudo", "install"] and cmd[-1] == "/etc/wsl.conf"
        for cmd in pending
    )


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wsl_systemd_sudo_with_flag_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DF-11 回歸防護：帶 --with-sudo 時恰執行一次 `sudo install ... /etc/wsl.conf`。"""
    _isolate_xdg_and_home(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_mod, "_rpc_call_traced", lambda *_a, **_k: {"ok": False})
    fx = _patch_effects(monkeypatch, wsl=True, systemd=False)

    cli_mod.main(["setup", "--system", "--with-sudo"])

    sudo_calls = [c for c in fx.calls if c and c[0] == "sudo"]
    assert len(sudo_calls) == 1
    assert sudo_calls[0][:2] == ["sudo", "install"]
    assert sudo_calls[0][-1] == "/etc/wsl.conf"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wsl_pending_is_reported_for_non_systemd_target(
    tmp_path: Path,
) -> None:
    """DF-11：WSL 未啟用 systemd 時，即使目標不是 systemd 也回報未執行的設定命令。"""
    fx = sysenv.FakeEffects(wsl=True, systemd=False)

    result = setup_cmd.ensure_wsl_systemd(
        fx,
        tmp_path,
        target_mode="on-demand",
        with_sudo=True,
    )

    pending = result.get("pending_sudo") or []
    assert len(pending) == 1
    assert pending[0][:2] == ["sudo", "install"]
    assert pending[0][-1] == "/etc/wsl.conf"
    assert fx.calls == []
    assert (tmp_path / ".local" / "share" / "serialwrap" / "wsl.conf").is_file()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wsl_already_enabled_is_reported_before_target_gate(
    tmp_path: Path,
) -> None:
    """DF-11：WSL systemd 已啟用時保留 already=True，即使目標為 on-demand。"""
    fx = sysenv.FakeEffects(wsl=True, systemd=True)

    result = setup_cmd.ensure_wsl_systemd(fx, tmp_path, target_mode="on-demand")

    assert result == {"wsl": True, "already": True, "enabled_now": False, "needs_restart": False}
    assert fx.calls == []


# ─────────────────────────────── DF-12：reconcile ran 欄位 ───────────────────────────────


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_reconcile_ran_lists_executed_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DF-12：以真實 SystemEffects（subprocess.run 替身）執行 reconcile 時，`ran` 必須列出實際執行的命令。

    FakeEffects 本來就有 `.calls`，故缺陷只在真實實作：SystemEffects 無 `.calls`，生產環境 `ran` 恆為 []。
    """
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_kwargs: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    fx = sysenv.SystemEffects()

    result = setup_cmd.reconcile(
        old_mode="systemd-user",
        target_mode="systemd-user",
        fx=fx,
        home=tmp_path / "home",
        daemon_running=False,
        any_flashing=False,
        with_sudo=False,
        force=False,
        socket_path=None,
        config_path=tmp_path / "config.yaml",
    )

    expected = [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "serialwrap"],
    ]
    assert result["ran"] == expected
    assert list(fx.calls) == expected


# ─────────────────────────────── DF-13：minicom broker 改名後的 router 查找 ───────────────────────────────


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.skipif(sys.platform == "win32", reason="minicom-broker 為 bash 腳本，僅 POSIX")
def test_minicom_broker_wrapper_finds_renamed_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DF-13：物化後 broker 須能找到改名為 serialwrap-minicom 的 router（不得 exit 2）。"""
    home = _isolate_xdg_and_home(monkeypatch, tmp_path)
    setup_cmd.materialize_assets(home=home, force=False)
    bin_dir = home / ".local" / "bin"

    # 以可辨識輸出的替身取代物化後的 serialwrap-minicom，確認 broker 實際呼叫的是它。
    router = bin_dir / "serialwrap-minicom"
    router.write_text('#!/usr/bin/env bash\necho "FAKE_ROUTER $*"\n', encoding="utf-8")
    router.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
    }
    proc = subprocess.run(
        ["bash", str(bin_dir / "serialwrap-minicom-broker"), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode != 2, proc.stderr
    assert proc.returncode == 0, proc.stderr
    assert "FAKE_ROUTER --help" in proc.stdout


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_minicom_broker_source_layout_falls_back_to_legacy_router(tmp_path: Path) -> None:
    """DF-13：source/assets 佈局仍可由 legacy sibling minicom_router.sh 提供 broker 入口。"""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    broker = tools_dir / "minicom-broker.sh"
    shutil.copy2(
        Path(__file__).resolve().parents[1] / "sw_core" / "assets" / "tools" / "minicom-broker.sh",
        broker,
    )
    router = tools_dir / "minicom_router.sh"
    router.write_text('#!/usr/bin/env bash\nprintf "LEGACY_ROUTER %s\\n" "$*"\n', encoding="utf-8")
    router.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(broker), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "LEGACY_ROUTER --help"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_reconcile_continues_when_run_user_resolution_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DF-10：state 遷移路徑 best-effort 解析失敗不得中止已開始的 reconcile。"""
    fx = sysenv.FakeEffects(systemd=True)
    monkeypatch.setattr(setup_cmd, "_resolve_run_user", lambda: (_ for _ in ()).throw(OSError("no user")))

    result = setup_cmd.reconcile(
        old_mode="systemd-system",
        target_mode="systemd-user",
        fx=fx,
        home=tmp_path / "home",
        daemon_running=False,
        ping=lambda _endpoint: False,
        alive=lambda _pid: False,
        with_sudo=True,
        force=False,
        config_path=tmp_path / "config.yaml",
    )

    assert result["applied"] is True
    assert result["mode"] == "systemd-user"


# ─────────────────────────────── DF-16：Windows skill link 權限不足時回退為複製 ───────────────────────────────


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_home_is_isolated_by_pytest_conftest() -> None:
    """Windows pytest 的 HOME 與 USERPROFILE 都必須指向本輪隔離目錄。"""
    if os.name == "nt":
        home = Path(os.environ["USERPROFILE"])
        assert Path(os.environ["HOME"]) == home
        assert home.name == "home"
        assert home.parent.name.startswith("sw-pytest-iso-")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_skill_link_falls_back_to_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DF-16：Windows 建立 symlink 遇 ERROR_PRIVILEGE_NOT_HELD（1314）時，skill 目錄改為複製，setup 不擲例外。"""
    home = _isolate_xdg_and_home(monkeypatch, tmp_path)
    # 非 WSL、systemd 未啟用：setup 走 on-demand 同模式刷新，不會產生任何系統指令。
    fx = _patch_effects(monkeypatch, wsl=False, systemd=False)
    monkeypatch.setattr(cli_mod, "_rpc_call_traced", lambda *_a, **_k: {"ok": False})

    def _no_symlink_privilege(self: pathlib.Path, target: Any, target_is_directory: bool = False) -> None:
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(pathlib.Path, "symlink_to", _no_symlink_privilege)
    monkeypatch.setattr(setup_cmd, "os", _NtOsProxy())

    rc = cli_mod.main(["setup", "--on-demand"])

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "Traceback" not in captured.err
    assert _last_json_line(captured.out)["ok"] is True
    assert [c for c in fx.calls if c and c[0] == "sudo"] == []
    link = home / ".agents" / "skills" / "serialwrap"
    assert link.is_dir() and not link.is_symlink()
    assert (link / "SKILL.md").is_file()
