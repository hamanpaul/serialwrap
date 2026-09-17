from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib
import importlib.util
import sys
import textwrap

import pytest


def _write_yaml(path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def _bench_registry():
    spec = importlib.util.find_spec("sw_core.bench_registry")
    if spec is None:
        pytest.fail(
            "sw_core.bench_registry 尚未實作；Task 1.1 先以 RED 測試鎖定 benches.yaml 契約"
        )
    sys.modules.pop("sw_core.bench_registry", None)
    try:
        module = importlib.import_module("sw_core.bench_registry")
    except Exception as exc:
        pytest.fail(f"sw_core.bench_registry 匯入失敗：{exc!r}")
    missing = [name for name in ("BenchEntry", "load_benches", "resolve") if not hasattr(module, name)]
    if missing:
        pytest.fail(f"sw_core.bench_registry 缺少必要 API：{', '.join(missing)}")
    return module


def test_resolve_uses_serialwrap_benches_file_override(tmp_path, monkeypatch) -> None:
    default_dir = tmp_path / "config"
    default_dir.mkdir()
    _write_yaml(
        default_dir / "benches.yaml",
        """
        benches:
          eit-test:
            target: default@default.example
            remote_socket: /tmp/default.sock
            local_port: 6000
            ssh_opts:
              - "-i"
              - "~/.ssh/default"
            autossh: false
        """,
    )
    override_path = tmp_path / "override-benches.yaml"
    _write_yaml(
        override_path,
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
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(default_dir))
    monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(override_path))

    module = _bench_registry()
    entry = module.resolve("eit-test")

    assert entry.target == "eit@eit-test.hamanpaul.cc"
    assert entry.remote_socket == "/tmp/serialwrap/serialwrapd.sock"
    assert entry.local_port == 7777
    assert tuple(entry.ssh_opts) == (
        "-o",
        "ProxyCommand=cloudflared access ssh --hostname %h",
        "-i",
        "~/.ssh/id_ed25519_serialwrap_bench",
    )
    assert entry.autossh is True


def test_load_benches_returns_frozen_entry_and_preserves_ssh_opts(tmp_path) -> None:
    benches_path = tmp_path / "benches.yaml"
    _write_yaml(
        benches_path,
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

    module = _bench_registry()
    benches = module.load_benches(str(benches_path))

    assert set(benches) == {"eit-test"}
    entry = benches["eit-test"]
    assert isinstance(entry, module.BenchEntry)
    assert entry.target == "eit@eit-test.hamanpaul.cc"
    assert entry.remote_socket == "/tmp/serialwrap/serialwrapd.sock"
    assert entry.local_port == 7777
    assert isinstance(entry.ssh_opts, tuple)
    assert entry.ssh_opts == (
        "-o",
        "ProxyCommand=cloudflared access ssh --hostname %h",
        "-i",
        "~/.ssh/id_ed25519_serialwrap_bench",
    )
    assert entry.autossh is True
    with pytest.raises(FrozenInstanceError):
        entry.local_port = 7788


@pytest.mark.parametrize("root_yaml", ["[]\n", "false\n", "0\n"])
def test_load_benches_rejects_non_mapping_root(tmp_path, root_yaml: str) -> None:
    benches_path = tmp_path / "invalid-root.yaml"
    benches_path.write_text(root_yaml, encoding="utf-8")

    module = _bench_registry()
    with pytest.raises(ValueError, match="benches.yaml.*object"):
        module.load_benches(str(benches_path))


def test_load_benches_rejects_unknown_root_level_keys(tmp_path) -> None:
    benches_path = tmp_path / "extra-root-key.yaml"
    _write_yaml(
        benches_path,
        """
        benches:
          eit-test:
            target: eit@eit-test.hamanpaul.cc
            remote_socket: /tmp/serialwrap/serialwrapd.sock
            local_port: 7777
            ssh_opts: []
            autossh: false
        cloudflare:
          hostname: eit-test.hamanpaul.cc
        """,
    )

    module = _bench_registry()
    with pytest.raises(ValueError, match=r"cloudflare.*benches|benches.*cloudflare"):
        module.load_benches(str(benches_path))


@pytest.mark.parametrize("provider_key", ["cloudflare", "tailscale"])
def test_load_benches_rejects_provider_specific_keys(tmp_path, provider_key: str) -> None:
    benches_path = tmp_path / f"{provider_key}.yaml"
    _write_yaml(
        benches_path,
        f"""
        benches:
          eit-test:
            target: eit@eit-test.hamanpaul.cc
            remote_socket: /tmp/serialwrap/serialwrapd.sock
            local_port: 7777
            ssh_opts: []
            autossh: true
            {provider_key}:
              enabled: true
        """,
    )

    module = _bench_registry()
    with pytest.raises(ValueError, match=rf"{provider_key}.*provider-neutral|provider-neutral.*{provider_key}"):
        module.load_benches(str(benches_path))


def test_load_benches_rejects_target_without_user_at(tmp_path) -> None:
    benches_path = tmp_path / "no-user.yaml"
    _write_yaml(
        benches_path,
        """
        benches:
          eit-test:
            target: eit-test.hamanpaul.cc
            remote_socket: /tmp/serialwrap/serialwrapd.sock
            local_port: 7777
            ssh_opts: []
            autossh: true
        """,
    )

    module = _bench_registry()
    with pytest.raises(ValueError, match="user@host"):
        module.load_benches(str(benches_path))


@pytest.mark.parametrize(
    ("raw_port", "message"),
    [
        ('"7777"', "local_port.*整數"),
        ("7777.0", "local_port.*整數"),
        ("65536", "local_port.*1.*65535"),
    ],
)
def test_load_benches_rejects_non_integer_or_out_of_range_local_port(
    tmp_path, raw_port: str, message: str
) -> None:
    benches_path = tmp_path / "invalid-port.yaml"
    _write_yaml(
        benches_path,
        f"""
        benches:
          eit-test:
            target: eit@eit-test.hamanpaul.cc
            remote_socket: /tmp/serialwrap/serialwrapd.sock
            local_port: {raw_port}
            ssh_opts: []
            autossh: true
        """,
    )

    module = _bench_registry()
    with pytest.raises(ValueError, match=message):
        module.load_benches(str(benches_path))
