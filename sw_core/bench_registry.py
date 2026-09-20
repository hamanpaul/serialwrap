from __future__ import annotations

import dataclasses
import os
from typing import Any

import yaml


_ALLOWED_ENTRY_KEYS = frozenset(
    {"target", "remote_socket", "local_port", "ssh_opts", "autossh"}
)
_ALLOWED_ROOT_KEYS = frozenset({"benches"})


@dataclasses.dataclass(frozen=True)
class BenchEntry:
    target: str
    remote_socket: str
    local_port: int
    ssh_opts: tuple[str, ...] = ()
    autossh: bool = False


def to_remote_argv(entry: BenchEntry) -> list[str]:
    argv = ["-L", "--remote-socket", entry.remote_socket]
    if entry.autossh:
        argv.append("--autossh")
    argv.extend(f"--ssh-opt={item}" for item in entry.ssh_opts)
    argv.append(f"{entry.target}:{entry.local_port}")
    return argv


def _env_path(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raw = default
    return os.path.expanduser(raw)


def _default_config_dir() -> str:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return _env_path("SERIALWRAP_CONFIG_DIR", os.path.join(config_home, "serialwrap"))


def _default_benches_path() -> str:
    return _env_path(
        "SERIALWRAP_BENCHES_FILE",
        os.path.join(_default_config_dir(), "benches.yaml"),
    )


def _require_mapping(raw: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} 必須是 object")
    return raw


def _require_string(raw: Any, *, field_name: str, code: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f'bench "{code}" 的 {field_name} 必須是字串')
    value = raw.strip()
    if not value:
        raise ValueError(f'bench "{code}" 的 {field_name} 不可為空')
    return value


def _require_port(raw: Any, *, code: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f'bench "{code}" 的 local_port 必須是整數')
    if raw < 1 or raw > 65535:
        raise ValueError(f'bench "{code}" 的 local_port 必須介於 1 到 65535')
    return raw


def _require_ssh_opts(raw: Any, *, code: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError(f'bench "{code}" 的 ssh_opts 必須是字串陣列')
    if not all(isinstance(item, str) for item in raw):
        raise ValueError(f'bench "{code}" 的 ssh_opts 必須是字串陣列')
    return tuple(raw)


def _require_autossh(raw: Any, *, code: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f'bench "{code}" 的 autossh 必須是布林值')
    return raw


def _validate_target(target: str, *, code: str) -> str:
    if target.count("@") != 1:
        raise ValueError(f'bench "{code}" 的 target 必須是 user@host 形式')
    user, host = target.split("@", 1)
    if not user or not host:
        raise ValueError(f'bench "{code}" 的 target 必須是 user@host 形式')
    if any(part.strip() != part for part in (user, host)):
        raise ValueError(f'bench "{code}" 的 target 必須是 user@host 形式')
    return target


def _validate_allowed_keys(raw: dict[str, Any], *, code: str) -> None:
    unexpected = sorted(str(key) for key in raw.keys() if key not in _ALLOWED_ENTRY_KEYS)
    if unexpected:
        allowed = ", ".join(sorted(_ALLOWED_ENTRY_KEYS))
        names = ", ".join(unexpected)
        raise ValueError(
            f'bench "{code}" 包含不支援欄位 {names}；benches.yaml 必須維持 '
            f'provider-neutral，只能使用 {allowed}'
        )
    missing = sorted(key for key in _ALLOWED_ENTRY_KEYS if key not in raw)
    if missing:
        names = ", ".join(missing)
        raise ValueError(f'bench "{code}" 缺少必要欄位 {names}')


def _validate_root_keys(raw: dict[str, Any]) -> None:
    unexpected = sorted(str(key) for key in raw.keys() if key not in _ALLOWED_ROOT_KEYS)
    if unexpected:
        allowed = ", ".join(sorted(_ALLOWED_ROOT_KEYS))
        names = ", ".join(unexpected)
        raise ValueError(f"benches.yaml 包含不支援根欄位 {names}；只能使用 {allowed}")


def _entry_from_raw(code: str, raw: Any) -> BenchEntry:
    obj = _require_mapping(raw, context=f'bench "{code}"')
    _validate_allowed_keys(obj, code=code)
    target = _validate_target(
        _require_string(obj["target"], field_name="target", code=code),
        code=code,
    )
    remote_socket = _require_string(obj["remote_socket"], field_name="remote_socket", code=code)
    local_port = _require_port(obj["local_port"], code=code)
    ssh_opts = _require_ssh_opts(obj["ssh_opts"], code=code)
    autossh = _require_autossh(obj["autossh"], code=code)
    return BenchEntry(
        target=target,
        remote_socket=remote_socket,
        local_port=local_port,
        ssh_opts=ssh_opts,
        autossh=autossh,
    )


def load_benches(path: str) -> dict[str, BenchEntry]:
    resolved_path = os.path.expanduser(path)
    if not os.path.exists(resolved_path):
        return {}
    with open(resolved_path, "r", encoding="utf-8") as fp:
        loaded = yaml.safe_load(fp)
    if loaded is None:
        loaded = {}
    root = _require_mapping(loaded, context="benches.yaml")
    _validate_root_keys(root)
    benches_raw = root.get("benches", {})
    benches_obj = _require_mapping(benches_raw, context="benches.yaml 的 benches")
    benches: dict[str, BenchEntry] = {}
    for raw_code, raw_entry in benches_obj.items():
        code = _require_string(raw_code, field_name="code", code="<root>")
        benches[code] = _entry_from_raw(code, raw_entry)
    return benches


def load_configured_benches() -> dict[str, BenchEntry]:
    return load_benches(_default_benches_path())


def resolve(code: str) -> BenchEntry:
    benches = load_configured_benches()
    try:
        return benches[code]
    except KeyError as exc:
        raise KeyError(f"unknown bench code: {code}") from exc
