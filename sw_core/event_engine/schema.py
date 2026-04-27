from __future__ import annotations

import re
import types
from dataclasses import dataclass, field
from typing import Any

_VALID_KIND = {"tool", "agent"}
_VALID_SCOPE = {"spontaneous", "command_output", "any"}
_VALID_LEVEL = {"INFO", "NOTYS", "WARN", "ERR", "ENMR", "CRITL"}
_VALID_PATTERN_KIND = {"contains", "regex"}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class Pattern:
    kind: str
    value: str
    flags: str = ""


@dataclass(frozen=True)
class Handler:
    exec: list[str] | None = None
    shell: str | None = None


@dataclass(frozen=True)
class Rule:
    schema_version: int
    owner: str
    name: str
    rule_id: str
    kind: str
    selectors: tuple[str, ...]
    profile: str
    level: str
    pattern: Pattern
    scope: str
    max_fires: int | None
    cooldown_ms: int
    timeout_ms: int
    handler: Handler
    auto_enable_com_on_load: bool
    debug: bool
    raw: types.MappingProxyType = field(default_factory=dict)


class RuleSchemaError(ValueError):
    """Raised when a rule definition does not satisfy the schema."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RuleSchemaError(msg)


def validate_rule_dict(obj: dict[str, Any]) -> Rule:
    _require(isinstance(obj, dict), "rule must be a JSON object")
    _require(obj.get("schema_version") == 1, "schema_version must be 1")
    owner = str(obj.get("owner") or "")
    name = str(obj.get("name") or "")
    _require(bool(_OWNER_RE.match(owner)), "owner must match [a-z0-9-]{1,64}")
    _require(bool(_NAME_RE.match(name)), "name must match [a-z0-9-]{1,64}")
    derived_id = f"{owner}.{name}"
    rid = obj.get("rule_id", derived_id)
    _require(rid == derived_id, f"rule_id must equal '{derived_id}'")

    kind = obj.get("kind")
    _require(kind in _VALID_KIND, f"kind must be one of {sorted(_VALID_KIND)}")

    selectors_raw = obj.get("selectors")
    _require(isinstance(selectors_raw, list) and selectors_raw, "selectors must be a non-empty list")
    selectors: list[str] = []
    for s in selectors_raw:
        _require(isinstance(s, str) and s, "each selector must be a non-empty string")
        selectors.append(s)

    profile = str(obj.get("profile", "ALL"))
    level = str(obj.get("level", "INFO"))
    _require(level in _VALID_LEVEL, f"level must be one of {sorted(_VALID_LEVEL)}")

    pat_raw = obj.get("pattern")
    _require(isinstance(pat_raw, dict), "pattern must be an object")
    pkind = pat_raw.get("kind")
    pvalue = pat_raw.get("value")
    pflags = str(pat_raw.get("flags", ""))
    _require(pkind in _VALID_PATTERN_KIND, "pattern.kind must be 'contains' or 'regex'")
    _require(isinstance(pvalue, str) and pvalue != "", "pattern.value must be non-empty string")
    if pkind == "regex":
        try:
            re.compile(pvalue, _flags_from_string(pflags))
        except re.error as exc:
            raise RuleSchemaError(f"pattern.value is not a valid regex: {exc}") from exc
    pattern = Pattern(kind=pkind, value=pvalue, flags=pflags)

    scope = str(obj.get("scope", "spontaneous"))
    _require(scope in _VALID_SCOPE, f"scope must be one of {sorted(_VALID_SCOPE)}")

    max_fires_raw = obj.get("max_fires", None)
    _require(
        max_fires_raw is None or (isinstance(max_fires_raw, int) and max_fires_raw >= 0),
        "max_fires must be null or non-negative int",
    )
    cooldown_ms = int(obj.get("cooldown_ms", 0))
    timeout_ms = int(obj.get("timeout_ms", 10000))
    _require(cooldown_ms >= 0, "cooldown_ms must be >= 0")
    _require(timeout_ms > 0, "timeout_ms must be > 0")

    h_raw = obj.get("handler")
    _require(isinstance(h_raw, dict), "handler must be an object")
    h_exec = h_raw.get("exec")
    h_shell = h_raw.get("shell")
    _require(
        bool(h_exec) ^ bool(h_shell),
        "handler must have exactly one of 'exec' (list[str]) or 'shell' (str)",
    )
    if h_exec is not None:
        _require(
            isinstance(h_exec, list) and all(isinstance(x, str) and x for x in h_exec),
            "handler.exec must be a non-empty list of non-empty strings",
        )
        handler = Handler(exec=list(h_exec), shell=None)
    else:
        _require(isinstance(h_shell, str) and h_shell, "handler.shell must be non-empty string")
        handler = Handler(exec=None, shell=h_shell)

    return Rule(
        schema_version=1,
        owner=owner,
        name=name,
        rule_id=derived_id,
        kind=kind,
        selectors=tuple(selectors),
        profile=profile,
        level=level,
        pattern=pattern,
        scope=scope,
        max_fires=max_fires_raw,
        cooldown_ms=cooldown_ms,
        timeout_ms=timeout_ms,
        handler=handler,
        auto_enable_com_on_load=bool(obj.get("auto_enable_com_on_load", True)),
        debug=bool(obj.get("debug", False)),
        raw=types.MappingProxyType(dict(obj)),
    )


def _flags_from_string(s: str) -> int:
    out = 0
    for ch in s:
        if ch == "i":
            out |= re.IGNORECASE
        elif ch == "s":
            out |= re.DOTALL
        elif ch == "m":
            out |= re.MULTILINE
        else:
            raise RuleSchemaError(f"unsupported regex flag: {ch}")
    return out
