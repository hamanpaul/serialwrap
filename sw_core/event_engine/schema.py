from __future__ import annotations

import json
import re
import subprocess
import sys
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


# ── ReDoS 防護（#83 STA-4 / #91 Codex 必修，三輪對抗式審查收斂）─────────────────────────────
# 標準 re 於 catastrophic backtracking 期間獨佔 GIL、不可中斷；任何「靜態 AST 啟發式」都被對抗式審查
# 逐一繞過（指數→多項式→寬原子等價形 [\s\S]*→大量有界 a{0,4096}→backreference (a*)\1），同時又誤拒
# 安全 pattern（\d+\.\d+）。改用**經驗式探測**：對「可疑」pattern（≥2 個回溯量詞，或含 backreference）
# 於 upsert 在獨立子行程以硬 timeout 實跑 re.search 對抗輸入；逾時＝catastrophic → 拒絕。empirical 不依賴
# 結構形狀，無法被任何 AST 形繞過；安全 pattern 數百毫秒內完成、不誤拒。僅在 pattern 將落到標準 re 路徑
# （re2 不可用或不支援）時啟用；裝 google-re2 線性引擎即一律免疫、不探測。
_OP_LITERAL = _sre_constants.LITERAL
_OP_NOT_LITERAL = _sre_constants.NOT_LITERAL
_OP_IN = _sre_constants.IN
_OP_NEGATE = _sre_constants.NEGATE
_OP_RANGE = _sre_constants.RANGE
_OP_CATEGORY = _sre_constants.CATEGORY
_OP_GROUPREF = _sre_constants.GROUPREF
_CATEGORY_REPR = {
    _sre_constants.CATEGORY_DIGIT: "0", _sre_constants.CATEGORY_WORD: "a",
    _sre_constants.CATEGORY_SPACE: " ", _sre_constants.CATEGORY_NOT_DIGIT: "a",
    _sre_constants.CATEGORY_NOT_WORD: ".", _sre_constants.CATEGORY_NOT_SPACE: "a",
}
# 子行程探測預算（秒）：catastrophic pattern 遠超此值 → 逾時即判；安全 pattern 數百毫秒內完成（巨大
# 裕度，CPU 競態下亦不誤判）。upsert 罕見，1.5s 單次成本可接受；測試可 monkeypatch 調小加速。
_REDOS_PROBE_BUDGET_S = 1.5
_REDOS_PROBE_SCRIPT = (
    "import sys,json,re\n"
    "d=json.load(sys.stdin)\n"
    "rx=re.compile(d['v'],d['f'])\n"
    "[rx.search(s) for s in d['a']]\n"
)


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
    模糊 → 指數爆炸。納入預篩，交由子行程探測確認。
    """
    for op, av in _walk_ast(parsed):
        if op in _BACKTRACK_REPEATS:
            _mn, mx, body = av
            if mx == _MAXREPEAT or (isinstance(mx, int) and mx > 1):
                if any(bop in _BACKTRACK_REPEATS or bop == _OP_BRANCH for bop, _b in _walk_ast(body)):
                    return True
    return False


def _maybe_redos_suspicious(parsed: Any) -> bool:
    """寬鬆預篩：是否值得做子行程探測（顯然安全的單量詞 pattern 不付探測成本）。

    過度涵蓋無妨——探測是精確仲裁者，會放行實際安全的可疑 pattern（如 `\\d+\\.\\d+`）。
    """
    return (
        _count_backtrack_repeats(parsed) >= 2
        or _has_backref(parsed)
        or _has_ambiguous_quantified_body(parsed)
    )


# 找代表字元的掃描順序：可見 ASCII 優先 → 其他控制 → BMP。排除集有限（pattern ≤512 字），故 IN class
# 必能找到被接受字元，杜絕「攻擊者排除掉固定代表字元」的盲點（round5）。
_ATTACK_CODEPOINTS = list(range(0x21, 0x7F)) + list(range(0x00, 0x21)) + list(range(0x7F, 0x2000))
# 類別 → class 內部來源片段，供以**實際 flags** 重建並編譯該原子（flag-aware 成員測試，round6）。
_CAT_SRC = {
    _sre_constants.CATEGORY_DIGIT: r"\d", _sre_constants.CATEGORY_NOT_DIGIT: r"\D",
    _sre_constants.CATEGORY_WORD: r"\w", _sre_constants.CATEGORY_NOT_WORD: r"\W",
    _sre_constants.CATEGORY_SPACE: r"\s", _sre_constants.CATEGORY_NOT_SPACE: r"\S",
}


def _in_class_regex(av: Any, flags: int):
    """把 IN class（含 NEGATE/literal/range/category）以**實際 flags** 重建並編譯為單字元 regex。

    用真實 re 語意（含 IGNORECASE 的大小寫折疊等）測試候選字元成員，而非手刻判定——後者忽略 flags
    會挑到實際**不被**負類接受的字元而漏放（Codex round6 [high]：`[^\\x00-\\x60]` + `flags='i'`）。
    """
    parts = []
    items = av
    if items and items[0][0] == _OP_NEGATE:
        parts.append("^")
        items = items[1:]
    for o, a in items:
        if o == _OP_LITERAL:
            parts.append(re.escape(chr(a)))
        elif o == _OP_RANGE:
            parts.append(re.escape(chr(a[0])) + "-" + re.escape(chr(a[1])))
        elif o == _OP_CATEGORY:
            parts.append(_CAT_SRC.get(a, ""))
        else:
            return None  # 罕見巢狀構造 → 放棄重建，由呼叫端 fallback
    try:
        return re.compile("[" + "".join(parts) + "]", flags)
    except re.error:  # pragma: no cover
        return None


def _scan_matching_char(rx, default: str = "a") -> str:
    """掃描候選 codepoint，回傳第一個被已編譯單字元 regex `rx` fullmatch 的字元。"""
    for cp in _ATTACK_CODEPOINTS:
        ch = chr(cp)
        if rx.fullmatch(ch):
            return ch
    return default  # pragma: no cover - 該 class 涵蓋整個掃描範圍（極罕）


def _atom_char(op: Any, av: Any, flags: int = 0) -> str:
    """能被該原子（在實際 flags 下）**確實**匹配的一個代表字元（用於建構對抗輸入）。

    IN class 與 NOT_LITERAL 以「重建+編譯+fullmatch」挑出真正被接受者（flag-aware，含 IGNORECASE）；
    literal/any/category 之代表本身即與 flags 無關地匹配。
    """
    if op == _OP_LITERAL:
        return chr(av) if 0 <= av < 0x110000 else "a"
    if op == _OP_CATEGORY:
        return _CATEGORY_REPR.get(av, "a")
    if op == _OP_NOT_LITERAL:
        try:
            rx = re.compile("[^" + re.escape(chr(av)) + "]", flags)
            return _scan_matching_char(rx)
        except (re.error, ValueError):  # pragma: no cover
            return "a" if av != ord("a") else "b"
    if op == _OP_IN:
        rx = _in_class_regex(av, flags)
        if rx is not None:
            return _scan_matching_char(rx)
        for o, a in av:                 # fallback：重建失敗時的結構式取值（正類）
            if o == _OP_LITERAL:
                return chr(a) if 0 <= a < 0x110000 else "a"
            if o == _OP_RANGE:
                return chr(a[0]) if 0 <= a[0] < 0x110000 else "a"
            if o == _OP_CATEGORY:
                return _CATEGORY_REPR.get(a, "a")
    return "a"


def _atom_regex(op: Any, av: Any, flags: int):
    """把單一原子（literal/not_literal/in/category/any）重建為單字元 regex（實際 flags），供成員測試。"""
    try:
        if op == _OP_LITERAL:
            return re.compile("[" + re.escape(chr(av)) + "]", flags)
        if op == _OP_NOT_LITERAL:
            return re.compile("[^" + re.escape(chr(av)) + "]", flags)
        if op == _OP_IN:
            return _in_class_regex(av, flags)
        if op == _OP_CATEGORY:
            src = _CAT_SRC.get(av)
            return re.compile("[" + src + "]", flags) if src else None
        if op == _OP_ANY:
            return re.compile(".", flags)            # 非 DOTALL：拒 \n
        if op == _OP_ANY_ALL:
            return None                              # DOTALL `.`：匹配一切，無拒絕字元
    except (re.error, ValueError):  # pragma: no cover
        return None
    return None


def _scan_nonmatching_char(rx) -> "str | None":
    """掃描候選 codepoint，回傳第一個**不**被單字元 regex `rx` fullmatch 的字元（無則 None）。"""
    for cp in _ATTACK_CODEPOINTS:
        ch = chr(cp)
        if not rx.fullmatch(ch):
            return ch
    return None  # pragma: no cover - 該原子匹配整個掃描範圍


def _atom_rejecting_char(op: Any, av: Any, flags: int) -> "str | None":
    """一個該原子（在實際 flags 下）**確實不**匹配的字元（None 表示匹配一切，如 DOTALL `.`）。

    用於建構**會失敗的尾字元**：固定 `\\x01` 對 `[^\\x02]` 這類負類其實仍被匹配 → witness 全程命中、
    從不走回溯失敗路徑而漏放（Codex round8 [critical]）。改用「以真實 re 語意挑真正被原子拒絕的字元」
    當失敗尾，強迫 anchored 量詞（如 `^([^\\x02]+)+$`）回溯。
    """
    rx = _atom_regex(op, av, flags)
    if rx is None:
        return None
    return _scan_nonmatching_char(rx)


def _subpattern_flags(av: Any, flags: int) -> int:
    """套用 SUBPATTERN 的 scoped inline flags（如 `(?i:...)`）：`(flags | add) & ~del`。

    sre 的 add/del flag 值與 `re.I/S/M` 常數相同，故可直接位元運算。結構為
    `(group, add_flags, del_flags, subpattern)`；舊版可能無 add/del 欄位 → 維持原 flags。
    """
    try:
        _group, add, dele, _sub = av
        return (flags | int(add)) & ~int(dele)
    except (ValueError, TypeError):  # pragma: no cover - 舊版/非預期結構
        return flags


def _representative_match(sp: Any, flags: int, depth: int = 0) -> str:
    """為一個 SubPattern 產生一段「可被它（在實際 flags 下）匹配」的字串（量詞取 body 一次、取首分支）。

    用於建構**多字元重複單元 witness**：char-only 全填無法觸發 `(?:ab|abab)*`、`(ab)*(ab)*` 這類
    需要多字元重複串的 catastrophic（Codex round4 [critical]）。
    """
    if depth > 24:  # 防深巢狀遞迴爆炸
        return ""
    parts: list[str] = []
    for op, av in sp:
        if op == _OP_LITERAL:
            parts.append(chr(av) if 0 <= av < 0x110000 else "a")
        elif op in (_OP_ANY, _OP_ANY_ALL):
            parts.append("a")
        elif op in (_OP_IN, _OP_CATEGORY, _OP_NOT_LITERAL):
            parts.append(_atom_char(op, av, flags))
        elif op in _BACKTRACK_REPEATS or (
            _OP_POSSESSIVE_REPEAT is not None and op == _OP_POSSESSIVE_REPEAT
        ):
            parts.append(_representative_match(av[2], flags, depth + 1))   # body 一次
        elif op == _OP_SUBPATTERN:
            parts.append(_representative_match(av[-1], _subpattern_flags(av, flags), depth + 1))
        elif _OP_ATOMIC_GROUP is not None and op == _OP_ATOMIC_GROUP:
            parts.append(_representative_match(av, flags, depth + 1))
        elif op == _OP_BRANCH:
            br = next((b for b in av[1] if b is not None), None)
            if br is not None:
                parts.append(_representative_match(br, flags, depth + 1))
        # AT（anchor）/ASSERT/GROUPREF 等不產生字元
    return "".join(parts)


def _collect_witness_units(sp: Any, flags: int, units: set, reject: set, depth: int = 0) -> None:
    """遞迴收集 witness 重複單元與「會失敗的尾字元」，沿 SUBPATTERN 邊界傳遞 effective flags。

    平掃（`_walk_ast`）會在進入 `(?i:...)` 子樹時用錯 flags（Codex round7 [high]），故遞迴 thread flags。
    收集兩類：(1) `units`＝可匹配重複單元（每 atom 代表字元、每量詞 body 與每 alternation 分支的代表匹配
    串）；(2) `reject`＝每個 atom **不**匹配的字元（用真實 re 語意求；round8 [critical]——固定 `\x01`
    對 `[^\x02]` 仍被匹配 → witness 全程命中、不走回溯失敗路徑而漏放）。
    """
    if depth > 24:
        return
    for op, av in sp:
        if op in (_OP_LITERAL, _OP_IN, _OP_CATEGORY, _OP_NOT_LITERAL, _OP_ANY):
            if op != _OP_ANY:
                units.add(_atom_char(op, av, flags))
            r = _atom_rejecting_char(op, av, flags)
            if r is not None:
                reject.add(r)
        if op in _BACKTRACK_REPEATS or (
            _OP_POSSESSIVE_REPEAT is not None and op == _OP_POSSESSIVE_REPEAT
        ):
            _mn, mx, body = av
            if mx == _MAXREPEAT or (isinstance(mx, int) and mx > 1):
                u = _representative_match(body, flags)
                if u:
                    units.add(u[:128])
            _collect_witness_units(body, flags, units, reject, depth + 1)
        elif op == _OP_SUBPATTERN:
            _collect_witness_units(av[-1], _subpattern_flags(av, flags), units, reject, depth + 1)
        elif _OP_ATOMIC_GROUP is not None and op == _OP_ATOMIC_GROUP:
            _collect_witness_units(av, flags, units, reject, depth + 1)
        elif op == _OP_BRANCH:
            for br in av[1]:
                if br is not None:
                    bu = _representative_match(br, flags)
                    if bu:
                        units.add(bu[:128])
                    _collect_witness_units(br, flags, units, reject, depth + 1)
        elif op in (_OP_ASSERT, _OP_ASSERT_NOT):
            _collect_witness_units(av[1], flags, units, reject, depth + 1)
        elif op == _OP_GROUPREF_EXISTS:
            for x in av[1:]:
                if x is not None:
                    _collect_witness_units(x, flags, units, reject, depth + 1)


def _redos_attack_inputs(parsed: Any, flags: int) -> list[str]:
    """為 pattern 建構對抗輸入：以「可匹配重複單元」鋪成 4096 長字串，各產生『全填』與『全填+失敗尾』。

    - 單字元 witness：pattern 的 literal/類別代表字元 ＋ 通用 {a,0,space}（觸發多項式/重疊回溯）。
    - 多字元 witness：每個量詞 body（及其 alternation 各分支）的代表匹配字串重複鋪滿（觸發
      `(?:ab|abab)*`、`(ab)*(ab)*` 這類需多字元重複串的 catastrophic；Codex round4 [critical]）。
    - 失敗尾：以**真實被原子拒絕的字元**（per-pattern 求得；round8）＋通用 `\x01`，強迫 anchored 量詞
      （如 `(a+)+$`、`^([^\x02]+)+$`）回溯失敗。
    代表字元/拒絕字元皆以「該節點生效的 flags」取得（flag-aware，含全域 `(?i)` 與 scoped `(?i:...)`）。
    """
    L = REGEX_MATCH_INPUT_MAX
    units: set = {"a", "0", " "}
    reject: set = set()
    _collect_witness_units(parsed, flags, units, reject)
    # 失敗尾候選：真實拒絕字元（上限 8 個，控成本）＋ 通用 \x01（多數正類 pattern 適用）。
    suffixes = ["\x01"] + sorted(reject)[:8]
    out: list[str] = []
    for u in units:
        if not u:
            continue
        s = (u * (L // len(u) + 1))[:L]
        out.append(s)
        for suf in suffixes:
            out.append(s[:-1] + suf)
    return out


def _stdlib_regex_is_catastrophic(value: str, flags: int) -> bool:
    """獨立子行程以硬 timeout 實測 re.search 對抗輸入；逾時＝catastrophic backtracking（fail closed）。

    僅對 `_maybe_redos_suspicious` 的 pattern 呼叫（罕見）。empirical 涵蓋指數/多項式/大量有界/backref
    等任意 AST 形，無法被結構繞過；安全 pattern 不誤拒。子行程跑全新直譯器（無 multiprocessing 的
    spawn 重匯入 / fork 鎖繼承風險），timeout 後由 subprocess 終結。探測基礎設施失敗 → fail closed。
    """
    try:
        # effective flags 含 pattern 內的**全域** inline flag（如開頭 `(?i)`）——反映於 compiled.flags；
        # scoped `(?i:...)` 則由 _collect_witness_units 沿 SUBPATTERN 邊界另行套用（round7）。子行程仍以
        # 原 flags compile（re 自會處理 pattern 內 inline flag），effective 僅用於挑代表字元。
        effective = re.compile(value, flags).flags
        parsed = _sre_parse.parse(value, flags)
        payload = json.dumps({"v": value, "f": int(flags), "a": _redos_attack_inputs(parsed, effective)})
        subprocess.run(
            [sys.executable, "-c", _REDOS_PROBE_SCRIPT],
            input=payload.encode("utf-8"),
            timeout=_REDOS_PROBE_BUDGET_S,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return False
    except subprocess.TimeoutExpired:
        return True
    except Exception:  # pragma: no cover - 探測基礎設施不可用 → fail closed
        return True


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
        # ReDoS fail-closed（#83 STA-4 / #91 Codex 必修）：僅當此 pattern 將落到**標準 re** 路徑才檢查；
        # re2 線性引擎可用且支援此 pattern 時一律不限制（re2 對任何 pattern 皆線性、無回溯）。判定以 matcher
        # 同款 `_re2_compile`：re2 不可用，或 pattern 用 re2 不支援的構造（backref/lookaround）→ 落 re。
        # 對「可疑」pattern（≥2 個回溯量詞或含 backreference）於子行程實測探測，catastrophic 即拒——
        # empirical 涵蓋指數/多項式/大量有界/backref 任意形，杜絕靜態 heuristic 的繞過與誤拒。
        if _re2_compile(pvalue, pflags) is None:
            try:
                parsed_for_check = _sre_parse.parse(pvalue, pflags_int)
            except re.error as exc:  # pragma: no cover - compile 已過，此處幾乎不會觸發
                raise RuleSchemaError(f"pattern.value is not a valid regex: {exc}") from exc
            if _maybe_redos_suspicious(parsed_for_check) and _stdlib_regex_is_catastrophic(pvalue, pflags_int):
                _require(
                    False,
                    "pattern.value 在標準 re 下對長輸入呈 catastrophic backtracking（ReDoS，子行程實測逾時）；"
                    "請改寫為非回溯形式，或安裝 google-re2（pip install 'serialwrap[redos]'）以線性引擎免疫",
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
