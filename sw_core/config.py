from __future__ import annotations

import dataclasses
import os
from typing import Any

import yaml


@dataclasses.dataclass(frozen=True)
class UartProfile:
    baud: int = 115200
    data_bits: int = 8
    parity: str = "N"
    stop_bits: int = 1
    flow_control: str = "none"
    xonxoff: bool = False


@dataclasses.dataclass(frozen=True)
class ProfileTemplate:
    profile_name: str
    platform: str = "prpl"
    prompt_regex: str = r"(?m)^root@prplOS:.*# "
    login_regex: str = r"(?mi)^login:\\s*$"
    password_regex: str = r"(?mi)^password:\\s*$"
    post_login_cmd: str = ""
    ready_probe: str = "echo __READY__${nonce}"
    username: str | None = None
    user_env: str | None = None
    pass_env: str | None = None
    timeout_s: float = 10.0
    quiet_window_s: float = 2.0
    hard_timeout_s: float = 60.0
    uart: UartProfile = dataclasses.field(default_factory=UartProfile)


@dataclasses.dataclass(frozen=True)
class SessionProfile:
    profile_name: str
    com: str
    act_no: int
    alias: str
    device_by_id: str
    platform: str
    prompt_regex: str = r"(?m)^root@prplOS:.*# "
    login_regex: str = r"(?mi)^login:\\s*$"
    password_regex: str = r"(?mi)^password:\\s*$"
    post_login_cmd: str = ""
    ready_probe: str = "echo __READY__${nonce}"
    username: str | None = None
    user_env: str | None = None
    pass_env: str | None = None
    timeout_s: float = 10.0
    quiet_window_s: float = 2.0
    hard_timeout_s: float = 60.0
    uart: UartProfile = dataclasses.field(default_factory=UartProfile)


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _as_str_keep_empty(v: Any, default: str) -> str:
    if v is None:
        return default
    return str(v).strip()


def _load_uart(raw: Any, default: UartProfile | None = None) -> UartProfile:
    base = default or UartProfile()
    obj = raw if isinstance(raw, dict) else {}
    return UartProfile(
        baud=_as_int(obj.get("baud"), base.baud),
        data_bits=_as_int(obj.get("data_bits"), base.data_bits),
        parity=str(obj.get("parity") or base.parity).upper(),
        stop_bits=_as_int(obj.get("stop_bits"), base.stop_bits),
        flow_control=str(obj.get("flow_control") or base.flow_control).lower(),
        xonxoff=bool(obj.get("xonxoff", base.xonxoff)),
    )


def _template_from_dict(name: str, raw: dict[str, Any]) -> ProfileTemplate:
    return ProfileTemplate(
        profile_name=name,
        platform=str(raw.get("platform") or "prpl").strip().lower(),
        prompt_regex=str(raw.get("prompt_regex") or r"(?m)^root@prplOS:.*# "),
        login_regex=str(raw.get("login_regex") or r"(?mi)^login:\\s*$").strip(),
        password_regex=str(raw.get("password_regex") or r"(?mi)^password:\\s*$").strip(),
        post_login_cmd=str(raw.get("post_login_cmd") or "").strip(),
        ready_probe=_as_str_keep_empty(raw.get("ready_probe"), "echo __READY__${nonce}"),
        username=_as_opt_str(raw.get("username")),
        user_env=_as_opt_str(raw.get("user_env") or raw.get("username_env") or raw.get("login_env")),
        pass_env=_as_opt_str(raw.get("pass_env") or raw.get("password_env") or raw.get("pw_env")),
        timeout_s=_as_float(raw.get("timeout_s"), 10.0),
        quiet_window_s=_as_float(raw.get("quiet_window_s"), 2.0),
        hard_timeout_s=_as_float(raw.get("hard_timeout_s"), 60.0),
        uart=_load_uart(raw.get("uart")),
    )


