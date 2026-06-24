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

# ReDoS 防護（#83 STA-4；Codex 必修強化）：使用者可控 regex 規則由單執行緒 matcher 逐行 re.search
# 求值（CPython re 在 catastrophic backtracking 期間獨佔 GIL、不檢查 signal，thread/signal timeout 無法
# 中斷），故無法靠 per-line runtime timeout。改採兩層防護：
#  (1) upsert 時以 **stdlib 解析器走訪 AST**（非脆弱的 regex-on-regex heuristic）結構性拒絕「重複>1 次且
#      重複單元模糊（含巢狀量詞或 alternation）」的指數回溯類——完整涵蓋 (a+)+、(a|aa)+、(a?)+、
#      (a{1,3})+、(.*)* 等（原 heuristic 可被後三者繞過）。
#  (2) matcher 對 regex 求值的輸入長度封頂（REGEX_MATCH_INPUT_MAX），使殘餘多項式回溯成本有界，即便
#      靜態分析漏放亦不致凍結單執行緒 matcher（line_buffer 已 16KB 截斷，此處對 regex 再收緊）。
# 完全消除多項式 ReDoS 需 re2 類線性引擎（無回溯），列為可選未來強化。
_REGEX_MAX_LEN = 512
REGEX_MATCH_INPUT_MAX = 4096
"""matcher 對單行 regex 求值時，最多餵入的字元數（ReDoS runtime 防護；超過部分不參與比對）。"""

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


def _body_has_ambiguous_repeat(body: Any) -> bool:
    """量詞的重複單元（body）內是否含「會回溯的量詞」或 alternation——模糊重複＝catastrophic 來源。"""
    for op, _av in _walk_ast(body):
        if op in _BACKTRACK_REPEATS or op == _OP_BRANCH:
            return True
    return False


def _regex_is_redos_risky(pattern: str, flags: int) -> bool:
    """AST 結構分析：是否存在「重複>1 次且重複單元模糊」的回溯型量詞（指數 ReDoS 類）。

    取代脆弱的 regex-on-regex heuristic：直接走訪 stdlib 解析的 AST，捕捉 (a+)+、(a|aa)+、(a?)+、
    (a{1,3})+、(.*)* 等可 catastrophic backtracking 的模式（皆為「量詞包住含量詞/alternation 的單元」）。
    保守——可能誤拒少數安全 pattern（如 (a+b)+、(a|b)+），但寧可誤拒不可漏放。
    """
    parsed = _sre_parse.parse(pattern, flags)
    for op, av in _walk_ast(parsed):
        if op not in _BACKTRACK_REPEATS:
            continue
        _mn, mx, body = av
        repeated_more_than_once = (mx == _MAXREPEAT) or (isinstance(mx, int) and mx > 1)
        if repeated_more_than_once and _body_has_ambiguous_repeat(body):
            return True
    return False


def _max_unbounded_in_path(sp: Any) -> int:
    """單一 concatenation 序列沿路徑可累積的最大「無界回溯量詞數」。

    規則：group 內聯（SUBPATTERN/atomic 併入主路徑、量詞 body 內部遞迴累加）、alternation 取分支
    最大、lookaround 內部獨立計（取 max 不沿主路徑累加）。沿路徑 ≥2 即代表序列上有 ≥2 個無界量詞
    （如 `a.*a.*`、`\\d*\\d*`、`[\\s\\S]*[\\s\\S]*`、`[^,]*[^,]*`），標準 re 下對長輸入可多項式回溯爆炸
    （實測即便 2 個皆於 4096 失敗輸入凍結，且**不限**原子寬窄——narrow 的 `\\d*\\d*` 亦然，故以「量詞
    數」而非「原子寬度」計，杜絕 `[\\s\\S]` 等寬原子等價形繞過）。possessive（`*+`）不回溯故不計。
    """
    total = 0
    for op, av in sp:
        if op in _BACKTRACK_REPEATS or (
            _OP_POSSESSIVE_REPEAT is not None and op == _OP_POSSESSIVE_REPEAT
        ):
            _mn, mx, body = av
            inner = _max_unbounded_in_path(body)
            if op in _BACKTRACK_REPEATS and mx == _MAXREPEAT:
                total += 1 + inner          # 本無界回溯量詞 + body 內部路徑
            else:
                total += inner              # 有界 / possessive：本身不計，body 內部仍算
        elif op == _OP_BRANCH:
            total += max((_max_unbounded_in_path(b) for b in av[1] if b is not None), default=0)
        elif op == _OP_SUBPATTERN:
            total += _max_unbounded_in_path(av[-1])          # group 內聯
        elif _OP_ATOMIC_GROUP is not None and op == _OP_ATOMIC_GROUP:
            total += _max_unbounded_in_path(av)
        elif op in (_OP_ASSERT, _OP_ASSERT_NOT):
            total = max(total, _max_unbounded_in_path(av[1]))  # lookaround 不消耗主路徑輸入
        elif op == _OP_GROUPREF_EXISTS:
            total += max((_max_unbounded_in_path(x) for x in av[1:] if x is not None), default=0)
        # 其餘 atom（literal/in/any/category…）不影響計數
    return total


def _regex_has_polynomial_redos(pattern: str, flags: int) -> bool:
    """是否含 ≥2 個序列式無界回溯量詞（多項式 ReDoS 來源）。

    保守以「量詞數」計（不分原子寬窄、不分析 separator 重疊），故亦會誤拒 `\\d+\\.\\d+`、`\\w+\\s+\\w+`
    這類實際安全（separator 不可被前量詞吞）的 pattern——但 fail-closed 寧可誤拒不可漏放，且裝
    google-re2（`serialwrap[redos]`）線性引擎後此限制即解除。alternation-of-singles（如
    `(error.*|warn.*)`）走分支取 max 故不誤判。
    """
    return _max_unbounded_in_path(_sre_parse.parse(pattern, flags)) >= 2


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
        # ReDoS fail-closed（#83 STA-4 / #91 Codex 必修）：僅當此 pattern 將落到**標準 re** 路徑
        # 才於 upsert 結構性拒絕 catastrophic backtracking 類；re2 線性引擎可用且支援此 pattern 時，
        # 一律不限制（re2 對任何 pattern 皆線性、無回溯）。判定以 matcher 同款 `_re2_compile`：
        # re2 不可用，或 pattern 用 re2 不支援的構造（backref/lookaround）→ 落 re → fail-closed。
        if _re2_compile(pvalue, pflags) is None:
            try:
                # (a) 指數類：量詞包住「含巢狀量詞 / alternation」的模糊單元（(a+)+、(a|aa)+、(.*)* …）。
                # (b) 多項式類：≥2 個序列式無界回溯量詞（a.*a.*X、\d*\d*X、[\s\S]*[\s\S]*X …，不分原子寬窄）。
                risky = _regex_is_redos_risky(pvalue, pflags_int)
                poly = _regex_has_polynomial_redos(pvalue, pflags_int)
            except re.error as exc:  # pragma: no cover - compile 已過，此處幾乎不會觸發
                raise RuleSchemaError(f"pattern.value is not a valid regex: {exc}") from exc
            _require(
                not (risky or poly),
                "pattern.value 含 catastrophic backtracking 風險（指數類如 (a+)+/(a|aa)+/(.*)*，或多項式類"
                "如 a.*a.*X／\\d*\\d*X 等 ≥2 個序列式無界量詞），標準 re 下易遭 ReDoS；請改寫為非歧義／非"
                "回溯形式，或安裝 google-re2（pip install 'serialwrap[redos]'）以線性引擎免疫此限制",
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
