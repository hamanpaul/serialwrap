from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import threading
import textwrap

import pytest

from sw_core import cli
from sw_core import remote_tunnel as rt


def _write_yaml(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def _write_benches_file(path: Path) -> None:
    _write_yaml(
        path,
        """
        benches:
          eit-test:
            target: eit@eit-test.hamanpaul.cc
            remote_socket: /tmp/serialwrap/serialwrapd.sock
            local_port: 7777
            ssh_opts:
              - "-o"
              - "ProxyCommand=cloudflared access ssh --hostname %h"
              - "-i"
              - "~/.ssh/id_ed25519_serialwrap_bench"
            autossh: true
        """,
    )


def _bench_state_path() -> Path:
    return Path(os.environ["SERIALWRAP_STATE_DIR"]) / "benches.state.json"


def _read_endpoint_memory(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        pytest.fail("benches.state.json 必須維持 object")
    benches = payload.get("benches")
    if benches is None:
        return payload
    if not isinstance(benches, dict):
        pytest.fail("benches.state.json 的 benches 欄位必須是 object")
    return benches


def _connect_subparser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparser = action.choices.get("connect")
            if isinstance(subparser, argparse.ArgumentParser):
                return subparser
    return None


def _run_main(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict | None]:
    try:
        rc = cli.main(argv)
    except SystemExit as exc:
        pytest.fail(f"cli.main({argv!r}) 不應以 SystemExit 結束：{exc.code}")
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else None


def _run_connect(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict | None]:
    parser = cli.build_parser()
    if _connect_subparser(parser) is None:
        pytest.fail("serialwrap connect 尚未實作；Task 2.1 先以 RED 測試鎖定 connect CLI 契約")
    try:
        parser.parse_args(["connect", *argv])
    except SystemExit as exc:
        pytest.fail(f"serialwrap connect 參數契約尚未完成：{exc.code}")
    return _run_main(["connect", *argv], capsys)


def _stub_resolve_ssh_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: f"/usr/bin/{via}")


@pytest.mark.parametrize(
    "argv",
    [
        ["connect"],
        ["connect", "eit-test", "--bogus"],
        ["--timeout", "connect", "eit-test"],
        ["--endpoint", "connect", "eit-test"],
        ["--socket", "connect", "eit-test"],
    ],
)
def test_connect_parse_errors_return_structured_invalid_args(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    rc, obj = _run_main(argv, capsys)

    assert rc == 1
    assert obj is not None
    assert obj["ok"] is False
    assert obj["error_code"] == "INVALID_ARGS"


def test_connect_known_code_opens_connect_tunnel_with_bench_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    benches_path = tmp_path / "benches.yaml"
    _write_benches_file(benches_path)
    monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(benches_path))
    _stub_resolve_ssh_bin(monkeypatch)
    state_path = _bench_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {"spare-bench": "tcp://127.0.0.1:7788"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def fake_open(spec, run_dir, **kwargs):
        captured["spec"] = spec
        captured["run_dir"] = run_dir
        return {
            "ok": True,
            "status": "active",
            "role": spec.role,
            "listen_port": spec.local or spec.port,
        }

    monkeypatch.setattr(rt, "open_tunnel", fake_open)

    rc, obj = _run_connect(["eit-test"], capsys)

    assert rc == 0
    assert obj is not None
    assert obj["ok"] is True
    assert obj["status"] == "active"
    spec = captured["spec"]
    assert spec.role == "connect"
    assert spec.ssh_target == "eit@eit-test.hamanpaul.cc"
    assert spec.port == 7777
    assert (spec.local or spec.port) == 7777
    assert spec.remote_socket == "/tmp/serialwrap/serialwrapd.sock"
    assert spec.via == "autossh"
    assert spec.ssh_opts == (
        "-o",
        "ProxyCommand=cloudflared access ssh --hostname %h",
        "-i",
        "~/.ssh/id_ed25519_serialwrap_bench",
    )
    entries = _read_endpoint_memory(state_path)
    assert entries["eit-test"] == "tcp://127.0.0.1:7777"
    assert entries["spare-bench"] == "tcp://127.0.0.1:7788"


def test_connect_unknown_code_returns_structured_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    benches_path = tmp_path / "benches.yaml"
    _write_benches_file(benches_path)
    monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(benches_path))

    rc, obj = _run_connect(["missing-code"], capsys)

    assert rc == 1
    assert obj is not None
    assert obj["ok"] is False
    assert "error_code" in obj


def test_remember_bench_endpoint_serializes_concurrent_updates_and_preserves_all_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _bench_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {"existing-bench": "tcp://127.0.0.1:7000"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    real_load = cli._load_bench_state_doc
    load_gate = threading.Barrier(2)
    start_gate = threading.Barrier(2)
    errors: list[BaseException] = []

    def gated_load(path: str):
        payload, benches = real_load(path)
        try:
            load_gate.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return payload, benches

    monkeypatch.setattr(cli, "_load_bench_state_doc", gated_load)

    def worker(code: str, endpoint: str) -> None:
        try:
            start_gate.wait(timeout=1.0)
            cli._remember_bench_endpoint(code, endpoint)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(
            target=worker,
            args=("bench-a", "tcp://127.0.0.1:7001"),
            name="remember-bench-a",
        ),
        threading.Thread(
            target=worker,
            args=("bench-b", "tcp://127.0.0.1:7002"),
            name="remember-bench-b",
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert not [thread.name for thread in threads if thread.is_alive()]
    assert errors == []
    assert _read_endpoint_memory(state_path) == {
        "bench-a": "tcp://127.0.0.1:7001",
        "bench-b": "tcp://127.0.0.1:7002",
        "existing-bench": "tcp://127.0.0.1:7000",
    }


def test_connect_close_closes_tunnel_and_clears_endpoint_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    benches_path = tmp_path / "benches.yaml"
    _write_benches_file(benches_path)
    monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(benches_path))

    state_path = _bench_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "eit-test": "tcp://127.0.0.1:7777",
                "spare-bench": "tcp://127.0.0.1:7788",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def fake_close(run_dir, selector, **kwargs):
        captured["run_dir"] = run_dir
        captured["selector"] = selector
        return {"ok": True, "closed": [7777]}

    monkeypatch.setattr(rt, "close", fake_close)
    monkeypatch.setattr(rt, "status", lambda run_dir: {"ok": True, "tunnels": []})

    rc, obj = _run_connect(["eit-test", "--close"], capsys)

    assert rc == 0
    assert obj is not None
    assert obj["ok"] is True
    assert obj["closed"] == [7777]
    assert str(captured["selector"]) == "7777"
    entries = _read_endpoint_memory(state_path)
    assert "eit-test" not in entries
    assert entries["spare-bench"] == "tcp://127.0.0.1:7788"


def test_connect_close_keeps_endpoint_memory_when_tunnel_still_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    benches_path = tmp_path / "benches.yaml"
    _write_benches_file(benches_path)
    monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(benches_path))

    state_path = _bench_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "eit-test": "tcp://127.0.0.1:7777",
                "spare-bench": "tcp://127.0.0.1:7788",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(rt, "close", lambda run_dir, selector, **kwargs: {"ok": True, "closed": [7777]})
    monkeypatch.setattr(
        rt,
        "status",
        lambda run_dir: {
            "ok": True,
            "tunnels": [
                {
                    "listen_port": 7777,
                    "status": "active",
                    "role": "connect",
                    "endpoint": "tcp://127.0.0.1:7777",
                }
            ],
        },
    )

    rc, obj = _run_connect(["eit-test", "--close"], capsys)

    assert rc == 1
    assert obj is not None
    assert obj["ok"] is False
    assert obj["error_code"] == "TUNNEL_STILL_ACTIVE"
    entries = _read_endpoint_memory(state_path)
    assert entries["eit-test"] == "tcp://127.0.0.1:7777"
    assert entries["spare-bench"] == "tcp://127.0.0.1:7788"


def test_connect_close_keeps_endpoint_memory_when_orphan_tunnel_still_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    benches_path = tmp_path / "benches.yaml"
    _write_benches_file(benches_path)
    monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(benches_path))

    state_path = _bench_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "eit-test": "tcp://127.0.0.1:7777",
                "spare-bench": "tcp://127.0.0.1:7788",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(rt, "close", lambda run_dir, selector, **kwargs: {"ok": True, "closed": [7777]})
    monkeypatch.setattr(
        rt,
        "status",
        lambda run_dir: {
            "ok": True,
            "tunnels": [
                {
                    "status": "orphan",
                    "control_path": "/tmp/serialwrap/remote/cm-7777",
                    "alive": True,
                }
            ],
        },
    )

    rc, obj = _run_connect(["eit-test", "--close"], capsys)

    assert rc == 1
    assert obj is not None
    assert obj["ok"] is False
    assert obj["error_code"] == "TUNNEL_STILL_ACTIVE"
    entries = _read_endpoint_memory(state_path)
    assert entries["eit-test"] == "tcp://127.0.0.1:7777"
    assert entries["spare-bench"] == "tcp://127.0.0.1:7788"