def _load_templates(file_name: str, obj: dict[str, Any]) -> tuple[dict[str, ProfileTemplate], str]:
    templates: dict[str, ProfileTemplate] = {}
    default_name = str(obj.get("profile_name") or os.path.splitext(file_name)[0]).strip()

    profiles_obj = obj.get("profiles")
    if isinstance(profiles_obj, dict):
        for k, v in profiles_obj.items():
            if not isinstance(v, dict):
                continue
            name = str(k).strip()
            if not name:
                continue
            templates[name] = _template_from_dict(name, v)
    elif isinstance(profiles_obj, list):
        for row in profiles_obj:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("profile_name") or "").strip()
            if not name:
                continue
            templates[name] = _template_from_dict(name, row)

    if not templates:
        templates[default_name] = _template_from_dict(default_name, obj)

    if default_name not in templates:
        default_name = sorted(templates.keys())[0]
    return templates, default_name


def _merge_session(template: ProfileTemplate, target: dict[str, Any], *, act_no: int, com: str, alias: str, device_by_id: str) -> SessionProfile:
    return SessionProfile(
        profile_name=template.profile_name,
        com=com,
        act_no=act_no,
        alias=alias,
        device_by_id=device_by_id,
        platform=str(target.get("platform") or template.platform).strip().lower(),
        prompt_regex=str(target.get("prompt_regex") or template.prompt_regex),
        login_regex=str(target.get("login_regex") or template.login_regex).strip(),
        password_regex=str(target.get("password_regex") or template.password_regex).strip(),
        post_login_cmd=str(target.get("post_login_cmd") or template.post_login_cmd).strip(),
        ready_probe=_as_str_keep_empty(target.get("ready_probe"), template.ready_probe),
        username=_as_opt_str(target.get("username")) if target.get("username") is not None else template.username,
        user_env=(
            _as_opt_str(target.get("user_env") or target.get("username_env") or target.get("login_env"))
            if any(k in target for k in ("user_env", "username_env", "login_env"))
            else template.user_env
        ),
        pass_env=(
            _as_opt_str(target.get("pass_env") or target.get("password_env") or target.get("pw_env"))
            if any(k in target for k in ("pass_env", "password_env", "pw_env"))
            else template.pass_env
        ),
        timeout_s=_as_float(target.get("timeout_s"), template.timeout_s),
        quiet_window_s=_as_float(target.get("quiet_window_s"), template.quiet_window_s),
        hard_timeout_s=_as_float(target.get("hard_timeout_s"), template.hard_timeout_s),
        uart=_load_uart(target.get("uart"), default=template.uart),
    )


def load_profiles(profile_dir: str) -> list[SessionProfile]:
    out: list[SessionProfile] = []
    if not os.path.isdir(profile_dir):
        return out

    for file_name in sorted(os.listdir(profile_dir)):
        if not (file_name.endswith(".yaml") or file_name.endswith(".yml")):
            continue
        path = os.path.join(profile_dir, file_name)
        with open(path, "r", encoding="utf-8") as fp:
            obj = yaml.safe_load(fp) or {}
        if not isinstance(obj, dict):
            continue

        templates, default_profile_name = _load_templates(file_name, obj)
        targets = obj.get("targets") or []
        if not isinstance(targets, list):
            continue

        for idx, t in enumerate(targets, start=1):
            if not isinstance(t, dict):
                continue

            profile_ref = str(t.get("profile") or t.get("profile_name") or default_profile_name).strip()
            tpl = templates.get(profile_ref)
            if tpl is None:
                continue

            act_no = _as_int(t.get("act_no", idx), idx)
            com = str(t.get("com") or f"COM{max(act_no - 1, 0)}").strip()
            device_by_id = str(t.get("device_by_id") or "").strip()
            if not device_by_id:
                continue
            alias = str(t.get("alias") or f"{tpl.profile_name}+{act_no}").strip()

            out.append(_merge_session(tpl, t, act_no=act_no, com=com, alias=alias, device_by_id=device_by_id))
    return out
