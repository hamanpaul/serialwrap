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


# ── STA-4 ────────────────────────────────────────────────────────────────
def test_safe_regex_accepted():
    _rule(pattern={"kind": "regex", "value": "root@.*# "})  # 正常 regex 不受影響


@pytest.mark.parametrize("bad", ["(a+)+$", "(a*)*", "(.+)+", "(\\d+)+", "(ab+)*"])
def test_nested_quantifier_regex_rejected(bad):
    with pytest.raises(RuleSchemaError):
        _rule(pattern={"kind": "regex", "value": bad})


def test_overlong_regex_rejected():
    with pytest.raises(RuleSchemaError):
        _rule(pattern={"kind": "regex", "value": "a" * 1000})


def test_contains_pattern_not_redos_checked():
    _rule(pattern={"kind": "contains", "value": "(a+)+"})  # contains 非 regex，不做 ReDoS 檢查


# ── STA-4 強化（Codex 必修）：原 heuristic 可被以下模式繞過，AST 結構分析須一律拒絕 ──────────
@pytest.mark.parametrize("bad", [
    "(a|aa)+$",      # alternation 重疊分支（原 heuristic 無 +/* 在群組內 → 漏放）
    "(a?)+$",        # optional 重複
    "(a{1,3})+$",    # bounded-range 重複
    "(a|aa)*",
    "(.*)*",
    "(.*)+",
    "((ab)*)*",      # 巢狀群組量詞
    "(\\d|\\d\\d)+",
    "(x+)+y",
])
def test_redos_bypass_patterns_rejected(bad):
    with pytest.raises(RuleSchemaError):
        _rule(pattern={"kind": "regex", "value": bad})


@pytest.mark.parametrize("ok", [
    r"temp=(\d+)C",       # 群組未被再施量詞 → 安全
    r"root@.*# ",         # 單一 .* → 安全
    r"error|warn|fail",   # 頂層 alternation（未被量詞包住）→ 安全
    r"\d{3,5}",           # 單一 bounded repeat → 安全
    r"(abc)+",            # 量詞群組但單元固定、無歧義 → 安全
    r"Kernel panic",
])
def test_safe_regex_patterns_still_accepted(ok):
    _rule(pattern={"kind": "regex", "value": ok})  # 不得誤拒


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
