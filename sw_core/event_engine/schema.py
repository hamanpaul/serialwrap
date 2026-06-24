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

# ReDoS 防護（#83 STA-4 / #91）：規則 regex 由單執行緒 matcher 逐行 re.search 求值，catastrophic
# backtracking 會凍結 matcher。最終設計（見下方 fail-closed 段）：有 re2＝線性免疫；無 re2＝對結構上
# 可疑 regex fail-closed 拒絕；matcher 另以 REGEX_MATCH_INPUT_MAX 封頂 re 路徑輸入長度作 runtime 兜底。
_REGEX_MAX_LEN = 512
REGEX_MATCH_INPUT_MAX = 4096
"""matcher 對單行 regex 求值時，最多餵入的字元數（ReDoS runtime 兜底；超過部分不參與比對）。"""

# stdlib regex AST 解析器：3.11+ 為 re._parser / re._constants；3.10 為 sre_parse / sre_constants。
try:  # pragma: no cover - 版本相依匯入
    from re import _parser as _sre_parse  # type: ignore[attr-defined]
    from re import _constants as _sre_constants  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    import sre_parse as _sre_parse  # type: ignore[no-redef]
    import sre_constants as _sre_constants  # type: ignore[no-redef]

_OP_MAX_REPEAT = _sre_constants.MAX_REPEAT
_OP_MIN_REPEAT = _sre_constants.MIN_REPEAT
_OP_BRANCH = _sre_constants.BRANCH
_OP_SUBPATTERN = _sre_constants.SUBPATTERN
_OP_ASSERT = _sre_constants.ASSERT
_OP_ASSERT_NOT = _sre_constants.ASSERT_NOT
_OP_GROUPREF_EXISTS = _sre_constants.GROUPREF_EXISTS
_OP_ATOMIC_GROUP = getattr(_sre_constants, "ATOMIC_GROUP", None)            # 3.11+ (?>...)
_OP_POSSESSIVE_REPEAT = getattr(_sre_constants, "POSSESSIVE_REPEAT", None)  # 3.11+ a++/a*+
_OP_ANY = _sre_constants.ANY                                  # `.`（非 DOTALL）
_OP_ANY_ALL = getattr(_sre_constants, "ANY_ALL", None)       # DOTALL 下的 `.`
_MAXREPEAT = _sre_constants.MAXREPEAT
# 會回溯的量詞（greedy / lazy）。possessive / atomic 不回溯，故不列入「危險量詞」。
_BACKTRACK_REPEATS = {_OP_MAX_REPEAT, _OP_MIN_REPEAT}

# google-re2 線性引擎（可選依賴 serialwrap[redos]）：可用時 matcher 以其求值 user regex，免疫所有
# catastrophic backtracking（指數＋多項式）。schema 以它判定「此 pattern 是否會落到標準 re 路徑」——
# 若會（re2 不可用，或 pattern 用 re2 不支援的構造），則於 upsert 階段 fail-closed 拒絕指數/多項式類。
try:  # pragma: no cover - 視環境是否安裝 re2
    import re2 as _re2
except ImportError:  # pragma: no cover
    _re2 = None


def _re2_compile(value: str, flags_str: str):
    """以 google-re2 編譯（flags 以 inline `(?ism)` 帶入）。re2 不可用或不支援構造（backref/lookaround）回 None。"""
    if _re2 is None:
        return None
    try:
        inline = f"(?{flags_str})" if flags_str else ""
        return _re2.compile(inline + value)
    except Exception:  # pragma: no cover - re2 不支援構造
        return None


def _child_subpatterns(op: Any, av: Any) -> list:
    """回傳 AST 節點 (op, av) 的子 SubPattern 串列，供遞迴走訪。"""
    if op in (_OP_MAX_REPEAT, _OP_MIN_REPEAT) or (
        _OP_POSSESSIVE_REPEAT is not None and op == _OP_POSSESSIVE_REPEAT
    ):
        return [av[2]]
    if op == _OP_BRANCH:
        return [b for b in av[1] if b is not None]
    if op == _OP_SUBPATTERN:
        return [av[-1]]
    if op in (_OP_ASSERT, _OP_ASSERT_NOT):
        return [av[1]]
    if _OP_ATOMIC_GROUP is not None and op == _OP_ATOMIC_GROUP:
        return [av]
    if op == _OP_GROUPREF_EXISTS:
        return [x for x in av[1:] if x is not None]
    return []


def _walk_ast(subpattern: Any):
    """深度走訪 SubPattern，yield 每個 (op, av) 節點（含巢狀子 SubPattern）。"""
    for op, av in subpattern:
        yield op, av
        for child in _child_subpatterns(op, av):
            yield from _walk_ast(child)


