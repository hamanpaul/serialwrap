"""tests/test_concurrency_races.py — #83 併發競態修正。

- RACE-1：RX fan-out 改在持 _state_lock 下逐一寫入（消除 snapshot-fd-then-close 之 use-after-close）。
- STA-4：規則 regex 加 ReDoS 靜態防護（長度上限 + 巢狀量詞拒絕）。
"""
import fcntl
import os
import threading

import pytest

import sw_core.uart_io as uio
from sw_core.event_engine.matcher import PatternMatcher
from sw_core.event_engine.schema import (
    REGEX_MATCH_INPUT_MAX,
    Pattern,
    RuleSchemaError,
    validate_rule_dict,
)
from sw_core.wal import WalWriter


def _bridge(tmp_path):
    b = object.__new__(uio.UARTBridge)
    b.com = "COM0"
    b.wal = WalWriter(wal_dir=str(tmp_path))
    b._state_lock = threading.RLock()
    b._rx_lock = threading.Lock()
    b._rx_text = ""
    b._rx_max_chars = 1000
    b._on_rx_data = None
    return b


def _rule(**overrides):
    base = {
        "schema_version": 1, "owner": "o", "name": "n", "kind": "tool",
        "selectors": ["COM0"],
        "pattern": {"kind": "regex", "value": "boot done"},
        "handler": {"exec": ["/bin/true"]},
    }
    base.update(overrides)
    return validate_rule_dict(base)


# ── RACE-1 ───────────────────────────────────────────────────────────────
def test_fanout_delivers_to_live_console(tmp_path):
    # 用 os.pipe 當 console（fan-out 只是 os.write 到 master_fd，不依賴 PTY termios），
    # 避免 PTY slave canonical 模式對無換行資料的讀取阻塞。
    b = _bridge(tmp_path)
    r, w = os.pipe()
    fcntl.fcntl(w, fcntl.F_SETFL, fcntl.fcntl(w, fcntl.F_GETFL) | os.O_NONBLOCK)
    b._clients = {"c1": uio.ConsoleClient(client_id="c1", label="c1", master_fd=w,
                                          slave_fd=r, slave_path="", attached_at=0.0)}
    try:
        b._handle_serial_rx(b"hello-rx")
        assert b"hello-rx" in os.read(r, 200)
    finally:
        os.close(r)
        os.close(w)


def test_fanout_skips_bad_fd_without_crash(tmp_path):
    """壞/已關 console fd（EBADF）不得讓 RX loop 崩潰。"""
    b = _bridge(tmp_path)
    b._clients = {"c1": uio.ConsoleClient(client_id="c1", label="c1", master_fd=99999,
                                          slave_fd=99998, slave_path="", attached_at=0.0)}
    b._handle_serial_rx(b"data")  # 不得拋例外


# ── STA-4 / #91：ReDoS 經驗式子行程探測（三輪 Codex 對抗式審查收斂）───────────────────────────
import sw_core.event_engine.schema as _schema_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_redos_probe(monkeypatch):
    """加速：catastrophic pattern 遠超此值即逾時被判，安全 pattern 數百毫秒完成 → 0.8s 有足夠裕度。"""
    monkeypatch.setattr(_schema_mod, "_REDOS_PROBE_BUDGET_S", 0.8, raising=False)


def test_overlong_regex_rejected():
    with pytest.raises(RuleSchemaError):
        _rule(pattern={"kind": "regex", "value": "a" * 1000})


def test_contains_pattern_not_redos_checked():
    _rule(pattern={"kind": "contains", "value": "(a+)+"})  # contains 非 regex，不做 ReDoS 檢查


@pytest.mark.parametrize("bad", [
    "(a+)+$",                 # 指數（anchored，需失敗尾觸發回溯）
    "(a|aa)+$",               # 指數：alternation 重疊（單量詞，由 ambiguous-body 預篩抓出）
    "a.*a.*X",                # 多項式：. 序列
    r"[\s\S]*[\s\S]*X",       # round2 繞過：寬原子等價形（非單一 ANY）
    r"\d*\d*X",               # round2：narrow 類（不分寬窄皆凍結）
    r"^(?:ab|abab)*X",        # round4：多字元重複單元（char-only 全填漏放，body witness 抓到）
    r"(ab)*(ab)*X",           # round4：多字元群組重複
    # round5：負類排除掉所有固定代表字元（\x01 失敗尾 / \x07 舊 fallback / a 0 space 通用 / Y）——
    # 須以「成員判定掃描」挑出真正被負類接受的字元（如 '!'）才能觸發。
    "^[^\x01\x07 a0Y]*[^\x01\x07 a0Y]*[^\x01\x07 a0Y]*[^\x01\x07 a0Y]*Y$",
])
def test_redos_catastrophic_rejected_without_re2(bad):
    """re2-可編譯的 catastrophic pattern：標準 re 路徑（re2 不可用）fail-closed 拒絕；re2 可用則接受。"""
    if _schema_mod._re2 is None:
        with pytest.raises(RuleSchemaError):
            _rule(pattern={"kind": "regex", "value": bad})
    else:
        _rule(pattern={"kind": "regex", "value": bad})  # re2 線性 → 免疫 → 接受