def test_connect_rolls_back_tunnel_when_state_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    benches_path = tmp_path / "benches.yaml"
    _write_benches_file(benches_path)
    monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(benches_path))
    _stub_resolve_ssh_bin(monkeypatch)
    state_path = _bench_state_path()
    state_path.unlink(missing_ok=True)

    monkeypatch.setattr(
        rt,
        "open_tunnel",
        lambda spec, run_dir, **kwargs: {
            "ok": True,
            "status": "active",
            "role": spec.role,
            "listen_port": spec.local or spec.port,
        },
    )
    rollback_calls: list[str] = []

    def fake_close(run_dir, selector, **kwargs):
        rollback_calls.append(str(selector))
        return {"ok": True, "closed": [7777]}

    monkeypatch.setattr(rt, "close", fake_close)
    monkeypatch.setattr(rt, "status", lambda run_dir: {"ok": True, "tunnels": []})

    def boom(path: str, payload: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_write_bench_state_doc", boom)

    rc, obj = _run_connect(["eit-test"], capsys)

    assert rc == 1
    assert obj is not None
    assert obj["ok"] is False
    assert obj["error_code"] == "BENCH_STATE_IO_ERROR"
    assert rollback_calls == ["7777"]
    assert _read_endpoint_memory(state_path) == {}


def test_connect_does_not_rollback_existing_tunnel_when_state_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    benches_path = tmp_path / "benches.yaml"
    _write_benches_file(benches_path)
    monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(benches_path))
    _stub_resolve_ssh_bin(monkeypatch)
    state_path = _bench_state_path()
    state_path.unlink(missing_ok=True)

    monkeypatch.setattr(
        rt,
        "open_tunnel",
        lambda spec, run_dir, **kwargs: {
            "ok": True,
            "already_running": True,
            "status": "active",
            "role": spec.role,
            "listen_port": spec.local or spec.port,
        },
    )
    rollback_calls: list[str] = []

    def fake_close(run_dir, selector, **kwargs):
        rollback_calls.append(str(selector))
        return {"ok": True, "closed": [7777]}

    monkeypatch.setattr(rt, "close", fake_close)

    def boom(path: str, payload: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_write_bench_state_doc", boom)

    rc, obj = _run_connect(["eit-test"], capsys)

    assert rc == 1
    assert obj is not None
    assert obj["ok"] is False
    assert obj["error_code"] == "BENCH_STATE_IO_ERROR"
    assert rollback_calls == []
    assert _read_endpoint_memory(state_path) == {}
