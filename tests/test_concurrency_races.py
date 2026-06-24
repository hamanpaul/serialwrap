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


# ── STA-4 / #91：ReDoS fail-closed（9 輪 Codex 對抗式審查後定案；確定性、無 flaky）───────────────
# 設計：有 re2＝線性免疫零限制；無 re2＝結構上可疑（指數/多項式/backref，完整超集）一律拒絕。
import sw_core.event_engine.schema as _schema_mod  # noqa: E402


def test_overlong_regex_rejected():
    with pytest.raises(RuleSchemaError):
        _rule(pattern={"kind": "regex", "value": "a" * 1000})


def test_contains_pattern_not_redos_checked():
    _rule(pattern={"kind": "contains", "value": "(a+)+"})  # contains 非 regex，不做 ReDoS 檢查


# 9 輪對抗式審查找到的全部 catastrophic（re2-可編譯）：無 re2 → fail-closed 拒絕；re2 → 線性接受。
@pytest.mark.parametrize("bad", [
    "(a+)+$",                                              # 指數：巢狀量詞
    "(a|aa)+$", "(a?)+$", "(a{1,3})+$", "(.*)*",           # 指數：模糊量詞 body（單量詞亦危險）
    "a.*a.*X", ".*.*", r"\d*\d*X",                         # 多項式：≥2 序列量詞（round1-2）
    r"[\s\S]*[\s\S]*X",                                    # round2：寬原子等價形
    r"^(?:ab|abab)*X", r"(ab)*(ab)*X",                     # round4：多字元重複單元
    "^[^\x01\x07 a0Y]*[^\x01\x07 a0Y]*Y$",                 # round5：負類排除固定字元
    r"\w+\s+\w+", r"\d+\.\d+", r"(error.*|warn.*)",        # round2/3 過往「邊界」O(n^2)/多量詞 → 保守拒
])
def test_redos_suspicious_rejected_without_re2(bad):
    if _schema_mod._re2 is None:
        with pytest.raises(RuleSchemaError):
            _rule(pattern={"kind": "regex", "value": bad})
    else:
        _rule(pattern={"kind": "regex", "value": bad})    # re2 線性 → 免疫 → 接受


# IGNORECASE 負類（外部/scoped/global flags）：結構可疑 → 無 re2 拒、re2 接受（round6/7）。
@pytest.mark.parametrize("pat,flags", [
    ("^[^\x00-\x60]*[^\x00-\x60]*Y$", "i"),               # 外部 flags=i
    ("(?i:^[^\x00-\x60]*[^\x00-\x60]*Y$)", ""),           # scoped inline (?i:...)
    ("(?i)^[^\x00-\x60]*[^\x00-\x60]*Y$", ""),            # global inline (?i)
    (r"^([^\x02]+)+$", ""), (r"^([^,]+)+$", ""),          # round8：負類 anchored 巢狀量詞
])
def test_redos_negated_suspicious_rejected_without_re2(pat, flags):
    if _schema_mod._re2 is None:
        with pytest.raises(RuleSchemaError):
            _rule(pattern={"kind": "regex", "value": pat, "flags": flags})
    else:
        _rule(pattern={"kind": "regex", "value": pat, "flags": flags})


# re2 無法編譯者（backref / 超大 repetition）→ 兩引擎皆落標準 re → 結構可疑 → **永遠**拒。
@pytest.mark.parametrize("bad", [r"(a*)\1X", r"a{0,4096}a{0,4096}X"])
def test_redos_re2_incompatible_always_rejected(bad):
    with pytest.raises(RuleSchemaError):
        _rule(pattern={"kind": "regex", "value": bad})


# 單一非歧義量詞 / 無量詞：非可疑（線性安全）→ 兩引擎皆接受、不誤擋（含 contains-like 常見規則）。
@pytest.mark.parametrize("ok", [
    r"temp=(\d+)C", r"root@.*# ", r"dhd_dpc.*firmware_trap", r"Kernel panic - not syncing: ",
    r"error|warn|fail", r"\d{3,5}", r"(abc)+", r"Kernel panic", r"a+$", r".*X",
])
def test_redos_safe_single_quantifier_accepted_both_engines(ok):
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