def test_redos_ignorecase_negated_range_rejected_without_re2():
    """round6：負類範圍 `[^\\x00-\\x60]` + IGNORECASE——代表字元選取須 flag-aware（用實際 flags 編譯+
    fullmatch）才能挑到真正被接受者（如 '{'）；flag-unaware 會挑到 'a'（i 下被排除）而漏放。
    """
    pat = "^[^\x00-\x60]*[^\x00-\x60]*[^\x00-\x60]*Y$"
    if _schema_mod._re2 is None:
        with pytest.raises(RuleSchemaError):
            _rule(pattern={"kind": "regex", "value": pat, "flags": "i"})
    else:
        _rule(pattern={"kind": "regex", "value": pat, "flags": "i"})


@pytest.mark.parametrize("pat", [
    "(?i:^[^\x00-\x60]*[^\x00-\x60]*[^\x00-\x60]*Y$)",   # scoped inline (?i:...)
    "(?i)^[^\x00-\x60]*[^\x00-\x60]*[^\x00-\x60]*Y$",    # global inline (?i)
])
def test_redos_inline_ignorecase_flags_rejected_without_re2(pat):
    """round7：pattern 內嵌 flags（scoped `(?i:...)` / 全域 `(?i)`）須被納入代表字元選取的 effective
    flags；否則 IGNORECASE 折疊使探測挑錯字元而漏放。flags 欄位為空、靠 inline 帶 IGNORECASE。
    """
    if _schema_mod._re2 is None:
        with pytest.raises(RuleSchemaError):
            _rule(pattern={"kind": "regex", "value": pat})
    else:
        _rule(pattern={"kind": "regex", "value": pat})


@pytest.mark.parametrize("bad", [
    r"(a*)\1X",               # round3：backreference（re2 不支援）→ 落 re → 探測拒
    r"a{0,4096}a{0,4096}X",   # round3：大量有界 repeat（re2 拒絕超大 repetition）→ 落 re → 探測拒
])
def test_redos_catastrophic_always_rejected(bad):
    """re2 無法處理者（backref / 超大 repetition）→ **兩引擎皆**落標準 re，經驗式探測必拒。"""
    with pytest.raises(RuleSchemaError):
        _rule(pattern={"kind": "regex", "value": bad})


@pytest.mark.parametrize("ok", [
    r"\d+\.\d+",          # round3：separator(\.) 不可被前量詞吞 → 安全；empirical 不誤拒（量詞數法會誤拒）
    r"\w+\s+\w+",         # 同上：disjoint 類分隔 → 安全
    r"(error.*|warn.*)",  # alternation-of-singles → 安全
    r"temp=(\d+)C",       # 單一量詞 → 非可疑、不探測
    r"root@.*# ",         # 單一 .*
    r"error|warn|fail",   # 頂層 alternation
    r"\d{3,5}",           # 單一小 bounded repeat
    r"(abc)+",            # 量詞群組但單元固定
    r"Kernel panic",
])
def test_redos_safe_accepted_both_engines(ok):
    """安全 pattern（含可疑但實測快速通過者）兩引擎皆不誤拒。"""
    _rule(pattern={"kind": "regex", "value": ok})


# ── ReDoS runtime 防護：re2 線性引擎（#91）/ re 回退路徑輸入封頂 ─────────────────────────
def test_matcher_regex_input_handling_by_engine():
    """re2 線性 → 全文求值（不截斷）；re 回退 → 對 regex 輸入封頂截掉尾端。"""
    pm = PatternMatcher(Pattern(kind="regex", value="needle", flags=""))
    tail_only = "x" * REGEX_MATCH_INPUT_MAX + "needle"
    if pm._engine == "re2":
        assert pm.eval(tail_only) is not None          # re2 不截斷，尾端 needle 仍命中
    else:
        assert pm.eval(tail_only) is None              # re 回退封頂 → 尾端被截斷
    assert pm.eval("a needle here") is not None         # 封頂之內/全文皆命中


def test_matcher_contains_not_capped():
    """contains（已 escape 的字面量、無回溯風險）不截斷，長行尾端仍可命中（兩引擎皆然）。"""
    pm = PatternMatcher(Pattern(kind="contains", value="needle", flags=""))
    tail_only = "x" * REGEX_MATCH_INPUT_MAX + "needle"
    assert pm.eval(tail_only) is not None


def test_matcher_re2_immune_to_polynomial_redos():
    """#91：re2 線性引擎對多項式 pattern（`a.*a.*a.*X`，AST 偵測器放行）長輸入不凍結。"""
    pytest.importorskip("re2")
    import time
    pm = PatternMatcher(Pattern(kind="regex", value="a.*a.*a.*X", flags=""))
    assert pm._engine == "re2"
    t0 = time.monotonic()
    assert pm.eval("a" * 200000) is None                # 無 X → 不命中；re2 線性 → 必須極快
    assert (time.monotonic() - t0) < 1.0, "re2 路徑對多項式 pattern 不應有 catastrophic backtracking"


def test_matcher_re2_flags_and_groups():
    """re2 路徑：inline flag（i/s/m）與群組擷取語意與 re 一致。"""
    pytest.importorskip("re2")
    pm = PatternMatcher(Pattern(kind="regex", value=r"temp=(\d+)c", flags="i"))
    assert pm._engine == "re2"
    r = pm.eval("XX TEMP=42C yy")
    assert r is not None and r.matched_text == "TEMP=42C" and r.groups == ["42"]


def test_matcher_re2_falls_back_for_backreference():
    """re2 不支援 backreference → 自動退回標準 re（不致無法載入規則）。"""
    pytest.importorskip("re2")
    pm = PatternMatcher(Pattern(kind="regex", value=r"(ab)\1", flags=""))
    assert pm._engine == "re"                            # 退回 re
    assert pm.eval("abab") is not None