# ── ReDoS 防護（#83 STA-4 / #91；9 輪 Codex 對抗式審查後收斂為 fail-closed）─────────────────────
# 結論：標準 re 於 catastrophic backtracking 期間獨佔 GIL、不可中斷；任何「靜態 AST 啟發式」或「經驗式
# 子行程探測」都被對抗式審查逐輪繞過（指數→多項式→寬原子 [\s\S]*→大量有界 a{0,4096}→backref (a*)\1→
# 多字元單元 (?:ab|abab)*→負類排除→inline flags (?i:…)→失敗尾→path-context prefix），是無界軍備競賽，
# 且 timing-based 探測本質 flaky。唯一**完整、確定、無 flaky** 的解：
#   • 有 re2 線性引擎（可選依賴 `pip install 'serialwrap[redos]'`）→ 零結構限制（re2 對任何 pattern 皆
#     線性、無回溯，徹底免疫）。
#   • 無 re2（pattern 將落到標準 re 路徑）→ 對「結構上可疑」的 regex 一律 **fail-closed 拒絕**。
# 可疑＝(a) ≥2 個會回溯量詞（多項式，含大量有界 {0,N}）、(b) backreference、(c) 量詞單元含 alternation／
# 巢狀量詞（指數）。此三者經驗證為 catastrophic 的**完整結構超集**（單一非歧義量詞為線性、安全），故
# fail-closed 無漏放、且不誤擋 contains 與單量詞 regex（如 Kernel panic、temp=(\d+)C、root@.*#）。
# matcher 另以 REGEX_MATCH_INPUT_MAX 封頂 re 路徑輸入長度作 runtime 兜底。
_OP_GROUPREF = _sre_constants.GROUPREF


def _count_backtrack_repeats(parsed: Any) -> int:
    """「會回溯且重複>1 次」的量詞數（含無界 `*`/`+`/`{n,}` 與大量有界 `{0,4096}`，後者亦可凍結）。"""
    n = 0
    for op, av in _walk_ast(parsed):
        if op in _BACKTRACK_REPEATS:
            _mn, mx, _body = av
            if mx == _MAXREPEAT or (isinstance(mx, int) and mx > 1):
                n += 1
    return n


def _has_backref(parsed: Any) -> bool:
    """是否含 backreference（如 `\\1`）——`(a*)\\1` 類在標準 re 下可 catastrophic，且 re2 不支援。"""
    return any(op == _OP_GROUPREF for op, _av in _walk_ast(parsed))


def _has_ambiguous_quantified_body(parsed: Any) -> bool:
    """是否有「重複>1 次」的量詞，其重複單元含 alternation 或巢狀量詞——單量詞亦可指數回溯。

    `(a|aa)+`、`(a?)+`、`(.*)*` 等：外層只有一個量詞（`_count_backtrack_repeats` 可能 <2），但重複單元
    模糊 → 指數爆炸。
    """
    for op, av in _walk_ast(parsed):
        if op in _BACKTRACK_REPEATS:
            _mn, mx, body = av
            if mx == _MAXREPEAT or (isinstance(mx, int) and mx > 1):
                if any(bop in _BACKTRACK_REPEATS or bop == _OP_BRANCH for bop, _b in _walk_ast(body)):
                    return True
    return False


def _maybe_redos_suspicious(parsed: Any) -> bool:
    """結構上是否可能 catastrophic backtracking——無 re2 時據此 fail-closed 拒絕。

    (a) ≥2 個會回溯量詞（多項式，含大量有界 `{0,N}`）、(b) backreference、(c) 量詞單元含 alternation／
    巢狀量詞（指數）。三者為 catastrophic 的**完整結構超集**（單一非歧義量詞為線性、安全）；保守拒絕，
    可能誤擋少數實際安全的多量詞 pattern（如 `\\d+\\.\\d+`、`\\w+\\s+\\w+`）——裝 re2 即不受此限。
    """
    return (
        _count_backtrack_repeats(parsed) >= 2
        or _has_backref(parsed)
        or _has_ambiguous_quantified_body(parsed)
    )


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
        _require(
            len(pvalue) <= _REGEX_MAX_LEN,
            f"pattern.value regex 過長（上限 {_REGEX_MAX_LEN} 字元，避免 ReDoS）",
        )
        try:
            pflags_int = _flags_from_string(pflags)
            re.compile(pvalue, pflags_int)
        except re.error as exc:
            raise RuleSchemaError(f"pattern.value is not a valid regex: {exc}") from exc
        # ReDoS fail-closed（#83 STA-4 / #91，9 輪對抗式審查後定案）：僅當此 pattern 將落到**標準 re**
        # 路徑才檢查（`_re2_compile` 回 None＝re2 不可用，或 pattern 用 re2 不支援的構造 backref/lookaround）；
        # re2 線性引擎可用且支援時一律不限制。落 re 路徑時，結構上可疑（指數/多項式/backref，完整超集）
        # 即 fail-closed 拒絕——不再嘗試逐一證明安全（靜態/經驗探測皆被逐輪繞過且 flaky）。
        if _re2_compile(pvalue, pflags) is None:
            try:
                parsed_for_check = _sre_parse.parse(pvalue, pflags_int)
            except re.error as exc:  # pragma: no cover - compile 已過，此處幾乎不會觸發
                raise RuleSchemaError(f"pattern.value is not a valid regex: {exc}") from exc
            _require(
                not _maybe_redos_suspicious(parsed_for_check),
                "pattern.value 結構上可能 catastrophic backtracking（ReDoS：≥2 個回溯量詞／backreference／"
                "量詞單元含 alternation 或巢狀量詞）；無 google-re2 時一律拒絕。請改寫為單一非歧義量詞形式，"
                "或安裝 google-re2（pip install 'serialwrap[redos]'）以線性引擎免疫此限制",
            )
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
